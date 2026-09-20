"""Read-only LLM widget tests; synthetic text/mock LLM, never real model output."""
import asyncio
from copy import deepcopy
import json
import time
import threading

import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.llm import sha
from app.llm_display import index, entry

class Backend:
    def __init__(self):self.calls=[];self.hold=threading.Event()
    async def generate(self,endpoint,body,timeout):
        self.calls.append(body)
        while self.hold.is_set():await asyncio.sleep(.01)
        result={'model':'mock','choices':[{'message':{'content':'[모의] 확인했습니다.','reasoning_content':'PRIVATE_REASONING'},'finish_reason':'stop'}]}
        return result,json.dumps(result)

@pytest.fixture
def hub(tmp_path):
    b=Backend();app=create_app(tmp_path/'data',weather_enabled=False,llm_backend=b)
    with TestClient(app) as c:
        c.headers.update({'Authorization':'Bearer '+app.state.admin_token,'X-Room-Request':'1'})
        pair=c.post('/api/devices/pair',json={'name':'Test iPad'}).json()
        # Without entering its lifespan again: client uses the same already-running app.
        d=TestClient(app);d.headers['X-Room-Request']='1'
        assert d.post('/api/devices/claim',json={'code':pair['path'].split('=')[1]}).status_code==200
        yield app,c,d,b,pair
        b.hold.clear();d.close()

def enable_widget(c):
    layout=c.get('/api/state').json()['layout']
    layout['widgets'][0].update(type='llm-response',title='LLM 응답')
    assert c.put('/api/layout',json=layout).status_code==200

def make_record(c,n=1):
    text=f'테스트 입력 {n} <img src=x onerror=alert(1)>'
    v=c.post('/api/voice/text',json={'request_id':f'voice-display-{n}','source':'test','text':text}).json()['id']
    r=c.post('/api/llm/requests',json={'request_id':f'llm-display-{n}','voice_id':v,'expected_text_sha256':sha(text)})
    assert r.status_code==202,r.text
    return r.json()

def fixture_sent(app,c,n=1,status='succeeded',response=None):
    r=make_record(c,n)
    if response is None:response={'output':f'테스트 응답 {n}','reasoning':'SECRET','tool_calls':None,'refusal':None,'finish_reason':'stop','metrics':{'generation_tps':99}}
    at=f'2026-09-20T01:{n%60:02d}:00+00:00'
    with app.state.store.connect() as db:
        db.execute('UPDATE llm_requests SET dispatch_attempted=1, started_at=?,status=?,response_json=?,response_raw=?,updated_at=? WHERE id=?',(at,status,json.dumps(response),'{"secret":"PRIVATE_RAW"}',at,r['id']))
    return r

def test_default_opt_out_and_no_model_invocation(hub):
    app,c,d,b,_=hub;r=fixture_sent(app,c)
    s=d.get('/api/state').json()
    assert s['llm_display']=={'enabled':False,'items':[],'total':0}
    assert d.get('/api/display/llm/'+r['id']).status_code==403
    assert b.calls==[]

def test_default_manifest_and_preserved_layout(hub):
    app,c,d,b,_=hub;s=d.get('/api/state').json();w=next(w for w in s['widgets'] if w['id']=='llm-response')
    assert w['defaultSize']=={'w':4,'h':2}
    assert len(s['layout']['widgets'])==4
    assert s['capabilities']['llm_response_widget'] is True

def test_prepared_queued_and_cancelled_before_dispatch_are_hidden(hub):
    app,c,d,b,_=hub;enable_widget(c)
    for n,status in enumerate(['prepared','queued','cancelled','interrupted','running'],1):
        r=make_record(c,n)
        with app.state.store.connect() as db:db.execute('UPDATE llm_requests SET status=?, started_at=? WHERE id=?',(status,'2026-09-20T01:00:00+00:00',r['id']))
        assert d.get('/api/display/llm/'+r['id']).status_code==404
    assert d.get('/api/state').json()['llm_display']['items']==[]

@pytest.mark.parametrize('status',['running','succeeded','failed','cancelled','interrupted'])
def test_attempted_states_visible(hub,status):
    app,c,d,_,_=hub;enable_widget(c);r=fixture_sent(app,c,status=status)
    feed=d.get('/api/state').json()['llm_display'];assert feed['total']==1
    out=d.get('/api/display/llm/'+r['id']);assert out.status_code==200
    assert out.json()['status']==status


