"""Room Hub base-only CPU experiment safety helpers. Standard library, outer Termux."""
from __future__ import annotations
import contextlib, datetime as dt, fcntl, hashlib, json, os, socket, sqlite3, tempfile
from pathlib import Path

PATCH_ID = 'room-hub-0.1.3-base-t8-1'
BASE_BYTES = 147951465
BASE_SHA256 = '60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe'
FIELDS = {'enabled','binary','model','model_name','ffmpeg','language','threads','max_seconds',
          'timeout_seconds','max_pending','max_bytes','max_stored_bytes'}

class Stop(RuntimeError): pass

def stamp(): return dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()
def encode(v): return (json.dumps(v,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode()
def no_links(path):
    path=Path(path).absolute()
    for p in (path,*path.parents):
        if p.is_symlink(): raise Stop('Symlink needs manual review: '+str(p))
def atomic(path, content, mode=0o600):
    path=Path(path);no_links(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,tmp=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f: f.write(content);f.flush();os.fsync(f.fileno());os.fchmod(f.fileno(),mode)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
@contextlib.contextmanager
def lock(path):
    path=Path(path);no_links(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with path.open('a') as f:
        os.chmod(path,0o600)
        try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as e:raise Stop('Server/model tool/logger is using lock: '+str(path)) from e
        try:yield
        finally:fcntl.flock(f,fcntl.LOCK_UN)
def listening(port=8088):
    with socket.socket() as s:s.settimeout(.5);return s.connect_ex(('127.0.0.1',port))==0
@contextlib.contextmanager
def db_read(root):
    path=Path(root)/'data/room-hub.sqlite3';no_links(path)
    if not path.is_file():raise Stop('Existing Room Hub database not found.')
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.3)
    db.row_factory=sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON');yield db
    finally:db.close()
def ensure_idle(root):
    with db_read(root) as db:
        n=db.execute("SELECT COUNT(*) FROM speech_jobs WHERE status IN ('queued','running')").fetchone()[0]
    if n:raise Stop('Finish/cancel all queued or running transcriptions first; no change made.')
def runtime_path(root,value):
    root=Path(root).resolve();v=Path(value)
    if not v.is_absolute():raise Stop('Runtime path is not absolute.')
    if str(v).startswith('/opt/room-hub/'):
        v=root/v.relative_to('/opt/room-hub')
    if not v.resolve().is_relative_to(root):raise Stop('Custom runtime outside room-hub requires manual review.')
    no_links(v);return v

def config(root, verify=True):
    root=Path(root).absolute();no_links(root);root=root.resolve()
    if (root/'VERSION').read_text().strip() not in {'0.1.3','0.1.4','0.1.5','0.1.6'}:raise Stop('Only Room Hub 0.1.3/0.1.4/0.1.5/0.1.6 is supported.')
    custom=os.getenv('HUB_DATA_DIR')
    if custom and Path(custom).resolve()!=root/'data':raise Stop('Non-default HUB_DATA_DIR; stop for manual review.')
    path=root/'data/speech-config.json';no_links(path);raw=path.read_bytes();cfg=json.loads(raw)
    if not isinstance(cfg,dict) or set(cfg)-FIELDS:raise Stop('Unknown speech config fields.')
    if cfg.get('enabled') is not True or cfg.get('language')!='ko':raise Stop('Existing enabled Korean transcription is required.')
    if not isinstance(cfg.get('model_name'),str) or len(cfg['model_name'])>100:raise Stop('Invalid model name.')
    limits={'threads':(1,8),'max_seconds':(3,60),'timeout_seconds':(20,600),'max_pending':(1,10),
            'max_bytes':(1024,10*1024*1024),'max_stored_bytes':(1024,1024*1024*1024)}
    defaults={'threads':2,'max_seconds':30,'timeout_seconds':180,'max_pending':4,
              'max_bytes':8*1024*1024,'max_stored_bytes':128*1024*1024}
    for k,(lo,hi) in limits.items():
        v=cfg.get(k,defaults[k])
        if type(v) is not int or not lo<=v<=hi:raise Stop('Invalid config: '+k)
    model=runtime_path(root,cfg.get('model',''));binary=runtime_path(root,cfg.get('binary',''))
    if model.name!='ggml-base.bin' or model.stat().st_size!=BASE_BYTES:raise Stop('Expected existing standard multilingual ggml-base.bin; do not change model.')
    if not binary.is_file() or not os.access(binary,os.X_OK):raise Stop('Existing whisper-cli is missing/not executable.')
    if verify and digest(model)!=BASE_SHA256:raise Stop('Base SHA256 mismatch; current files were not changed.')
    return root,cfg,raw,model,binary

def snapshot(root, verify=True):
    root,c,raw,model,binary=config(root,verify)
    knobs={k:v for k,v in c.items() if k not in {'binary','model','threads'}}
    fingerprint=hashlib.sha256(encode(knobs)).hexdigest()
    return {'model':'ggml-base.bin','model_sha256':BASE_SHA256 if verify else None,
            'model_sha256_verified':verify,'model_bytes':model.stat().st_size,
            'engine_sha256':digest(binary),'speech_source_sha256':digest(root/'app/speech.py'),
            'config_sha256':hashlib.sha256(raw).hexdigest(),'settings_except_threads_sha256':fingerprint,
            'threads':c.get('threads',2),'language':c['language'],'cpu_only':True,
            'beam_size':1,'best_of':1,'single_job_queue':True,'app_version':(root/'VERSION').read_text().strip()}

def set_threads(root,n):
    if type(n) is not int or not 1<=n<=8:raise Stop('threads must be 1..8.')
    root=Path(root).resolve()
    if "('threads', 1, 8)" not in (root/'app/speech.py').read_text():raise Stop('Apply the t8 source patch and restart server first.')
    with lock(root/'data/speech-cpu-lab/.capture.lock'),lock(root/'data/speech-model-tests/.experiment.lock'):
        _,cfg,raw,model,binary=config(root,True);ensure_idle(root)
        if cfg.get('threads',2)==n:print(f'UNCHANGED: base, threads={n}');return
        backup=root/'data/speech-cpu-lab'/('config-before-threads-'+stamp()+'.json')
        atomic(backup,raw);new=dict(cfg,threads=n)
        # No busy job may be inserted between checking and changing config.
        db=sqlite3.connect(root/'data/room-hub.sqlite3',timeout=1)
        try:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT COUNT(*) FROM speech_jobs WHERE status IN ('queued','running')").fetchone()[0]:raise Stop('A job just started; retry when idle.')
            if (root/'data/speech-config.json').read_bytes()!=raw:raise Stop('Configuration changed concurrently.')
            atomic(root/'data/speech-config.json',encode(new))
        finally:db.rollback();db.close()
        print(f'THREADS: {cfg.get("threads",2)} -> {n}; model stays ggml-base.bin. Next job uses this value.')
        print('CONFIG BACKUP:',backup)
