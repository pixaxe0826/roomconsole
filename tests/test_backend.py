import copy,io,json,sqlite3
from datetime import date
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from pydantic import ValidationError
from app.main import create_app
from app.models import TaskCreate,Layout,HubSettings
from app.recurrence import occurrences

@pytest.fixture
def setup(tmp_path):
 app=create_app(tmp_path,weather_enabled=False)
 with TestClient(app) as client:
  client.headers['X-Room-Request']='1'
  assert client.post('/api/auth/login',json={'token':app.state.admin_token}).status_code==200
  yield app,client

def task(**kw):return {'title':'테스트 작업','date':'2026-09-17',**kw}
def dates(start,frequency,until,**kw):return [d.isoformat() for d in occurrences(TaskCreate(**task(date=start,repeat={'frequency':frequency,'until':until,**kw})))]
@pytest.mark.parametrize('start,freq,end,kw,expected',[
 ('2026-09-17','none',None,{},['2026-09-17']),
 ('2026-09-17','daily','2026-09-19',{},['2026-09-17','2026-09-18','2026-09-19']),
 ('2026-09-17','daily','2026-09-23',{'interval':2},['2026-09-17','2026-09-19','2026-09-21','2026-09-23']),
 ('2026-09-18','weekdays','2026-09-22',{},['2026-09-18','2026-09-21','2026-09-22']),
 ('2026-09-14','weekly','2026-09-27',{'weekdays':[0,2,4]},['2026-09-14','2026-09-16','2026-09-18','2026-09-21','2026-09-23','2026-09-25']),
 ('2026-09-17','weekly','2026-10-02',{},['2026-09-17','2026-09-24','2026-10-01']),
 ('2026-09-14','weekly','2026-10-01',{'weekdays':[0],'interval':2},['2026-09-14','2026-09-28']),
 ('2026-01-31','monthly','2026-04-30',{},['2026-01-31','2026-02-28','2026-03-31','2026-04-30']),
 ('2028-01-31','monthly','2028-03-31',{},['2028-01-31','2028-02-29','2028-03-31']),
 ('2026-11-30','monthly','2027-03-30',{'interval':2},['2026-11-30','2027-01-30','2027-03-30']),
 ('2026-09-17','daily','2026-09-17',{},['2026-09-17']),
])
def test_recurrence(start,freq,end,kw,expected):assert dates(start,freq,end,**kw)==expected
@pytest.mark.parametrize('repeat',[
 {'frequency':'daily'}, {'frequency':'daily','until':'2026-01-01'}, {'frequency':'daily','until':'2032-01-01'},
 {'frequency':'weekly','until':'2026-09-20','weekdays':[7]}, {'frequency':'weekly','until':'2026-09-20','weekdays':[1,1]},
 {'frequency':'daily','until':'2026-09-20','interval':0}, {'frequency':'unsupported'},
])
def test_invalid_recurrence(repeat):
 with pytest.raises(ValidationError):TaskCreate(**task(repeat=repeat))
def test_no_occurrences():
 with pytest.raises(ValueError):dates('2026-09-19','weekdays','2026-09-20')
@pytest.mark.parametrize('route',['/api/state','/api/admin/overview','/api/admin/export','/api/admin/backup','/api/admin/integration'])
def test_anonymous_denied(setup,route):
 app,c=setup;c.cookies.clear();assert c.get(route).status_code==401
def test_login_cookie_flags(setup):
 app,c=setup;r=c.post('/api/auth/login',json={'token':app.state.admin_token});cookie=r.headers['set-cookie'];assert 'HttpOnly' in cookie and 'SameSite=strict' in cookie
@pytest.mark.parametrize('payload',[{'token':'wrong'}, {'token':'x'*50}])
def test_wrong_key(setup,payload):assert setup[1].post('/api/auth/login',json=payload).status_code==401
@pytest.mark.parametrize('route,method,body',[
 ('/api/tasks','POST',task()),('/api/layout','PUT',{'bad':'data'}),('/api/settings','PUT',{}),('/api/admin/overview','GET',None),('/api/commands','POST',{'action':'home'}),
])
def test_display_cannot_write(setup,route,method,body):
 app,c=setup;pair=c.post('/api/devices/pair',json={'name':'iPad'}).json();code=pair['path'].split('=')[1];c.cookies.clear();assert c.post('/api/devices/claim',json={'code':code}).status_code==200
 assert c.get('/api/state').status_code==200
 assert c.request(method,route,json=body).status_code==401

def test_pair_one_use_and_revoke(setup):
 app,c=setup;pair=c.post('/api/devices/pair',json={'name':'iPad'}).json();code=pair['path'].split('=')[1];c.cookies.clear();assert c.post('/api/devices/claim',json={'code':code}).status_code==200;assert c.post('/api/devices/claim',json={'code':code}).status_code==401
 assert c.delete('/api/devices/'+pair['device_id'],headers={'Authorization':'Bearer '+app.state.admin_token}).status_code==200;assert c.get('/api/state').status_code==401

