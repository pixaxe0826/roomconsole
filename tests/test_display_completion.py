"""Regression for limited display writes. Uses temporary DBs; no user data."""
import time
import pytest
from fastapi.testclient import TestClient
from app.main import create_app

@pytest.fixture
def clients(tmp_path):
 app=create_app(tmp_path,weather_enabled=False)
 with TestClient(app) as admin,TestClient(app) as display:
  admin.headers.update({'Authorization':'Bearer '+app.state.admin_token,'X-Room-Request':'1'})
  display.headers['X-Room-Request']='1'
  ids=admin.post('/api/tasks',json={'title':'반복 작업','date':'2026-09-17','time':'10:00','notes':'보존되는 메모','repeat':{'frequency':'daily','until':'2026-09-19'}}).json()['ids']
  pair=admin.post('/api/devices/pair',json={'name':'iPad existing pairing'}).json()
  assert display.post('/api/devices/claim',json={'code':pair['path'].split('=')[1]}).status_code==200
  yield app,admin,display,ids,pair

def path(tid):return f'/api/tasks/{tid}/completion'
def get_task(c,tid):return next(t for t in c.get('/api/state').json()['tasks'] if t['id']==tid)

def test_complete_and_undo_single_occurrence(clients):
 app,admin,display,ids,_=clients
 before=get_task(admin,ids[0]);r=display.patch(path(ids[0]),json={'version':1,'completed':True})
 assert r.status_code==200 and r.json()['completed'] is True and r.json()['version']==2
 for key in ['id','title','date','time','notes','series_id','category','priority','created_at']:assert r.json()[key]==before[key]
 assert sum(t['completed'] for t in admin.get('/api/state').json()['tasks'])==1
 assert display.patch(path(ids[0]),json={'version':2,'completed':False}).json()['version']==3
 assert sum(t['completed'] for t in admin.get('/api/state').json()['tasks'])==0

def test_noop_does_not_change_version_or_revision(clients):
 _,_,display,ids,_=clients;rev=display.get('/api/state').json()['revision']
 r=display.patch(path(ids[0]),json={'version':1,'completed':False})
 assert r.status_code==200 and r.json()['version']==1
 assert display.get('/api/state').json()['revision']==rev

def test_stale_replay_never_toggles_twice(clients):
 _,_,display,ids,_=clients
 assert display.patch(path(ids[0]),json={'version':1,'completed':True}).status_code==200
 assert display.patch(path(ids[0]),json={'version':1,'completed':True}).status_code==409
 assert get_task(display,ids[0])['completed'] is True
 assert get_task(display,ids[0])['version']==2

def test_conflict_with_manager_edit_preserves_new_fields(clients):
 _,admin,display,ids,_=clients
 assert admin.patch('/api/tasks/'+ids[0],json={'version':1,'title':'관리자 변경'}).status_code==200
 assert display.patch(path(ids[0]),json={'version':1,'completed':True}).status_code==409
 t=get_task(display,ids[0]);assert t['title']=='관리자 변경' and not t['completed']

@pytest.mark.parametrize('extra',[
 {'title':'not permitted'},{'date':'2026-10-01'},{'time':'12:00'},
 {'notes':'not permitted'},{'priority':'high'},{'series_id':'fake'},
 {'category':'work'},{'device_id':'fake'},{'role':'admin'}])
def test_completion_rejects_other_fields(clients,extra):
 _,_,display,ids,_=clients
 assert display.patch(path(ids[0]),json={'version':1,'completed':True,**extra}).status_code==422
 assert not get_task(display,ids[0])['completed']

@pytest.mark.parametrize('payload',[
 {'completed':'false','version':1},{'completed':'true','version':1},
 {'completed':1,'version':1},{'completed':None,'version':1},
 {'completed':True,'version':True},{'completed':True,'version':'1'},
 {'completed':True,'version':0},{'completed':True,'version':-1},
 {'completed':True},{'version':1},{}])
def test_strict_body(clients,payload):
 _,_,display,ids,_=clients
 assert display.patch(path(ids[0]),json=payload).status_code==422

@pytest.mark.parametrize('verb,suffix,body',[
 ('PATCH','',{'version':1,'completed':True}),('PATCH','',{'version':1,'title':'illegal'}),
 ('DELETE','?scope=series&version=1',None)])
def test_general_task_routes_remain_admin_only(clients,verb,suffix,body):
 _,_,display,ids,_=clients
 assert display.request(verb,'/api/tasks/'+ids[0]+suffix,json=body).status_code==401
 assert len(display.get('/api/state').json()['tasks'])==3

@pytest.mark.parametrize('kind',['anonymous','ingest','revoked','expired'])
def test_invalid_sessions_cannot_complete(clients,kind):
 app,admin,display,ids,pair=clients
 if kind=='anonymous':display.cookies.clear()
 if kind=='ingest':display.cookies.clear();display.headers['Authorization']='Bearer '+app.state.ingest_token
 if kind=='revoked':admin.delete('/api/devices/'+pair['device_id'])
 if kind=='expired':
  with app.state.store.connect() as db:db.execute("UPDATE sessions SET expires_at=? WHERE role='display'",(time.time()-5,))
 assert display.patch(path(ids[0]),json={'version':1,'completed':True}).status_code==401
 assert not get_task(admin,ids[0])['completed']

@pytest.mark.parametrize('headers',[{'X-Room-Request':''},{'Origin':'https://evil.example'}])
def test_display_completion_csrf(clients,headers):
 _,_,display,ids,_=clients
 assert display.patch(path(ids[0]),json={'version':1,'completed':True},headers=headers).status_code==403
 assert not get_task(display,ids[0])['completed']

def test_deleted_task(clients):
 _,admin,display,ids,_=clients
 admin.delete('/api/tasks/'+ids[0])
 assert display.patch(path(ids[0]),json={'version':1,'completed':True}).status_code==404

def test_existing_session_survives_restart_and_completion_persists(clients):
 app,_,display,ids,_=clients
 display.patch(path(ids[0]),json={'version':1,'completed':True})
 again=create_app(app.state.store.path.parent,weather_enabled=False)
 with TestClient(again) as client:
  client.headers['X-Room-Request']='1';client.cookies.update(display.cookies)
  assert get_task(client,ids[0])['completed'] is True
  assert client.patch(path(ids[0]),json={'version':2,'completed':False}).status_code==200

def test_completion_broadcasts_to_manager_and_display(clients):
 app,admin,display,ids,_=clients
 # WebSockets authenticate using cookies, not the HTTP bearer header.
 admin.post('/api/auth/login',json={'token':app.state.admin_token})
 with admin.websocket_connect('/ws/manager') as wm,display.websocket_connect('/ws/display') as wd:
  assert wm.receive_json()['type']=='hello';assert wd.receive_json()['type']=='hello'
  assert display.patch(path(ids[0]),json={'version':1,'completed':True}).status_code==200
  assert wm.receive_json()['type']=='invalidate';assert wd.receive_json()['type']=='invalidate'
 assert any(e['event']=='task.completion' for e in admin.get('/api/admin/overview').json()['events'])

def test_capability_and_cache_headers(clients):
 _,_,display,_,_=clients
 assert display.get('/api/state').json()['capabilities']=={'task_completion':True,'speech_upload':True,'llm_response_widget':True}
 assert '?v=0.1.3' in display.get('/client').text
 assert display.get('/static/client.js').headers['cache-control']=='no-cache'
 assert display.get('/widgets/todos/widget.js').headers['cache-control']=='no-cache'
