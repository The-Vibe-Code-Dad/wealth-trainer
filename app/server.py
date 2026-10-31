"""Dependency-free HTTP service, intended behind a TLS reverse proxy."""
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app import domain as d

LOG = logging.getLogger('wealth')
STATIC = Path(__file__).parent / 'static'
LOGIN_LOCK = threading.Lock()
ATTEMPTS = {}


def username(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{3,32}',value.strip()):
        raise ValueError('Usernames must be 3–32 letters, numbers, dots, underscores, or hyphens.')
    return value.strip().lower()


def password_hash(password, salt=None):
    if not isinstance(password,str) or not 10 <= len(password) <= 256:
        raise ValueError('Passwords must be 10–256 characters.')
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return salt + ':' + digest


def password_ok(password, saved):
    try:
        return hmac.compare_digest(password_hash(password, saved.split(':')[0]),saved)
    except (ValueError,TypeError):
        return False


from app.market import refresh_prices


MARKET_LOCK = threading.Lock()


def maintenance():
    next_quotes = 0
    while True:
        try:
            with d.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                d.scheduled(db)
                db.execute('DELETE FROM sessions WHERE expires<?',(time.time(),))
                db.commit()
            from app import backups
            try:
                backups.run()
            except Exception:
                LOG.exception('Backup failed; market updates will continue')
            if time.time() >= next_quotes:
                next_quotes = time.time() + 15 * 60
                if MARKET_LOCK.acquire(blocking=False):
                    try:
                        result = refresh_prices()
                        LOG.info('Market refresh: %s',result)
                    finally:
                        MARKET_LOCK.release()
        except Exception:
            LOG.exception('Maintenance failed; will retry')
        time.sleep(60)


class Handler(BaseHTTPRequestHandler):
    server_version = 'WealthTrainer'

    def log_message(self, fmt, *args):
        LOG.info('%s %s',self.address_string(),fmt % args)

    def respond(self, payload, status=200, cookie=None):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        if cookie:
            self.send_header('Set-Cookie',cookie)
        self.end_headers()
        self.wfile.write(raw)

    def session(self, db):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie',''))
            token = cookie['session'].value
        except (KeyError,ValueError):
            return None
        return db.execute('SELECT u.*,s.csrf,s.token FROM sessions s JOIN users u ON u.id=s.user_id WHERE token=? AND expires>?',
                          (hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()

    def do_GET(self):
        try:
            self.get_page()
        except PermissionError as exc:
            self.respond({'error':str(exc)},403)
        except ValueError as exc:
            self.respond({'error':str(exc)},400)

    def get_page(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == '/health':
            self.respond({'ok':True})
            return
        if path == '/api/state':
            with d.connect() as db:
                if not db.execute('SELECT 1 FROM users LIMIT 1').fetchone():
                    self.respond({'setup':True})
                    return
                user = self.session(db)
                if not user:
                    self.respond({'login':True},401)
                    return
                db.execute('BEGIN IMMEDIATE')
                d.scheduled(db)
                selected=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('child_id',[None])[0]
                payload = d.state(db,user,selected)
                payload['csrf'] = user['csrf']
                db.commit()
            self.respond(payload)
            return
        files = {'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8'),'/favicon.svg':('favicon.svg','image/svg+xml')}
        if path not in files:
            self.respond({'error':'Not found'},404)
            return
        name, content_type = files[path]
        raw = (STATIC / name).read_bytes()
        self.send_response(200)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(raw)))
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','same-origin')
        self.send_header('Cache-Control','no-cache')
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length','0'))
            if length <= 0 or length > 16384:
                raise ValueError('Invalid request size.')
            if self.headers.get('Content-Type','').split(';')[0] != 'application/json':
                raise ValueError('JSON is required.')
            # JSON-only requests + SameSite cookies + session CSRF token; no CORS.
            if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                raise PermissionError('Cross-site request blocked.')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data,dict):
                raise ValueError('Expected an object.')
            path = urllib.parse.urlsplit(self.path).path.removeprefix('/api/')
            with d.connect() as db:
                if path in ('login','setup'):
                    self.auth(db,path,data)
                    return
                user = self.session(db)
                if not user:
                    self.respond({'error':'Please sign in again.'},401)
                    return
                if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),user['csrf']):
                    raise PermissionError('Please refresh and try again.')
                if path == 'logout':
                    db.execute('DELETE FROM sessions WHERE token=?',(user['token'],))
                    self.respond({'ok':True},cookie='session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict')
                    return
                if path == 'backup-now':
                    if user['role']!='parent':
                        raise PermissionError('Only the parent can create backups.')
                    from app import backups
                    self.respond({'at':backups.run(force=True)})
                    return
                if path == 'refresh-prices':
                    if user['role'] != 'parent':
                        raise PermissionError('Only the parent can refresh prices.')
                    if not MARKET_LOCK.acquire(blocking=False):
                        raise ValueError('A price refresh is already running.')
                    try:
                        self.respond(refresh_prices())
                    finally:
                        MARKET_LOCK.release()
                    return
                db.execute('BEGIN IMMEDIATE')
                try:
                    d.scheduled(db)
                    key = self.headers.get('Idempotency-Key','')
                    if not 8 <= len(key) <= 100:
                        raise ValueError('A unique request key is required.')
                    # Tracks all actions, not just actions which write ledger entries.
                    db.execute('CREATE TABLE IF NOT EXISTS requests(key TEXT PRIMARY KEY, user_id INTEGER NOT NULL)')
                    if db.execute('SELECT 1 FROM requests WHERE key=?',(key,)).fetchone():
                        self.respond({'ok':True})
                        db.rollback()
                        return
                    if path in ('account-save','account-add'):
                        self.save_account(db,user,data,create=path=='account-add')
                    else:
                        d.action(db,user,path,data,key)
                    db.execute('INSERT INTO requests VALUES (?,?)',(key,user['id']))
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
            self.respond({'ok':True})
        except PermissionError as exc:
            self.respond({'error':str(exc)},403)
        except sqlite3.IntegrityError:
            self.respond({'error':'That request already exists, or this name/ticker is already in use.'},409)
        except (ValueError,TypeError,KeyError,InvalidOperation) as exc:
            self.respond({'error':str(exc) or 'Invalid request.'},400)
        except Exception:
            LOG.exception('Request failed')
            self.respond({'error':'The request could not be completed. No partial transaction was saved.'},500)

    def save_account(self, db, user, data, create=False):
        if user['role'] != 'parent':
            raise PermissionError('Only the parent can manage accounts.')
        if not password_ok(data.get('current_password'),user['password']):
            raise PermissionError('Enter your current parent password to manage accounts.')
        new_username=username(data.get('username'))
        new_name=d.label(data.get('name'))
        if create:
            ident=db.execute("INSERT INTO users(name,role,password,username) VALUES (?,'child',?,?)",(new_name,password_hash(data.get('password')),new_username)).lastrowid
            d.f.initialize_child(db,ident)
            d.record_action(db,user,'account-add',ident)
            return
        account=d.required(db,'users',data.get('id'))
        hashed=password_hash(data['password']) if data.get('password') else account['password']
        db.execute('UPDATE users SET name=?,username=?,password=? WHERE id=?',(new_name,new_username,hashed,account['id']))
        d.record_action(db,user,'account-save',account['id'] if account['role']=='child' else None)
        db.execute('DELETE FROM sessions WHERE user_id=?',(account['id'],))
        with LOGIN_LOCK:
            for key in list(ATTEMPTS):
                if key[1] in (account['username'],new_username):
                    ATTEMPTS.pop(key,None)

    def auth(self, db, path, data):
        address = self.client_address[0]
        supplied_username = data.get('username','')
        supplied_username = supplied_username.strip().lower() if isinstance(supplied_username,str) else ''
        attempt_key = (address, supplied_username if path == 'login' else '<setup>')
        with LOGIN_LOCK:
            reset=db.execute("SELECT value FROM settings WHERE key=?",('reset-login:'+supplied_username,)).fetchone()
            if reset:
                for attempt in list(ATTEMPTS):
                    if attempt[1]==supplied_username:
                        ATTEMPTS.pop(attempt,None)
                db.execute('DELETE FROM settings WHERE key=?',('reset-login:'+supplied_username,))
            attempts = [t for t in ATTEMPTS.get(attempt_key,[]) if t > time.time()-300]
            ATTEMPTS[attempt_key] = attempts
            if len(attempts) >= 10:
                remaining=max(1,int(attempts[0]+300-time.time())+1)
                self.respond({'error':f'Too many attempts for this username. Try again in {remaining} seconds, or reset its password from the parent bank.'},429)
                return
            attempts.append(time.time())
        if path == 'setup':
            expected = os.environ.get('BOOTSTRAP_TOKEN','')
            if not expected or not hmac.compare_digest(str(data.get('token','')),expected):
                raise PermissionError('The setup token is incorrect.')
            parent = password_hash(data.get('parent_password'))
            child = password_hash(data.get('child_password'))
            parent_username = username(data.get('parent_username'))
            child_username = username(data.get('child_username'))
            if parent_username == child_username:
                raise ValueError('Choose a different username for each account.')
            db.execute('BEGIN IMMEDIATE')
            try:
                if db.execute('SELECT 1 FROM users').fetchone():
                    raise ValueError('Your family is already set up.')
                db.execute('INSERT INTO users(name,role,password,username) VALUES (?,\'parent\',?,?)',(d.label(data.get('parent_name')),parent,parent_username))
                child_id=db.execute('INSERT INTO users(name,role,password,username) VALUES (?,\'child\',?,?)',(d.label(data.get('child_name')),child,child_username)).lastrowid
                d.f.initialize_child(db,child_id)
                db.commit()
            except Exception:
                db.rollback()
                raise
            self.respond({'ok':True})
            return
        user = db.execute('SELECT * FROM users WHERE username=?',(supplied_username,)).fetchone()
        if not user or not password_ok(data.get('password'),user['password']):
            raise PermissionError('The account or password is incorrect.')
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        db.execute('INSERT INTO sessions VALUES (?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),user['id'],csrf,time.time()+12*3600))
        with LOGIN_LOCK:
            ATTEMPTS.pop(attempt_key,None)
        secure = '; Secure' if os.environ.get('COOKIE_SECURE','false').lower() == 'true' else ''
        self.respond({'ok':True},cookie=f'session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200{secure}')


def main():
    logging.basicConfig(level=logging.INFO)
    d.init()
    if not os.environ.get('BOOTSTRAP_TOKEN'):
        with d.connect() as db:
            if not db.execute('SELECT 1 FROM users').fetchone():
                raise SystemExit('Set BOOTSTRAP_TOKEN to a random secret before the first start.')
    threading.Thread(target=maintenance,daemon=True).start()
    server = ThreadingHTTPServer((os.environ.get('HOST','0.0.0.0'),int(os.environ.get('PORT','8080'))),Handler)
    LOG.info('Wealth Trainer listening on port %s',server.server_port)
    server.serve_forever()


if __name__ == '__main__':
    main()