def test_index_only_metadata_and_detail_field_allowlist(hub):
    app,c,d,_,_=hub;enable_widget(c);r=fixture_sent(app,c)
    feed=d.get('/api/state').json()['llm_display']
    assert set(feed['items'][0])=={'id','status','sent_at','updated_at'}
    out=d.get('/api/display/llm/'+r['id']).json()
    assert set(out)=={'id','status','sent_at','updated_at','finished_at','model','input','output','refusal','output_kind','truncated'}
    assert out['input']==r['source_text'] and out['output']=='테스트 응답 1'
    body=json.dumps(out);assert 'SECRET' not in body and 'PRIVATE_RAW' not in body
    for key in ['request_body','system_prompt','request_payload','config_json','response_json','source_meta','endpoint','request_key','voice_id','metrics','reasoning','tool_calls']:
        assert key not in out

@pytest.mark.parametrize('mode',['anonymous','ingest','expired','revoked'])
def test_unauthorized_viewers_blocked(hub,mode):
    app,c,d,_,pair=hub;enable_widget(c);r=fixture_sent(app,c)
    if mode=='anonymous':d.cookies.clear()
    elif mode=='ingest':d.cookies.clear();d.headers['Authorization']='Bearer '+app.state.ingest_token
    elif mode=='expired':
        with app.state.store.connect() as db:db.execute("UPDATE sessions SET expires_at=? WHERE role='display'",(time.time()-1,))
    else:c.delete('/api/devices/'+pair['device_id'])
    assert d.get('/api/display/llm/'+r['id']).status_code==401
    assert d.get('/api/state').status_code==401

@pytest.mark.parametrize('path,method',[
    ('/api/llm/config','get'),('/api/llm/config','put'),('/api/llm/requests','get'),
    ('/api/llm/requests','post'),('/api/llm/requests/{id}','get'),('/api/llm/requests/{id}/export','get'),
    ('/api/llm/requests/{id}/send','post'),('/api/llm/requests/{id}/retry','post'),
    ('/api/llm/requests/{id}/cancel','post'),('/api/llm/requests/{id}','delete')])
def test_admin_api_boundary_unchanged(hub,path,method):
    app,c,d,_,_=hub;enable_widget(c);r=fixture_sent(app,c)
    assert d.request(method,path.replace('{id}',r['id']),json={}).status_code==401

@pytest.mark.parametrize('method',['post','patch','put','delete'])
def test_read_only_route(hub,method):
    app,c,d,b,_=hub;enable_widget(c);r=fixture_sent(app,c)
    assert d.request(method,'/api/display/llm/'+r['id'],json={}).status_code==405
    assert not b.calls


def test_removing_widget_revokes_projection_immediately(hub):
    app,c,d,_,_=hub;enable_widget(c);r=fixture_sent(app,c)
    assert d.get('/api/display/llm/'+r['id']).status_code==200
    l=c.get('/api/state').json()['layout'];l['widgets'][0]['type']='clock'
    assert c.put('/api/layout',json=l).status_code==200
    assert d.get('/api/display/llm/'+r['id']).status_code==403
    assert d.get('/api/state').json()['llm_display']['total']==0


def test_no_implicit_generation_or_writes_on_reads(hub):
    app,c,d,b,_=hub;enable_widget(c);r=fixture_sent(app,c);before=c.get('/api/llm/requests/'+r['id']).json();rev=c.get('/api/state').json()['revision']
    for _ in range(3):d.get('/api/display/llm/'+r['id']);d.get('/api/state')
    assert not b.calls and c.get('/api/state').json()['revision']==rev
    assert c.get('/api/llm/requests/'+r['id']).json()==before
    assert c.get('/api/state').json()['tasks']==[]


def test_original_voice_edit_or_delete_does_not_change_snapshot(hub):
    app,c,d,_,_=hub;enable_widget(c);r=fixture_sent(app,c)
    c.patch('/api/voice/'+r['voice_id'],json={'text':'변경','status':'pending_review'})
    c.delete('/api/voice/'+r['voice_id'])
    assert d.get('/api/display/llm/'+r['id']).json()['input']==r['source_text']
    assert c.delete('/api/llm/requests/'+r['id']).status_code==200
    assert d.get('/api/display/llm/'+r['id']).status_code==404
    assert d.get('/api/state').json()['llm_display']['total']==0

