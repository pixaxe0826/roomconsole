"""Synthetic notes/tasks + real HTTP/SQLite. Backends are test doubles, not Qwen."""
import asyncio
import json
import sqlite3
import time
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.assistant import Proposal, detect, schema
from app.fast_reads import match_read, execute_read
from app.life import memo_snapshot
from test_assistant import hub, request, wait, add, enable, confirm, AT

TZ = 'Asia/Seoul'
MEMOS = [
    '현재 메모 읽어줘', '메모 내용 읽어줘', '현재 메모에 남아있는 내용 읽어줘.',
    '방금 메모에 작성한 내용 읽어줘.', '방금 적은 메모 보여줘', '메모 뭐라고 적혀 있어',
    '방금 메모 읽어줘', '아까 작성한 메모 읽어 줘', '현재 메모 본문을 읽어 주세요',
]
TODOS = ['오늘 할 일 확인해줘', '오늘 할 일 알려줘', '오늘 할 일 뭐 있어', '내일 할 일 확인해줘', '내일 할 일 알려줘', '오늘 할 일', '내일 할 일']
CALENDARS = ['오늘 일정 알려줘', '오늘 일정 확인해줘', '오늘 오후 일정 확인해줘', '오늘 오전 일정 알려줘', '내일 일정 알려줘', '오늘 오후 일정', '내일 오전 일정', '오늘 일정']


def place_widgets(app):
    layout=app.state.store.get('layout');layout['rows']=10
    layout['widgets'] += [
        {'id':'note-test','type':'note','title':'메모','x':0,'y':6,'w':4,'h':2,'config':{}},
        {'id':'reply-test','type':'llm-response','title':'응답','x':4,'y':6,'w':4,'h':2,'config':{}},
    ]
    app.state.store.set('layout',layout)


def note(c, text, **values):
    r=c.post('/api/life/notes',json={'request_id':f'memo-{time.time_ns()}', 'title':'합성 메모',
                                  'body':text,'shared':True,'pinned':False,**values})
    assert r.status_code==201,r.text
    return r.json()['id']


def assert_local(d, intent):
    assert d['status']=='succeeded',d
    assert not d['dispatch_attempted'] and not d['assistant']['calls']
    assert d['endpoint']=='local://room-hub'
    assert d['assistant']['routing']['route']=='FAST_PATH'
    assert d['assistant']['routing']['resolved_intent']==intent
    assert d['assistant']['routing']['llm_called'] is False
    assert d['assistant']['routing']['llm_seconds']==0
    assert d['response_json']['metrics']=={} and not d['response_raw']


@pytest.mark.parametrize('text',MEMOS+TODOS+CALENDARS)
def test_anchored_variants(text):
    r=detect(text,AT,TZ)
    assert r['fast_read'] and r['routing']['route']=='FAST_PATH'


@pytest.mark.parametrize('text',[
    '메모리 6GB 장점 알려줘', '현재 메모 읽어주지 마', '메모 삭제해',
    '방금 메모에 우유 사기 작성해', '메모를 읽어주면 추가해', '"메모 읽어줘"를 번역해',
    '오늘 할 일을 보여주고 전부 삭제해', '오늘부터 내일까지 일정 알려줘',
    '오늘 오후 3시 이후 일정 알려줘', '오늘 오후 일정에서 회의만 보여줘',
    '내일 하늘에 우유 사기 추가해.', '메모 읽어줘; 전부 삭제해',
])
def test_no_loose_fast_matches(text):
    assert match_read(text,AT,TZ) is None


def test_case1_current_memo_uses_actual_widget_and_not_private_or_fiction(hub):
    app,c,b=hub;place_widgets(app)
    app.state.store.set('widget_data:note',{'text':'실제 저장된 고정 문구\n두 번째 줄'})
    note(c,'공유 메모');note(c,'숨겨야 할 개인 정보',shared=False)
    enable(c)
    d=wait(c,request(c,'현재 메모에 남아있는 내용 읽어줘.'))
    assert_local(d,'MEMO_READ')
    assert d['response_json']['output'].endswith('실제 저장된 고정 문구\n두 번째 줄')
    assert d['assistant']['routing']['resolved_context']['widget_id']=='note-test'
    assert d['assistant']['routing']['resolved_context']['active_card_known'] is False
    assert not b.calls


