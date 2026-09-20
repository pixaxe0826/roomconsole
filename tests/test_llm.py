"""LLM notebook tests. Every generated answer is a MOCK, not a real model run."""
import asyncio
from copy import deepcopy
import hashlib
import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import create_app
from app.llm import ChatBackend, LLMConfig, LLMFailure, parse_result, sha, MAX_BODY

SAMPLE={'id':'mock','model':'Qwen3-0.6B-Q5_K_M.gguf','choices':[{'message':{'role':'assistant','content':'[TEST ONLY] 입력을 확인했습니다.'},'finish_reason':'stop'}],
        'usage':{'prompt_tokens':89,'completion_tokens':40,'total_tokens':129},
        'timings':{'prompt_ms':100.0,'predicted_n':40,'predicted_ms':2000.0,'predicted_per_second':20.0}}
class FakeBackend:
    def __init__(self):self.calls=[];self.hold=threading.Event();self.response=deepcopy(SAMPLE);self.fail=False;self.live=0;self.peak=0
    async def generate(self,endpoint,body,timeout):
        self.calls.append((endpoint,body,timeout));self.live+=1;self.peak=max(self.peak,self.live)
        try:
            while self.hold.is_set():await asyncio.sleep(.01)
            if self.fail:raise LLMFailure('시험용 연결 실패','connection_failed')
            await asyncio.sleep(.015)
            return deepcopy(self.response),json.dumps(self.response,ensure_ascii=False)
        finally:self.live-=1
    async def probe(self,cfg):return {'ok':True,'reachable':True,'checked_at':'2026-09-20T00:00:00+00:00','models':[cfg.model],'message':'MOCK /models only'}

@pytest.fixture
def hub(tmp_path):
    b=FakeBackend();a=create_app(tmp_path/'data',weather_enabled=False,llm_backend=b)
    with TestClient(a) as c:
        c.headers.update({'X-Room-Request':'1','Authorization':'Bearer '+a.state.admin_token})
        yield a,c,b

def voice(c,text='내일 오후 세 시에 택배 보내기',key='voice-test-1'):
    r=c.post('/api/voice/text',json={'request_id':key,'source':'unit-test','text':text});assert r.status_code==202,r.text
    return r.json()['id']

def submit(c,vid,text='내일 오후 세 시에 택배 보내기',key='llm-test-1'):
    return c.post('/api/llm/requests',json={'request_id':key,'voice_id':vid,'expected_text_sha256':sha(text)})

def enable(c,**kwargs):
    cfg=c.get('/api/llm/config').json()['config'];cfg.update(enabled=True,**kwargs)
    r=c.put('/api/llm/config',json=cfg);assert r.status_code==200,r.text
    return r

def wait(c,rid,terminal=('succeeded','failed','cancelled','interrupted'),seconds=3):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        d=c.get('/api/llm/requests/'+rid).json()
        if d['status'] in terminal:return d
        time.sleep(.01)
    raise AssertionError(d)

def test_disabled_prepares_no_auto_send_after_enable(hub):
    a,c,b=hub;vid=voice(c);j=submit(c,vid).json()
    assert j['status']=='prepared' and j['response_json'] is None and j['started_at'] is None and not j['dispatch_attempted']
    assert j['request_payload']['messages'][-1]['content']=='내일 오후 세 시에 택배 보내기'
    assert '입력 기준 시각:' in j['request_payload']['messages'][0]['content']
    assert j['request_sha256']==sha(j['request_body'])
    assert j['request_payload']['chat_template_kwargs']=={'enable_thinking':False}
    assert not b.calls
    enable(c);time.sleep(.1);assert not b.calls
    c.post('/api/llm/requests/'+j['id']+'/send');done=wait(c,j['id'])
    assert done['status']=='succeeded' and len(b.calls)==1
    assert b.calls[0][1]==j['request_body']
    assert c.get('/api/state').json()['tasks']==[]
    assert a.state.store.tasks()==[]