@pytest.mark.parametrize('response,kind',[
    ({'output':None,'refusal':'[모의] 답변 거절'},'refusal'),
    ({'output':None,'reasoning':'PRIVATE_REASONING'},'no_final_output'),
    ({'output':None,'tool_calls':[{'function':{'name':'never_execute'}}]},'tool_only'),
    ({'output':'','refusal':None},'empty'),
    ({'output':{'bad':'NOT A STRING'},'refusal':{'private':'NO'}},'empty'),
    ({'output':'부분 출력','finish_reason':'length'},'text')])
def test_response_shapes(hub,response,kind):
    app,c,d,_,_=hub;enable_widget(c);r=fixture_sent(app,c,response=response)
    out=d.get('/api/display/llm/'+r['id']).json();assert out['output_kind']==kind
    assert out['truncated']==(response.get('finish_reason')=='length')
    assert 'PRIVATE_REASONING' not in json.dumps(out) and 'never_execute' not in json.dumps(out)


def test_long_output_is_not_silently_truncated(hub):
    app,c,d,_,_=hub;enable_widget(c);text='긴 답변\n'*15000;r=fixture_sent(app,c,response={'output':text})
    assert d.get('/api/display/llm/'+r['id']).json()['output']==text


def test_1000_index_entries_and_deterministic_order(hub):
    app,c,d,_,_=hub;enable_widget(c);r=fixture_sent(app,c)
    with app.state.store.connect() as db:
        cols=[x['name'] for x in db.execute('PRAGMA table_info(llm_requests)')]
        original=dict(db.execute('SELECT * FROM llm_requests WHERE id=?',(r['id'],)).fetchone())
        db.execute('DELETE FROM llm_requests')
        for n in range(1000):
            data=dict(original,id=f'id-{n:04d}',request_key=f'key-{n:04d}',started_at='2026-09-20T00:00:00+00:00')
            db.execute('INSERT INTO llm_requests('+','.join(cols)+') VALUES('+','.join('?' for _ in cols)+')',[data[k] for k in cols])
    feed=d.get('/api/state').json()['llm_display'];assert feed['total']==1000
    assert feed['items'][0]['id']=='id-0000' and feed['items'][-1]['id']=='id-0999'
    assert len(json.dumps(feed))<220000


def test_live_mock_transition_and_cache_headers(hub):
    app,c,d,b,_=hub;enable_widget(c);b.hold.set()
    cfg=c.get('/api/llm/config').json()['config'];cfg['enabled']=True;c.put('/api/llm/config',json=cfg)
    r=make_record(c)
    end=time.monotonic()+3
    while time.monotonic()<end and not d.get('/api/state').json()['llm_display']['items']:time.sleep(.02)
    assert d.get('/api/display/llm/'+r['id']).json()['status']=='running'
    b.hold.clear()
    while time.monotonic()<end and d.get('/api/display/llm/'+r['id']).json()['status']!='succeeded':time.sleep(.02)
    out=d.get('/api/display/llm/'+r['id']);assert out.json()['output']=='[모의] 확인했습니다.'
    assert out.headers['cache-control']=='no-store'
    assert len(b.calls)==1


def test_ws_invalidation_for_new_dispatch(hub):
    app,c,d,b,_=hub;enable_widget(c);r=make_record(c)
    cfg=c.get('/api/llm/config').json()['config'];cfg['enabled']=True;c.put('/api/llm/config',json=cfg)
    b.hold.set()
    with d.websocket_connect('/ws/display') as ws:
        assert ws.receive_json()['type']=='hello'
        assert c.post('/api/llm/requests/'+r['id']+'/send').status_code==200
        # queued, running and dispatched publish invalidations. At least the
        # dispatch notification must expose the in-flight request without waiting
        # for its completion or the 30-second fallback poll.
        saw=False
        for _ in range(3):
            assert ws.receive_json()['type']=='invalidate'
            if d.get('/api/state').json()['llm_display']['items']:
                saw=True;break
        assert saw
        b.hold.clear()