def test_case2_latest_uses_updated_at_not_pin_or_legacy(hub):
    app,c,b=hub;place_widgets(app)
    app.state.store.set('widget_data:note',{'text':'오래된 고정 문구'})
    app.state.life.clock=lambda:1000.0
    note(c,'고정된 과거 메모',pinned=True)
    app.state.life.clock=lambda:1001.0
    nid=note(c,'방금 저장된 본문',shared=False)
    d=wait(c,request(c,'방금 메모에 작성한 내용 읽어줘.'))
    assert_local(d,'MEMO_READ')
    assert d['response_json']['output'].endswith('방금 저장된 본문')
    assert d['assistant']['tool_result']['note_id']==nid
    assert d['assistant']['routing']['resolved_context']['selection_policy']=='updated_at'
    assert not b.calls


@pytest.mark.parametrize('word,offset',[('오늘',0),('내일',1)])
def test_cases3_4_todos_are_exact_db_rows(hub,word,offset):
    app,c,b=hub
    today=c.get('/api/state').json()['today'];tomorrow=(date.fromisoformat(today)+timedelta(days=1)).isoformat()
    add(c,'오늘의 실제 항목',today);add(c,'내일의 실제 항목',tomorrow)
    d=wait(c,request(c,f'{word} 할 일 알려줘.'))
    assert_local(d,'TODO_LIST')
    assert len(d['assistant']['tool_result']['items'])==1
    assert d['assistant']['routing']['resolved_context']['date']==[today,tomorrow][offset]
    assert f'{word}의 실제 항목' in d['response_json']['output'] and not b.calls


def test_case5_afternoon_filters_before_limit_and_reports_untimed(hub):
    app,c,b=hub
    for n in range(52):add(c,f'오전 항목 {n}',time_='11:59')
    add(c,'정오 회의',time_='12:00');add(c,'마지막 일정',time_='23:59');add(c,'시간 없음')
    d=wait(c,request(c,'오늘 오후 일정 확인해줘.'))
    assert_local(d,'CALENDAR_QUERY')
    result=d['assistant']['tool_result']
    assert [t['title'] for t in result['items']]==['정오 회의','마지막 일정']
    assert result['count']==2 and result['untimed_count']==1 and not result['truncated']
    assert '시간 미지정 항목 1개' in d['response_json']['output']
    assert d['assistant']['routing']['resolved_context']['period']=='afternoon' and not b.calls


def test_morning_boundary_and_read_only_business_data(hub):
    app,c,b=hub
    for title,tm in [('자정','00:00'),('오전','11:59'),('정오','12:00'),('미정',None)]:add(c,title,time_=tm)
    before=app.state.store.tasks()
    d=wait(c,request(c,'오늘 오전 일정 알려줘'))
    assert [t['title'] for t in d['assistant']['tool_result']['items']]==['자정','오전']
    assert app.state.store.tasks()==before and not b.calls


def test_case6_uncertain_input_still_calls_structured_parser(hub):
    app,c,b=hub;enable(c);b.output=json.dumps({'intent':'unknown','time':None})
    d=wait(c,request(c,'내일 하늘에 우유 사기 추가해.'))
    assert len(b.calls)==1 and d['dispatch_attempted']
    assert d['assistant']['route']=='parser'
    assert d['assistant']['routing']['route']=='LLM_FALLBACK'
    assert d['assistant']['routing']['llm_called'] is True
    assert d['status']=='needs_clarification' and not app.state.store.tasks()
    assert b.calls[0]['response_format']['type']=='json_schema'


@pytest.mark.parametrize('tm',[None,'unknown',' NONE ','null','N/A',''])
def test_case7_missing_optional_time_is_null(hub,tm):
    app,c,b=hub;enable(c)
    b.output=json.dumps({'widget':'todo','action':'add','target':None,'args':{'date':c.get('/api/clock').json()['tomorrow'],'title':'우유 사기','time':tm}})
    d=wait(c,request(c,'내일 하늘에 우유 사기 추가해.'))
    assert d['status']=='awaiting_confirmation',d
    assert d['assistant']['widget_trace']['widget_request']['args'].get('time') is None
    assert not app.state.store.tasks() # write STILL requires confirmation


def test_case7_omitted_time_and_required_missing_value(hub):
    app,c,b=hub;enable(c)
    b.output=json.dumps({'widget':'todo','action':'add','target':None,'args':{'date':c.get('/api/clock').json()['tomorrow'],'title':'우유 사기'}})
    d=wait(c,request(c,'내일 하늘에 우유 사기 추가해.'))
    assert d['status']=='awaiting_confirmation' and d['assistant']['widget_trace']['widget_request']['args'].get('time') is None
    b.output=json.dumps({'widget':'todo','action':'add','target':None,'args':{'time':None}})
    d=wait(c,request(c,'내일 하늘에 우유 사기 추가해.'))
    assert d['status']=='needs_clarification' and not app.state.store.tasks()


