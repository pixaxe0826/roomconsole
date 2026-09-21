"""Synthetic clock, actual SQLite/API. Physical V35 timing/sound NOT tested."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time

import pytest
from pydantic import ValidationError
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import create_app
from app.store import Store
from app.timers import TimerService, TimerStart, TimerStop, MAX_ACTIVE
from app.widget_protocol import WidgetRequest, ExecutionContext
from app.widget_protocol.timers import TimerAdapter
from app.widget_protocol.core import WidgetRegistry


@pytest.fixture
def timers(tmp_path):
    at=[time.time()];events=[]
    async def changed(event, detail=''):events.append((event,detail))
    service=TimerService(Store(tmp_path/'timer-test.sqlite3'),changed,clock=lambda:at[0])
    return service,at,events


def start(s,n=240,key='first',actor='test'):
    return s.start(TimerStart(request_id=key,duration_seconds=n),actor)


@pytest.mark.parametrize('duration',[1,2,59,60,61,239,240,599,600])
def test_seconds_exact_and_deadline(timers,duration):
    s,at,_=timers;r=start(s,duration)
    assert r['duration_seconds']==duration and r['remaining_seconds']==duration
    at[0]+=duration-.1;assert s.get(r['id'])['remaining_seconds']==1
    at[0]+=.1;s.tick();assert s.get(r['id'])['state']=='expired'
    assert s.get(r['id'])['remaining_seconds']==0


@pytest.mark.parametrize('value',[0,-1,601,True,False,1.5,'240',None])
def test_invalid_duration_rejected(value):
    with pytest.raises(ValidationError):TimerStart(request_id='test',duration_seconds=value)


def test_multiple_timers_current_and_stopping_independently(timers):
    s,at,_=timers;a=start(s,240,'first');b=start(s,120,'second')
    assert s.snapshot()['active_count']==2 and s.snapshot()['current_id']==b['id']
    result=s.stop('current',TimerStop(request_id='stop-current'),'test')
    assert result['id']==b['id'] and result['state']=='stopped'
    assert s.snapshot()['current_id']==a['id'] and s.snapshot()['active_count']==1
    assert s.get(a['id'])['state']=='running'


def test_atomic_replay_current_cannot_stop_next_timer_even_after_restart(timers):
    s,at,_=timers;a=start(s,240,'first');b=start(s,240,'second')
    stop=TimerStop(request_id='stop-once')
    assert s.stop('current',stop,'test')['id']==b['id']
    s2=TimerService(s.store,s.changed,clock=s.clock)
    repeated=s2.stop('current',stop,'test')
    assert repeated['duplicate'] and not repeated['changed'] and repeated['id']==b['id']
    assert s2.get(a['id'])['state']=='running'


def test_start_replay_no_new_deadline_after_restart(timers):
    s,at,_=timers;r=start(s);at[0]+=12
    s2=TimerService(s.store,s.changed,clock=s.clock);again=start(s2)
    assert again['duplicate'] and again['id']==r['id'] and again['deadline_at']==r['deadline_at']
    assert again['remaining_seconds']==228 and s2.snapshot()['active_count']==1


def test_restart_downtime_marks_expired_does_not_restart(timers):
    s,at,_=timers;r=start(s,2);at[0]+=300
    recovered=TimerService(s.store,s.changed,clock=s.clock)
    assert recovered.snapshot()['items'][0]['state']=='expired'
    assert recovered.tick()==[r['id']]
    assert recovered.tick()==[] and recovered.snapshot()['active_count']==0


def test_same_key_changed_request_conflicts_and_actor_scope(timers):
    s,at,_=timers;start(s)
    with pytest.raises(HTTPException) as exc:start(s,300)
    assert exc.value.status_code==409
    assert not start(s,300,actor='another')['duplicate']
    assert s.snapshot()['active_count']==2


def test_concurrent_same_start_is_one_countdown(timers):
    s,_,_=timers
    with ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(lambda _:start(s),range(12)))
    assert len({r['id'] for r in results})==1
    assert sum(not r['duplicate'] for r in results)==1
    assert s.snapshot()['active_count']==1


def test_stop_terminal_and_stale_versions(timers):
    s,at,_=timers;r=start(s)
    with pytest.raises(HTTPException) as exc:s.stop(r['id'],TimerStop(request_id='stale',version=99),'test')
    assert exc.value.status_code==409 and s.get(r['id'])['state']=='running'
    assert s.stop(r['id'],TimerStop(request_id='ok',version=1),'test')['changed']
    assert not s.stop(r['id'],TimerStop(request_id='again',version=1),'test')['changed']


def test_current_none_does_not_claim_success(timers):
    s,_,_=timers
    with pytest.raises(HTTPException) as exc:s.stop('current',TimerStop(request_id='stop'),'test')
    assert exc.value.status_code==404


def test_capacity_bounds_and_freeing_slot(timers):
    s,at,_=timers
    for n in range(MAX_ACTIVE):start(s,key=str(n))
    with pytest.raises(HTTPException) as exc:start(s,key='overflow')
    assert exc.value.status_code==429
    s.stop('current',TimerStop(request_id='release'),'test')
    assert start(s,key='after-release')['state']=='running'


def test_get_snapshot_does_not_write_every_countdown_second(timers):
    s,at,_=timers;r=start(s);rev=s.snapshot()['revision']
    at[0]+=2
    assert s.snapshot()['revision']==rev
    assert s.get(r['id'])['remaining_seconds']==238


def test_expiration_no_extra_notification_after_second_tick(timers):
    s,at,events=timers;r=start(s,1);at[0]+=1
    async def run():
        task=asyncio.create_task(s.run());await asyncio.sleep(.55);task.cancel()
        try:await task
        except asyncio.CancelledError:pass
    asyncio.run(run())
    assert events==[('timer.expired',r['id'])]


def test_adapter_metadata_and_actual_protocol_actions(timers):
    s,_,_=timers;registry=WidgetRegistry();registry.register(TimerAdapter(s))
    caps={c.action:c for c in registry.get_capabilities('timer')}
    assert set(caps)=={'start','stop','get','list'}
    assert not caps['start'].requires_confirmation and caps['start'].permission_level=='control'
    assert registry.manifest()['http_immediate_actions']==['timer.start','timer.stop']
    auth=ExecutionContext(principal='test',role='admin',permissions=frozenset({'read','control'}))
    async def run():
        r=await registry.execute({'request_id':'p-start','widget':'timer','action':'start','args':{'duration_seconds':240}},auth)
        assert r.status=='success' and r.meta.changed and r.meta.adapter=='TimerAdapter'
        assert r.meta.events[0].event=='timer.started'
        r2=await registry.execute({'request_id':'p-stop','widget':'timer','action':'stop','target':{'type':'reference','value':'current'}},auth)
        assert r2.status=='success' and r2.data['state']=='stopped'
        read=await registry.execute({'request_id':'p-list','widget':'timer','action':'list'},auth)
        assert read.data['active_count']==0
        denied=await registry.execute({'request_id':'p-denied','widget':'timer','action':'start','args':{'duration_seconds':240}},ExecutionContext())
        assert denied.status=='permission_denied'
    asyncio.run(run())


def place(c):
    layout=c.get('/api/state').json()['layout']
    layout['widgets'][0].update(type='timers',w=2,h=2)
    assert c.put('/api/layout',json=layout).status_code==200


def test_authenticated_http_and_display_narrow_permissions(tmp_path):
    app=create_app(tmp_path,weather_enabled=False)
    with TestClient(app) as admin,TestClient(app) as device:
        admin.headers.update({'Authorization':'Bearer '+app.state.admin_token,'X-Room-Request':'1'})
        device.headers['X-Room-Request']='1'
        assert device.get('/api/timers').status_code==401
        pair=admin.post('/api/devices/pair',json={'name':'timer-test'}).json()
        assert device.post('/api/devices/claim',json={'code':pair['path'].split('=')[1]}).status_code==200
        assert device.post('/api/timers/start',json={'request_id':'d1','duration_seconds':240}).status_code==403
        place(admin)
        assert device.get('/api/state').json()['capabilities']['timer_control']
        r=device.post('/api/timers/start',json={'request_id':'d1','duration_seconds':240}).json()
        assert r['state']=='running'
        assert device.post('/api/tasks',json={'title':'not allowed','date':'2026-09-22'}).status_code==401
        assert device.post('/api/widget-protocol/requests',json={'request_id':'p','widget':'timer','action':'list'}).status_code==401
        response=device.post('/api/timers/'+r['id']+'/stop',json={'request_id':'s1','version':1})
        assert response.status_code==200 and response.json()['state']=='stopped'
        assert device.post('/api/timers/start',json={'request_id':'csrf','duration_seconds':1},headers={'Origin':'http://evil.invalid'}).status_code==403
        del device.headers['X-Room-Request']
        assert device.post('/api/timers/start',json={'request_id':'csrf2','duration_seconds':1}).status_code==403
        assert not any('request_key' in x or 'principal' in x for x in admin.get('/api/timers').json()['items'])


def test_no_default_layout_migration_and_widget_size(tmp_path):
    from app.widgets import discover
    app=create_app(tmp_path,weather_enabled=False)
    assert not app.state.timers.visible()
    widgets,_=discover(Path(__file__).resolve().parents[1]/'widgets')
    w=next(w for w in widgets if w['id']=='timers')
    assert w['defaultSize']=={'w':2,'h':2}
    assert app.state.timers.display()['items']==[]