def test_live_metrics_exact_snapshot_and_restart(hub):
    a,c,b=hub;enable(c);vid=voice(c);j=submit(c,vid).json();done=wait(c,j['id']);m=done['response_json']['metrics']
    assert m['output_tokens']==40 and m['generation_tps']==20 and m['generation_seconds']==2
    assert m['request_seconds']>0 and m['end_to_end_tps']!=20
    assert done['response_raw']==json.dumps(SAMPLE,ensure_ascii=False)
    assert done['request_body']==b.calls[0][1] and done['dispatch_attempted']
    assert done['wait_seconds']>=0
    c.patch('/api/voice/'+vid,json={'status':'pending_review','text':'원본을 수정했습니다'})
    assert c.get('/api/llm/requests/'+j['id']).json()['source_text']=='내일 오후 세 시에 택배 보내기'
    overview=c.get('/api/admin/overview').json()['voice'][0]
    assert overview['llm_count']==1 and overview['text_sha256']==sha('원본을 수정했습니다')

def test_idempotent_exactly_once_and_hash_conflict(hub):
    _,c,b=hub;enable(c);vid=voice(c);first=submit(c,vid);assert first.status_code==202
    second=submit(c,vid);assert second.json()['duplicate'] and second.json()['id']==first.json()['id']
    assert submit(c,vid,text='different').status_code==409
    assert submit(c,vid,text='different',key='llm-new-key').status_code==409
    wait(c,first.json()['id']);assert len(b.calls)==1

@pytest.mark.parametrize('method,path',[
 ('GET','/api/llm/config'),('PUT','/api/llm/config'),('POST','/api/llm/probe'),('GET','/api/llm/requests'),
 ('POST','/api/llm/requests'),('GET','/api/llm/requests/nope'),('POST','/api/llm/requests/nope/send'),
 ('POST','/api/llm/requests/nope/retry'),('POST','/api/llm/requests/nope/cancel'),('DELETE','/api/llm/requests/nope'),
 ('GET','/api/llm/requests/nope/export')])
def test_admin_only(hub,method,path):
    a,c,_=hub
    p=c.post('/api/devices/pair',json={'name':'iPad'}).json()
    c.headers.pop('Authorization');c.post('/api/devices/claim',json={'code':p['path'].split('=')[1]})
    assert c.request(method,path,json={}).status_code==401
    c.cookies.clear();c.headers['Authorization']='Bearer '+a.state.ingest_token
    assert c.request(method,path,json={}).status_code==401
    c.headers.pop('Authorization');assert c.request(method,path,json={}).status_code==401

@pytest.mark.parametrize('url',[
 'http://example.com:8090/v1','http://localhost:8090/v1','http://192.168.0.14:8090/v1','https://127.0.0.1:8090/v1',
 'http://127.0.0.1:8088/v1','http://127.0.0.1:8080/v1','http://127.0.0.1:8443/v1','http://127.0.0.1:8022/v1',
 'http://a:b@127.0.0.1:8090/v1','file:///tmp/private','http://127.0.0.1:8090/v1?x=1',
 'http://127.0.0.1:8090/v1#secret','http://127.0.0.1:8090/v1/../../admin','http://[::ffff:127.0.0.1]:8090/v1',
 'http://127.0.0.1:80/v1','http://127.0.0.1:8090\n/v1','http://127.0.0.1:notaport/v1'])
def test_restrict_endpoint(url):
    with pytest.raises(ValidationError):LLMConfig(base_url=url)

@pytest.mark.parametrize('url',['http://127.0.0.1:8090/v1','http://[::1]:18090/v1/'])
def test_accept_local_endpoint(url):assert LLMConfig(base_url=url).base_url==url.rstrip('/')

@pytest.mark.parametrize('change',[{'enabled':'false'},{'model':' x '},{'max_tokens':True},{'max_tokens':2048},
 {'temperature':float('inf')},{'timeout_seconds':0},{'system_prompt':'x'*6001},{'api_key':'secret'}])
def test_invalid_config(change):
    with pytest.raises(ValidationError):LLMConfig(**change)

