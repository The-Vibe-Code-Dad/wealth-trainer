"""Upgrade the original single-child database without rewriting its ledger."""
import datetime as dt

SCOPED = {'transactions','chores','completions','rewards','redemptions','positions','cashouts'}


def migrate(db):
    if db.execute("SELECT 1 FROM settings WHERE key='multi_child_v1'").fetchone():
        return
    db.execute('PRAGMA foreign_keys=OFF')
    db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('CREATE TABLE users_new(id INTEGER PRIMARY KEY,name TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN (\'parent\',\'child\')),password TEXT NOT NULL,username TEXT UNIQUE NOT NULL)')
        db.execute('INSERT INTO users_new SELECT id,name,role,password,username FROM users')
        db.execute('DROP TABLE users')
        db.execute('ALTER TABLE users_new RENAME TO users')
        db.execute("CREATE UNIQUE INDEX one_parent ON users(role) WHERE role='parent'")
        child=db.execute("SELECT id FROM users WHERE role='child' ORDER BY id LIMIT 1").fetchone()
        legacy=child['id'] if child else 2
        for table in SCOPED:
            db.execute(f'ALTER TABLE {table} ADD COLUMN child_id INTEGER NOT NULL DEFAULT {int(legacy)}')
            db.execute(f'CREATE INDEX {table}_child ON {table}(child_id)')
        db.execute('DROP INDEX IF EXISTS pending_chore')
        db.execute('DROP INDEX IF EXISTS pending_reward')
        db.execute("CREATE UNIQUE INDEX pending_chore ON completions(child_id,chore) WHERE status='pending'")
        db.execute("CREATE UNIQUE INDEX pending_reward ON redemptions(child_id,reward) WHERE status='pending'")
        db.execute('CREATE TABLE child_settings(child_id INTEGER NOT NULL REFERENCES users,key TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(child_id,key))')
        if child:
            db.execute("INSERT INTO child_settings SELECT ?,key,value FROM settings WHERE key IN ('apr','interest_day','allowance','allowance_next')",(legacy,))
        db.execute("INSERT INTO settings VALUES ('multi_child_v1','1')")
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Migration failed foreign-key validation')
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute('PRAGMA foreign_keys=ON')


def initialize_child(db, ident):
    today=dt.datetime.now(dt.timezone.utc).date()
    defaults={'learning_level':'beginner','apr':'5','interest_day':str(today),'allowance':'0','allowance_next':str(today+dt.timedelta(days=7))}
    db.executemany('INSERT OR IGNORE INTO child_settings VALUES (?,?,?)',[(ident,k,v) for k,v in defaults.items()])


def current(db):
    ident=getattr(db,'child_id',None)
    if ident is None:
        row=db.execute("SELECT id FROM users WHERE role='child' ORDER BY id LIMIT 1").fetchone()
        if not row:
            raise ValueError('Create a child account first.')
        ident=row['id']
    return ident


def select(db,user,requested=None):
    if user['role']=='child':
        if requested not in (None,'',user['id'],str(user['id'])):
            raise PermissionError('You can only access your own account.')
        ident=user['id']
    else:
        ident=int(requested) if requested not in (None,'') else current(db)
    row=db.execute("SELECT id FROM users WHERE id=? AND role='child'",(ident,)).fetchone()
    if not row:
        raise ValueError('Choose an existing child account.')
    db.child_id=ident
    initialize_child(db,ident)
    return ident
