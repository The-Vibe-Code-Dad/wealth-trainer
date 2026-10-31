import datetime as dt
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from decimal import Decimal
import urllib.request
import urllib.error
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from app import domain as d
from app import server, market


class EconomyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = d.DB_PATH
        d.DB_PATH = self.tmp.name + '/test.db'
        d.init()
        self.db = d.connect()
        self.db.execute("UPDATE committees SET enabled=1 WHERE id IN (1,2)")
        self.db.execute("INSERT INTO users VALUES (1,'Parent','parent','test','test-parent'),(2,'Child','child','test','test-child')")
        self.parent = {'id':1,'role':'parent'}
        self.child = {'id':2,'role':'child'}
        d.f.initialize_child(self.db,2)
        self.db.execute("UPDATE child_settings SET value='intermediate' WHERE key='learning_level'")
        self.db.execute("INSERT INTO bank_funds(at,amount,reason,actor,request_key) VALUES ('2026-01-01',1000000000,'Test reserve',1,'test-reserve')")
        self.n = 0

    def test_learning_controls_and_mix_migration(self):
        self.fund()
        self.prices()
        self.act('buy',{'id':1,'amount':100},self.child)
        self.act('learning-settings',{'coin_name':'Stars','learning_level':'beginner'})
        with self.assertRaises(PermissionError):
            self.act('buy',{'id':1,'amount':100},self.child)
        with self.assertRaises(PermissionError):
            self.act('learning-settings',{'coin_name':'Stars','learning_level':'intermediate'},self.child)
        with self.assertRaises(ValueError):
            self.act('learning-settings',{'coin_name':'<script>','learning_level':'beginner'})
        mix=self.db.execute("SELECT id FROM committees WHERE name='Balanced'").fetchone()[0]
        self.act('buy',{'id':mix,'type':'committee','amount':200},self.child)
        position=self.db.execute('SELECT id FROM positions ORDER BY id LIMIT 1').fetchone()[0]
        self.act('sell',{'id':position},self.child)
        state=d.state(self.db,dict(self.child,name='Child'))
        self.assertEqual(state['settings']['coin_name'],'Stars')
        self.assertEqual(state['settings']['learning_level'],'beginner')
        d.init()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM committees WHERE name IN ('Aggressive','Balanced','Defensive')").fetchone()[0],3)

    def test_parent_log_and_child_privacy(self):
        self.act('adjust',{'amount':100,'direction':'add','reason':'Private parent note'})
        self.act('transfer',{'amount':25,'source':'wallet'},self.child)
        self.db.execute("INSERT INTO settings VALUES ('private_config','parent-only')")
        child=d.state(self.db,dict(self.child,name='Child'))
        parent=d.state(self.db,dict(self.parent,name='Parent'))
        self.assertEqual(child['wallet'],7500)
        self.assertEqual(child['audit'],[])
        self.assertEqual(child['accounts'],[])
        self.assertNotIn('private_config',child['settings'])
        self.assertNotIn('Private parent note',json.dumps(child))
        self.assertNotIn('request_key',child['ledger'][0])
        self.assertEqual(parent['ledger'][1]['reason'],'Private parent note')
        self.assertEqual([a['actor_role'] for a in parent['audit']],['child','parent'])
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("DELETE FROM audit_log")
        self.db.execute("INSERT INTO users VALUES (3,'Sibling','child','test','sibling')")
        other=d.state(self.db,dict(self.parent,name='Parent'),3)
        self.assertEqual(other['audit'],[])

    def test_bank_funding_transfers_privacy_and_limits(self):
        initial=d.bank_balance(self.db)
        self.act('bank-fund',{'amount':100,'reason':'Private bank deposit'})
        self.assertEqual(d.balance(self.db,'wallet'),0)
        self.assertEqual(d.bank_balance(self.db),initial+10000)
        self.act('adjust',{'amount':40,'reason':'Gift'})
        self.assertEqual(d.bank_balance(self.db),initial+6000)
        self.assertEqual(d.balance(self.db,'wallet'),4000)
        tx=self.db.execute('SELECT MAX(id) FROM transactions').fetchone()[0]
        self.act('reverse',{'id':tx,'reason':'Undo gift'})
        self.assertEqual(d.bank_balance(self.db),initial+10000)
        with self.assertRaises(ValueError):
            self.act('adjust',{'amount':100000000,'reason':'Overdraw'})
        self.assertEqual(d.balance(self.db,'wallet'),0)
        with self.assertRaises(PermissionError):
            self.act('bank-fund',{'amount':10,'reason':'Child attempt'},self.child)
        child=d.state(self.db,dict(self.child,name='Child'))
        self.assertIsNone(child['bank_balance'])
        self.assertEqual(child['bank_funds'],[])
        self.assertNotIn('Private bank deposit',json.dumps(child))

    def test_scheduled_payments_wait_for_bank_funding(self):
        self.db.execute("INSERT INTO bank_funds(at,amount,reason,actor,request_key) VALUES ('2026-01-01',-1000000000,'Empty test reserve',1,'empty-reserve')")
        today=dt.datetime.now(dt.timezone.utc).date()
        self.act('settings',{'apr':5,'allowance':20})
        self.db.execute("UPDATE child_settings SET value=? WHERE key='allowance_next'",(str(today),))
        d.scheduled(self.db,today)
        self.assertEqual(d.balance(self.db,'wallet'),0)
        self.assertEqual(self.db.execute("SELECT value FROM child_settings WHERE key='allowance_next'").fetchone()[0],str(today))
        self.act('bank-fund',{'amount':30,'reason':'Fund allowance'})
        d.scheduled(self.db,today)
        d.scheduled(self.db,today)
        self.assertEqual(d.balance(self.db,'wallet'),2000)
        self.assertEqual(d.bank_balance(self.db),1000)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('DELETE FROM bank_funds')

    def test_preset_cash_migration_preserves_existing_purchases(self):
        self.fund()
        self.prices()
        for row in self.db.execute('SELECT allocations FROM committees'):
            allocation=json.loads(row[0])
            self.assertNotIn('cash',allocation)
            self.assertEqual(sum(Decimal(str(v)) for v in allocation.values()),100)
        self.db.execute("DELETE FROM settings WHERE key='fully_invested_presets_v1'")
        old={'1':50,'2':30,'cash':20}
        self.db.execute('UPDATE committees SET allocations=? WHERE id=1',(json.dumps(old),))
        self.act('buy',{'id':1,'type':'committee','amount':1000})
        before=tuple(self.db.execute('SELECT * FROM positions').fetchone())
        self.act('committee-save',{'name':'My own mix','allocations':{'1':70,'cash':30}})
        d.init()
        d.init()
        self.assertEqual(tuple(self.db.execute('SELECT * FROM positions').fetchone()),before)
        self.assertNotIn('cash',json.loads(self.db.execute('SELECT allocations FROM committees WHERE id=1').fetchone()[0]))
        self.assertEqual(json.loads(self.db.execute("SELECT allocations FROM committees WHERE name='My own mix'").fetchone()[0])['cash'],'30')

    def test_portfolio_history_values_and_child_isolation(self):
        self.fund()
        self.prices()
        self.act('buy',{'id':1,'amount':1000},self.child)
        self.prices(1.1)
        first=d.state(self.db,dict(self.child,name='Child'))
        self.assertEqual(first['portfolio_history'][-1]['value'],110000)
        self.assertEqual(sum(json.loads(first['portfolio_history'][-1]['assets']).values()),110000)
        count=len(first['portfolio_history'])
        self.assertEqual(len(d.state(self.db,dict(self.child,name='Child'))['portfolio_history']),count)
        self.act('sell',{'id':1},self.child)
        self.assertEqual(d.state(self.db,dict(self.child,name='Child'))['portfolio_history'][-1]['value'],0)
        self.db.execute("INSERT INTO users VALUES (3,'Sibling','child','test','sibling')")
        sibling=d.state(self.db,dict(self.parent,name='Parent'),3)
        self.assertTrue(all(p['value']==0 for p in sibling['portfolio_history']))
        self.assertGreater(count,1)

    def test_parent_chore_inbox_across_children_and_payment(self):
        self.db.execute("INSERT INTO users VALUES (3,'Sibling','child','test','sibling')")
        self.act('chore-save',{'child_id':3,'title':'Dishes','amount':25})
        chore=self.db.execute('SELECT id FROM chores WHERE child_id=3').fetchone()[0]
        self.act('chore-complete',{'id':chore},{'id':3,'role':'child'})
        parent=d.state(self.db,dict(self.parent,name='Parent'),2)
        self.assertEqual(len(parent['pending_chores']),1)
        request=parent['pending_chores'][0]
        self.assertEqual(request['child_name'],'Sibling')
        self.assertEqual(d.state(self.db,dict(self.child,name='Child'))['pending_chores'],[])
        before=d.bank_balance(self.db)
        self.act('chore-review',{'id':request['id'],'child_id':3,'approve':True})
        self.assertEqual(d.bank_balance(self.db),before-2500)
        self.assertEqual(d.state(self.db,dict(self.parent,name='Parent'),3)['wallet'],2500)
        self.assertEqual(d.state(self.db,dict(self.parent,name='Parent'),2)['wallet'],0)
        self.assertEqual(d.state(self.db,dict(self.parent,name='Parent'))['pending_chores'],[])
        with self.assertRaises(ValueError):
            self.act('chore-review',{'id':request['id'],'child_id':3,'approve':True})
        self.assertEqual(d.bank_balance(self.db),before-2500)

    def test_simple_presets_retire_duplicates_without_changing_holdings(self):
        self.fund()
        self.prices()
        self.act('buy',{'id':1,'type':'committee','amount':100})
        before=tuple(self.db.execute('SELECT * FROM positions').fetchone())
        self.db.execute("DELETE FROM settings WHERE key='simple_presets_v1'")
        d.init()
        d.init()
        self.assertEqual([r[0] for r in self.db.execute('SELECT name FROM committees WHERE enabled=1')],['Aggressive','Balanced','Defensive'])
        self.assertEqual(tuple(self.db.execute('SELECT * FROM positions').fetchone()),before)
        self.assertEqual(json.loads(self.db.execute("SELECT allocations FROM committees WHERE name='Balanced'").fetchone()[0]),{'1':60,'3':40})
        self.act('sell',{'id':1})

    def tearDown(self):
        self.db.close()
        d.DB_PATH = self.old
        self.tmp.cleanup()

    def act(self,path,data,user=None):
        self.n += 1
        self.db.execute('BEGIN IMMEDIATE')
        try:
            d.action(self.db,user or self.parent,path,data,'test-key-'+str(self.n))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def fund(self,amount=5000):
        self.act('adjust',{'amount':amount,'reason':'Welcome','direction':'add'})

    def prices(self,multiplier=1):
        day = str(dt.date.today())
        for ident in range(1,5):
            self.db.execute('INSERT INTO prices(investment,price,day,source,fetched) VALUES (?,?,?,?,?) ON CONFLICT(investment,day) DO UPDATE SET price=excluded.price',
                            (ident,str(100*multiplier),day,'test fixture',d.now()))

    def test_money_precision(self):
        self.assertEqual(d.money('1.25'),125)
        for v in ['NaN','Infinity','-1','0','0.001','100000001']:
            with self.assertRaises(ValueError): d.money(v)

    def test_children_have_separate_balances_and_private_records(self):
        self.db.execute("INSERT INTO users VALUES (3,'Second child','child','test','second-child')")
        other={'id':3,'role':'child','name':'Second child'}
        first={**self.child,'name':'Child'}
        self.act('adjust',{'child_id':2,'amount':1000,'reason':'First child'})
        self.act('adjust',{'child_id':3,'amount':200,'reason':'Second child'})
        self.prices()
        self.act('buy',{'id':1,'amount':100},first)
        self.act('cashout-request',{'amount':50},first)
        self.act('chore-save',{'child_id':2,'title':'First chore','amount':10})
        self.act('reward-save',{'child_id':2,'title':'First reward','amount':10})
        a=d.state(self.db,first)
        b=d.state(self.db,other)
        self.assertEqual(a['wallet'],85000)
        self.assertEqual(a['invested'],10000)
        self.assertEqual(a['held'],5000)
        self.assertEqual(b['wallet'],20000)
        for key in ('positions','cashouts','chores','rewards'):
            self.assertEqual(b[key],[])
        self.assertEqual(len(b['ledger']),1)
        with self.assertRaises(PermissionError): d.state(self.db,other,2)
        for path,data in [('sell',{'id':1}),('cashout-cancel',{'id':1}),('chore-complete',{'id':1}),('redeem',{'id':1})]:
            with self.assertRaises(ValueError): self.act(path,data,other)
        with self.assertRaises(PermissionError): self.act('transfer',{'source':'wallet','amount':1,'child_id':2},other)
        with self.assertRaises(ValueError): self.act('reverse',{'child_id':3,'id':1,'reason':'Wrong child'})
        self.assertEqual(d.state(self.db,{**self.parent,'name':'Parent'},3)['wallet'],20000)

    def test_allowances_are_scheduled_independently(self):
        self.db.execute("INSERT INTO users VALUES (3,'Second child','child','test','second-child')")
        self.act('settings',{'child_id':2,'apr':5,'allowance':100})
        self.act('settings',{'child_id':3,'apr':10,'allowance':200})
        today=dt.datetime.now(d.UTC).date()
        self.db.execute("UPDATE child_settings SET value=? WHERE key='allowance_next'",(str(today),))
        d.scheduled(self.db,today)
        d.scheduled(self.db,today)
        self.db.child_id=2
        self.assertEqual(d.balance(self.db,'wallet'),10000)
        self.db.child_id=3
        self.assertEqual(d.balance(self.db,'wallet'),20000)

    def test_ledger_balanced_immutable_and_no_overdraft(self):
        self.fund(500)
        self.act('transfer',{'source':'wallet','amount':200},self.child)
        self.assertEqual(d.balance(self.db,'wallet'),30000)
        self.assertEqual(d.balance(self.db,'savings'),20000)
        with self.assertRaises(ValueError): self.act('transfer',{'source':'wallet','amount':301})
        self.assertEqual(self.db.execute('SELECT SUM(amount) FROM entries').fetchone()[0],0)
        with self.assertRaises(Exception): self.db.execute('DELETE FROM entries')
        with self.assertRaises(Exception): self.db.execute("UPDATE transactions SET reason='changed'")

    def test_parent_only(self):
        for path in ['adjust','reverse','chore-save','chore-review','reward-save','redemption-review','investment-save','committee-save','settings','cashout-review']:
            with self.assertRaises(PermissionError): self.act(path,{},self.child)

    def test_chore_approval_exactly_once_and_snapshot_reward(self):
        self.act('chore-save',{'title':'Clean room','amount':200})
        self.act('chore-complete',{'id':1},self.child)
        self.act('chore-save',{'id':1,'title':'Clean room','amount':500})
        self.act('chore-review',{'id':1,'approve':True})
        with self.assertRaises(ValueError): self.act('chore-review',{'id':1,'approve':True})
        self.assertEqual(d.balance(self.db,'wallet'),20000)

    def test_redemption_approval_checks_current_balance(self):
        self.fund(500)
        self.act('reward-save',{'title':'Movie night','amount':400})
        self.act('redeem',{'id':1},self.child)
        self.act('transfer',{'source':'wallet','amount':200})
        with self.assertRaises(ValueError): self.act('redemption-review',{'id':1,'approve':True})
        self.assertEqual(self.db.execute('SELECT status FROM redemptions').fetchone()[0],'pending')
        self.act('transfer',{'source':'savings','amount':200})
        self.act('redemption-review',{'id':1,'approve':True})
        self.assertEqual(d.balance(self.db,'wallet'),10000)

    def test_reverse_is_append_only_and_once(self):
        self.fund(500)
        self.act('reverse',{'id':1,'reason':'Correction'})
        self.assertEqual(d.balance(self.db,'wallet'),0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0],2)
        with self.assertRaises(ValueError): self.act('reverse',{'id':1,'reason':'Again'})

    def test_investment_market_return_and_no_double_sale(self):
        self.fund()
        self.prices()
        self.act('buy',{'id':1,'amount':1000},self.child)
        self.prices(1.08)
        self.act('sell',{'id':1},self.child)
        self.assertEqual(d.balance(self.db,'wallet'),508000)
        with self.assertRaises(ValueError): self.act('sell',{'id':1},self.child)

    def test_committee_cash_and_snapshot(self):
        self.act('committee-save',{'id':1,'name':'Custom cash mix','allocations':{'1':50,'2':30,'cash':20}})
        self.fund()
        self.prices()
        self.act('buy',{'id':1,'type':'committee','amount':1000})
        self.act('committee-save',{'id':1,'name':'Changed','allocations':{'cash':100}})
        self.prices(1.10)
        self.act('sell',{'id':1})
        self.assertEqual(d.balance(self.db,'wallet'),508000)

    def test_missing_stale_disabled_quotes_cannot_buy(self):
        self.fund()
        with self.assertRaises(ValueError): self.act('buy',{'id':1,'amount':1000})
        self.prices()
        self.db.execute("UPDATE prices SET day='2020-01-01'")
        with self.assertRaises(ValueError): self.act('buy',{'id':1,'amount':1000})
        self.assertEqual(d.balance(self.db,'wallet'),500000)
        self.prices()
        self.db.execute('UPDATE investments SET enabled=0 WHERE id=1')
        with self.assertRaises(ValueError): self.act('buy',{'id':1,'amount':1000})

    def test_concurrent_withdrawals_cannot_overdraw(self):
        self.fund(100)
        barrier = threading.Barrier(2)
        results = []
        def withdraw(key):
            with d.connect() as db:
                barrier.wait()
                db.execute('BEGIN IMMEDIATE')
                try:
                    d.action(db,self.child,'transfer',{'source':'wallet','amount':80},key)
                    db.commit()
                    results.append('ok')
                except ValueError:
                    db.rollback()
                    results.append('rejected')
        threads = [threading.Thread(target=withdraw,args=('concurrent-'+str(i),)) for i in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertCountEqual(results,['ok','rejected'])
        self.assertEqual(d.balance(self.db,'wallet'),2000)

    def test_daily_interest_and_allowance_are_idempotent(self):
        self.fund(1000)
        self.act('transfer',{'source':'wallet','amount':1000})
        today = dt.datetime.now(d.UTC).date()
        self.db.execute("UPDATE child_settings SET value='36.5' WHERE key='apr'")
        self.db.execute("UPDATE child_settings SET value=? WHERE key='interest_day'",(str(today-dt.timedelta(days=2)),))
        self.db.execute("UPDATE child_settings SET value='20000' WHERE key='allowance'")
        self.db.execute("UPDATE child_settings SET value=? WHERE key='allowance_next'",(str(today),))
        d.scheduled(self.db,today)
        d.scheduled(self.db,today)
        self.assertEqual(d.balance(self.db,'savings'),100200)
        self.assertEqual(d.balance(self.db,'wallet'),20000)

    def test_quote_provider_contract(self):
        import io
        import time
        payload={'chart':{'result':[{'meta':{'symbol':'SPY','currency':'USD','regularMarketPrice':101.50,'regularMarketTime':int(time.time())}}]}}
        with patch('urllib.request.urlopen',side_effect=lambda *a,**kw:io.BytesIO(json.dumps(payload).encode())):
            result=server.refresh_prices()
        self.assertEqual(result['updated'],['SPY'])
        self.assertEqual(len(result['failed']),9)
        self.assertEqual(d.quote(self.db,1),Decimal('101.50'))

    def test_cashout_hold_approval_decline_cancel(self):
        self.fund(1000)
        self.act('cashout-request',{'amount':400,'reason':'Pocket money'},self.child)
        self.assertEqual(d.balance(self.db,'wallet'),60000)
        self.assertEqual(d.balance(self.db,'cashout_hold'),40000)
        with self.assertRaises(ValueError): self.act('cashout-request',{'amount':601})
        self.act('cashout-review',{'id':1,'approve':True})
        self.assertEqual(d.balance(self.db,'cashout_hold'),0)
        with self.assertRaises(ValueError): self.act('cashout-review',{'id':1,'approve':True})
        self.act('cashout-request',{'amount':200})
        self.act('cashout-review',{'id':2,'approve':False})
        self.act('cashout-request',{'amount':100})
        self.act('cashout-cancel',{'id':3},self.child)
        self.assertEqual(d.balance(self.db,'wallet'),60000)
        self.assertEqual(self.db.execute('SELECT SUM(amount) FROM entries').fetchone()[0],0)
        with self.assertRaises(ValueError): self.act('cashout-request',{'amount':'0.5'})

    def test_split_adjusts_only_positions_owned_before_event(self):
        self.fund()
        self.prices()
        self.act('buy',{'id':1,'amount':1000})
        yesterday=(dt.datetime.now(d.UTC)-dt.timedelta(days=1)).isoformat()
        self.db.execute('UPDATE positions SET opened=?',(yesterday,))
        stamp=int(dt.datetime.now(d.UTC).timestamp())
        self.db.execute('INSERT INTO splits VALUES (1,?,?)',(stamp,'2'))
        self.prices(0.5)
        self.act('sell',{'id':1})
        self.assertEqual(d.balance(self.db,'wallet'),500000)
        self.act('buy',{'id':1,'amount':1000})
        self.act('sell',{'id':2})
        self.assertEqual(d.balance(self.db,'wallet'),500000)

    def test_yahoo_failure_keeps_last_good_price(self):
        self.prices()
        with patch('app.market.fetch',side_effect=ValueError('unavailable')):
            result=market.refresh_prices()
        self.assertEqual(result['updated'],[])
        self.assertEqual(d.quote(self.db,1),100)


class HttpTests(unittest.TestCase):
    def test_existing_role_accounts_migrate_without_losing_passwords(self):
        with tempfile.TemporaryDirectory() as temp:
            old=d.DB_PATH
            d.DB_PATH=temp+'/legacy.db'
            try:
                db=sqlite3.connect(d.DB_PATH)
                db.execute('CREATE TABLE users(id INTEGER PRIMARY KEY,name TEXT NOT NULL,role TEXT UNIQUE NOT NULL,password TEXT NOT NULL)')
                db.execute("INSERT INTO users VALUES (1,'Parent','parent','existing-hash')")
                db.execute("INSERT INTO users VALUES (2,'First child','child','child-hash')")
                db.executescript('''CREATE TABLE accounts(id TEXT PRIMARY KEY);
                    INSERT INTO accounts VALUES ('wallet'),('bank');
                    CREATE TABLE transactions(id INTEGER PRIMARY KEY,at TEXT NOT NULL,kind TEXT NOT NULL,reason TEXT NOT NULL,actor INTEGER REFERENCES users,reversal_of INTEGER UNIQUE REFERENCES transactions,request_key TEXT UNIQUE);
                    CREATE TABLE entries(id INTEGER PRIMARY KEY,tx INTEGER REFERENCES transactions,account TEXT REFERENCES accounts,amount INTEGER NOT NULL);
                    INSERT INTO transactions VALUES (1,'2026-01-01','adjustment','Existing deposit',1,NULL,'old-request');
                    INSERT INTO entries VALUES (1,1,'wallet',12345),(2,1,'bank',-12345);
                ''')
                db.commit()
                db.close()
                d.init()
                d.init()
                with d.connect() as db:
                    row=db.execute('SELECT * FROM users').fetchone()
                    self.assertEqual(row['username'],'parent')
                    self.assertEqual(row['password'],'existing-hash')
                    self.assertEqual(d.balance(db,'wallet'),12345)
                    self.assertEqual(db.execute('SELECT reason FROM transactions WHERE id=1').fetchone()[0],'Existing deposit')
                    self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())
                    db.execute("INSERT INTO users VALUES (3,'Another child','child','new-hash','another-child')")
            finally:
                d.DB_PATH=old

    def test_setup_auth_csrf_roles_and_replay(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{'BOOTSTRAP_TOKEN':'test-bootstrap-token'}):
            old=d.DB_PATH
            d.DB_PATH=temp+'/http.db'
            d.init()
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
            thread=threading.Thread(target=http.serve_forever,daemon=True)
            thread.start()
            base=f'http://127.0.0.1:{http.server_port}'
            server.ATTEMPTS.clear()
            def req(path,data=None,headers=None):
                request=urllib.request.Request(base+path,data=None if data is None else json.dumps(data).encode(),headers={'Content-Type':'application/json',**(headers or {})})
                try: response=urllib.request.urlopen(request)
                except urllib.error.HTTPError as exc: response=exc
                with response: return response.status,json.load(response),response.headers
            try:
                self.assertEqual(req('/api/state')[1],{'setup':True})
                self.assertEqual(req('/api/setup',{'token':'wrong'})[0],403)
                setup={'token':'test-bootstrap-token','parent_name':'P','child_name':'C','parent_username':'family-parent','child_username':'young-saver','parent_password':'parent-secret-123','child_password':'child-secret-123'}
                self.assertEqual(req('/api/setup',{**setup,'child_username':'FAMILY-PARENT'})[0],400)
                self.assertEqual(req('/api/setup',setup)[0],200)
                self.assertEqual(req('/api/state')[0],401)
                self.assertEqual(req('/api/login',{'role':'child','password':'child-secret-123'})[0],403)
                status,_,headers=req('/api/login',{'username':'YOUNG-SAVER','password':'child-secret-123'})
                self.assertEqual(status,200)
                cookie=headers['Set-Cookie'].split(';')[0]
                csrf=req('/api/state',headers={'Cookie':cookie})[1]['csrf']
                self.assertEqual(req('/api/adjust',{'amount':10,'reason':'hack'},{'Cookie':cookie,'X-CSRF-Token':csrf,'Idempotency-Key':'child-attempt'})[0],403)
                _,_,headers=req('/api/login',{'username':'family-parent','password':'parent-secret-123'})
                cookie=headers['Set-Cookie'].split(';')[0]
                self.assertEqual(req('/api/adjust',{'amount':10,'reason':'test'},{'Cookie':cookie})[0],403)
                csrf=req('/api/state',headers={'Cookie':cookie})[1]['csrf']
                headers={'Cookie':cookie,'X-CSRF-Token':csrf,'Idempotency-Key':'unique-request-123'}
                self.assertEqual(req('/api/bank-fund',{'amount':1000,'reason':'Bank funding'},{**headers,'Idempotency-Key':'bank-funding-http'})[0],200)
                for _ in range(2):self.assertEqual(req('/api/adjust',{'amount':10,'reason':'test'},headers)[0],200)
                self.assertEqual(req('/api/state',headers={'Cookie':cookie})[1]['wallet'],1000)
                account={'id':2,'name':'New child name','username':'new-child','password':'changed-child-secret','current_password':'wrong-password'}
                headers['Idempotency-Key']='account-change-1'
                self.assertEqual(req('/api/account-save',account,headers)[0],403)
                account['current_password']='parent-secret-123'
                self.assertEqual(req('/api/account-save',account,headers)[0],200)
                self.assertEqual(req('/api/login',{'username':'new-child','password':'changed-child-secret'})[0],200)
                self.assertEqual(req('/api/login',{'username':'young-saver','password':'child-secret-123'})[0],403)
                headers['Idempotency-Key']='create-second-child'
                self.assertEqual(req('/api/account-add',{'name':'Second','username':'second-child','password':'second-child-password','current_password':'parent-secret-123'},headers)[0],200)
                second=req('/api/state?child_id=3',headers={'Cookie':cookie})[1]
                self.assertEqual(second['wallet'],0)
                self.assertEqual(len(second['children']),2)
                headers['Idempotency-Key']='deposit-second-child'
                self.assertEqual(req('/api/adjust',{'child_id':3,'amount':50,'reason':'Second child deposit'},headers)[0],200)
                self.assertEqual(req('/api/state?child_id=3',headers={'Cookie':cookie})[1]['wallet'],5000)
                self.assertEqual(req('/api/state?child_id=2',headers={'Cookie':cookie})[1]['wallet'],1000)
                _,_,second_headers=req('/api/login',{'username':'second-child','password':'second-child-password'})
                second_cookie=second_headers['Set-Cookie'].split(';')[0]
                self.assertEqual(req('/api/state?child_id=2',headers={'Cookie':second_cookie})[0],403)
                for _ in range(10):
                    self.assertEqual(req('/api/login',{'username':'family-parent','password':'incorrect-password'})[0],403)
                self.assertEqual(req('/api/login',{'username':'FAMILY-PARENT','password':'parent-secret-123'})[0],429)
                self.assertEqual(req('/api/login',{'username':'new-child','password':'changed-child-secret'})[0],200)
            finally:
                http.shutdown()
                http.server_close()
                thread.join()
                d.DB_PATH=old


if __name__=='__main__': unittest.main()