def test_empty_pending_missing_changed_voice(hub):
    a,c,_=hub
    assert submit(c,'nope').status_code==404
    vid=voice(c)
    c.patch('/api/voice/'+vid,json={'status':'reviewed','text':''})
    assert submit(c,vid,text='').status_code==422
    with a.state.store.connect() as db:
        db.execute("UPDATE voice SET text='abc' WHERE id=?",(vid,))
        now='2026-09-20T00:00:00+00:00'
        db.execute("INSERT INTO speech_jobs(id,voice_id,owner,request_id,status,digest,created_at,updated_at) VALUES('stt',?,'admin','stt','running','x',?,?)",(vid,now,now))
    assert submit(c,vid,text='abc').status_code==409

def test_queue_bound_sequential_and_cancel(hub):
    a,c,b=hub;enable(c);vid=voice(c);b.hold.set()
    jobs=[submit(c,vid,key='queue-key-'+str(i)).json() for i in range(4)]
    wait(c,jobs[0]['id'],('running',))
    assert submit(c,vid,key='queue-overflow').status_code==429
    assert c.put('/api/llm/config',json=LLMConfig().model_dump()).status_code==409
    assert c.delete('/api/voice/'+vid).status_code==409
    assert c.delete('/api/llm/requests/'+jobs[0]['id']).status_code==409
    assert c.get('/healthz').status_code==200
    c.post('/api/llm/requests/'+jobs[-1]['id']+'/cancel')
    c.post('/api/llm/requests/'+jobs[0]['id']+'/cancel')
    b.hold.clear()
    assert wait(c,jobs[0]['id'])['status']=='cancelled'
    for j in jobs[1:3]:assert wait(c,j['id'])['status']=='succeeded'
    assert b.peak==1

def test_transcription_already_running_waits(hub):
    a,c,b=hub;enable(c);vid=voice(c);a.state.speech.active_id='fake-stt'
    j=submit(c,vid).json();time.sleep(.1)
    assert c.get('/api/llm/requests/'+j['id']).json()['status']=='queued' and not b.calls
    a.state.speech.active_id=None
    assert wait(c,j['id'])['status']=='succeeded'

def test_failed_retry_new_record_and_original_snapshot(hub):
    a,c,b=hub;enable(c);vid=voice(c);b.fail=True
    j=submit(c,vid).json();assert wait(c,j['id'])['status']=='failed'
    time.sleep(.1);assert len(b.calls)==1
    c.patch('/api/voice/'+vid,json={'status':'pending_review','text':'edited'})
    b.fail=False
    body={'request_id':'retry-key-1'}
    new=c.post('/api/llm/requests/'+j['id']+'/retry',json=body).json()
    assert new['id']!=j['id'] and new['source_text']==j['source_text'] and new['parent_id']==j['id']
    assert c.post('/api/llm/requests/'+j['id']+'/retry',json=body).json()['id']==new['id']
    assert wait(c,new['id'])['status']=='succeeded'
    assert c.get('/api/llm/requests/'+j['id']).json()['status']=='failed'

def test_prepared_changed_config_explicit_retry(hub):
    _,c,b=hub;vid=voice(c);j=submit(c,vid).json()
    enable(c,temperature=.7)
    assert c.post('/api/llm/requests/'+j['id']+'/send').status_code==409
    assert not b.calls
    new=c.post('/api/llm/requests/'+j['id']+'/retry',json={'request_id':'new-settings'}).json()
    assert new['request_payload']['temperature']==.7
    assert wait(c,new['id'])['status']=='succeeded'

def test_history_survives_voice_deletion_then_explicit_delete(hub):
    _,c,b=hub;vid=voice(c);j=submit(c,vid).json()
    assert c.delete('/api/voice/'+vid).status_code==200
    old=c.get('/api/llm/requests/'+j['id']).json();assert old['voice_id'] is None and old['source_text']==j['source_text']
    export=c.get('/api/llm/requests/'+j['id']+'/export');assert export.status_code==200
    assert 'attachment;' in export.headers['content-disposition']
    assert c.delete('/api/llm/requests/'+j['id']).status_code==200
    assert c.get('/api/llm/requests/'+j['id']).status_code==404

