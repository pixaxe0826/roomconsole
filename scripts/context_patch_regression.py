"""Temporary-source-only patch migration/rollback checks, never user installation.
Usage: python scripts/assistant_patch_regression.py --package /path/to/patch --baseline /path/to/0.1.5
"""
import argparse,hashlib,importlib.util,json,shutil,socket,subprocess,sys,tempfile,os
from pathlib import Path

def main():
 p=argparse.ArgumentParser();p.add_argument('--package',required=True,type=Path);p.add_argument('--baseline',required=True,type=Path);args=p.parse_args()
 spec=importlib.util.spec_from_file_location('patch',args.package/'apply_update.py');u=importlib.util.module_from_spec(spec);spec.loader.exec_module(u)
 result={'scope':'Temporary copy of actual 0.1.5 source; real SQLite, TestClient API and patch installer. Synthetic records, no user files/services.','checks':[]}
 def check(name,ok):
  assert ok,name;result['checks'].append(name);print('PASS',name,flush=True)
 def run(root,code):
  r=subprocess.run([sys.executable,'-c',code],cwd=root,env={**os.environ,'HUB_DATA_DIR':str(root/'data')},capture_output=True,text=True)
  if r.returncode:raise AssertionError(r.stdout+r.stderr)
  return r
 with tempfile.TemporaryDirectory() as tmp:
  target=Path(tmp)/'room-hub';shutil.copytree(args.baseline,target)
  # Original 0.1.5 session/task/settings, made by the original code before applying.
  run(target,r'''
import json
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import create_app
app=create_app(Path('data'),weather_enabled=False)
with TestClient(app) as c:
 c.headers['X-Room-Request']='1';assert c.post('/api/auth/login',json={'token':app.state.admin_token}).status_code==200
 assert c.post('/api/tasks',json={'title':'기존 보존 항목','date':'2026-09-20'}).status_code==201
 s=c.get('/api/state').json();c.post('/api/voice/text',json={'request_id':'old-synthetic-voice','source':'test','text':'과거 전사 보존'})
 Path('sessions-fixture.json').write_text(json.dumps({'cookies':dict(c.cookies),'settings':s['settings'],'layout':s['layout']}))
''')
  protected={'data/speech-config.json':b'{"model":"/opt/room-hub/runtime/stt/models/ggml-base.bin","model_name":"base multilingual","language":"ko","threads":6,"enabled":false}', 'data/stt-accuracy.json':b'{"schema":1,"profile":"careful","include_task_titles":true,"max_task_titles":4}', 'data/llm-protected-test.txt':b'not-a-real-key', '.env':b'# synthetic env unchanged', 'runtime/qwen/build/bin/llama-server':b'fixture-not-executable', 'data/https/private/fixture.key':b'not-a-real-private-key'}
  for rel,b in protected.items():f=target/rel;f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(b)
  before_db=(target/'data/room-hub.sqlite3')
  keys={p.name:p.read_bytes() for p in (target/'data').glob('*token.txt')}
  _,plan=u.plan_update(args.package,target);check('0.1.5 current source accepted',len(plan)>0)
  backups=u.apply_update(args.package,target)
  check('new version installed',(target/'VERSION').read_text().strip()=='0.1.6')
  check('source backup made',backups.exists())
  check('all private/model/settings files preserved',all((target/r).read_bytes()==b for r,b in protected.items()))
  check('admin and ingest keys byte-preserved',all((target/'data'/n).read_bytes()==b for n,b in keys.items()))
  check('speech engine code unchanged',(target/'app/speech.py').read_bytes()==(args.baseline/'app/speech.py').read_bytes())
  check('reapply idempotent',u.apply_update(args.package,target) is None)
  check('new helper installed',(target/'deploy/termux/assistant-selftest.sh').exists())
  # Remove ONLY the intentionally invalid speech fixture for app startup; it was
  # checked above. Real app test uses speech disabled, no actual model needed.
  # Synthetic settings are valid but the real engine is deliberately disabled.
  run(target,r'''
import json,time
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import create_app
from app.llm import sha
saved=json.loads(Path('sessions-fixture.json').read_text());app=create_app(Path('data'),weather_enabled=False)
with TestClient(app) as c:
 c.headers['X-Room-Request']='1';c.cookies.update(saved['cookies'])
 assert c.get('/api/auth/me').status_code==200
 s=c.get('/api/state').json();assert s['tasks'][0]['title']=='기존 보존 항목';assert s['settings']==saved['settings'];assert s['layout']==saved['layout'];assert s['clock']['today']==s['today'];assert c.get('/api/assistant/capabilities').json()['version']==1
 v=c.post('/api/voice/text',json={'request_id':'new-migration-test','source':'test','text':'오늘 할 일에 새 항목 추가해?'}).json()['id']
 j=c.post('/api/llm/requests',json={'voice_id':v,'expected_text_sha256':sha('오늘 할 일에 새 항목 추가해?'),'request_id':'new-migration-request','mode':'auto'}).json()
 for _ in range(150):
  d=c.get('/api/llm/requests/'+j['id']).json()
  if d['status']=='awaiting_confirmation':break
  time.sleep(.01)
 assert len(c.get('/api/state').json()['tasks'])==1
 assert c.post('/api/assistant/'+d['id']+'/confirm',json={'preview_sha256':d['assistant']['preview_sha256']}).status_code==200
 assert len(c.get('/api/state').json()['tasks'])==2
''')
  check('old cookie/settings/layout survive and new confirmation works',True)
  u.rollback(target,backups);check('code rollback restores 0.1.5',(target/'VERSION').read_text().strip()=='0.1.5')
  check('new module removed on rollback',not (target/'app/clock_service.py').exists())
  run(target,r'''
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import create_app
app=create_app(Path('data'),weather_enabled=False)
with TestClient(app):assert len(app.state.store.tasks())==2
''')
  check('rollback keeps real task effect; does not silently undo data',True)
  # Tampered source is refused before any files are written.
  original=(target/'web/manager.js').read_bytes();(target/'web/manager.js').write_bytes(original+b'\n// local unknown edit\n')
  try:u.plan_update(args.package,target);ok=False
  except ValueError:ok=True
  check('unknown local source refused',ok);(target/'web/manager.js').write_bytes(original)
  with socket.socket() as s:
   s.bind(('127.0.0.1',0));s.listen();port=s.getsockname()[1]
   try:u.confirm_stopped(True,port);ok=False
   except ValueError:ok=True
   check('live TCP listener blocks applying',ok)
  try:u.confirm_stopped(False,port);ok=False
  except ValueError:ok=True
  check('explicit stopped confirmation required',ok)
  for path in ['../data/key','data/test','/etc/shadow','web/../../data/db']:
   try:u.safe_path(target,path);ok=False
   except ValueError:ok=True
   check('unsafe patch path rejected '+path,ok)
  (target/'app/clock_service.py').symlink_to(target/'app/main.py')
  try:u.plan_update(args.package,target);ok=False
  except ValueError:ok=True
  check('symlink payload target rejected',ok)
  (target/'app/clock_service.py').unlink()
  check('STT setting bytes still identical after rollback',all((target/r).read_bytes()==b for r,b in protected.items()))
 result['passed']=len(result['checks'])
 out=Path(__file__).resolve().parents[1]/'artifacts/test-results/context-patch.json';out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(result,ensure_ascii=False,indent=2))
 print('TOTAL',result['passed'])
if __name__=='__main__':main()
