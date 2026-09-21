"""Actual HTTP/SQLite/Adapters with a counted fake model; not V35/Qwen measurements."""
import asyncio
from contextlib import closing
from dataclasses import replace
from datetime import date, timedelta
import json
import sqlite3
import time
from types import MappingProxyType

import pytest
from fastapi.testclient import TestClient

from app.assistant import digest, dump
from app.main import create_app
from app.widget_bridge import WidgetProposal, normalize_missing, proposal_schema
from app.widget_protocol import WidgetRequest
from test_assistant import hub, request, wait, add, enable, confirm, Model
from test_fast_reads import place_widgets, note


def tomorrow(c):
    return (date.fromisoformat(c.get('/api/clock').json()['today']) + timedelta(days=1)).isoformat()


def proposal(b, widget, action, args=None, target=None):
    b.output = json.dumps({'widget': widget, 'action': action, 'target': target, 'args': args or {}}, ensure_ascii=False)


def fallback(c, b, text, widget, action, args=None, target=None, mode='auto'):
    enable(c)
    proposal(b, widget, action, args, target)
    d = wait(c, request(c, text, mode))
    assert d['assistant']['routing']['route'] == 'LLM_FALLBACK', d
    assert d['assistant']['routing']['llm_called'] is True
    return d


def trace(d):
    return d['assistant']['widget_trace']


def spy_adapter(app, name, monkeypatch):
    a = app.state.widget_protocol.get(name)
    original = a._execute
    calls = []
    async def execute(req, args, authority):
        calls.append((req.model_dump(mode='json'), authority))
        return await original(req, args, authority)
    monkeypatch.setattr(a, '_execute', execute)
    return calls


@pytest.mark.parametrize('text,widget,action', [
    ('오늘 할 일 확인해줘', 'todo', 'list'), ('내일 할 일 알려줘', 'todo', 'list'),
    ('현재 메모 읽어줘', 'memo', 'read'), ('방금 메모 읽어줘', 'memo', 'read'),
    ('오늘 오전 일정 알려줘', 'calendar', 'list'), ('오늘 오후 일정 확인해줘', 'calendar', 'list'),
])
def test_cases1_2_fast_paths_use_actual_adapter_without_any_model(hub, monkeypatch, text, widget, action):
    app,c,b=hub; place_widgets(app); note(c, '실제 저장 본문'); add(c, '실제 할 일', time_='14:00')
    calls=spy_adapter(app,widget,monkeypatch)
    d=wait(c,request(c,text))
    assert d['status']=='succeeded',d
    assert not b.calls and not d['dispatch_attempted']
    assert d['assistant']['routing']['route']=='FAST_PATH'
    assert not d['assistant']['routing']['llm_called']
    assert len(calls)==1 and calls[0][0]['action']==action
    assert trace(d)['adapter']==widget.title()+'Adapter'
    assert trace(d)['widget_response']['status']=='success'
    assert trace(d)['latency_ms']['llm_ms']==0
    assert trace(d)['source_of_truth']


def test_case3_proposal_confirm_actual_adapter_and_receipt(hub,monkeypatch):
    app,c,b=hub;calls=spy_adapter(app,'todo',monkeypatch)
    d=fallback(c,b,'내일 하늘에 우유 사기 추가해','todo','add',{'date':tomorrow(c),'title':'우유 사기','time':None})
    assert d['status']=='awaiting_confirmation', d
    assert not app.state.store.tasks() and not calls
    assert trace(d)['policy_result']=='needs_confirmation'
    assert '아직 변경하지 않았습니다' in d['response_json']['output']
    assert confirm(c,d,'0'*64).status_code==409
    result=confirm(c,d)
    assert result.status_code==200,result.text
    assert result.json()['status']=='succeeded'
    assert len(calls)==1 and calls[0][1].confirmed_digest
    assert [t['title'] for t in app.state.store.tasks()]==['우유 사기']
    assert confirm(c,d).json()['execution']['duplicate'] is True
    assert len(calls)==1
    assert trace(result.json())['widget_response']['status']=='success'