def test_pagination_search_filters_and_limit(hub):
    a,c,_=hub;vid=voice(c,'텍스트 100% _ test')
    for i in range(67):assert submit(c,vid,'텍스트 100% _ test','page-key-'+str(i)).status_code==202
    seen=[]
    for offset in (0,30,60):
        out=c.get('/api/llm/requests',params={'offset':offset,'limit':30}).json();assert out['total']==67
        seen.extend(j['id'] for j in out['items'])
    assert len(set(seen))==67
    assert c.get('/api/llm/requests?query=100%25').json()['total']==67
    assert c.get('/api/llm/requests?query=absent').json()['total']==0
    assert c.get('/api/llm/requests?status=succeeded').json()['total']==0
    assert c.get('/api/llm/requests?limit=101').status_code==422
    assert c.get('/api/llm/requests?status=unknown').status_code==422

def test_disabled_does_not_install_model_or_touch_speech(hub):
    a,c,b=hub;p=a.state.store.path.parent/'speech-config.json'
    content=b'{"enabled":false,"threads":6,"model":"/test/ggml-base.bin"}\n';p.write_bytes(content)
    vid=voice(c);submit(c,vid)
    assert p.read_bytes()==content and not b.calls
    with a.state.store.connect() as db:
        audit=' '.join(str(tuple(row)) for row in db.execute('SELECT * FROM audit'))
    assert '내일 오후' not in audit
    c.headers['Origin']='http://evil.test';assert c.post('/api/llm/probe').status_code==403

def test_restart_marks_pending_not_resubmitted(tmp_path):
    b=FakeBackend();a=create_app(tmp_path,weather_enabled=False,llm_backend=b)
    with TestClient(a) as c:
        c.headers.update({'Authorization':'Bearer '+a.state.admin_token,'X-Room-Request':'1'})
        enable(c);a.state.speech.active_id='held'
        vid=voice(c);j=submit(c,vid).json();assert j['status']=='queued'
    a2=create_app(tmp_path,weather_enabled=False,llm_backend=b)
    with TestClient(a2) as c2:
        c2.headers['Authorization']='Bearer '+a2.state.admin_token
        out=c2.get('/api/llm/requests/'+j['id']).json()
        assert out['status']=='interrupted' and out['source_text']==j['source_text']
    assert not b.calls

@pytest.mark.parametrize('usage,timings,expect_tokens,expect_tps',[
 ({}, {},None,None),({'completion_tokens':40},{},40,None),({}, {'predicted_n':12,'predicted_ms':3000},12,4),
 ({'completion_tokens':0},{'predicted_n':0,'predicted_ms':30},0,0),
 ({'completion_tokens':-1},{},None,None),({'completion_tokens':True},{},None,None),
 ({'completion_tokens':8},{'predicted_n':8,'predicted_ms':0},8,None),
 ({'completion_tokens':4},{'predicted_n':4,'predicted_ms':2000,'predicted_per_second':2.4},4,2.4),
 ({'completion_tokens':4},{'predicted_ms':2000,'predicted_per_second':2.4},4,None)])
def test_metrics_no_fake_token_counts(usage,timings,expect_tokens,expect_tps):
    obj=deepcopy(SAMPLE);obj['usage']=usage;obj['timings']=timings
    r=parse_result(obj,5);m=r['metrics']
    assert m['output_tokens']==expect_tokens and m['generation_tps']==expect_tps
    assert m['end_to_end_tps']==(expect_tokens/5 if expect_tokens is not None else None)

def test_raw_content_reasoning_and_tools_never_executed(hub):
    a,c,b=hub;enable(c);b.response['choices'][0]={'message':{'content':'<script>alert(1)</script>','reasoning_content':'backend text','tool_calls':[{'type':'function','function':{'name':'delete_all','arguments':'{}'}}]},'finish_reason':'length'}
    vid=voice(c);j=submit(c,vid).json();result=wait(c,j['id'])['response_json']
    assert result['output']=='<script>alert(1)</script>' and result['tool_calls']
    assert len(result['warnings'])>=3
    assert a.state.store.tasks()==[] and c.get('/api/admin/overview').json()['voice']