def test_invalid_time_remains_invalid_but_no_pydantic_trace_in_response(hub):
    app,c,b=hub;enable(c)
    b.output=json.dumps({'widget':'todo','action':'add','target':None,'args':{'date':c.get('/api/clock').json()['tomorrow'],'title':'우유 사기','time':'25:99'}})
    d=wait(c,request(c,'내일 하늘에 우유 사기 추가해.'))
    assert d['status']=='needs_clarification'
    assert 'ValidationError' not in d['response_json']['output']
    assert 'string_pattern_mismatch' not in d['response_json']['output']
    assert not app.state.store.tasks()
    with pytest.raises(ValidationError):Proposal(intent='todo.create',time='25:99')


def test_model_cannot_silently_drop_explicit_source_time(hub):
    app,c,b=hub;enable(c)
    b.output=json.dumps({'widget':'todo','action':'add','target':None,'args':{'date':c.get('/api/clock').json()['tomorrow'],'title':'우유 사기','time':'unknown'}})
    d=wait(c,request(c,'내일 하늘에 오후 3시 우유 사기 추가해.'))
    assert d['status']=='needs_clarification' and not app.state.store.tasks()


@pytest.mark.parametrize('mode',['auto','chat','legacy'])
@pytest.mark.parametrize('enabled',[False,True])
def test_clear_reads_cannot_be_forced_to_model_by_old_mode(hub,mode,enabled):
    app,c,b=hub
    if enabled:enable(c)
    d=wait(c,request(c,'오늘 할 일 확인해 줘.',mode=mode))
    assert_local(d,'TODO_LIST');assert not b.calls
    assert d['response_json']['output']=='오늘 등록된 남은 할 일이 없습니다.'


def test_fast_path_does_not_wait_for_busy_model_or_cancel_it(hub):
    app,c,b=hub;enable(c);b.hold.set()
    slow=request(c,'저녁 메뉴 추천해 줘.',mode='chat')
    for _ in range(150):
        if b.calls:break
        time.sleep(.01)
    assert b.calls
    active=app.state.llm.active_id;call=app.state.llm.call
    try:
        d=wait(c,request(c,'오늘 할 일 확인해 줘.'))
        assert_local(d,'TODO_LIST')
        assert app.state.llm.active_id==active==slow['id'] and app.state.llm.call is call
        assert len(b.calls)==1 and not call.cancelled()
    finally:b.hold.clear()
    assert wait(c,slow)['status']=='succeeded'


def test_fast_path_ignores_other_active_transcription(hub):
    app,c,b=hub;app.state.speech.active_id='synthetic-other-transcription'
    try:assert_local(wait(c,request(c,'오늘 할 일 알려줘')),'TODO_LIST')
    finally:app.state.speech.active_id=None
    assert not b.calls


def test_db_error_is_failure_not_empty_or_model_fallback(hub,monkeypatch):
    app,c,b=hub;enable(c)
    def unavailable(*a,**kw):raise sqlite3.OperationalError('synthetic source unavailable')
    monkeypatch.setattr(app.state.store,'query_tasks',unavailable)
    d=wait(c,request(c,'오늘 오후 일정 확인해줘'))
    assert d['status']=='failed' and d['error_code']=='source_read_failed'
    assert d['assistant']['widget_trace']['widget_response']['status']=='error' and not b.calls
    assert '없습니다' not in d['response_json']['output']


def test_current_memo_empty_no_widget_and_tied_recency_do_not_guess(hub):
    app,c,b=hub
    d=wait(c,request(c,'현재 메모 읽어줘'));assert d['status']=='needs_clarification'
    place_widgets(app);d=wait(c,request(c,'현재 메모 읽어줘'))
    assert_local(d,'MEMO_READ');assert d['assistant']['tool_result']['count']==0
    app.state.life.clock=lambda:1000.0
    note(c,'A');note(c,'B')
    d=wait(c,request(c,'방금 메모 읽어줘'))
    assert d['status']=='needs_clarification' and not b.calls


def test_default_memo_respects_pin_and_nullish_legacy_precedence(hub):
    app,c,b=hub;place_widgets(app)
    layout=app.state.store.get('layout');layout['widgets'][-2]['config']={'text':'설정 본문'}
    app.state.store.set('layout',layout)
    assert memo_snapshot(app.state.store)['body']=='설정 본문'
    app.state.store.set('widget_data:note',{'text':''})
    note(c,'고정 카드',pinned=True);note(c,'최신 카드')
    assert memo_snapshot(app.state.store)['body']=='고정 카드'