def test_invalid_pair(setup):assert setup[1].post('/api/devices/claim',json={'code':'x'*30}).status_code==401
@pytest.mark.parametrize('headers',[{'Origin':'https://evil.example'}, {'X-Room-Request':''}])
def test_csrf(setup,headers):assert setup[1].post('/api/tasks',json=task(),headers=headers).status_code==403

def test_preview_not_persisted(setup):
 app,c=setup;r=c.post('/api/tasks/preview',json=task(repeat={'frequency':'daily','until':'2026-09-19'}));assert r.json()['count']==3;assert c.get('/api/state').json()['tasks']==[]

def test_independent_completion_version_and_time(setup):
 app,c=setup;r=c.post('/api/tasks',json=task(time='12:00',repeat={'frequency':'daily','until':'2026-09-19'}));assert r.status_code==201;ids=r.json()['ids'];assert len(ids)==3
 assert c.patch('/api/tasks/'+ids[0],json={'version':1,'completed':True,'time':None}).status_code==200
 assert c.patch('/api/tasks/'+ids[0],json={'version':1,'title':'stale'}).status_code==409
 tasks=c.get('/api/state').json()['tasks'];assert sum(t['completed'] for t in tasks)==1;assert next(t for t in tasks if t['id']==ids[0])['time'] is None
@pytest.mark.parametrize('field',['title','date','category','priority','notes','completed'])
def test_required_patch_not_null(setup,field):
 _,c=setup;tid=c.post('/api/tasks',json=task()).json()['ids'][0];assert c.patch('/api/tasks/'+tid,json={'version':1,field:None}).status_code==422
@pytest.mark.parametrize('scope,index,remaining',[('one',1,2),('future',1,1),('series',1,0)])
def test_repeat_delete(setup,scope,index,remaining):
 _,c=setup;ids=c.post('/api/tasks',json=task(repeat={'frequency':'daily','until':'2026-09-19'})).json()['ids'];assert c.delete(f'/api/tasks/{ids[index]}?scope={scope}&version=1').status_code==200;assert len(c.get('/api/state').json()['tasks'])==remaining
@pytest.mark.parametrize('case',['overlap','bounds','duplicate','unknown'])
def test_bad_layout(setup,case):
 _,c=setup;l=c.get('/api/state').json()['layout']
 if case=='overlap':l['widgets'][1]['x']=0
 if case=='bounds':l['widgets'][0]['w']=16
 if case=='duplicate':l['widgets'][1]['id']=l['widgets'][0]['id']
 if case=='unknown':l['widgets'][0]['type']='not-installed'
 assert c.put('/api/layout',json=l).status_code==422

def test_layout_large_small_and_version(setup):
 _,c=setup;l=c.get('/api/state').json()['layout'];w=l['widgets'][0];w.update(w=1,h=1);l.update(columns=16,rows=16,widgets=[w,{**w,'id':'huge','x':1,'w':8,'h':8}]);assert c.put('/api/layout',json=l).status_code==200;assert c.put('/api/layout',json=l).status_code==409
@pytest.mark.parametrize('change',[{'timezone':'Bad/Zone'},{'latitude':37.5},{'longitude':180.1},{'weather_interval_minutes':1}])
def test_bad_settings(setup,change):
 _,c=setup;s=c.get('/api/state').json()['settings'];s.update(change);assert c.put('/api/settings',json=s).status_code==422

def test_fresh_no_weather_or_samples(setup):
 s=setup[1].get('/api/state').json();assert s['weather'] is None and not s['tasks'] and s['settings']['latitude'] is None

def test_widget_data_reload(setup):
 _,c=setup;assert len(c.post('/api/widgets/reload').json()['widgets'])==7;assert c.put('/api/widgets/note/data',json={'data':{'text':'hello'}}).status_code==200;assert c.get('/api/state').json()['widget_data']['note']['text']=='hello';assert c.put('/api/widgets/nope/data',json={'data':{}}).status_code==404

def test_voice_idempotent_and_conflict(setup):
 app,c=setup;body={'request_id':'same','source':'test','text':'안녕하세요'};r=c.post('/api/voice/text',json=body);assert r.status_code==202;assert c.post('/api/voice/text',json=body).json()['duplicate'];body['text']='다른 내용';assert c.post('/api/voice/text',json=body).status_code==409;assert c.get('/api/state').json()['tasks']==[]

def test_text_upload(setup):
 _,c=setup;r=c.post('/api/voice/upload',data={'request_id':'txt1','source':'test'},files={'file':('input.txt','\ufeff할 일 추가'.encode('utf-8'),'text/plain')});assert r.status_code==202 and r.json()['status']=='pending_review'

def test_audio_upload_review_delete(setup):
 app,c=setup;r=c.post('/api/voice/upload',data={'request_id':'audio1'},files={'file':('a.wav',b'RIFFexample','audio/wav')});assert r.status_code==202;v=r.json();assert v['status']=='awaiting_transcription';assert c.get('/api/voice/'+v['id']+'/audio').content==b'RIFFexample';assert c.patch('/api/voice/'+v['id'],json={'status':'pending_review','text':'전사 내용'}).status_code==200;assert c.delete('/api/voice/'+v['id']).status_code==200;assert not list((app.state.store.path.parent/'audio').iterdir())