@pytest.mark.parametrize('value',[None,'unknown','NONE','null','n/a',''])
def test_cases4_8_alarm_uncertain_time_clarifies_without_control(hub,monkeypatch,value):
    app,c,b=hub;calls=spy_adapter(app,'alarm',monkeypatch)
    d=fallback(c,b,'오늘 오전 두시 좀 아람 맞춰줘','alarm','set',{'time':value})
    assert d['status']=='needs_clarification',d
    assert trace(d)['widget_response']['error']['field']=='args.time'
    assert trace(d)['widget_request']['args']['time'] is None
    assert '다시 말씀' in d['response_json']['output']
    assert not calls and app.state.life.alarms()==[]


def test_alarm_fallback_requires_confirmation_never_claims_execution(hub,monkeypatch):
    app,c,b=hub;calls=spy_adapter(app,'alarm',monkeypatch)
    d=fallback(c,b,'내일 오전 12시 40분 아람 맞춰줘','alarm','set',
               {'date':tomorrow(c),'time':'00:40','label':'아람'})
    assert d['status']=='awaiting_confirmation',d
    assert trace(d)['widget_response']['status']=='needs_confirmation'
    assert not calls and not app.state.life.alarms()
    assert '아직 변경하지 않았습니다' in d['response_json']['output']


def test_alarm_cancel_without_target_clarifies_without_model(hub):
    app,c,b=hub;enable(c)
    d=wait(c,request(c,'알람 취소해줘'))
    assert d['status']=='needs_clarification' and not b.calls
    assert not app.state.life.alarms()


def test_case5_unavailable_action_rejected_before_adapter(hub,monkeypatch):
    app,c,b=hub;calls=spy_adapter(app,'todo',monkeypatch)
    d=fallback(c,b,'내일 하늘에 우유 사기 추가해','todo','fly',{'title':'우유 사기'})
    assert trace(d)['widget_response']['error']['code']=='UNSUPPORTED_ACTION'
    assert d['status']=='needs_clarification' and not calls


@pytest.mark.parametrize('raw',[
    '현재 메모에는 비밀번호 1234가 적혀 있습니다.',
    '{"widget":"memo","action":"read","target":null,"args":{},"answer":"가짜 메모"}',
    '```json\n{"widget":"memo","action":"read","target":null,"args":{}}\n```',
    '{"widget":"memo","action":"read","action":"write","target":null,"args":{}}',
    '{"widget":"memo","action":"read","target":null,"args":{"x":NaN}}',
])
def test_case6_rejects_natural_language_and_invalid_envelope(hub,monkeypatch,raw):
    app,c,b=hub;enable(c);calls=spy_adapter(app,'memo',monkeypatch)
    b.output=raw
    d=wait(c,request(c,'메모 내용 좀 읽어 볼래'))
    assert d['status']=='needs_clarification',d
    assert not calls and '가짜 메모' not in d['response_json']['output']
    assert trace(d)['widget_response']['error']['code']=='INVALID_PROPOSAL'
    assert raw in d['assistant']['calls'][0]['result']['output']


def test_case7_adapter_failure_cannot_be_empty_or_model_success(hub,monkeypatch):
    app,c,b=hub
    def fail(*a,**k):raise sqlite3.OperationalError('PRIVATE DATABASE LOCATION')
    monkeypatch.setattr(app.state.store,'query_tasks',fail)
    d=fallback(c,b,'오늘 일정 목록 좀 확인해 줄래','calendar','list',{})
    assert d['status']=='failed',d
    assert trace(d)['widget_response']['status']=='error'
    assert 'PRIVATE' not in d['response_json']['output']
    assert '없습니다' not in d['response_json']['output']
    assert len(b.calls)==1


