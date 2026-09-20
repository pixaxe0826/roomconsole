"""Explicit temporary-tree updater/migration regression. Never use a live data folder.
python scripts/llm_patch_regression.py --package /tmp/patch --baseline /tmp/original-0.1.3
Only generated fixtures are written. Requires requirements-dev.txt.
"""
import argparse,hashlib,importlib.util,json,os,shutil,socket,subprocess,sys,tempfile
from pathlib import Path

def run(package,baseline):
 spec=importlib.util.spec_from_file_location('patch_under_test',package/'apply_update.py');u=importlib.util.module_from_spec(spec);spec.loader.exec_module(u)
 checks=[]
 def check(name,value):
  if not value:raise AssertionError(name)
  checks.append(name);print('PASS',name)
 def rejects(name,fn):
  try:fn()
  except (ValueError,OSError):check(name,True)
  else:check(name,False)
 def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
 with tempfile.TemporaryDirectory(prefix='room-hub-patch-test-') as tmp:
  tmp=Path(tmp);target=tmp/'room-hub';shutil.copytree(baseline,target,ignore=shutil.ignore_patterns('__pycache__','.pytest_cache','data','runtime','artifacts'))
  # Seed actual old application's SQLite, task, login and display session in an isolated process.
  seed=r'''
import json,os
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import create_app
root=Path.cwd();a=create_app(root/'data',weather_enabled=False)
with TestClient(a) as c:
 c.headers['X-Room-Request']='1'
 assert c.post('/api/auth/login',json={'token':a.state.admin_token}).status_code==200
 assert c.post('/api/tasks',json={'title':'PATCH TEST','date':'2026-09-20'}).status_code==201
 admin=dict(c.cookies);pair=c.post('/api/devices/pair',json={'name':'PATCH TEST DISPLAY'}).json();c.cookies.clear()
 assert c.post('/api/devices/claim',json={'code':pair['path'].split('=')[1]}).status_code==200
 display=dict(c.cookies);t=c.get('/api/state').json()['tasks'][0]
 assert c.patch('/api/tasks/'+t['id']+'/completion',json={'version':t['version'],'completed':True}).status_code==200
 (root/'fixture-sessions.json').write_text(json.dumps({'admin':admin,'display':display}))
'''
  env={k:v for k,v in os.environ.items() if not k.startswith('HUB_')};env['HUB_DATA_DIR']=str(target/'data');env['PYTHONDONTWRITEBYTECODE']='1'
  subprocess.run([sys.executable,'-c',seed],cwd=target,env=env,check=True,capture_output=True)
  (target/'data/speech-config.json').write_text(json.dumps({'enabled':False,'model_name':'base · multilingual','threads':6,'language':'ko'}))
  (target/'runtime/stt/models').mkdir(parents=True);(target/'runtime/stt/models/ggml-base.bin').write_bytes(b'TEST-NOT-A-MODEL')
  (target/'data/https/private').mkdir(parents=True);(target/'data/https/private/test.key').write_text('TEST-NOT-A-KEY')
  private={str(x.relative_to(target)):digest(x) for d in ['data','runtime'] for x in (target/d).rglob('*') if x.is_file()}
  originalspeech=digest(target/'app/speech.py');manifest,plan=u.plan_update(package,target)
  check('source check produces plan',bool(plan));check('speech engine not in patch',all(e['path']!='app/speech.py' for e in plan))
  check('client runtime not in patch',all(e['path'] not in ['web/client.js','web/client.css','web/client.html'] for e in plan))
  rejects('explicit stopped confirmation required',lambda:u.confirm_stopped(False,8088))
  with socket.socket() as s:
   s.bind(('127.0.0.1',0));s.listen();port=s.getsockname()[1];rejects('active TCP listener blocks writes',lambda:u.confirm_stopped(True,port))
  backup=u.apply_update(package,target)
  check('new version applied',(target/'VERSION').read_text().strip()=='0.1.4')
  check('all private bytes preserved by patch',all(digest(target/f)==sha for f,sha in private.items()))
  check('speech engine bytes preserved',digest(target/'app/speech.py')==originalspeech)
  check('new LLM module and GUI installed',(target/'app/llm.py').is_file() and (target/'web/manager-llm.js').is_file())
  check('second apply is a no-op',u.apply_update(package,target) is None)
  # Load new app from the same SQLite and validate persistent permissions after migration.
  migrated=r'''
import json
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import create_app
root=Path.cwd();a=create_app(root/'data',weather_enabled=False);cookies=json.loads((root/'fixture-sessions.json').read_text())
with TestClient(a) as c:
 c.headers['X-Room-Request']='1';c.cookies.update(cookies['display'])
 assert c.get('/healthz').json()['version']=='0.1.4'
 task=c.get('/api/state').json()['tasks'][0];assert task['title']=='PATCH TEST' and task['completed']
 assert c.get('/api/llm/config').status_code==401
 assert c.patch('/api/tasks/'+task['id']+'/completion',json={'version':task['version'],'completed':False}).status_code==200
 c.cookies.clear();c.cookies.update(cookies['admin']);assert c.get('/api/llm/config').json()['config']['enabled'] is False
 assert c.get('/api/llm/requests').json()['total']==0
 assert c.get('/api/state').json()['tasks'][0]['completed'] is False
 assert json.loads((root/'data/speech-config.json').read_text())['threads']==6
'''
  subprocess.run([sys.executable,'-c',migrated],cwd=target,env=env,check=True,capture_output=True)
  check('old SQLite and task completion survive startup',True)
  check('admin and display cookies survive; LLM admin only',True)
  check('new LLM defaults off with empty history',True)
  check('6T speech config preserved through actual startup',json.loads((target/'data/speech-config.json').read_text())['threads']==6)
  modified=target/'web/manager-llm.js';modified.write_text(modified.read_text()+'\n// changed\n')
  rejects('rollback blocks subsequent local edits',lambda:u.rollback(target,backup));modified.write_bytes((package/'files/web/manager-llm.js').read_bytes())
  u.rollback(target,backup)
  check('rollback restores source0.1.3',(target/'VERSION').read_text().strip()=='0.1.3' and not (target/'app/llm.py').exists())
  check('rollback leaves speech config and model',json.loads((target/'data/speech-config.json').read_text())['threads']==6 and (target/'runtime/stt/models/ggml-base.bin').exists())
  old=(target/'web/manager.js').read_bytes();(target/'web/manager.js').write_bytes(old+b'\n// user change\n')
  rejects('unknown source blocks entire update',lambda:u.plan_update(package,target));(target/'web/manager.js').write_bytes(old)
  (target/'web/manager.js').write_bytes(old.replace(b'\n',b'\r\n'));check('Windows CRLF accepted',bool(u.plan_update(package,target)[1]));(target/'web/manager.js').write_bytes(old)
  # No installation of partial CPU/model add-ons when missing.
  optional=[e['path'] for e in manifest['files'] if e.get('optional_existing')]
  for rel in optional:(target/rel).unlink(missing_ok=True)
  planned=u.plan_update(package,target)[1];check('absent optional model tools skipped',all(x['path'] not in optional for x in planned))
  for rel in ['data/admin-token.txt','../secret.txt','runtime/model.gguf','web/../../secret']:
   rejects('unsafe path rejected '+rel,lambda rel=rel:u.safe_path(target,rel))
  # Payload hashes and symlinks are fail closed.
  copied=tmp/'tamper';shutil.copytree(package,copied);(copied/'files/app/llm.py').write_text('altered')
  rejects('tampered payload rejected',lambda:u.read_manifest(copied))
  (target/'web/manager.js').unlink();(target/'web/manager.js').symlink_to(baseline/'web/manager.js')
  rejects('source symlink refused',lambda:u.plan_update(package,target))
 return {'passed':len(checks),'checks':checks,'scope':'Temporary copied0.1.3 source/SQLite, real FastAPI TestClient migration; not V35, no real model.'}

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--package',required=True,type=Path);p.add_argument('--baseline',required=True,type=Path);p.add_argument('--report',type=Path)
 a=p.parse_args();result=run(a.package.resolve(),a.baseline.resolve());print(json.dumps(result,ensure_ascii=False))
 if a.report:a.report.parent.mkdir(parents=True,exist_ok=True);a.report.write_text(json.dumps(result,ensure_ascii=False,indent=2))