def test_private_memo_not_leaked_through_shared_llm_reply(hub):
    app,c,b=hub;place_widgets(app);note(c,'PRIVATE-SYNTHETIC',shared=False)
    pair=c.post('/api/devices/pair',json={'name':'synthetic'}).json()
    viewer=TestClient(app)
    assert viewer.post('/api/devices/claim',json={'code':pair['path'].split('=')[1]}).status_code==200
    d=wait(c,request(c,'방금 메모 읽어줘'))
    assert 'PRIVATE-SYNTHETIC' in d['response_json']['output']
    reply=viewer.get('/api/display/llm/'+d['id'])
    assert reply.status_code==200 and 'PRIVATE-SYNTHETIC' not in reply.text
    assert viewer.get('/api/llm/requests/'+d['id']).status_code==401
    assert viewer.get('/api/life/notes').status_code==401


def test_memo_sharing_rechecked_after_revoke_and_delete(hub):
    app,c,b=hub;place_widgets(app);nid=note(c,'PUBLIC-THEN-PRIVATE')
    d=wait(c,request(c,'현재 메모 읽어줘'))
    path='/api/display/llm/'+d['id']
    assert 'PUBLIC-THEN-PRIVATE' in c.get(path).text
    assert c.put('/api/life/notes/'+nid,json={'version':1,'title':'private','body':'PUBLIC-THEN-PRIVATE','shared':False,'pinned':False}).status_code==200
    assert 'PUBLIC-THEN-PRIVATE' not in c.get(path).text
    assert c.request('DELETE','/api/life/notes/'+nid,json={'version':2}).status_code==200
    assert 'PUBLIC-THEN-PRIVATE' not in c.get(path).text


def test_old_rule_write_confirmation_and_model_chat_still_work(hub):
    app,c,b=hub
    d=wait(c,request(c,'내일 할 일에 검증 작업 추가해'))
    assert d['status']=='awaiting_confirmation' and not b.calls and not app.state.store.tasks()
    assert confirm(c,d).status_code==200 and len(app.state.store.tasks())==1
    enable(c);d=wait(c,request(c,'저녁 메뉴 추천해 줘.',mode='chat'))
    assert d['status']=='succeeded' and len(b.calls)==1


def test_today_tomorrow_use_request_timezone_near_midnight(hub):
    app,_,_=hub
    at='2026-09-20T15:01:00+00:00' # Seoul is already Sep 21
    for word,wanted in [('오늘','2026-09-21'),('내일','2026-09-22')]:
        plan=match_read(word+' 오후 일정 알려줘',at,TZ)
        _,_,context=execute_read(app.state.store,plan,at,TZ)
        assert context['date']==wanted


def test_llm_schema_does_not_offer_memo_as_a_model_tool():
    assert 'memo.read' not in schema()['properties']['intent']['enum']
    assert 'null' in schema()['properties']['time']['type']


def test_latest_memo_tracks_edit_not_only_creation(hub):
    app,c,b=hub;place_widgets(app)
    app.state.life.clock=lambda:1000.0
    nid=note(c,'첫 번째 저장값')
    app.state.life.clock=lambda:1001.0
    note(c,'나중에 만든 다른 메모')
    app.state.life.clock=lambda:1002.0
    assert c.put('/api/life/notes/'+nid,json={'version':1,'title':'수정됨',
        'body':'방금 수정한 실제 값','shared':True,'pinned':False}).status_code==200
    d=wait(c,request(c,'아까 작성한 메모 읽어줘'))
    assert_local(d,'MEMO_READ')
    assert d['response_json']['output'].endswith('방금 수정한 실제 값')
    assert d['assistant']['tool_result']['version']==2 and not b.calls


def test_nondefault_widget_does_not_impersonate_active_selection(hub):
    app,c,b=hub;place_widgets(app)
    layout=app.state.store.get('layout')
    layout['widgets'][-2]['config']={'text':'첫 기본 위젯'}
    layout['widgets'].append({'id':'other-note','type':'note','title':'다른 메모',
                             'x':0,'y':8,'w':4,'h':2,'config':{'text':'둘째 위젯'}})
    app.state.store.set('layout',layout)
    d=wait(c,request(c,'현재 메모 읽어줘'))
    assert d['response_json']['output'].endswith('첫 기본 위젯')
    assert d['assistant']['routing']['resolved_context']['widget_id']=='note-test'
    assert d['assistant']['routing']['resolved_context']['active_card_known'] is False
    assert not b.calls
