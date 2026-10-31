"""Family economy. Integer hundredths of a Coin; append-only, balanced ledger."""
import datetime as dt
import json
import os
import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from app import families as f
from app import features as features

DB_PATH = os.environ.get('DATABASE_PATH', 'data/wealth.db')
UTC = dt.timezone.utc


def now():
    return dt.datetime.now(UTC).isoformat()


class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, factory=Connection)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA journal_mode=WAL')
    return db


def init():
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT NOT NULL, role TEXT UNIQUE NOT NULL, password TEXT NOT NULL, username TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER REFERENCES users, csrf TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY);
        INSERT OR IGNORE INTO accounts VALUES ('wallet'),('savings'),('bank'),('market'),('store'),('cashout_hold');
        CREATE TABLE IF NOT EXISTS transactions(id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, reason TEXT NOT NULL, actor INTEGER REFERENCES users, reversal_of INTEGER UNIQUE REFERENCES transactions, request_key TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS entries(id INTEGER PRIMARY KEY, tx INTEGER NOT NULL REFERENCES transactions, account TEXT NOT NULL REFERENCES accounts, amount INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS entries_account ON entries(account);
        CREATE TRIGGER IF NOT EXISTS immutable_tx_update BEFORE UPDATE ON transactions BEGIN SELECT RAISE(ABORT,'Ledger is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_tx_delete BEFORE DELETE ON transactions BEGIN SELECT RAISE(ABORT,'Ledger is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_entry_update BEFORE UPDATE ON entries BEGIN SELECT RAISE(ABORT,'Ledger is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_entry_delete BEFORE DELETE ON entries BEGIN SELECT RAISE(ABORT,'Ledger is immutable'); END;
        CREATE TABLE IF NOT EXISTS chores(id INTEGER PRIMARY KEY, title TEXT NOT NULL, reward INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS completions(id INTEGER PRIMARY KEY, chore INTEGER REFERENCES chores, title TEXT NOT NULL, reward INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', at TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS pending_chore ON completions(chore) WHERE status='pending';
        CREATE TABLE IF NOT EXISTS rewards(id INTEGER PRIMARY KEY, title TEXT NOT NULL, price INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS redemptions(id INTEGER PRIMARY KEY, reward INTEGER REFERENCES rewards, title TEXT NOT NULL, price INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', at TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS pending_reward ON redemptions(reward) WHERE status='pending';
        CREATE TABLE IF NOT EXISTS investments(id INTEGER PRIMARY KEY, name TEXT NOT NULL, symbol TEXT UNIQUE NOT NULL, risk TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS prices(id INTEGER PRIMARY KEY, investment INTEGER REFERENCES investments, price TEXT NOT NULL, day TEXT NOT NULL, source TEXT NOT NULL, fetched TEXT NOT NULL, UNIQUE(investment,day));
        CREATE TABLE IF NOT EXISTS committees(id INTEGER PRIMARY KEY, name TEXT NOT NULL, allocations TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY, name TEXT NOT NULL, cost INTEGER NOT NULL, holdings TEXT NOT NULL, opened TEXT NOT NULL, closed TEXT, proceeds INTEGER);
        CREATE TABLE IF NOT EXISTS splits(investment INTEGER REFERENCES investments,at INTEGER NOT NULL,ratio TEXT NOT NULL,PRIMARY KEY(investment,at));
        CREATE TABLE IF NOT EXISTS cashouts(id INTEGER PRIMARY KEY,amount INTEGER NOT NULL,reason TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',at TEXT NOT NULL,reviewed TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT OR IGNORE INTO settings VALUES ('apr','5'),('interest_day',date('now')),('allowance','0'),('allowance_next',date('now','+7 days'));
        INSERT OR IGNORE INTO investments(id,name,symbol,risk) VALUES (1,'S&P 500','SPY','Balanced'),(2,'Technology','QQQ','Growth'),(3,'US bonds','BND','Lower'),(4,'Gold','GLD','Growth');
        INSERT OR IGNORE INTO committees(id,name,allocations) VALUES (1,'Growth committee','{"1":50,"2":30,"cash":20}'),(2,'Steady committee','{"3":50,"1":30,"cash":20}');
        ''')
        # Upgrade databases created before username sign-in was introduced.
        if 'username' not in {row['name'] for row in db.execute('PRAGMA table_info(users)')}:
            db.execute('ALTER TABLE users ADD COLUMN username TEXT')
            db.execute('UPDATE users SET username=role WHERE username IS NULL')
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS users_username ON users(username)')
        f.migrate(db)
        features.migrate(db)
        db.executescript('''
        CREATE TABLE IF NOT EXISTS bank_funds(id INTEGER PRIMARY KEY,at TEXT NOT NULL,amount INTEGER NOT NULL,reason TEXT NOT NULL,actor INTEGER REFERENCES users,request_key TEXT UNIQUE NOT NULL);
        CREATE TRIGGER IF NOT EXISTS immutable_funds_update BEFORE UPDATE ON bank_funds BEGIN SELECT RAISE(ABORT,'Bank history is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_funds_delete BEFORE DELETE ON bank_funds BEGIN SELECT RAISE(ABORT,'Bank history is immutable'); END;
        CREATE TABLE IF NOT EXISTS portfolio_history(id INTEGER PRIMARY KEY,child_id INTEGER NOT NULL REFERENCES users,at TEXT NOT NULL,value INTEGER NOT NULL,assets TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS portfolio_history_child ON portfolio_history(child_id,id);
        CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY,at TEXT NOT NULL,actor INTEGER REFERENCES users,child_id INTEGER REFERENCES users,action TEXT NOT NULL,detail TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS immutable_audit_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT,'Audit log is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_audit_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT,'Audit log is immutable'); END;
        ''')
        if 'quote_at' not in {row['name'] for row in db.execute('PRAGMA table_info(prices)')}:
            db.execute('ALTER TABLE prices ADD COLUMN quote_at INTEGER')
        # Seed the expanded catalog only once, without re-enabling anything a parent paused.
        if not db.execute("SELECT 1 FROM settings WHERE key='yahoo_catalog_v1'").fetchone():
            db.execute('BEGIN IMMEDIATE')
            db.executemany('INSERT OR IGNORE INTO investments(name,symbol,risk) VALUES (?,?,?)',[
                ('Apple','AAPL','Growth'),('Microsoft','MSFT','Growth'),('Nvidia','NVDA','High'),
                ('Disney','DIS','Growth'),('Bitcoin','BTC-USD','High'),('Ethereum','ETH-USD','High')])
            db.execute("INSERT OR IGNORE INTO settings VALUES ('yahoo_catalog_v1','1')")
            db.commit()


        if not db.execute("SELECT 1 FROM settings WHERE key='learning_mixes_v1'").fetchone():
            db.execute('BEGIN IMMEDIATE')
            ids={r['symbol']:str(r['id']) for r in db.execute('SELECT id,symbol FROM investments')}
            for name, weights in [('Aggressive',{'SPY':40,'QQQ':60}),('Balanced',{'SPY':50,'BND':40,'cash':10}),('Defensive',{'BND':60,'SPY':20,'cash':20})]:
                allocation={ids[k] if k!='cash' else k:v for k,v in weights.items()}
                db.execute('INSERT INTO committees(name,allocations) VALUES (?,?)',(name,json.dumps(allocation)))
            db.execute("INSERT INTO settings VALUES ('learning_mixes_v1','1')")
            db.commit()


        # Update untouched preset mixes only; purchases store their own holdings.
        if not db.execute("SELECT 1 FROM settings WHERE key='fully_invested_presets_v1'").fetchone():
            db.execute('BEGIN IMMEDIATE')
            ids={r['symbol']:str(r['id']) for r in db.execute('SELECT id,symbol FROM investments')}
            presets=[('Growth committee',{'SPY':50,'QQQ':30,'cash':20},{'SPY':62.5,'QQQ':37.5}),
                     ('Steady committee',{'BND':50,'SPY':30,'cash':20},{'BND':62.5,'SPY':37.5}),
                     ('Balanced',{'SPY':50,'BND':40,'cash':10},{'SPY':55.56,'BND':44.44}),
                     ('Defensive',{'BND':60,'SPY':20,'cash':20},{'BND':75,'SPY':25})]
            for name,old,new in presets:
                old={ids[k] if k!='cash' else k:v for k,v in old.items()}
                new={ids[k]:v for k,v in new.items()}
                for row in db.execute('SELECT id,allocations FROM committees WHERE name=?',(name,)).fetchall():
                    if json.loads(row['allocations'])==old:
                        db.execute('UPDATE committees SET allocations=? WHERE id=?',(json.dumps(new),row['id']))
            db.execute("INSERT INTO settings VALUES ('fully_invested_presets_v1','1')")
            db.commit()


        if not db.execute("SELECT 1 FROM settings WHERE key='simple_presets_v1'").fetchone():
            db.execute('BEGIN IMMEDIATE')
            ids={r['symbol']:str(r['id']) for r in db.execute('SELECT id,symbol FROM investments')}
            for name,allocation in [('Growth committee',{'SPY':62.5,'QQQ':37.5}),('Steady committee',{'BND':62.5,'SPY':37.5})]:
                expected={ids[k]:Decimal(str(v)) for k,v in allocation.items()}
                for row in db.execute('SELECT id,allocations FROM committees WHERE name=?',(name,)).fetchall():
                    if {k:Decimal(str(v)) for k,v in json.loads(row['allocations']).items()}==expected:
                        db.execute('UPDATE committees SET enabled=0 WHERE id=?',(row['id'],))
            for row in db.execute("SELECT id,allocations FROM committees WHERE name='Balanced'").fetchall():
                old={k:Decimal(str(v)) for k,v in json.loads(row['allocations']).items()}
                if old=={ids['SPY']:Decimal('55.56'),ids['BND']:Decimal('44.44')}:
                    db.execute('UPDATE committees SET allocations=? WHERE id=?',(json.dumps({ids['SPY']:60,ids['BND']:40}),row['id']))
            db.execute("INSERT INTO settings VALUES ('simple_presets_v1','1')")
            db.commit()


def money(value, allow_zero=False):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number > 100000000 or (number == 0 and not allow_zero) or number.as_tuple().exponent < -2:
            raise ValueError()
        return int(number * 100)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('Enter a positive Coin amount with at most two decimal places (maximum 100 million).')


def label(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 160:
        raise ValueError('Enter a name or reason between 1 and 160 characters.')
    return value.strip()


def balance(db, account):
    return db.execute('SELECT COALESCE(SUM(e.amount),0) FROM entries e JOIN transactions t ON t.id=e.tx WHERE e.account=? AND t.child_id=?', (account,f.current(db))).fetchone()[0]


def bank_balance(db):
    return db.execute('SELECT COALESCE(SUM(amount),0) FROM bank_funds').fetchone()[0]


def post(db, kind, reason, actor, moves, key=None, reversal=None):
    if sum(moves.values()) != 0:
        raise ValueError('Unbalanced transaction')
    for account in ('wallet', 'savings', 'cashout_hold'):
        if balance(db, account) + moves.get(account, 0) < 0:
            raise ValueError('Not enough Coins in ' + account + '.')
    bank_change=moves.get('bank',0) if kind in ('adjustment','chore','allowance','interest','goal-match') else 0
    if reversal:
        original=db.execute('SELECT amount FROM bank_funds WHERE request_key=?',('payment:'+str(reversal),)).fetchone()
        bank_change=-original['amount'] if original else 0
    if bank_change and bank_balance(db)+bank_change<0:
        raise ValueError('Not enough money in the parent bank. Add money under Bank funds first.')
    if moves.get('savings',0)<0 and balance(db,'savings')+moves['savings']<features.goal_reserved(db,f.current(db)):
        raise ValueError('Some savings are reserved for goals. Release money from a goal first.')
    tx = db.execute('INSERT INTO transactions(at,kind,reason,actor,request_key,reversal_of,child_id) VALUES (?,?,?,?,?,?,?)', (now(), kind, reason, actor, key, reversal,f.current(db))).lastrowid
    db.executemany('INSERT INTO entries(tx,account,amount) VALUES (?,?,?)', [(tx, account, amount) for account, amount in moves.items() if amount])
    if bank_change:
        child=db.execute('SELECT name FROM users WHERE id=?',(f.current(db),)).fetchone()[0]
        db.execute('INSERT INTO bank_funds(at,amount,reason,actor,request_key) VALUES (?,?,?,?,?)',(now(),bank_change,kind.replace('-',' ').capitalize()+' · '+child,actor,'payment:'+str(tx)))
    return tx


def required(db, table, ident):
    # Table names are internal constants, never request values.
    row = db.execute(f'SELECT * FROM {table} WHERE id=?', (ident,)).fetchone()
    if not row or (table in (f.SCOPED | {'goals'}) and row['child_id'] != f.current(db)):
        raise ValueError('That item does not exist for the selected child.')
    return row


def quote(db, ident, fresh=False):
    row = db.execute('SELECT * FROM prices WHERE investment=? ORDER BY day DESC LIMIT 1', (ident,)).fetchone()
    if not row:
        raise ValueError('Waiting for Yahoo market data. Prices refresh automatically; please try again shortly.')
    if fresh and (dt.date.today() - dt.date.fromisoformat(row['day'])).days > 5:
        raise ValueError('Market price is over five days old. Refresh prices before trading.')
    if fresh and row['source']=='Yahoo Finance' and (dt.datetime.now(UTC)-dt.datetime.fromisoformat(row['fetched'])).total_seconds()>3600:
        raise ValueError('Yahoo prices have not refreshed recently. Trading will resume automatically when quotes recover.')
    return Decimal(row['price'])


def valuation(db, holdings, fresh=False, opened=None):
    total = Decimal(holdings.get('cash', '0'))
    for ident, units in holdings.items():
        if ident != 'cash':
            factor=Decimal(1)
            if opened:
                after=int(dt.datetime.fromisoformat(opened).timestamp())
                for split in db.execute('SELECT ratio FROM splits WHERE investment=? AND at>? AND at<=?',(int(ident),after,int(dt.datetime.now(UTC).timestamp()))):
                    factor *= Decimal(split['ratio'])
            total += Decimal(units) * factor * quote(db, int(ident), fresh)
    return int(total.quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def scheduled(db, today=None):
    previous=getattr(db,'child_id',None)
    try:
        for child in db.execute("SELECT id FROM users WHERE role='child'").fetchall():
            db.child_id=child['id']
            f.initialize_child(db,child['id'])
            scheduled_child(db,today)
    finally:
        db.child_id=previous


def scheduled_child(db, today=None):
    today = today or dt.datetime.now(UTC).date()
    settings = dict(db.execute('SELECT key,value FROM child_settings WHERE child_id=?',(f.current(db),)).fetchall())
    last = dt.date.fromisoformat(settings['interest_day'])
    # Every elapsed day is posted separately, so daily compounding is auditable.
    while last < today:
        last += dt.timedelta(days=1)
        interest = int((Decimal(balance(db, 'savings')) * Decimal(settings['apr']) / 36500).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        if interest>bank_balance(db):
            last -= dt.timedelta(days=1)
            break
        if interest:
            post(db, 'interest', 'Savings interest Â· ' + str(last), None, {'bank': -interest, 'savings': interest}, 'interest:' + str(f.current(db))+':' + str(last))
    db.execute("UPDATE child_settings SET value=? WHERE key='interest_day' AND child_id=?", (str(last),f.current(db)))
    due = dt.date.fromisoformat(settings['allowance_next'])
    amount = int(settings['allowance'])
    while due <= today:
        if amount>bank_balance(db):
            break
        if amount:
            post(db, 'allowance', 'Weekly allowance Â· ' + str(due), None, {'bank': -amount, 'wallet': amount}, 'allowance:' + str(f.current(db))+':' + str(due))
        due += dt.timedelta(days=7)
    db.execute("UPDATE child_settings SET value=? WHERE key='allowance_next' AND child_id=?", (str(due),f.current(db)))


def record_action(db,user,path,child_id=None,detail=''):
    db.execute('INSERT INTO audit_log(at,actor,child_id,action,detail) VALUES (?,?,?,?,?)',(now(),user['id'],child_id,path,detail))


def action(db,user,path,data,key):
    perform_action(db,user,path,data,key)
    detail = ('Sent back for revision' if data.get('revise') else 'Approved' if data.get('approve') else 'Declined') if path.endswith('-review') else ''
    record_action(db,user,path,None if path=='bank-fund' else f.current(db),detail)
    record_portfolio(db,f.current(db))


def perform_action(db, user, path, data, key):
    parent_paths = {'bank-fund','adjust','reverse','chore-save','chore-review','reward-save','redemption-review','investment-save','committee-save','settings','cashout-review','learning-settings'}
    if path in parent_paths and user['role'] != 'parent':
        raise PermissionError('Only the parent can do that.')
    f.select(db,user,data.get('child_id'))
    uid = user['id']
    if path.startswith('goal-'):
        features.goal_action(db,user,path,data,key)
    elif path == 'bank-fund':
        amount=money(data.get('amount'))
        db.execute('INSERT INTO bank_funds(at,amount,reason,actor,request_key) VALUES (?,?,?,?,?)',(now(),amount,label(data.get('reason')),uid,key))
    elif path == 'cashout-request':
        amount=money(data.get('amount'))
        if amount % 100:
            raise ValueError('Cash-outs use whole Coins (one Coin equals one US cent).')
        reason=label(data.get('reason','Cash out'))
        post(db,'cashout-request',reason,uid,{'wallet':-amount,'cashout_hold':amount},key)
        db.execute('INSERT INTO cashouts(amount,reason,at,child_id) VALUES (?,?,?,?)',(amount,reason,now(),f.current(db)))
    elif path in ('cashout-review','cashout-cancel'):
        row=required(db,'cashouts',data.get('id'))
        if row['status'] != 'pending':
            raise ValueError('This cash-out has already been resolved.')
        approved=path=='cashout-review' and data.get('approve') is True
        status='approved' if approved else ('cancelled' if path=='cashout-cancel' else 'declined')
        destination='bank' if approved else 'wallet'
        post(db,'cashout-'+status,'Cash-out #'+str(row['id'])+' Â· '+row['reason'],uid,{'cashout_hold':-row['amount'],destination:row['amount']},key)
        db.execute('UPDATE cashouts SET status=?,reviewed=? WHERE id=?',(status,now(),row['id']))
    elif path == 'adjust':
        amount = money(data.get('amount'))
        if data.get('direction') == 'remove':
            amount = -amount
        post(db, 'adjustment', label(data.get('reason')), uid, {'wallet': amount, 'bank': -amount}, key)
    elif path == 'transfer':
        amount = money(data.get('amount'))
        source = data.get('source')
        if source not in ('wallet','savings'):
            raise ValueError('Choose wallet or savings.')
        dest = 'savings' if source == 'wallet' else 'wallet'
        post(db, 'transfer', 'Transfer to ' + dest, uid, {source:-amount, dest:amount}, key)
    elif path == 'reverse':
        tx = required(db, 'transactions', data.get('id'))
        if tx['kind'] not in ('adjustment','transfer','allowance','interest'):
            raise ValueError('Only bank adjustments, transfers, allowances, and interest can be reversed. Sell investments or issue a labeled refund for purchases.')
        if db.execute('SELECT 1 FROM transactions WHERE reversal_of=?',(tx['id'],)).fetchone():
            raise ValueError('This transaction has already been reversed.')
        moves = {r['account']: -r['amount'] for r in db.execute('SELECT account,SUM(amount) amount FROM entries WHERE tx=? GROUP BY account',(tx['id'],))}
        post(db, 'reversal', label(data.get('reason')), uid, moves, key, tx['id'])
    elif path in ('chore-save','reward-save'):
        table = 'chores' if path == 'chore-save' else 'rewards'
        field = 'reward' if table == 'chores' else 'price'
        title, amount = label(data.get('title')), money(data.get('amount'))
        if data.get('id'):
            required(db,table,data['id'])
            db.execute(f'UPDATE {table} SET title=?,{field}=?,active=? WHERE id=?',(title,amount,int(bool(data.get('active',True))),data['id']))
        else:
            ident=db.execute(f'INSERT INTO {table}(title,{field},child_id) VALUES (?,?,?)',(title,amount,f.current(db))).lastrowid
        if table=='chores':
            recurrence,due=features.schedule_fields(data)
            db.execute('UPDATE chores SET recurrence=?,due_date=? WHERE id=?',(recurrence,due,data.get('id') or ident))
    elif path == 'chore-complete':
        row = required(db,'chores',data.get('id'))
        if not row['active']:
            raise ValueError('This chore is paused.')
        if row['recurrence']!='anytime' and row['due_date']>str(features.local_today()):
            raise ValueError('This chore is available on its due date.')
        db.execute('INSERT INTO completions(chore,title,reward,at,child_id,due_date,recurrence) VALUES (?,?,?,?,?,?,?)',(row['id'],row['title'],row['reward'],now(),f.current(db),row['due_date'],row['recurrence']))
    elif path == 'chore-review':
        row = required(db,'completions',data.get('id'))
        if row['status'] != 'pending':
            raise ValueError('Already reviewed.')
        if data.get('revise') is True:
            note=label(data.get('note'))
            db.execute("UPDATE completions SET status='revision',review_note=? WHERE id=?",(note,row['id']))
            return
        approve = data.get('approve') is True
        if approve:
            post(db,'chore',row['title'],uid,{'wallet':row['reward'],'bank':-row['reward']},key)
        db.execute('UPDATE completions SET status=? WHERE id=?',('approved' if approve else 'declined',row['id']))
        if approve:
            chore=required(db,'chores',row['chore'])
            if row['due_date']==chore['due_date'] and row['recurrence']==chore['recurrence']:
                if row['recurrence']=='once':
                    db.execute('UPDATE chores SET active=0 WHERE id=?',(chore['id'],))
                elif row['recurrence'] in ('daily','weekly'):
                    due=features.next_due(chore['due_date'],chore['recurrence'],features.local_today())
                    db.execute('UPDATE chores SET due_date=? WHERE id=?',(due,chore['id']))
    elif path == 'redeem':
        row = required(db,'rewards',data.get('id'))
        if not row['active']:
            raise ValueError('This reward is unavailable.')
        reserved = db.execute("SELECT COALESCE(SUM(price),0) FROM redemptions WHERE status='pending' AND child_id=?",(f.current(db),)).fetchone()[0]
        if balance(db,'wallet') - reserved < row['price']:
            raise ValueError('Not enough wallet Coins for this request, including your pending requests.')
        db.execute('INSERT INTO redemptions(reward,title,price,at,child_id) VALUES (?,?,?,?,?)',(row['id'],row['title'],row['price'],now(),f.current(db)))
    elif path == 'redemption-review':
        row = required(db,'redemptions',data.get('id'))
        if row['status'] != 'pending':
            raise ValueError('Already reviewed.')
        approve = data.get('approve') is True
        if approve:
            post(db,'redemption',row['title'],uid,{'wallet':-row['price'],'store':row['price']},key)
        db.execute('UPDATE redemptions SET status=? WHERE id=?',('approved' if approve else 'declined',row['id']))
    elif path == 'investment-save':
        name, symbol = label(data.get('name')), label(data.get('symbol')).upper()
        import re
        if not re.fullmatch(r'[A-Z0-9.\-]{1,16}',symbol):
            raise ValueError('Use a stock or ETF ticker, for example SPY.')
        risk = data.get('risk','Growth')
        if risk not in ('Lower','Balanced','Growth','High'):
            raise ValueError('Choose a valid risk level.')
        if data.get('id'):
            row = required(db,'investments',data['id'])
            if row['symbol'] != symbol:
                raise ValueError('Tickers cannot change after creation; create a new investment.')
            db.execute('UPDATE investments SET name=?,risk=?,enabled=? WHERE id=?',(name,risk,int(bool(data.get('enabled',True))),row['id']))
        else:
            db.execute('INSERT INTO investments(name,symbol,risk) VALUES (?,?,?)',(name,symbol,risk))
    elif path == 'committee-save':
        allocations = data.get('allocations')
        if not isinstance(allocations,dict) or not allocations:
            raise ValueError('Add allocations totaling 100%.')
        total = Decimal(0)
        clean = {}
        for ident, weight in allocations.items():
            try:
                weight = Decimal(str(weight))
                if not weight.is_finite() or weight <= 0 or weight > 100:
                    raise ValueError()
                if ident != 'cash':
                    required(db,'investments',int(ident))
                total += weight
                clean[ident] = str(weight)
            except (InvalidOperation,ValueError):
                raise ValueError('Each allocation must be positive and reference an available investment.')
        if total != 100:
            raise ValueError('Allocations must total exactly 100%.')
        if data.get('id'):
            required(db,'committees',data['id'])
            db.execute('UPDATE committees SET name=?,allocations=?,enabled=? WHERE id=?',(label(data.get('name')),json.dumps(clean),int(bool(data.get('enabled',True))),data['id']))
        else:
            db.execute('INSERT INTO committees(name,allocations) VALUES (?,?)',(label(data.get('name')),json.dumps(clean)))
    elif path == 'buy':
        amount = money(data.get('amount'))
        table = 'committees' if data.get('type') == 'committee' else 'investments'
        level = db.execute("SELECT value FROM child_settings WHERE child_id=? AND key='learning_level'",(f.current(db),)).fetchone()[0]
        if user['role']=='child' and table=='investments' and level=='beginner':
            raise PermissionError('Individual investments unlock at Intermediate. Ask your parent to change your learning level.')
        row = required(db,table,data.get('id'))
        if not row['enabled']:
            raise ValueError('This investment is paused.')
        allocations = json.loads(row['allocations']) if table == 'committees' else {str(row['id']):100}
        holdings = {}
        for ident, weight in allocations.items():
            part = Decimal(amount) * Decimal(str(weight)) / 100
            if ident == 'cash':
                holdings[ident] = str(part)
            else:
                asset = required(db,'investments',int(ident))
                if not asset['enabled']:
                    raise ValueError('A committee investment is paused.')
                holdings[ident] = str(part / quote(db,int(ident),True))
        post(db,'investment-buy','Invested in ' + row['name'],uid,{'wallet':-amount,'market':amount},key)
        db.execute('INSERT INTO positions(name,cost,holdings,opened,child_id) VALUES (?,?,?,?,?)',(row['name'],amount,json.dumps(holdings),now(),f.current(db)))
    elif path == 'sell':
        row = required(db,'positions',data.get('id'))
        if row['closed']:
            raise ValueError('This position is already sold.')
        amount = valuation(db,json.loads(row['holdings']),True,row['opened'])
        post(db,'investment-sell','Sold ' + row['name'],uid,{'market':-amount,'wallet':amount},key)
        db.execute('UPDATE positions SET closed=?,proceeds=? WHERE id=?',(now(),amount,row['id']))
    elif path == 'learning-settings':
        name = label(data.get('coin_name'))
        if len(name)>24 or not all(c.isalnum() or c in " -'" for c in name):
            raise ValueError('Currency name must be 1–24 letters, numbers, spaces, apostrophes or hyphens.')
        level=data.get('learning_level')
        if level not in ('beginner','intermediate'):
            raise ValueError('Choose Beginner or Intermediate.')
        db.execute("INSERT INTO settings VALUES ('coin_name',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(name,))
        db.execute("UPDATE child_settings SET value=? WHERE child_id=? AND key='learning_level'",(level,f.current(db)))
    elif path == 'settings':
        apr = Decimal(str(data.get('apr')))
        if not apr.is_finite() or apr < 0 or apr > 100:
            raise ValueError('Annual interest must be between 0 and 100%.')
        allowance = money(data.get('allowance'),True)
        db.execute("UPDATE child_settings SET value=? WHERE key='apr' AND child_id=?",(str(apr),f.current(db)))
        db.execute("UPDATE child_settings SET value=? WHERE key='allowance' AND child_id=?",(str(allowance),f.current(db)))
    else:
        raise ValueError('Unknown action.')


def record_portfolio(db,child_id):
    assets={}
    for p in db.execute('SELECT * FROM positions WHERE child_id=? AND closed IS NULL',(child_id,)):
        value=valuation(db,json.loads(p['holdings']),opened=p['opened'])
        assets[p['name']]=assets.get(p['name'],0)+value
    encoded=json.dumps(assets,sort_keys=True)
    stamp=now()
    last=db.execute('SELECT at,assets FROM portfolio_history WHERE child_id=? ORDER BY id DESC LIMIT 1',(child_id,)).fetchone()
    if not last or last['assets']!=encoded or (dt.datetime.fromisoformat(stamp)-dt.datetime.fromisoformat(last['at'])).total_seconds()>=900:
        db.execute('INSERT INTO portfolio_history(child_id,at,value,assets) VALUES (?,?,?,?)',(child_id,stamp,sum(assets.values()),encoded))


def portfolio_history(db,child_id):
    points=[dict(r) for r in db.execute("""SELECT at,value,assets FROM portfolio_history WHERE child_id=? AND id IN
        (SELECT MAX(id) FROM portfolio_history WHERE child_id=? GROUP BY CASE WHEN at<datetime('now','-7 days') THEN substr(at,1,10) ELSE at END) ORDER BY id""",(child_id,child_id))]
    positions=db.execute('SELECT name,cost,opened,closed,proceeds FROM positions WHERE child_id=?',(child_id,)).fetchall()
    for point in points:
        gains=json.loads(point['assets'])
        at=dt.datetime.fromisoformat(point['at'])
        for p in positions:
            if dt.datetime.fromisoformat(p['opened'])<=at:
                gains[p['name']]=gains.get(p['name'],0)-p['cost']
                if p['closed'] and dt.datetime.fromisoformat(p['closed'])<=at:
                    gains[p['name']]+=p['proceeds']
        point['gain']=sum(gains.values())
        point['gains']=gains
    return points


def state(db, user, selected=None):
    child_id=f.select(db,user,selected)
    record_portfolio(db,child_id)
    def rows(table):
        return [dict(r) for r in db.execute('SELECT * FROM ' + table + (' WHERE child_id=?' if table in (f.SCOPED | {'goals'}) else ''),(child_id,) if table in (f.SCOPED | {'goals'}) else ())]
    investments = rows('investments')
    for asset in investments:
        price = db.execute('SELECT * FROM prices WHERE investment=? ORDER BY day DESC LIMIT 1',(asset['id'],)).fetchone()
        asset['quote'] = dict(price) if price else None
    positions = rows('positions')
    for p in positions:
        p['value'] = p['proceeds'] if p['closed'] else valuation(db,json.loads(p['holdings']),opened=p['opened'])
        p['profit'] = p['value'] - p['cost']
    ledger = [dict(r) for r in db.execute('''SELECT t.*,COALESCE(u.name,'System') actor_name,COALESCE(u.role,'system') actor_role,COALESCE(SUM(CASE WHEN e.account='wallet' THEN e.amount ELSE 0 END),0) wallet,
        COALESCE(SUM(CASE WHEN e.account='savings' THEN e.amount ELSE 0 END),0) savings,
        COALESCE(SUM(CASE WHEN e.account='cashout_hold' THEN e.amount ELSE 0 END),0) held,
        EXISTS(SELECT 1 FROM transactions r WHERE r.reversal_of=t.id) reversed FROM transactions t LEFT JOIN users u ON u.id=t.actor LEFT JOIN entries e ON e.tx=t.id WHERE t.child_id=? GROUP BY t.id ORDER BY t.id DESC''',(child_id,))]
    settings={**dict(db.execute('SELECT key,value FROM settings').fetchall()),**dict(db.execute('SELECT key,value FROM child_settings WHERE child_id=?',(child_id,)).fetchall())}
    audit=[]
    if user['role']=='parent':
        audit=[dict(r) for r in db.execute('SELECT a.*,u.name actor_name,u.role actor_role FROM audit_log a LEFT JOIN users u ON u.id=a.actor WHERE a.child_id=? OR a.child_id IS NULL ORDER BY a.id DESC LIMIT 500',(child_id,))]
    else:
        settings={k:v for k,v in settings.items() if k in ('apr','coin_name','learning_level')}
        for t in ledger:
            if t['actor_role']=='parent':
                t['reason']={'adjustment':'Wallet updated','reversal':'Account correction','investment-buy':'Investment added','investment-sell':'Investment sold','transfer':'Money transferred'}.get(t['kind'],t['kind'].replace('-',' ').capitalize())
            for key in ('actor','actor_name','actor_role','request_key'):
                t.pop(key,None)
    return dict(goals=rows('goals'),reserved_savings=features.goal_reserved(db,child_id),bank_needs=features.bank_needs(db,dt.datetime.now(UTC).date()) if user['role']=='parent' else None,today=str(features.local_today()),pending_chores=[dict(r) for r in db.execute("SELECT c.*,u.name child_name FROM completions c JOIN users u ON u.id=c.child_id WHERE c.status='pending' ORDER BY c.at,c.id")] if user['role']=='parent' else [],portfolio_history=portfolio_history(db,child_id),bank_funds=[dict(r) for r in db.execute('SELECT id,at,amount,reason FROM bank_funds ORDER BY id DESC')] if user['role']=='parent' else [],bank_balance=db.execute('SELECT COALESCE(SUM(amount),0) FROM bank_funds').fetchone()[0] if user['role']=='parent' else None,audit=audit,user={k:user[k] for k in ('id','name','role')},child=db.execute("SELECT name FROM users WHERE id=?",(child_id,)).fetchone()[0],child_id=child_id,children=[dict(r) for r in db.execute("SELECT id,name FROM users WHERE role='child' ORDER BY id")] if user['role']=='parent' else [],
        accounts=[dict(r) for r in db.execute('SELECT id,name,role,username FROM users')] if user['role']=='parent' else [],cashouts=rows('cashouts'),held=balance(db,'cashout_hold'),
        wallet=balance(db,'wallet'),savings=balance(db,'savings'),invested=sum(p['value'] for p in positions if not p['closed']),
        profit=sum(p['profit'] for p in positions),positions=positions,investments=investments,committees=rows('committees'),
        chores=rows('chores'),completions=rows('completions'),rewards=rows('rewards'),redemptions=rows('redemptions'),ledger=ledger,
        settings=settings,market_configured=True)
