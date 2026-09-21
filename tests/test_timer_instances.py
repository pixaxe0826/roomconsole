"""Instance ownership, additive migration, exact durations and HTTP isolation."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import subprocess
import shutil

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app.main import create_app
from app.store import Store
from app.timers import TimerService, TimerStart, TimerStop
from app.widget_protocol import WidgetRegistry, ExecutionContext
from app.widget_protocol.timers import TimerAdapter


def layout(store, ids=('timer-a', 'timer-b')):
    value = store.get('layout')
    value['widgets'] = [dict(id=i, type='timers', title=f'Timer {n+1}', x=n*2, y=0, w=2, h=2, config={}) for n, i in enumerate(ids)]
    store.set('layout', value)
    return value


@pytest.fixture
def service(tmp_path):
    store = Store(tmp_path / 'synthetic.sqlite3')
    layout(store)
    at = [1790000000.125]
    async def changed(*args): pass
    return TimerService(store, changed, clock=lambda: at[0]), at


def start(s, widget='timer-a', duration=240, key='start-a'):
    return s.start(TimerStart(widget_id=widget, duration_seconds=duration, request_id=key), 'test')


@pytest.mark.parametrize('duration', [1, 59, 60, 90, 239, 240, 599, 600])
def test_instance_duration_exact_and_independent(service, duration):
    s, at = service
    a = start(s, duration=duration)
    at[0] += .25
    b = start(s, 'timer-b', 300, 'start-b')
    assert a['widget_id'] == 'timer-a' and b['widget_id'] == 'timer-b'
    assert (datetime.fromisoformat(a['deadline_at']) - datetime.fromisoformat(a['started_at'])).total_seconds() == duration
    assert a['id'] != b['id'] and a['remaining_seconds'] == duration
    at[0] += .5
    before = s.get(b['id'])
    s.stop(a['id'], TimerStop(request_id='stop-a', widget_id='timer-a'), 'test')
    assert s.get(b['id']) == before and before['state'] == 'running'


def test_slot_busy_does_not_restart_or_create_hidden_timer(service):
    s, at = service
    a = start(s); at[0] += 3
    with pytest.raises(HTTPException) as e: start(s, duration=60, key='new-key')
    assert e.value.status_code == 409
    assert s.get(a['id'])['deadline_at'] == a['deadline_at']
    assert len(s.snapshot()['items']) == 1
    b = start(s, 'timer-b', 90, 'b')
    with pytest.raises(HTTPException) as e: start(s, None, 120, 'voice-when-full')
    assert e.value.status_code == 409 and s.snapshot()['active_count'] == 2
    assert s.get(b['id'])['state'] == 'running'


def test_unscoped_voice_protocol_uses_first_idle_widget(service):
    s, _ = service
    registry = WidgetRegistry(); registry.register(TimerAdapter(s))
    authority = ExecutionContext(principal='voice', role='admin', permissions=frozenset({'read','control'}))
    async def run():
        a = await registry.execute({'widget':'timer','action':'start','request_id':'one','args':{'duration_seconds':240}}, authority)
        b = await registry.execute({'widget':'timer','action':'start','request_id':'two','args':{'duration_seconds':90}}, authority)
        assert a.status == b.status == 'success'
        assert a.data['widget_id'] == 'timer-a' and b.data['widget_id'] == 'timer-b'
        again = await registry.execute({'widget':'timer','action':'start','request_id':'one','args':{'duration_seconds':240}}, authority)
        assert again.data['id'] == a.data['id'] and again.meta.duplicate
    asyncio.run(run())


def test_scoped_current_and_wrong_widget_target(service):
    s, _ = service; a=start(s); b=start(s, 'timer-b', 90, 'b')
    with pytest.raises(HTTPException) as e:
        s.stop(b['id'], TimerStop(request_id='wrong', widget_id='timer-a'), 'test')
    assert e.value.status_code == 409
    result=s.stop('current', TimerStop(request_id='local-current', widget_id='timer-a'), 'test')
    assert result['id'] == a['id'] and s.get(b['id'])['state'] == 'running'
    # Replaying after starting another run in A must not stop the new run or B.
    newer=start(s, key='again')
    replay=s.stop('current', TimerStop(request_id='local-current', widget_id='timer-a'), 'test')
    assert replay['duplicate'] and replay['id'] == a['id']
    assert s.get(newer['id'])['state'] == s.get(b['id'])['state'] == 'running'


def test_scoped_start_receipt_and_cross_widget_key_conflict(service):
    s, at = service; a=start(s); at[0] += 12
    assert start(s)['duplicate'] and s.snapshot()['active_count'] == 1
    with pytest.raises(HTTPException) as e: start(s, 'timer-b')
    assert e.value.status_code == 409
    assert s.get(a['id'])['remaining_seconds'] == 228


def test_race_same_widget_and_parallel_different_widgets(service):
    s, _ = service
    def attempt(i):
        try: return start(s, key=f'parallel-{i}')
        except HTTPException as e: return e.status_code
    with ThreadPoolExecutor(max_workers=8) as pool: results=list(pool.map(attempt, range(8)))
    assert sum(isinstance(r,dict) for r in results) == 1
    assert results.count(409) == 7
    b=start(s, 'timer-b', 2, 'parallel-b')
    assert s.snapshot()['active_count'] == 2 and b['widget_id'] == 'timer-b'


def test_expiry_and_restart_keep_bindings_and_original_deadlines(service):
    s, at = service; a=start(s, duration=1); b=start(s, 'timer-b', 600, 'b')
    at[0] += 2; s.tick()
    newer=TimerService(s.store,s.changed,clock=s.clock)
    assert newer.get(a['id'])['state']=='expired'
    assert newer.get(b['id'])['widget_id']=='timer-b'
    assert newer.get(b['id'])['deadline_at']==b['deadline_at']
    assert newer.get(b['id'])['remaining_seconds']==598
    c=start(newer, duration=90, key='restart-a')
    assert c['widget_id']=='timer-a' and newer.get(b['id'])['state']=='running'


def test_migrate_legacy_rows_preserves_deadlines_and_receipts(tmp_path):
    store=Store(tmp_path/'old.sqlite3');layout(store)
    # Actual pre-instance schema, not a pre-upgraded fixture.
    with closing(store.connect()) as db:
        with db:
            db.executescript('''CREATE TABLE hub_timers(sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
              label TEXT NOT NULL,duration_seconds INTEGER NOT NULL,state TEXT NOT NULL,
              started_at REAL NOT NULL,deadline_at REAL NOT NULL,ended_at REAL,version INTEGER NOT NULL);
              CREATE TABLE hub_timer_requests(request_key TEXT PRIMARY KEY,signature TEXT NOT NULL,response TEXT NOT NULL);''')
            for i in range(3):
                db.execute('INSERT INTO hub_timers(id,label,duration_seconds,state,started_at,deadline_at,version) VALUES(?,?,?,?,?,?,?)',
                           (f'old-{i}','기존',240,'running',1000.,1240.,1))
            old={'id':'old-0','changed':True,'duplicate':False}
            db.execute('INSERT INTO hub_timer_requests VALUES(?,?,?)',(TimerService._key('test','old-request'),json.dumps(['start',240,'타이머'],ensure_ascii=False),json.dumps(old)))
    async def changed(*a): pass
    s=TimerService(store,changed,clock=lambda:1010.)
    assert s.get('old-0')['widget_id']=='timer-a'
    assert s.get('old-1')['widget_id']=='timer-b'
    assert s.get('old-2')['widget_id'] is None  # overflow is preserved for recovery UI
    assert all(r['remaining_seconds']==230 and r['version']==1 for r in s.snapshot()['items'])
    replay=s.start(TimerStart(request_id='old-request',duration_seconds=240),'test')
    assert replay['duplicate'] and replay['id']=='old-0' and len(s.snapshot()['items'])==3
    assert s.reconcile_widgets()==0
    assert [r['widget_id'] for r in TimerService(store,changed,clock=lambda:1010.).snapshot()['items']]==[None,'timer-b','timer-a']


def test_removed_instance_is_not_reused_or_silently_stopped(service):
    s,_=service;a=start(s);layout(s.store,('timer-b','timer-c'));s.reconcile_widgets()
    assert s.get(a['id'])['widget_id']=='timer-a' and s.get(a['id'])['state']=='running'
    with pytest.raises(HTTPException) as e: start(s,key='stale-widget')
    assert e.value.status_code==409
    assert start(s,None,90,'new-slot')['widget_id']=='timer-b'


def test_latest_duration_retained_for_each_placed_widget(service):
    s,at=service;a=start(s,duration=90);s.stop(a['id'],TimerStop(request_id='stop'),'test')
    for i in range(260):
        b=start(s,'timer-b',1,f'b-{i}');at[0]+=2;s.tick()
    assert s.get(a['id'])['duration_seconds']==90
    assert len(s.snapshot()['items'])<=258


def test_http_instance_scopes_and_immediate_snapshot(tmp_path):
    app=create_app(tmp_path,weather_enabled=False)
    with TestClient(app) as admin,TestClient(app) as display:
        admin.headers.update({'Authorization':'Bearer '+app.state.admin_token,'X-Room-Request':'1'})
        display.headers['X-Room-Request']='1'
        value=layout(app.state.store)
        assert admin.put('/api/layout',json=value).status_code==200
        pairing=admin.post('/api/devices/pair',json={'name':'synthetic'}).json()
        assert display.post('/api/devices/claim',json={'code':pairing['path'].split('=')[1]}).status_code==200
        a=display.post('/api/timers/start',json={'widget_id':'timer-a','duration_seconds':240,'request_id':'a'}).json()
        b=display.post('/api/timers/start',json={'widget_id':'timer-b','duration_seconds':90,'request_id':'b'}).json()
        assert b['snapshot']['active_count']==2 and a['snapshot']['active_count']==1
        assert {t['widget_id'] for t in b['snapshot']['items']}=={'timer-a','timer-b'}
        assert display.post('/api/timers/'+b['id']+'/stop',json={'widget_id':'timer-a','request_id':'wrong'}).status_code==409
        response=display.post('/api/timers/'+a['id']+'/stop',json={'widget_id':'timer-a','request_id':'stop-a'}).json()
        assert response['snapshot']['active_count']==1 and app.state.timers.get(b['id'])['state']=='running'
        assert display.post('/api/timers/start',json={'widget_id':'nonexistent','duration_seconds':1,'request_id':'bad'}).status_code==409
        assert display.post('/api/timers/start',json={'widget_id':'timer-a','duration_seconds':1,'request_id':'csrf'},headers={'Origin':'https://evil.invalid'}).status_code==403
        assert display.post('/api/widget-protocol/requests',json={'widget':'timer','action':'list','request_id':'no'}).status_code==401
        assert admin.delete('/api/devices/'+pairing['device_id']).status_code==200
        assert display.post('/api/timers/start',json={'widget_id':'timer-a','duration_seconds':1,'request_id':'revoked'}).status_code==401


def test_timer_transport_clock_and_races():
    node=shutil.which('node')
    if not node: pytest.skip('Node.js is unavailable')
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run([node,str(root/'scripts/timer_transport_regression.js')],cwd=root,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