@pytest.mark.parametrize('mime,blob,status',[('text/html',b'<script/>',415),('audio/wav',b'',413),('audio/wav',b'x'*(10*1024*1024+1),413)])
def test_file_rejections(setup,mime,blob,status):assert setup[1].post('/api/voice/upload',data={'request_id':'bad'},files={'file':('a.bin',blob,mime)}).status_code==status

def test_ingest_key_cannot_admin(setup):
 app,c=setup;c.cookies.clear();c.headers['Authorization']='Bearer '+app.state.ingest_token;assert c.post('/api/voice/text',json={'request_id':'ingest1','text':'hello'}).status_code==202;assert c.get('/api/admin/overview').status_code==401;assert c.post('/api/tasks',json=task()).status_code==401

def test_export_backup(setup,tmp_path):
 _,c=setup;c.post('/api/tasks',json=task());assert len(c.get('/api/admin/export').json()['tasks'])==1;r=c.get('/api/admin/backup');assert r.content.startswith(b'SQLite format 3');p=tmp_path/'check.sqlite';p.write_bytes(r.content)
 with sqlite3.connect(p) as db:assert db.execute('SELECT count(*) FROM tasks').fetchone()[0]==1

def test_websocket_invalidates_and_commands(setup):
 _,c=setup
 with c.websocket_connect('/ws/display') as ws:
  assert ws.receive_json()['type']=='hello';c.post('/api/tasks',json=task());assert ws.receive_json()['type']=='invalidate';c.post('/api/commands',json={'action':'expand','widget_id':'calendar'});msg=ws.receive_json();assert msg['type']=='command' and msg['widget_id']=='calendar';ws.send_json({'type':'ping'});assert ws.receive_json()['type']=='pong'

def test_websocket_anonymous(setup):
 _,c=setup;c.cookies.clear()
 with pytest.raises(WebSocketDisconnect):
  with c.websocket_connect('/ws/display'):pass

def test_bad_remote_widget(setup):assert setup[1].post('/api/commands',json={'action':'expand','widget_id':'missing'}).status_code==422

def test_private_state_and_xss_as_data(setup):
 app,c=setup;c.post('/api/tasks',json=task(title='<img src=x onerror=alert(1)>'));r=c.get('/api/state');assert r.json()['tasks'][0]['title'].startswith('<img');assert app.state.admin_token not in r.text and app.state.ingest_token not in r.text and 'voice' not in r.json()

def test_persistence(setup):
 app,c=setup;c.post('/api/tasks',json=task());again=create_app(app.state.store.path.parent,False)
 with TestClient(again) as c2:assert len(c2.get('/api/state',headers={'Authorization':'Bearer '+again.state.admin_token}).json()['tasks'])==1

def test_unknown_task_field_rejected(setup):assert setup[1].post('/api/tasks',json=task(admin=True)).status_code==422

def test_invalid_time(setup):assert setup[1].post('/api/tasks',json=task(time='25:00')).status_code==422

def test_audio_idempotence_keeps_one_file(setup):
 app,c=setup;data={'request_id':'audio-retry','source':'adapter'}
 first=c.post('/api/voice/upload',data=data,files={'file':('a.wav',b'RIFFsame','audio/wav')}).json()
 second=c.post('/api/voice/upload',data=data,files={'file':('a.wav',b'RIFFsame','audio/wav')}).json()
 assert first['id']==second['id'] and second['duplicate'];assert len(list((app.state.store.path.parent/'audio').iterdir()))==1
 assert c.post('/api/voice/upload',data=data,files={'file':('a.wav',b'RIFFdifferent','audio/wav')}).status_code==409

def test_request_body_limit(setup):assert setup[1].post('/api/voice/text',content=b'x'*(11*1024*1024+1),headers={'Content-Type':'application/json'}).status_code==413

def test_pair_expiration(setup):
 app,c=setup;r=c.post('/api/devices/pair',json={'name':'expired'}).json()
 with app.state.store.connect() as db:db.execute('UPDATE pairs SET expires_at=0')
 c.cookies.clear();assert c.post('/api/devices/claim',json={'code':r['path'].split('=')[1]}).status_code==401

def test_client_page_no_editor_controls(setup):
 html=setup[1].get('/client').text
 assert '<input' not in html and '<textarea' not in html and '<select' not in html

def test_qr_svg_and_bad_scheme(setup):
 _,c=setup;r=c.get('/api/admin/qr',params={'url':'http://192.168.0.20:8088/client#pair=example'});assert r.status_code==200 and '<svg' in r.text;assert c.get('/api/admin/qr',params={'url':'javascript:alert(1)'}).status_code==422

def test_missing_task_not_found(setup):assert setup[1].delete('/api/tasks/missing').status_code==404

def test_voice_never_creates_task_automatically(setup):
 _,c=setup;r=c.post('/api/voice/text',json={'request_id':'noauto','text':'내일 할 일에 산책하기 추가해 줘'});assert r.status_code==202
 c.patch('/api/voice/'+r.json()['id'],json={'status':'reviewed'});assert c.get('/api/state').json()['tasks']==[]
