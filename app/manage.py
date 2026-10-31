"""Maintenance commands run inside the same Docker container."""
from contextlib import closing
import argparse
import getpass
import sqlite3
from app import domain as d
from app.server import password_hash


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    backup = sub.add_parser('backup', help='Create a consistent SQLite backup')
    backup.add_argument('destination')
    reset = sub.add_parser('reset-password', help='Reset an account password and revoke sessions')
    reset.add_argument('account', help='Username, or an unambiguous role')
    sub.add_parser('recover-parent',help='Reset the parent password locally without changing family data')
    args = parser.parse_args()
    if args.command == 'backup':
        with d.connect() as source, closing(sqlite3.connect(args.destination)) as target:
            source.backup(target)
        print('Backup saved to',args.destination)
    else:
        password = getpass.getpass('New password (at least 10 characters): ')
        if password != getpass.getpass('Confirm password: '):
            raise SystemExit('Passwords did not match.')
        hashed = password_hash(password)
        with d.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            account='parent' if args.command=='recover-parent' else args.account.lower()
            matches = db.execute("SELECT id,username FROM users WHERE role='parent'").fetchall() if args.command=='recover-parent' else db.execute('SELECT id,username FROM users WHERE username=?',(account,)).fetchall()
            if not matches:
                matches = db.execute('SELECT id,username FROM users WHERE role=?',(account,)).fetchall()
            if len(matches)>1:
                raise SystemExit('Multiple children exist. Specify the child username instead.')
            user = matches[0] if matches else None
            if not user:
                raise SystemExit('Set up your family first.')
            db.execute('UPDATE users SET password=? WHERE id=?',(hashed,user['id']))
            db.execute('DELETE FROM sessions WHERE user_id=?',(user['id'],))
            db.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",('reset-login:'+user['username'],d.now()))
            db.commit()
        print('Password changed. Existing sessions revoked.')


if __name__ == '__main__':
    main()