@pytest.mark.parametrize('code',[301,302,307,400,401,403,404,429,500,503])
def test_backend_http_errors_no_echo_or_redirect(code):
    calls=[]
    async def handler(req):
        calls.append(req);return httpx.Response(code,json={'error':'SECRET_DO_NOT_ECHO'},headers={'Location':'http://external.test'})
    backend=ChatBackend(httpx.MockTransport(handler))
    async def run():
        with pytest.raises(LLMFailure) as exc:await backend.generate('http://127.0.0.1:8090/v1/chat/completions','{}',10)
        assert 'SECRET' not in str(exc.value) and exc.value.http_status==code
    asyncio.run(run());assert len(calls)==1

def test_http_exact_body_no_proxy_env_and_real_usage():
    captured=[]
    async def handler(req):captured.append(req);return httpx.Response(200,json=SAMPLE)
    backend=ChatBackend(httpx.MockTransport(handler));text='{"model":"x","messages":[{"role":"user","content":"한글\n테스트"}]}'
    asyncio.run(backend.generate('http://127.0.0.1:8090/v1/chat/completions',text,10))
    assert captured[0].content==text.encode('utf-8')

@pytest.mark.parametrize('content',[b'not json',b'{"x":NaN}',b'x'*(MAX_BODY+1)])
def test_bad_bounded_response(content):
    async def handler(req):return httpx.Response(200,content=content)
    async def run():
        with pytest.raises(LLMFailure):await ChatBackend(httpx.MockTransport(handler)).generate('http://127.0.0.1:8090/v1/chat/completions','{}',10)
    asyncio.run(run())

def test_voice_transcription_to_llm_full_flow(tmp_path):
    """Real upload/voice DB/LLM queue, fake ASR and fake LLM (no accuracy claim)."""
    import sys, io, wave, struct, math
    from app.speech import SpeechConfig
    class ASR:
        async def transcribe(self,source,cfg,cancel,temp_root):return '이번 주 토요일에 화분 물 주기',1.0
    model=tmp_path/'model';model.write_bytes(b'x'*2048)
    cfg=SpeechConfig(enabled=True,binary=sys.executable,model=str(model),ffmpeg=sys.executable,threads=6)
    b=FakeBackend();a=create_app(tmp_path/'data',weather_enabled=False,speech_config=cfg,speech_runner=ASR(),llm_backend=b)
    with TestClient(a) as c:
        c.headers.update({'Authorization':'Bearer '+a.state.admin_token,'X-Room-Request':'1'})
        enable(c)
        out=io.BytesIO()
        with wave.open(out,'wb') as w:
            w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(b''.join(struct.pack('<h',int(2000*math.sin(i*.1))) for i in range(16000)))
        j=c.post('/api/speech/jobs',data={'request_id':'full-flow'},files={'file':('test.wav',out.getvalue(),'audio/wav')}).json()
        end=time.monotonic()+2
        while time.monotonic()<end:
            stt=c.get('/api/speech/jobs/'+j['id']).json()
            if stt['status']=='succeeded':break
            time.sleep(.01)
        assert stt['status']=='succeeded'
        v=c.get('/api/admin/overview').json()['voice'][0]
        end=time.monotonic()+2;rid=None
        while time.monotonic()<end:
            with a.state.store.connect() as db:
                row=db.execute('SELECT id FROM llm_requests WHERE source_voice_id=? ORDER BY created_at DESC LIMIT 1',(v['id'],)).fetchone()
            if row:rid=row['id'];break
            time.sleep(.01)
        assert rid is not None and wait(c,rid)['status']=='succeeded'
        assert len(b.calls)==1
        assert json.loads(b.calls[0][1])['messages'][-1]['content']==stt['text']
        status=c.get('/api/speech/status').json();assert status['threads']==6 and status['auto_submit_llm'] is True
        assert a.state.store.tasks()==[]

def test_llm_invalidation_over_real_app_websocket(hub):
    a,c,b=hub
    # Use an authenticated HTTP cookie in TestClient's ASGI websocket channel.
    assert c.post('/api/auth/login',json={'token':a.state.admin_token}).status_code==200
    vid=voice(c)
    with c.websocket_connect('/ws/manager') as ws:
        assert ws.receive_json()['type']=='hello'
        j=submit(c,vid).json()
        assert j['status']=='prepared'
        assert ws.receive_json()['type']=='invalidate'
        ws.send_json({'type':'ping'});assert ws.receive_json()['type']=='pong'
