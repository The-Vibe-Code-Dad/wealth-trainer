"""Daily consistent backups outside the database volume, with bounded retention."""
from contextlib import closing
import datetime as dt
import os
from pathlib import Path
import sqlite3
import threading
from app import domain as d

LOCK=threading.Lock()


def run(force=False):
    root=Path(os.environ.get('BACKUP_DIR',str(Path(d.DB_PATH).parent/'backups')))
    stamp=dt.datetime.now(d.UTC)
    today=stamp.date().isoformat()
    with LOCK:
        with d.connect() as db:
            last=db.execute("SELECT value FROM settings WHERE key='backup_last'").fetchone()
        if not force and last and last[0].startswith(today):
            return last[0]
        root.mkdir(parents=True,exist_ok=True)
        destination=root/('wealth-'+stamp.strftime('%Y%m%d-%H%M%S-%f')+'.db')
        temporary=destination.with_suffix('.tmp')
        try:
            with d.connect() as source,closing(sqlite3.connect(temporary)) as target:
                source.backup(target)
                assert target.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            temporary.chmod(0o600)
            temporary.replace(destination)
            copies=sorted(root.glob('wealth-*.db'))
            for old in copies[:-14]:
                old.unlink()
            with d.connect() as db:
                db.execute("INSERT INTO settings VALUES ('backup_last',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(stamp.isoformat(),))
                db.execute("INSERT INTO settings VALUES ('backup_error','') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            return stamp.isoformat()
        except Exception:
            temporary.unlink(missing_ok=True)
            with d.connect() as db:
                db.execute("INSERT INTO settings VALUES ('backup_error','Backup failed; check the backup folder permissions and free space.') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            raise