@pytest.mark.parametrize('value',[None,'unknown','N/A',''])
def test_missing_optional_time_null_not_crash(hub,value):
    app,c,b=hub
    d=fallback(c,b,'내일 하늘에 우유 사기 추가해','todo','add',{'date':tomorrow(c),'title':'우유 사기','time':value})
    assert d['status']=='awaiting_confirmation',d
    assert trace(d)['widget_request']['args']['time'] is None
    assert 'ValidationError' not in d['response_json']['output']


@pytest.mark.parametrize('args',[
    {'date':None,'title':'우유 사기'}, {'title':'우유 사기'}, {'time':None},
])
def test_required_missing_slot_clarifies(hub,args):
    app,c,b=hub
    d=fallback(c,b,'내일 하늘에 우유 사기 추가해','todo','add',args)
    assert d['status']=='needs_clarification' and not app.state.store.tasks()
    assert trace(d)['widget_response']['error']['field'] in {'args.date','args.title'}


@pytest.mark.parametrize('time_value',['12:40','02:40','25:99',False,3,'unknown'])
def test_numeric_time_never_repaired_or_fabricated(hub,time_value):
    app,c,b=hub
    d=fallback(c,b,'내일 오전 12시 40분 하늘에 우유 사기 추가해','todo','add',
               {'date':tomorrow(c),'title':'우유 사기','time':time_value})
    assert d['status']=='needs_clarification',d
    assert trace(d)['widget_response']['error']['field']=='args.time'
    assert not app.state.store.tasks()


def test_explicit_midnight_correct_conversion_still_needs_confirmation(hub):
    app,c,b=hub
    d=fallback(c,b,'내일 오전 12시 40분 하늘에 우유 사기 추가해','todo','add',
               {'date':tomorrow(c),'title':'우유 사기','time':'00:40'})
    assert d['status']=='awaiting_confirmation',d
    assert trace(d)['widget_request']['args']['time']=='00:40'
    assert not app.state.store.tasks()


def test_model_cannot_recover_unheard_twelve_from_two(hub):
    app,c,b=hub
    d=fallback(c,b,'오늘 오전 2시 40분 아람 맞춰줘','alarm','set',
               {'date':c.get('/api/clock').json()['today'],'label':'아람','time':'00:40'})
    assert trace(d)['widget_response']['error']['code']=='SOURCE_NOT_GROUNDED'
    assert trace(d)['widget_response']['error']['field']=='args.time'


@pytest.mark.parametrize('bad',[
    {'title':'달걀 사기'}, {'date':'2099-12-31'}, {'version':1}, {'shared':True},
    {'category':'work'}, {'time':'13:00'}, {'notes':'source data guessed'},
])
def test_model_cannot_invent_or_widen_fields(hub,bad):
    app,c,b=hub
    args={'date':tomorrow(c),'title':'우유 사기','time':None}|bad
    d=fallback(c,b,'내일 하늘에 우유 사기 추가해','todo','add',args)
    assert d['status']=='needs_clarification' and not app.state.store.tasks()


def test_domain_scoped_dynamic_schema_and_constraints(hub):
    app,c,b=hub
    d=fallback(c,b,'내일 하늘에 우유 사기 추가해','todo','add',{'date':tomorrow(c),'title':'우유 사기'})
    body=b.calls[0]; schema=body['response_format']['json_schema']['schema']
    assert body['max_tokens']==64 and body['temperature']==0
    assert body['chat_template_kwargs']=={'enable_thinking':False}
    assert trace(d)['available_capabilities']==['todo.add']
    props=schema['anyOf'][0]['properties']
    assert props['widget']=={'const':'todo'} and props['action']=={'const':'add'}
    assert 'title' in props['args']['properties'] # do not strip a field named title as an annotation
    assert 'version' not in props['args']['properties']
    assert 'memo' not in body['messages'][0]['content']
    assert 'response_format' in body and 'schema' in body['response_format']['json_schema']
    assert WidgetProposal.model_validate({'widget':'todo','action':'add','target':None,'args':{}})
    assert not app.state.store.tasks()


