"""Synthetic migration/rollback test. Requires original 0.1.4 source + patch dirs."""
import argparse, hashlib, importlib.util, json, os, shutil, socket, subprocess, sys, tempfile
from pathlib import Path

def run(package,baseline):
 spec=importlib.util.spec_from_file_location('updater',package/'apply_update.py');u=importlib.util.module_from_spec(spec);spec.loader.exec_module(u)
 checks=[]
 def check(n,c):
  assert c,n;checks.append(n);print('PASS',n,flush=True)
 def rejects(n,f):
  try:f()
  except (ValueError,OSError):check(n,True)
  else:check(n,False)
 def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
 with tempfile.TemporaryDirectory() as td:
  td=Path(td);root=td/'room-hub';shutil.copytree(baseline,root,ignore=shutil.ignore_patterns('data','runtime','__pycache__','.pytest_cache','artifacts'))
  env={k:v for k,v in os.environ.items() if not k.startswith('HUB_')};env.update(HUB_DATA_DIR=str(root/'data'),PYTHONDONTWRITEBYTECODE='1')
  seed=r'''
from pathlib import Path
import json
from app.main import create_app
from app.llm import sha
from fastapi.testclient import TestClient
app=create_app(Path('data'),weather_enabled=False)
with TestClient(app) as c:
 c.headers['X-Room-Request']='1';c.post('/api/auth/login',json={'token':app.state.admin_token})
 admin=dict(c.cookies)
 c.post('/api/tasks',json={'title':'PRESERVE','date':'2026-09-20'})
 v=c.post('/api/voice/text',json={'request_id':'patch-voice','source':'test','text':'PRESERVE INPUT'}).json()['id']
 j=c.post('/api/llm/requests',json={'request_id':'patch-llm','voice_id':v,'expected_text_sha256':sha('PRESERVE INPUT')}).json()
 with app.state.store.connect() as db:db.execute("UPDATE llm_requests SET status='succeeded',dispatch_attempted=1,started_at=created_at,response_json=? WHERE id=?",(json.dumps({'output':'PRESERVE OUTPUT'}),j['id']))
 pair=c.post('/api/devices/pair',json={'name':'TEST'}).json();c.cookies.clear();c.post('/api/devices/claim',json={'code':pair['path'].split('=')[1]})
 Path('fixture.json').write_text(json.dumps({'admin':admin,'display':dict(c.cookies),'llm':j['id']}))
'''
  subprocess.run([sys.executable,'-c',seed],cwd=root,env=env,check=True,capture_output=True)
  (root/'data/speech-config.json').write_text(json.dumps({'enabled':False,'threads':6,'model_name':'base · multilingual'}))
  (root/'data/https/private').mkdir(parents=True);(root/'data/https/private/test.key').write_text('TEST ONLY')
  (root/'runtime/stt/models').mkdir(parents=True);(root/'runtime/stt/models/ggml-base.bin').write_bytes(b'NOT A REAL MODEL')
  before={str(p.relative_to(root)):digest(p) for d in ['data','runtime'] for p in (root/d).rglob('*') if p.is_file()}
  speech=digest(root/'app/speech.py');stop=digest(root/'deploy/termux/stop-room-hub.py')
  manifest,plan=u.plan_update(package,root);check('known 0.1.4 produces plan',bool(plan))
  rejects('requires explicit stopped confirmation',lambda:u.confirm_stopped(False,8088))
  with socket.socket() as sock:
   sock.bind(('127.0.0.1',0));sock.listen();rejects('live TCP listener blocks apply',lambda:u.confirm_stopped(True,sock.getsockname()[1]))
  backup=u.apply_update(package,root)
  check('patch version remains 0.1.4',(root/'VERSION').read_text().strip()=='0.1.4')
  check('all private bytes preserved by installer',all(digest(root/n)==h for n,h in before.items()))
  check('Whisper and stop script unchanged',digest(root/'app/speech.py')==speech and digest(root/'deploy/termux/stop-room-hub.py')==stop)
  check('new widget default 4x2',json.loads((root/'widgets/llm-response/manifest.json').read_text())['defaultSize']=={'w':4,'h':2})
  check('repeated update is no-op',u.apply_update(package,root) is None)
  migrated=r'''
from pathlib import Path
import json
from app.main import create_app
from fastapi.testclient import TestClient
app=create_app(Path('data'),weather_enabled=False);f=json.loads(Path('fixture.json').read_text())
with TestClient(app) as c:
 c.headers['X-Room-Request']='1';c.cookies.update(f['display'])
 assert c.get('/api/state').json()['tasks'][0]['title']=='PRESERVE'
 assert c.get('/api/llm/config').status_code==401
 assert c.get('/api/display/llm/'+f['llm']).status_code==403
 c.cookies.clear();c.cookies.update(f['admin']);old=c.get('/api/llm/requests/'+f['llm']).json()
 assert old['source_text']=='PRESERVE INPUT' and old['response_json']['output']=='PRESERVE OUTPUT'
 l=c.get('/api/state').json()['layout'];assert len(l['widgets'])==4;l['widgets'][0]['type']='llm-response';c.put('/api/layout',json=l)
 c.cookies.clear();c.cookies.update(f['display']);out=c.get('/api/display/llm/'+f['llm']).json();assert out['input']=='PRESERVE INPUT' and out['output']=='PRESERVE OUTPUT'
 assert json.loads(Path('data/speech-config.json').read_text())['threads']==6
'''
  subprocess.run([sys.executable,'-c',migrated],cwd=root,env=env,check=True,capture_output=True)
  check('old cookies, tasks and LLM history survive actual new startup',True)
  check('existing display projection works after explicit layout opt-in',True)
  check('Whisper Base/6T configuration preserved',json.loads((root/'data/speech-config.json').read_text())['threads']==6)
  p=root/'web/client.js';wanted=p.read_bytes();p.write_bytes(wanted+b'\n//local edit')
  rejects('rollback refuses later source edits',lambda:u.rollback(root,backup));p.write_bytes(wanted)
  u.rollback(root,backup)
  check('rollback restores original client',digest(root/'web/client.js')==digest(baseline/'web/client.js'))
  check('rollback removes new widget only',not (root/'widgets/llm-response/widget.js').exists() and (root/'widgets/todos/widget.js').exists())
  check('rollback preserves model/config/TLS',all(digest(root/n)==h for n,h in before.items() if n.startswith('runtime/') or n.endswith(('.key','speech-config.json'))))
  old=(root/'web/client.js').read_bytes();(root/'web/client.js').write_bytes(old+b'\n//unknown')
  rejects('unknown local changes block entire update',lambda:u.plan_update(package,root));(root/'web/client.js').write_bytes(old.replace(b'\n',b'\r\n'))
  check('CRLF conversion accepted',bool(u.plan_update(package,root)[1]));(root/'web/client.js').write_bytes(old)
  for path in ['data/a','runtime/a','../a','web/../../a','web/admin-token.txt']:
   rejects('unsafe path '+path,lambda path=path:u.safe_path(root,path))
  altered=td/'altered';shutil.copytree(package,altered);(altered/'files/app/llm_display.py').write_text('changed')
  rejects('payload hash mismatch blocked',lambda:u.read_manifest(altered))
  if hasattr(os,'symlink'):
   p=root/'web/client.js';p.unlink();p.symlink_to(baseline/'web/client.js');rejects('symlink target blocked',lambda:u.plan_update(package,root))
 result={'passed':len(checks),'checks':checks,'scope':'Temporary original source tree + synthetic SQLite/cookies/keys/config. No actual user data or model run.'}
 out=Path(__file__).resolve().parents[1]/'artifacts/test-results/llm-widget-patch.json';out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(result,ensure_ascii=False,indent=2))
 print('TOTAL',len(checks))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--package',type=Path,required=True);p.add_argument('--baseline',type=Path,required=True);a=p.parse_args();run(a.package,a.baseline)
