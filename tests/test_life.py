"""Notes/alarms: synthetic SQLite and controllable server clock, never physical audio."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException
from pydantic import ValidationError
from app.main import create_app
from app.life import AlarmInput, AlarmCreate, AlarmUpdate, NoteCreate, NoteUpdate, EventAction, LifeService, next_fire


def ts(s):return datetime.fromisoformat(s).timestamp()
START=ts('2026-09-20T10:00:00+09:00')

def key():return uuid.uuid4().hex

def note(**kw):return {'request_id':key(),'title':'실험 메모','body':'<script>window.PWNED=true</script>\n두 번째 줄','shared':False,'pinned':False}|kw

def alarm(**kw):return {'request_id':key(),'label':'실험 알람','time':'10:01','timezone':'Asia/Seoul','repeat':'once','date':'2026-09-20','weekdays':[],'enabled':True}|kw

@pytest.fixture
def hub(tmp_path):
    app=create_app(tmp_path/'data',weather_enabled=False)
    clock=[START];app.state.life.clock=lambda:clock[0]
    with TestClient(app) as admin:
        admin.headers.update({'Authorization':'Bearer '+app.state.admin_token,'X-Room-Request':'1'})
        display=TestClient(app);display.headers['X-Room-Request']='1'
        pair=admin.post('/api/devices/pair',json={'name':'synthetic display'}).json()
        assert display.post('/api/devices/claim',json={'code':pair['path'].split('=')[1]}).status_code==200
        yield app,admin,display,clock
        display.close()


def place(app,*kinds):
    layout=app.state.store.get('layout')
    for widget,kind in zip(layout['widgets'],kinds):widget['type']=kind
    app.state.store.set('layout',layout)


def due(hub):
    app,a,d,c=hub
    r=a.post('/api/life/alarms',json=alarm());assert r.status_code==201,r.text
    c[0]=START+60;app.state.life.tick()
    return app.state.life.events()[0]


def test_notes_version_privacy_and_legacy_preservation(hub):
    app,a,d,_=hub;app.state.store.set('widget_data:note',{'text':'기존 고정 메모'})
    body=note();n=a.post('/api/life/notes',json=body);assert n.status_code==201
    nid=n.json()['id'];assert a.post('/api/life/notes',json=body).json()['duplicate']
    assert len(a.get('/api/life/notes').json()['items'])==1
    place(app,'note')
    assert d.get('/api/life/display').json()['notes']['items']==[]
    update={k:v for k,v in body.items() if k!='request_id'}|{'version':1,'shared':True}
    assert a.put('/api/life/notes/'+nid,json=update).status_code==200
    rich=d.get('/api/state').json();assert rich['life']['notes']['items'][0]['body']==body['body']
    assert rich['widget_data']['note']['text']=='기존 고정 메모'
    assert a.put('/api/life/notes/'+nid,json=update).status_code==409
    assert a.request('DELETE','/api/life/notes/'+nid,json={'version':1}).status_code==409
    assert a.request('DELETE','/api/life/notes/'+nid,json={'version':2}).status_code==200
    assert a.post('/api/life/notes',json=body).json()['duplicate']
    assert not a.get('/api/life/notes').json()['items'] # late retry cannot resurrect deleted data


def test_shared_note_requires_widget_and_private_titles_never_leak(hub):
    app,a,d,_=hub
    a.post('/api/life/notes',json=note(title='PUBLIC',shared=True))
    a.post('/api/life/notes',json=note(title='PRIVATE'))
    assert not d.get('/api/life/display').json()['notes']['items']
    place(app,'note');raw=d.get('/api/life/display').text
    assert 'PUBLIC' in raw and 'PRIVATE' not in raw

@pytest.mark.parametrize('route',['notes','alarms'])
def test_no_display_or_ingest_crud(hub,route):
    app,a,d,_=hub
    payload=note() if route=='notes' else alarm()
    assert d.get('/api/life/'+route).status_code==401
    for method,url in [('POST','/api/life/'+route),('PUT','/api/life/'+route+'/anything'),('DELETE','/api/life/'+route+'/anything')]:
        assert d.request(method,url,json=payload).status_code==401
    with TestClient(app) as anonymous:
        assert anonymous.get('/api/life/display').status_code==401
        anonymous.headers['Authorization']='Bearer '+app.state.ingest_token
        assert anonymous.post('/api/life/'+route,json=payload).status_code==401

@pytest.mark.parametrize('bad',[{'title':' '},{'body':'x'*8001},{'shared':'true'},{'pinned':1},{'unknown':1}])
def test_note_validation(hub,bad):
    assert hub[1].post('/api/life/notes',json=note(**bad)).status_code==422

@pytest.mark.parametrize('bad',[{'label':' '},{'time':'25:00'},{'time':'7:00'},{'date':'2026-02-30'},{'date':'9999-12-31'},{'timezone':'invalid/zone'},{'enabled':1},{'weekdays':[0]}, {'repeat':'weekly','date':None,'weekdays':[]},{'repeat':'weekly','date':None,'weekdays':[True]},{'repeat':'weekly','date':None,'weekdays':[7]}])
def test_alarm_validation(hub,bad):
    assert hub[1].post('/api/life/alarms',json=alarm(**bad)).status_code==422


def test_past_alarm_rejected(hub):
    assert hub[1].post('/api/life/alarms',json=alarm(time='09:59')).status_code==422
    assert hub[1].post('/api/life/alarms',json=alarm(time='09:59',enabled=False)).status_code==201


def test_same_request_different_payload_conflict(hub):
    a=hub[1];body=note();a.post('/api/life/notes',json=body)
    assert a.post('/api/life/notes',json=body|{'title':'changed'}).status_code==409


def test_once_exactly_one_occurrence_and_restart(hub):
    app,a,d,c=hub;ev=due(hub)
    assert ev['state']=='ringing';assert not app.state.life.alarms()[0]['enabled']
    app.state.life.tick();assert len(app.state.life.events())==1
    new=LifeService(app.state.store,app.state.life.changed,clock=lambda:c[0]);new.tick()
    assert len(new.events())==1
    place(app,'alarms');dto=d.get('/api/life/display').json()
    assert dto['scheduler']['sound_confirmed'] is False
    assert dto['scheduler']['delivery']=='foreground_browser_only'
    assert dto['alarms']['events'][0]['id']==ev['id']


def test_ack_permission_idempotence_and_version(hub):
    app,a,d,_=hub;ev=due(hub);url='/api/life/events/'+ev['id']+'/ack';body={'version':ev['version'],'request_id':key()}
    assert d.post(url,json=body).status_code==403
    place(app,'alarms')
    assert d.post(url,json=body|{'version':999}).status_code==409
    assert d.post(url,json=body).status_code==200
    assert d.post(url,json=body).json()['duplicate']
    assert app.state.life.events()[0]['state']=='acknowledged'
    assert not d.get('/api/life/display').json()['alarms']['events']


def test_snooze_three_times_and_server_restart(hub):
    app,a,d,c=hub;ev=due(hub);place(app,'alarms')
    for n in range(3):
        body={'version':ev['version'],'request_id':key()};url='/api/life/events/'+ev['id']+'/snooze'
        assert d.post(url,json=body).status_code==200
        assert d.post(url,json=body).json()['duplicate']
        ev=app.state.life.events()[0];assert ev['snoozes']==n+1 and ev['state']=='snoozed'
        c[0]+=300
        newer=LifeService(app.state.store,app.state.life.changed,clock=lambda:c[0]);newer.tick()
        ev=newer.events()[0];assert ev['state']=='ringing'
    assert d.post(url,json={'version':ev['version'],'request_id':key()}).status_code==409


def test_overdue_and_auto_expiry_never_claim_audio(hub):
    app,a,d,c=hub;a.post('/api/life/alarms',json=alarm());c[0]=START+3600;app.state.life.tick()
    assert app.state.life.events()[0]['state']=='missed'
    c[0]=START;a.post('/api/life/alarms',json=alarm());c[0]=START+60;app.state.life.tick()
    c[0]+=121;app.state.life.tick();assert all(e['state']=='missed' for e in app.state.life.events())


def test_weekly_timezone_and_downtime_coalesce(hub):
    app,a,d,c=hub
    body=alarm(repeat='weekly',date=None,weekdays=list(range(7)))
    a.post('/api/life/alarms',json=body);c[0]=START+10*86400;app.state.life.tick()
    assert len(app.state.life.events())==1 and app.state.life.events()[0]['state']=='missed'
    row=app.state.life.alarms()[0];assert ts(row['next_fire_at'])>c[0] and row['enabled']
    settings=app.state.store.get('settings');settings['timezone']='UTC';app.state.store.set('settings',settings)
    assert app.state.life.alarms()[0]['timezone']=='Asia/Seoul'


def test_dst_once_reject_and_weekly_policy():
    def spec(**kw):return AlarmInput(label='DST',time='02:30',timezone='America/New_York',date='2026-03-08',**kw).model_dump()
    with pytest.raises(HTTPException):next_fire(spec(),ts('2026-03-07T00:00:00+00:00'))
    s=spec();s.update(time='01:30',date='2026-11-01')
    with pytest.raises(HTTPException):next_fire(s,ts('2026-10-31T00:00:00+00:00'))
    s.update(repeat='weekly',date=None,weekdays=[6],time='02:30')
    assert next_fire(s,ts('2026-03-07T00:00:00+00:00'))==ts('2026-03-15T02:30:00-04:00')
    s['time']='01:30';assert next_fire(s,ts('2026-11-01T05:45:00+00:00'))==ts('2026-11-08T01:30:00-05:00')


def test_edit_cancels_existing_snooze_and_conflict(hub):
    app,a,d,c=hub;ev=due(hub);r=app.state.life.alarms()[0]
    body={k:r[k] for k in ['label','time','timezone','repeat','date','weekdays','enabled','version']}
    body.update(time='11:00',enabled=True)
    assert a.put('/api/life/alarms/'+r['id'],json=body).status_code==200
    assert app.state.life.events()[0]['state']=='cancelled'
    assert a.put('/api/life/alarms/'+r['id'],json=body).status_code==409
    assert a.request('DELETE','/api/life/alarms/'+r['id'],json={'version':body['version']+1}).status_code==200
    assert app.state.life.events()==[]


def test_concurrent_snooze_has_one_effect(hub):
    app,a,d,c=hub;ev=due(hub);body=EventAction(version=ev['version'],request_id=key())
    with ThreadPoolExecutor(2) as pool:
        out=list(pool.map(lambda _:app.state.life.act(ev['id'],'snooze',body),range(2)))
    assert sum(x['duplicate'] for x in out)==1
    assert app.state.life.events()[0]['snoozes']==1


def test_export_preserves_existing_data_and_new_notes(hub):
    app,a,d,c=hub;a.post('/api/life/notes',json=note())
    exported=a.get('/api/admin/export').json()
    assert len(exported['life']['notes'])==1 and 'tasks' in exported and 'series' in exported
    assert not app.state.store.tasks()
    with app.state.store.connect() as db:
        assert db.execute('select count(*) from llm_requests').fetchone()[0]==0


def test_cookie_csrf(hub):
    app,a,d,c=hub;a.post('/api/auth/login',json={'token':app.state.admin_token})
    headers={'Authorization':'','X-Room-Request':''}
    assert a.post('/api/life/notes',headers=headers,json=note()).status_code==403
    assert a.post('/api/life/notes',headers={'Origin':'https://evil.example'},json=note()).status_code==403


def test_real_lifespan_scheduler_starts(hub):
    app,a,d,c=hub;a.post('/api/life/alarms',json=alarm());c[0]+=60
    end=time.monotonic()+4
    while time.monotonic()<end and not app.state.life.events():time.sleep(.05)
    assert app.state.life.events()[0]['state']=='ringing'
    assert app.state.life.last_tick_at


def test_snapshot_revision_increases_only_on_changes(hub):
    app,a,d,c=hub;v=app.state.life.display()['revision'];app.state.life.tick()
    assert app.state.life.display()['revision']==v
    a.post('/api/life/notes',json=note());assert app.state.life.display()['revision']>v