@pytest.mark.parametrize('finish',['length',None,'tool_calls'])
def test_truncated_or_nonfinal_json_never_dispatches(hub,monkeypatch,finish):
    app,c,b=hub;b.finish=finish;calls=spy_adapter(app,'memo',monkeypatch)
    d=fallback(c,b,'메모 내용 좀 읽어 볼래','memo','read')
    assert trace(d)['widget_response']['error']['code']=='INCOMPLETE_PROPOSAL' and not calls


def test_grounded_fallback_memo_reads_actual_state_and_respects_display_privacy(hub):
    app,c,b=hub;place_widgets(app);nid=note(c,'PRIVATE-BODY-ONLY',shared=False)
    d=fallback(c,b,'방금 작성한 메모 내용 좀 읽어 볼래','memo','read',target={'type':'reference','value':'last'})
    assert d['status']=='succeeded',d
    assert d['response_json']['output'].endswith('PRIVATE-BODY-ONLY')
    assert 'PRIVATE-BODY-ONLY' not in json.dumps(b.calls)
    assert 'PRIVATE-BODY-ONLY' not in c.get('/api/display/llm/'+d['id']).text
    assert trace(d)['widget_response']['data']['id']==nid


def test_fallback_todo_period_before_count_and_deterministic_formatter(hub):
    app,c,b=hub;add(c,'오전',time_='11:59');add(c,'오후 실제',time_='12:00')
    d=fallback(c,b,'오늘 오후 일정 목록 좀 확인해 줄래','calendar','list')
    assert d['status']=='succeeded' and '오후 실제' in d['response_json']['output']
    assert [x['title'] for x in trace(d)['widget_response']['data']['items']]==['오후 실제']
    assert len(b.calls)==1


def test_fallback_memo_write_requires_exact_body_confirmation_and_private_preview(hub):
    app,c,b=hub;place_widgets(app)
    d=fallback(c,b,'새 메모 제목 장보기 본문 우유 사기 작성해','memo','write',{'title':'장보기','body':'우유 사기'})
    assert d['status']=='awaiting_confirmation',d
    assert app.state.life.notes()==[]
    assert '우유 사기' not in c.get('/api/display/llm/'+d['id']).text
    r=confirm(c,d)
    assert r.status_code==200,r.text
    notes=app.state.life.notes()
    assert len(notes)==1 and notes[0]['body']=='우유 사기' and not notes[0]['shared']
    assert '우유 사기' not in c.get('/api/display/llm/'+d['id']).text


def test_version_bound_memo_append_conflict_and_no_duplicate(hub):
    app,c,b=hub;place_widgets(app);nid=note(c,'기존')
    d=fallback(c,b,'현재 메모에 우유 사기 덧붙여줘','memo','append',{'text':'우유 사기'})
    assert d['status']=='awaiting_confirmation',d
    assert trace(d)['widget_request']['args']['version']==1
    c.put('/api/life/notes/'+nid,json={'version':1,'title':'외부수정','body':'다른 본문','shared':True,'pinned':False})
    r=confirm(c,d)
    assert r.status_code==200,r.text
    assert trace(r.json())['widget_response']['status']=='conflict'
    assert app.state.life.notes()[0]['body']=='다른 본문'
    assert confirm(c,d).status_code==409


def test_retry_family_retains_durable_effect_and_no_second_write(hub):
    app,c,b=hub
    d=fallback(c,b,'내일 하늘에 우유 사기 추가해','todo','add',{'date':tomorrow(c),'title':'우유 사기'})
    assert confirm(c,d).status_code==200
    r=c.post('/api/llm/requests/'+d['id']+'/retry',json={'request_id':'bridge-retry-01','mode':'auto'})
    assert r.status_code==202,r.text
    new=wait(c,r.json())
    assert new['status']=='succeeded' and new['assistant']['validation']=='already_executed',new
    assert len(app.state.store.tasks())==1


