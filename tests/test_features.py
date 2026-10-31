from contextlib import closing
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
import test_economy as economy
from app import domain as d, features, backups, manage, server


class FeatureTests(unittest.TestCase):
    setUp=economy.EconomyTests.setUp
    tearDown=economy.EconomyTests.tearDown
    act=economy.EconomyTests.act
    fund=economy.EconomyTests.fund
    prices=economy.EconomyTests.prices

    def test_goals_hold_real_money_and_match_only_once(self):
        self.fund(1000)
        self.act('goal-save',{'title':'Bike','target':500,'match_percent':20})
        self.act('goal-contribute',{'id':1,'amount':500},self.child)
        self.assertEqual(d.balance(self.db,'wallet'),50000)
        self.assertEqual(d.balance(self.db,'savings'),50000)
        with self.assertRaises(ValueError):
            self.act('transfer',{'source':'savings','amount':1},self.child)
        with self.assertRaises(ValueError):
            self.act('goal-contribute',{'id':1,'amount':1},self.child)
        with self.assertRaises(PermissionError):
            self.act('goal-match',{'id':1},self.child)
        bank=d.bank_balance(self.db)
        self.act('goal-match',{'id':1})
        self.assertEqual(d.bank_balance(self.db),bank-10000)
        self.assertEqual(d.balance(self.db,'wallet'),60000)
        with self.assertRaises(ValueError):
            self.act('goal-match',{'id':1})
        self.act('goal-withdraw',{'id':1,'amount':500},self.child)
        self.act('goal-contribute',{'id':1,'amount':500},self.child)
        with self.assertRaises(ValueError):
            self.act('goal-match',{'id':1})
        self.assertEqual(features.goal_reserved(self.db,2),50000)
        self.db.execute("INSERT INTO users VALUES (3,'Other','child','test','other')")
        for action in ('goal-withdraw','goal-save','goal-match'):
            with self.assertRaises(ValueError):
                self.act(action,{'child_id':3,'id':1,'amount':1,'title':'Wrong','target':1})

    def test_child_cannot_set_match_and_failed_contribution_rolls_back(self):
        self.act('goal-save',{'title':'Game','target':100,'match_percent':100},self.child)
        self.assertEqual(self.db.execute('SELECT match_percent FROM goals').fetchone()[0],0)
        with self.assertRaises(ValueError):
            self.act('goal-contribute',{'id':1,'amount':50},self.child)
        self.assertEqual(features.goal_reserved(self.db,2),0)

    def test_revision_and_daily_chore_pays_once_per_due_date(self):
        today=features.local_today()
        self.act('chore-save',{'title':'Dishes','amount':20,'recurrence':'daily','due_date':str(today)})
        self.act('chore-complete',{'id':1},self.child)
        before=d.balance(self.db,'wallet')
        self.act('chore-review',{'id':1,'revise':True,'note':'Put the plates away too.'})
        self.assertEqual(d.balance(self.db,'wallet'),before)
        self.assertEqual(self.db.execute('SELECT review_note FROM completions').fetchone()[0],'Put the plates away too.')
        self.act('chore-complete',{'id':1},self.child)
        self.act('chore-review',{'id':2,'approve':True})
        self.assertEqual(d.balance(self.db,'wallet'),before+2000)
        self.assertEqual(self.db.execute('SELECT due_date FROM chores').fetchone()[0],str(today+dt.timedelta(days=1)))
        with self.assertRaises(ValueError):
            self.act('chore-complete',{'id':1},self.child)
        with self.assertRaises(ValueError):
            self.act('chore-review',{'id':2,'approve':True})
        with patch('app.features.local_today',return_value=today+dt.timedelta(days=1)):
            self.act('chore-complete',{'id':1},self.child)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM completions WHERE status='pending'").fetchone()[0],1)

    def test_weekly_date_keeps_weekday_and_one_time_closes(self):
        day=dt.date(2026,10,1)
        self.assertEqual(features.next_due(str(day),'weekly',dt.date(2026,10,10)),'2026-10-15')
        self.act('chore-save',{'title':'One job','amount':10,'recurrence':'once','due_date':str(features.local_today())})
        self.act('chore-complete',{'id':1},self.child)
        self.act('chore-review',{'id':1,'approve':True})
        self.assertEqual(self.db.execute('SELECT active FROM chores').fetchone()[0],0)

    def test_gain_chart_ignores_purchase_sale_and_wallet_deposit(self):
        self.fund()
        self.prices()
        self.act('buy',{'id':1,'amount':1000},self.child)
        self.assertEqual(d.portfolio_history(self.db,2)[-1]['gain'],0)
        self.prices(1.1)
        d.state(self.db,dict(self.child,name='Child'))
        self.assertEqual(d.portfolio_history(self.db,2)[-1]['gain'],10000)
        self.act('sell',{'id':1},self.child)
        self.assertEqual(d.portfolio_history(self.db,2)[-1]['value'],0)
        self.assertEqual(d.portfolio_history(self.db,2)[-1]['gain'],10000)
        self.fund(200)
        self.act('buy',{'id':1,'amount':500},self.child)
        self.assertEqual(d.portfolio_history(self.db,2)[-1]['gain'],10000)

    def test_bank_needs_includes_pending_rewards_and_next_week(self):
        today=dt.datetime.now(d.UTC).date()
        self.act('settings',{'apr':5,'allowance':50})
        self.db.execute("UPDATE child_settings SET value=? WHERE key='allowance_next'",(str(today+dt.timedelta(days=2)),))
        self.act('chore-save',{'title':'Job','amount':25})
        self.act('chore-complete',{'id':1},self.child)
        self.assertEqual(features.bank_needs(self.db,today),{'pending_rewards':2500,'allowances':5000,'needed':7500})
        self.assertIsNone(d.state(self.db,dict(self.child,name='Child'))['bank_needs'])

    def test_backup_daily_retention_and_restore_readability(self):
        root=Path(self.tmp.name)/'backups'
        self.fund(100)
        with patch.dict(os.environ,{'BACKUP_DIR':str(root)}):
            first=backups.run()
            self.assertEqual(backups.run(),first)
            self.assertEqual(len(list(root.glob('*.db'))),1)
            for _ in range(15):
                backups.run(force=True)
        copies=sorted(root.glob('*.db'))
        self.assertEqual(len(copies),14)
        with closing(sqlite3.connect(copies[-1])) as copy:
            self.assertEqual(copy.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(copy.execute('SELECT COUNT(*) FROM transactions').fetchone()[0],1)

    def test_local_parent_recovery_preserves_money_and_revokes_sessions(self):
        self.fund(100)
        self.db.execute("INSERT INTO sessions VALUES ('old',1,'csrf',9999999999)")
        before=self.db.execute('SELECT COUNT(*) FROM entries').fetchone()[0]
        with patch('sys.argv',['manage','recover-parent']),patch('getpass.getpass',return_value='new-parent-password'):
            manage.main()
        parent=self.db.execute('SELECT * FROM users WHERE id=1').fetchone()
        self.assertTrue(server.password_ok('new-parent-password',parent['password']))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM entries').fetchone()[0],before)
        self.assertIsNotNone(self.db.execute("SELECT value FROM settings WHERE key='reset-login:test-parent'").fetchone())
