import sqlite3,json,uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime,timezone
from pathlib import Path
from .sqlite_connection import ClosingConnection

_UOW_STATE: ContextVar[dict[str, '_UnitOfWorkState'] | None] = ContextVar('_UOW_STATE', default=None)


class _UnitOfWorkState:
 def __init__(self, conn: ClosingConnection):
  self.conn = conn
  self.failed = False


class _BorrowedConnection:
 """Borrowed view over an active Unit-of-Work connection.

 Suppresses nested BEGIN/COMMIT/CLOSE so multi-step adapter writes execute
 atomically inside the outer unit_of_work() transaction.
 """
 def __init__(self, state: _UnitOfWorkState):
  self._state = state
  self._conn = state.conn

 def __enter__(self):
  return self

 def __exit__(self, exc_type, exc_val, exc_tb):
  if exc_type is not None:
   self._state.failed = True
  return False

 def execute(self, sql, *args, **kwargs):
  if isinstance(sql, str) and sql.strip().upper() in {
   'BEGIN', 'BEGIN IMMEDIATE', 'BEGIN EXCLUSIVE', 'BEGIN DEFERRED', 'COMMIT', 'END'
  }:
   return self._conn.execute('SELECT 1')
  return self._conn.execute(sql, *args, **kwargs)

 def executemany(self, sql, *args, **kwargs):
  return self._conn.executemany(sql, *args, **kwargs)

 def executescript(self, sql_script):
  return self._conn.executescript(sql_script)

 def commit(self):
  return None

 def rollback(self):
  self._state.failed = True
  return None

 def close(self):
  return None

 def __getattr__(self, name):
  return getattr(self._conn, name)

def uid():return uuid.uuid4().hex
def utcnow():return datetime.now(timezone.utc).isoformat()
DEFAULT_LAYOUT={'columns':8,'rows':6,'version':1,'widgets':[
 {'id':'clock','type':'clock','title':'지금','x':0,'y':0,'w':4,'h':2,'config':{}},
 {'id':'weather','type':'weather','title':'날씨','x':4,'y':0,'w':4,'h':2,'config':{}},
 {'id':'tasks','type':'todos','title':'할 일','x':0,'y':2,'w':4,'h':4,'config':{}},
 {'id':'calendar','type':'calendar','title':'달력','x':4,'y':2,'w':4,'h':4,'config':{}}]}