def test_uncertain_reserved_effect_is_never_automatically_retried(hub):
    app,c,b=hub
    d=fallback(c,b,'내일 하늘에 우유 사기 추가해','todo','add',{'date':tomorrow(c),'title':'우유 사기'})
    with closing(app.state.store.connect()) as db,db:
        db.execute('INSERT INTO assistant_effects VALUES(?,?,?,?)',(d['assistant']['action_key'],d['id'],dump({'protocol_state':'reserved','message':'uncertain'}),'2026-09-21'))
    assert confirm(c,d).status_code==409
    assert not app.state.store.tasks()


def test_new_protocol_api_still_cannot_self_confirm(hub):
    app,c,b=hub
    r=c.post('/api/widget-protocol/requests',json={'request_id':'still-no-http-write','widget':'todo','action':'add',
        'args':{'title':'합성','date':tomorrow(c)},'context':{'user_confirmed':True}})
    assert r.json()['status']=='needs_confirmation' and not app.state.store.tasks()


def test_auto_general_question_is_clarify_but_explicit_chat_preserved(hub):
    app,c,b=hub;enable(c)
    d=wait(c,request(c,'저녁 메뉴 추천해 줘'))
    assert d['status']=='needs_clarification' and not b.calls
    d=wait(c,request(c,'저녁 메뉴 추천해 줘',mode='chat'))
    assert d['status']=='succeeded' and len(b.calls)==1


@pytest.mark.parametrize('mode',['auto','chat','legacy'])
def test_personal_request_cannot_bypass_schema_by_chat_mode(hub,monkeypatch,mode):
    app,c,b=hub;enable(c);calls=spy_adapter(app,'memo',monkeypatch)
    b.output='현재 메모에는 가짜 값이 있습니다.'
    d=wait(c,request(c,'메모 내용 좀 읽어 볼래',mode=mode))
    assert d['status']=='needs_clarification' and not calls
    assert '가짜 값' not in d['response_json']['output']


def test_false_authority_or_fabricated_id_are_rejected(hub,monkeypatch):
    app,c,b=hub;calls=spy_adapter(app,'memo',monkeypatch)
    d=fallback(c,b,'메모 내용 좀 읽어 볼래','memo','read',target={'type':'item_id','value':'invented-secret-id'})
    assert trace(d)['widget_response']['error']['field']=='target' and not calls
    enable(c);b.output='{"widget":"memo","action":"read","target":null,"args":{},"context":{"user_confirmed":true}}'
    d=wait(c,request(c,'메모 내용 좀 읽어 볼래'))
    assert d['status']=='needs_clarification' and not calls


