"""Restore a Room Hub SQLite backup ONLY while the server is stopped."""
import argparse,shutil,sqlite3,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('backup',type=Path);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--server-stopped',action='store_true');a=p.parse_args()
if not a.server_stopped:raise SystemExit('Stop the server, then pass --server-stopped. This tool cannot detect every running instance.')
if not a.backup.is_file():raise SystemExit('Backup file missing.')
with sqlite3.connect(a.backup) as db:
 if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise SystemExit('Backup integrity check failed.')
 tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
 if not {'kv','tasks','series','devices','sessions','pairs','voice','audit'}<=tables:raise SystemExit('Not a Room Hub backup.')
a.data_dir.mkdir(parents=True,exist_ok=True);target=a.data_dir/'room-hub.sqlite3'
if a.backup.resolve()==target.resolve():raise SystemExit('Source and destination must differ.')
if target.exists():
 with sqlite3.connect(target) as src,sqlite3.connect(a.data_dir/f'before-restore-{int(time.time())}.sqlite3') as dest:src.backup(dest)
for suffix in ['-wal','-shm']:Path(str(target)+suffix).unlink(missing_ok=True)
shutil.copy2(a.backup,target)
with sqlite3.connect(target) as db:
 db.execute('DELETE FROM sessions');db.execute('DELETE FROM pairs');db.execute('UPDATE devices SET revoked=1')
print('Restored. Log in again and re-pair displays. Audio files and widget code must be restored separately.')