DEFAULT_SETTINGS={'title':'My room','timezone':'Asia/Seoul','location_name':'','latitude':None,'longitude':None,'weather_interval_minutes':15,'theme':'light'}
class Store:
 def __init__(self,path):
    self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
    with self.connect() as db:
     db.executescript('''
     PRAGMA journal_mode=WAL;
     CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT NOT NULL);
     CREATE TABLE IF NOT EXISTS series(id TEXT PRIMARY KEY,rule TEXT NOT NULL,created_at TEXT NOT NULL);
     CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,title TEXT NOT NULL,date TEXT NOT NULL,time TEXT,category TEXT NOT NULL,priority TEXT NOT NULL,notes TEXT NOT NULL,completed INTEGER NOT NULL DEFAULT 0,series_id TEXT REFERENCES series(id),version INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
     CREATE INDEX IF NOT EXISTS tasks_date ON tasks(date);
     CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY,name TEXT NOT NULL,created_at TEXT NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,last_seen TEXT,viewport TEXT);
     CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY,role TEXT NOT NULL,device_id TEXT,expires_at REAL NOT NULL);
     CREATE TABLE IF NOT EXISTS pairs(hash TEXT PRIMARY KEY,device_id TEXT NOT NULL,expires_at REAL NOT NULL,used INTEGER NOT NULL DEFAULT 0);
     CREATE TABLE IF NOT EXISTS voice(id TEXT PRIMARY KEY,request_id TEXT NOT NULL,source TEXT NOT NULL,kind TEXT NOT NULL,text TEXT NOT NULL DEFAULT '',locale TEXT NOT NULL,status TEXT NOT NULL,metadata TEXT NOT NULL,filename TEXT,media_type TEXT,digest TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(source,request_id));
     CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,event TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL);
     ''')
     for k,v in [('layout',DEFAULT_LAYOUT),('settings',DEFAULT_SETTINGS),('weather',None),('revision',0)]:db.execute('INSERT OR IGNORE INTO kv VALUES(?,?)',(k,json.dumps(v,ensure_ascii=False)))
 def in_unit_of_work(self) -> bool:
    active = _UOW_STATE.get()
    return bool(active and str(self.path) in active)

 @contextmanager
 def unit_of_work(self):
    active = _UOW_STATE.get() or {}
    key = str(self.path)
    if key in active:
     yield _BorrowedConnection(active[key])
     return
    db = sqlite3.connect(self.path, timeout=15, check_same_thread=False, factory=ClosingConnection)
    try:
     db.row_factory = sqlite3.Row
     db.execute('PRAGMA foreign_keys=ON')
     db.execute('BEGIN IMMEDIATE')
    except BaseException:
     db.close()
     raise
    state = _UnitOfWorkState(db)
    token = _UOW_STATE.set({**active, key: state})
    try:
     yield _BorrowedConnection(state)
     if state.failed:
      raise RuntimeError('Unit of work marked failed by nested operation.')
     db.commit()
    except BaseException:
     try:
      db.rollback()
     except Exception:
      pass
     raise
    finally:
     _UOW_STATE.reset(token)
     db.close()

 def connect(self):
    active = _UOW_STATE.get()
    if active and str(self.path) in active:
     return _BorrowedConnection(active[str(self.path)])
    db=sqlite3.connect(self.path,timeout=15,factory=ClosingConnection)
    try:
     db.row_factory=sqlite3.Row
     db.execute('PRAGMA foreign_keys=ON')
     return db
    except BaseException:
     db.close()
     raise
 def get(self,k):
    with self.connect() as db:r=db.execute('SELECT value FROM kv WHERE key=?',(k,)).fetchone()
    return json.loads(r['value']) if r else None
 def set(self,k,v):
    with self.connect() as db:db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)',(k,json.dumps(v,ensure_ascii=False)))
 def bump(self,event,detail=''):
    with self.connect() as db:
     db.execute("UPDATE kv SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key='revision'")
     db.execute('INSERT INTO audit(event,detail,created_at) VALUES(?,?,?)',(event,detail,utcnow()))
     db.execute('DELETE FROM audit WHERE id < (SELECT COALESCE(MAX(id),0)-999 FROM audit)')
 @staticmethod
 def task_dict(row):
    value=dict(row);value['completed']=bool(value['completed']);return value
 def tasks(self):
    return self.query_tasks('0001-01-01','9999-12-31','all',10000)['items']

 def query_tasks(self,start,end,status='all',limit=50,*,period=None):
    """Shared server read service. Read count+items within one SQLite snapshot."""
    from datetime import date
    start=date.fromisoformat(start).isoformat();end=date.fromisoformat(end).isoformat()
    if start>end or status not in {'all','pending','completed'} or not 1<=limit<=10000:
     raise ValueError('잘못된 할 일 조회 범위입니다.')
    where='date>=? AND date<=?';args=[start,end]
    if status!='all':where+=' AND completed=?';args.append(int(status=='completed'))
    if period not in {None,'morning','afternoon'}:raise ValueError('잘못된 오전/오후 조회 범위입니다.')
    base_where,base_args=where,list(args)
    if period:
     where+=' AND time IS NOT NULL AND time>=? AND time<?'
     args+=['00:00','12:00'] if period=='morning' else ['12:00','24:00']
    with self.connect() as db:
     db.execute('BEGIN')
     untimed=db.execute('SELECT count(*) FROM tasks WHERE '+base_where+' AND time IS NULL',base_args).fetchone()[0] if period else 0
     total=db.execute('SELECT count(*) FROM tasks WHERE '+where,args).fetchone()[0]
     rows=db.execute('SELECT * FROM tasks WHERE '+where+' ORDER BY date,time IS NULL,time,created_at,id LIMIT ?',(*args,limit)).fetchall()
     rev=db.execute("SELECT value FROM kv WHERE key='revision'").fetchone()[0]
    return {'ok':True,'source':'room_hub_sqlite','count':total,'items':[self.task_dict(r) for r in rows],
            'range':[start,end],'status':status,'as_of':utcnow(),'revision':int(rev),
            'limit':limit,'returned':len(rows),'truncated':total>len(rows),
            **({'period':period,'untimed_count':untimed} if period else {})}