def test_confirm_concurrency_and_cancel_after_dispatch_never_lie(hub,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    app,c,b=hub;adapter=app.state.widget_protocol.get('todo');original=adapter._execute
    started,release=threading.Event(),threading.Event();calls=[]
    async def held(req,args,auth):
        calls.append(req.action);started.set()
        while not release.is_set():await asyncio.sleep(.005)
        return await original(req,args,auth)
    monkeypatch.setattr(adapter,'_execute',held)
    d=fallback(c,b,'내일 하늘에 우유 사기 추가해','todo','add',{'date':tomorrow(c),'title':'우유 사기'})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(confirm,c,d)
        assert started.wait(3)
        second=pool.submit(confirm,c,d)
        try:
            assert c.post('/api/llm/requests/'+d['id']+'/cancel').status_code==409
            assert not app.state.store.tasks()
        finally:release.set()
        responses=[first.result(4),second.result(4)]
    assert all(r.status_code==200 for r in responses)
    assert sum(r.json()['execution']['duplicate'] for r in responses)==1
    assert len(app.state.store.tasks())==1 and calls==['add']


def test_commit_then_failure_stays_uncertain_across_retry_and_restart(hub,monkeypatch):
    app,c,b=hub;adapter=app.state.widget_protocol.get('todo');original=adapter._execute
    async def commit_then_fail(req,args,auth):
        await original(req,args,auth)
        raise RuntimeError('private untrusted failure after commit')
    monkeypatch.setattr(adapter,'_execute',commit_then_fail)
    d=fallback(c,b,'내일 하늘에 우유 사기 추가해','todo','add',{'date':tomorrow(c),'title':'우유 사기'})
    result=confirm(c,d)
    assert result.status_code==200
    assert result.json()['status']=='failed'
    assert trace(result.json())['widget_response']['meta']['outcome_uncertain']
    assert '반영했습니다' not in result.json()['response_json']['output']
    assert len(app.state.store.tasks())==1
    assert confirm(c,d).status_code==409
    assert c.post('/api/llm/requests/'+d['id']+'/cancel').status_code==409
    retry=c.post('/api/llm/requests/'+d['id']+'/retry',json={'request_id':'uncertain-retry-01','mode':'auto'})
    latest=wait(c,retry.json())
    assert latest['assistant']['validation']=='outcome_uncertain'
    restarted=create_app(app.state.store.path.parent,weather_enabled=False,llm_backend=Model())
    with TestClient(restarted) as client:
        client.headers.update({'Authorization':'Bearer '+restarted.state.admin_token,'X-Room-Request':'1'})
        assert confirm(client,d).status_code==409
        assert len(restarted.state.store.tasks())==1


def test_capability_change_after_preview_blocks_confirmation(hub,monkeypatch):
    app,c,b=hub
    d=fallback(c,b,'내일 하늘에 우유 사기 추가해','todo','add',{'date':tomorrow(c),'title':'우유 사기'})
    a=app.state.widget_protocol.get('todo')
    monkeypatch.setattr(a,'operations',MappingProxyType(dict(a.operations)|{'add':replace(a.operations['add'],available=False)}))
    assert confirm(c,d).status_code==409
    assert not app.state.store.tasks()


def test_failed_reservation_never_calls_write_adapter(hub,monkeypatch):
    app,c,b=hub;calls=spy_adapter(app,'todo',monkeypatch)
    d=fallback(c,b,'내일 하늘에 우유 사기 추가해','todo','add',{'date':tomorrow(c),'title':'우유 사기'})
    with closing(app.state.store.connect()) as db,db:
        db.executescript("CREATE TRIGGER synthetic_ledger_failure BEFORE INSERT ON assistant_effects BEGIN SELECT RAISE(ABORT,'synthetic reservation failure'); END;")
    with pytest.raises(sqlite3.IntegrityError):confirm(c,d)
    assert calls==[] and not app.state.store.tasks()


def test_cancel_before_confirm_keeps_source_unchanged(hub):
    app,c,b=hub
    d=fallback(c,b,'내일 하늘에 우유 사기 추가해','todo','add',{'date':tomorrow(c),'title':'우유 사기'})
    assert c.post('/api/llm/requests/'+d['id']+'/cancel').status_code==200
    assert confirm(c,d).status_code==409 and not app.state.store.tasks()


@pytest.mark.parametrize('mode',['chat','legacy'])
def test_mixed_widget_request_cannot_escape_to_free_chat(hub,mode):
    app,c,b=hub;enable(c)
    d=wait(c,request(c,'메모와 오늘 할 일 목록 보여줘',mode=mode))
    assert d['status']=='needs_clarification' and not b.calls


def test_old_prepared_widget_prompt_never_runs_after_migration(hub):
    app,c,b=hub
    bridge=app.state.llm.widget_bridge;app.state.llm.widget_bridge=None
    d=request(c,'메모 내용 좀 읽어 볼래',mode='legacy')
    snapshot=d['request_body'];app.state.llm.widget_bridge=bridge;enable(c)
    assert c.post('/api/llm/requests/'+d['id']+'/send').status_code==200
    new=wait(c,d)
    assert new['status']=='needs_clarification' and not b.calls
    assert new['request_body']==snapshot


def test_schema_locks_time_and_date_to_transcript_not_acoustic_guesses(hub):
    app,c,b=hub
    d=fallback(c,b,'오늘 오전 2시 40분 아람 맞춰줘','alarm','set',{'time':None})
    args=b.calls[0]['response_format']['json_schema']['schema']['anyOf'][0]['properties']['args']['properties']
    assert args['time']=={'enum':['02:40',None]}
    assert args['date']=={'enum':[c.get('/api/clock').json()['today'],None]}
    assert trace(d)['schema_validation']=='valid'
    assert trace(d)['policy_result']=='needs_clarification'


@pytest.mark.parametrize('source',['오전 두시 사십분','오전 2시 400분','오전 2시쯤','-3시'])
def test_unsupported_uncertain_minutes_never_silently_become_zero(hub,source):
    app,c,b=hub
    d=fallback(c,b,'내일 '+source+' 하늘에 우유 사기 추가해','todo','add',{'date':tomorrow(c),'title':'우유 사기','time':'02:00'})
    assert d['status']=='needs_clarification'
    assert trace(d)['widget_response']['error']['field']=='args.time'


def test_supported_spaced_minutes_use_shared_clock_parser():
    from app.assistant import parse_clock
    assert parse_clock('오전 12시 40분 우유 사기')[0]=='00:40'
    assert parse_clock('오후 12시 40분 우유 사기')[0]=='12:40'


def test_backend_schema_rejection_never_retries_unconstrained(hub,monkeypatch):
    from app.llm import LLMFailure
    app,c,b=hub;calls=[]
    async def reject(endpoint,body,timeout):
        calls.append(json.loads(body));raise LLMFailure('schema unsupported','http_error',400)
    monkeypatch.setattr(b,'generate',reject);enable(c)
    d=wait(c,request(c,'내일 하늘에 우유 사기 추가해'))
    assert d['status']=='failed' and len(calls)==1 and 'response_format' in calls[0]
    assert not app.state.store.tasks()


def test_probe_is_opt_in_and_does_not_claim_unrun_decoding(monkeypatch):
    import importlib.util
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('probe_widget_decoding',Path(__file__).resolve().parents[1]/'scripts/probe_widget_decoding.py')
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    calls=[]
    def reply(url,payload=None,timeout=30):
        calls.append((url,payload))
        if url.endswith('/models'):return {'data':[{'id':'test-model'}]}
        if url.endswith('/props'):return {'build_info':'b6000-4762ad7','model_path':'PRIVATE/PATH'}
        assert payload['response_format']['json_schema']['schema']['properties']['probe']['const']=='room-hub-widget-schema'
        return {'choices':[{'finish_reason':'stop','message':{'content':'{"probe":"room-hub-widget-schema"}'}}]}
    monkeypatch.setattr(mod,'exchange',reply)
    data=mod.probe('http://127.0.0.1:8090/v1','test-model')
    assert data['schema_probe']=='not_run' and len(calls)==2
    assert all(payload is None for _,payload in calls) and 'PRIVATE' not in str(data)
    data=mod.probe('http://127.0.0.1:8090/v1','test-model',True)
    assert data['schema_probe']=='passed'
    assert sum(payload is not None for _,payload in calls)==1
    with pytest.raises(ValueError):mod.probe('https://example.com/v1','test-model',True)


@pytest.mark.parametrize('mode',['auto','chat','legacy'])
def test_refused_mixed_memo_input_is_private_before_domain_resolution(hub,mode):
    app,c,b=hub;place_widgets(app);enable(c)
    d=wait(c,request(c,'메모에 비밀암호1234 적어줘 그리고 오늘 할 일 알려줘',mode=mode))
    assert d['status']=='needs_clarification' and not b.calls
    assert d['assistant']['protocol_private']
    assert '비밀암호1234' not in c.get('/api/display/llm/'+d['id']).text
