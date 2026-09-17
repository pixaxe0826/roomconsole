"""Real loopback HTTP server smoke test; not physical LAN/iPad or Docker validation."""
import json,os,socket,subprocess,sys,tempfile,time
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parents[1]
REPORTS=ROOT/'artifacts'/'test-results';REPORTS.mkdir(parents=True,exist_ok=True)
SHOTS=ROOT/'artifacts'/'screenshots';SHOTS.mkdir(parents=True,exist_ok=True)

def run():
 checks=[]
 with tempfile.TemporaryDirectory() as tmp:
  with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
  env={**os.environ,'HUB_DATA_DIR':tmp,'HUB_ADMIN_TOKEN':'temporary-local-test-admin-key-123456789','HUB_INGEST_TOKEN':'temporary-local-test-ingest-key-12345678'}
  log=open(Path(tmp)/'server.log','w+')
  process=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(port),'--workers','1'],cwd=ROOT,env=env,stdout=log,stderr=log)
  try:
   url=f'http://127.0.0.1:{port}'
   with httpx.Client(base_url=url,headers={'X-Room-Request':'1'},timeout=5) as c:
    for _ in range(50):
     try:
      if c.get('/healthz').status_code==200:break
     except httpx.RequestError:pass
     time.sleep(.15)
    else:raise RuntimeError('Server did not start')
    checks.append('Real Uvicorn process and health endpoint')
    for path in ['/client','/manager','/static/client.js','/static/manager.js','/widgets/calendar/widget.js']:
     assert c.get(path).status_code==200;checks.append('Served '+path)
    assert c.get('/api/state').status_code==401;checks.append('Anonymous data access denied')
    assert c.post('/api/auth/login',json={'token':env['HUB_ADMIN_TOKEN']}).status_code==200
    assert c.post('/api/tasks',json={'title':'Loopback test','date':'2026-09-17'}).status_code==201
    assert len(c.get('/api/state').json()['tasks'])==1;checks.append('Authenticated HTTP write/read')
    pair=c.post('/api/devices/pair',json={'name':'Test display'}).json()
    with httpx.Client(base_url=url,headers={'X-Room-Request':'1'}) as d:
     assert d.post('/api/devices/claim',json={'code':pair['path'].split('=')[1]}).status_code==200
     assert d.get('/api/state').status_code==200
     assert d.post('/api/tasks',json={'title':'denied','date':'2026-09-17'}).status_code==401
     checks.append('Separate display session cannot create tasks')
     item=d.get('/api/state').json()['tasks'][0];tid=item['id'];endpoint='/api/tasks/'+tid+'/completion'
     assert d.get('/api/state').json()['capabilities']['task_completion'] is True
     assert d.patch(endpoint,json={'version':1,'completed':True}).json()['completed'] is True
     assert c.get('/api/state').json()['tasks'][0]['completed'] is True
     checks.append('Paired display saves completion over real HTTP')
     assert d.patch(endpoint,json={'version':1,'completed':False}).status_code==409
     checks.append('Stale real HTTP write rejected')
     assert d.patch(endpoint,json={'version':2,'completed':False,'title':'denied'}).status_code==422
     checks.append('Completion API rejects unrelated fields')
     assert d.patch('/api/tasks/'+tid,json={'version':2,'title':'denied'}).status_code==401
     assert d.delete('/api/tasks/'+tid).status_code==401
     checks.append('Display still cannot edit or delete tasks')
     assert d.patch(endpoint,json={'version':2,'completed':False}).json()['completed'] is False
     checks.append('Display undo is persisted and visible to manager')
     assert c.delete('/api/devices/'+pair['device_id']).status_code==200
     assert d.patch(endpoint,json={'version':3,'completed':True}).status_code==401
     checks.append('Revoked display cannot write completion')
  finally:
   process.terminate()
   try:process.wait(timeout=5)
   except subprocess.TimeoutExpired:process.kill();process.wait()
   log.close()
 result={'passed':len(checks),'checks':checks,'scope':'Real Uvicorn + loopback HTTP, not physical iPad/LAN/TrueNAS/Docker'}
 (REPORTS/'live-test-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result))
if __name__=='__main__':run()
