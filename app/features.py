"""Savings goals and recurring chore helpers."""
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP


def local_today():
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        return dt.datetime.now(ZoneInfo('America/New_York')).date()
    except ZoneInfoNotFoundError:
        return dt.datetime.now().astimezone().date()


def migrate(db):
    for table, fields in {
        'chores': [('recurrence', "TEXT NOT NULL DEFAULT 'anytime'"),('due_date',"TEXT NOT NULL DEFAULT ''")],
        'completions': [('review_note',"TEXT NOT NULL DEFAULT ''"),('due_date',"TEXT NOT NULL DEFAULT ''"),('recurrence',"TEXT NOT NULL DEFAULT 'anytime'")],
    }.items():
        existing={r['name'] for r in db.execute('PRAGMA table_info('+table+')')}
        for name,kind in fields:
            if name not in existing:
                db.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+kind)
    db.execute("CREATE TABLE IF NOT EXISTS goals(id INTEGER PRIMARY KEY,child_id INTEGER NOT NULL REFERENCES users,title TEXT NOT NULL,target INTEGER NOT NULL,saved INTEGER NOT NULL DEFAULT 0,match_percent INTEGER NOT NULL DEFAULT 0,matched INTEGER NOT NULL DEFAULT 0)")


def goal_reserved(db,child):
    return db.execute('SELECT COALESCE(SUM(saved),0) FROM goals WHERE child_id=?',(child,)).fetchone()[0]


def schedule_fields(data):
    recurrence=data.get('recurrence','anytime')
    if recurrence not in ('anytime','once','daily','weekly'):
        raise ValueError('Choose a valid chore schedule.')
    due=data.get('due_date','')
    if due:
        due=dt.date.fromisoformat(due).isoformat()
    if recurrence!='anytime' and not due:
        raise ValueError('Choose the first due date for a scheduled chore.')
    return recurrence,due


def next_due(due,recurrence,today):
    day=dt.date.fromisoformat(due)
    step=dt.timedelta(days=1 if recurrence=='daily' else 7)
    while day<=today:
        day+=step
    return str(day)


def goal_action(db,user,path,data,key):
    from app import domain as d
    child=d.f.current(db)
    if path=='goal-save':
        title=d.label(data.get('title'))
        target=d.money(data.get('target'))
        row=d.required(db,'goals',data['id']) if data.get('id') else None
        match=row['match_percent'] if row else 0
        if user['role']=='parent':
            raw=Decimal(str(data.get('match_percent',match)))
            if not raw.is_finite() or raw<0 or raw>100 or raw!=int(raw):
                raise ValueError('Matching must be a whole percentage from 0 to 100.')
            match=int(raw)
        if row:
            if target<row['saved']:
                raise ValueError('The target cannot be below money already saved.')
            db.execute('UPDATE goals SET title=?,target=?,match_percent=? WHERE id=?',(title,target,match,row['id']))
        else:
            db.execute('INSERT INTO goals(child_id,title,target,match_percent) VALUES (?,?,?,?)',(child,title,target,match))
        return
    goal=d.required(db,'goals',data.get('id'))
    if path=='goal-match':
        if user['role']!='parent':
            raise PermissionError('Only the parent can pay a matching reward.')
        if goal['matched'] or not goal['match_percent'] or goal['saved']<goal['target']:
            raise ValueError('This goal is not eligible for a matching reward.')
        amount=int((Decimal(goal['target'])*goal['match_percent']/100).quantize(Decimal(1),rounding=ROUND_HALF_UP))
        if amount<=0:
            raise ValueError('The matching reward rounds to zero.')
        d.post(db,'goal-match','Goal match: '+goal['title'],user['id'],{'bank':-amount,'wallet':amount},key)
        db.execute('UPDATE goals SET matched=1 WHERE id=?',(goal['id'],))
    elif path=='goal-contribute':
        amount=d.money(data.get('amount'))
        if goal['saved']+amount>goal['target']:
            raise ValueError('That amount exceeds the remaining goal target.')
        d.post(db,'goal-save','Saved for '+goal['title'],user['id'],{'wallet':-amount,'savings':amount},key)
        db.execute('UPDATE goals SET saved=saved+? WHERE id=?',(amount,goal['id']))
    elif path=='goal-withdraw':
        amount=d.money(data.get('amount'))
        if amount>goal['saved']:
            raise ValueError('That amount exceeds this goal balance.')
        db.execute('UPDATE goals SET saved=saved-? WHERE id=?',(amount,goal['id']))
        d.post(db,'goal-withdraw','Released from '+goal['title'],user['id'],{'savings':-amount,'wallet':amount},key)
    else:
        raise ValueError('Unknown goal action.')


def bank_needs(db,today):
    pending=db.execute("SELECT COALESCE(SUM(reward),0) FROM completions WHERE status='pending'").fetchone()[0]
    allowances=0
    for row in db.execute("SELECT child_id,value FROM child_settings WHERE key='allowance'"):
        due=db.execute("SELECT value FROM child_settings WHERE child_id=? AND key='allowance_next'",(row['child_id'],)).fetchone()
        if due:
            day=dt.date.fromisoformat(due[0])
            while day<=today+dt.timedelta(days=7):
                allowances+=int(row['value'])
                day+=dt.timedelta(days=7)
    return {'pending_rewards':pending,'allowances':allowances,'needed':pending+allowances}
