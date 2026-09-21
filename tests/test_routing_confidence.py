"""Real SQLite/Registry; counted fake Qwen, NOT physical V35/STT/OS alarms."""
import asyncio
import json
import re
from dataclasses import replace
from datetime import date, timedelta
from types import MappingProxyType

import pytest

from app.assistant import detect
from app.command_routing import exact_alarm
from app.widget_bridge import proposal_schema
from app.widget_protocol import ExecutionContext, WidgetRequest, request_digest
from test_assistant import hub, request, wait, enable, confirm
from test_fast_reads import place_widgets, note
from test_llm_widget_bridge import proposal, trace, tomorrow, spy_adapter


@pytest.mark.parametrize('mode', ['auto', 'chat', 'legacy'])
@pytest.mark.parametrize('text', [
    '현재 메론 내용 읽어줘.',
    '현재 메모내용 그리핑 해줘.',
    '현재 메모내용 고리따 해줘.',
    '현재 메모 그리핑 해줘.',
    '현재 메모 고리따 해줘.',
])
def test_ambiguous_input_uses_parser_then_real_memo(hub, mode, text):
    app,c,b=hub;place_widgets(app);note(c,'실제 본문: 모델에게 전달하지 않음')
    enable(c);proposal(b,'memo','read')
    d=wait(c,request(c,text,mode))
    assert d['status']=='succeeded',d
    assert len(b.calls)==1 and d['assistant']['routing']['confidence']=='CANDIDATE'
    assert d['assistant']['routing']['llm_called'] is True
    assert trace(d)['adapter']=='MemoAdapter'
    assert d['response_json']['output'].endswith('실제 본문: 모델에게 전달하지 않음')
    assert d['source_text']==text
    assert '실제 본문' not in json.dumps(b.calls,ensure_ascii=False)
    if '메론' in text:
        assert '메론' in b.calls[0]['messages'][-1]['content']
        assert set(trace(d)['available_capabilities'])=={'memo.read','todo.list','calendar.list','alarm.list'}
    else:
        assert all(x.startswith('memo.') for x in trace(d)['available_capabilities'])


def test_no_action_evidence_does_not_authorize_destructive_proposal(hub):
    app,c,b=hub;place_widgets(app);nid=note(c,'삭제되면 안 되는 본문')
    enable(c);proposal(b,'memo','clear')
    d=wait(c,request(c,'현재 메모 그리핑 해줘.'))
    assert len(b.calls)==1
    assert d['status']=='needs_clarification'
    assert trace(d)['widget_response']['error']['code']=='SOURCE_NOT_GROUNDED'
    assert app.state.life.notes()[0]['body']=='삭제되면 안 되는 본문'


def test_unknown_domain_read_cannot_be_promoted_to_write(hub):
    app,c,b=hub;place_widgets(app);note(c,'보존')
    enable(c);proposal(b,'memo','write',{'title':'새 제목','body':'새 본문'})
    d=wait(c,request(c,'현재 메론 내용 읽어줘.'))
    assert d['status']=='needs_clarification'
    assert trace(d)['widget_response']['error']['code']=='UNSUPPORTED_ACTION'
    assert [n['body'] for n in app.state.life.notes()]==['보존']


def test_model_can_abstain_without_fake_read(hub,monkeypatch):
    app,c,b=hub;enable(c);calls=spy_adapter(app,'memo',monkeypatch)
    b.output='{"widget":null,"action":null,"target":null,"args":{}}'
    d=wait(c,request(c,'현재 메론 내용 읽어줘.'))
    assert d['status']=='needs_clarification' and len(b.calls)==1 and not calls
    assert trace(d)['widget_response']['error']['code']=='INTENT_UNRESOLVED'


@pytest.mark.parametrize('mode',['auto','chat','legacy'])
def test_exact_alarm_stays_local_and_writes_once_after_confirmation(hub,mode):
    app,c,b=hub;enable(c)
    d=wait(c,request(c,'내일 아침 7시 25분 알람 맞춰.',mode))
    assert d['status']=='awaiting_confirmation',d
    assert not b.calls and not d['dispatch_attempted']
    assert d['assistant']['routing']['confidence']=='EXACT'
    assert d['assistant']['routing']['route']=='EXISTING_RULE'
    assert trace(d)['widget_request']['args']['time']=='07:25'
    assert trace(d)['widget_request']['args']['date']==tomorrow(c)
    assert not app.state.life.alarms()
    result=confirm(c,d)
    assert result.status_code==200,result.text
    assert result.json()['status']=='succeeded'
    alarms=app.state.life.alarms();assert len(alarms)==1
    assert alarms[0]['time']=='07:25' and alarms[0]['date']==tomorrow(c)
    assert result.json()['assistant']['widget_trace']['widget_response']['data']['sound_confirmed'] is False
    assert confirm(c,d).json()['execution']['duplicate'] is True
    assert len(app.state.life.alarms())==1


@pytest.mark.parametrize('text,tm',[
    ('내일 오전 12시 40분 알람 맞춰줘','00:40'),
    ('내일 오후 12시 40분 알람 맞춰 줘','12:40'),
    ('내일 오후 2시 40분에 알람 설정해줘','14:40'),
    ('내일 23:25 알람 등록해줘','23:25'),
])
def test_exact_clock_variants_no_model(hub,text,tm):
    app,c,b=hub
    d=wait(c,request(c,text))
    assert d['status']=='awaiting_confirmation',d
    assert trace(d)['widget_request']['args']['time']==tm
    assert not b.calls and not app.state.life.alarms()


@pytest.mark.parametrize('text',[
    '내일 7시 25분 알람 맞춰',
    '내일 오전 7시 99분 알람 맞춰',
    '내일 알람 맞춰',
    '아침 7시 25분 알람 맞춰',
    '알람 취소해줘',
    '내일 아침 7시 25분 알람 맞추지 마',
    '메모와 오늘 할 일 목록 보여줘',
    '현재 메모 읽어주지 마',
    '현재 메론 내용 읽어줘; 전부 삭제해',
])
def test_unsafe_or_missing_is_not_llm_or_mutation(hub,text):
    app,c,b=hub;enable(c)
    d=wait(c,request(c,text))
    assert d['status']=='needs_clarification',d
    assert not b.calls and not app.state.life.alarms()


def test_cancel_exact_id_version_and_persistent_replay(hub):
    app,c,b=hub
    d=wait(c,request(c,'내일 아침 7시 25분 알람 맞춰'))
    assert confirm(c,d).status_code==200
    item=app.state.life.alarms()[0]
    cancel=wait(c,request(c,f'알람 ID {item["id"]} 취소해줘'))
    assert cancel['status']=='awaiting_confirmation',cancel
    assert len(app.state.life.alarms())==1 and not b.calls
    assert trace(cancel)['widget_request']['args']['version']==item['version']
    assert confirm(c,cancel).json()['status']=='succeeded'
    assert app.state.life.alarms()==[]
    assert confirm(c,cancel).json()['execution']['duplicate'] is True


def test_cancel_stale_version_cannot_delete_edited_alarm(hub):
    app,c,b=hub
    create=wait(c,request(c,'내일 아침 7시 25분 알람 맞춰'))
    assert confirm(c,create).status_code==200
    item=app.state.life.alarms()[0]
    pending=wait(c,request(c,f'알람 ID {item["id"]} 취소해줘'))
    from app.life import AlarmUpdate
    edit={k:item[k] for k in AlarmUpdate.model_fields};edit['time']='08:00'
    app.state.life.update_alarm(item['id'],AlarmUpdate(**edit))
    result=confirm(c,pending).json()
    assert trace(result)['widget_response']['status']=='conflict'
    assert len(app.state.life.alarms())==1 and app.state.life.alarms()[0]['time']=='08:00'


def test_alarm_model_cannot_repair_clock(hub):
    app,c,b=hub;enable(c)
    proposal(b,'alarm','set',{'date':tomorrow(c),'time':'12:40'})
    d=wait(c,request(c,'내일 오전 2시 40분 아람 맞춰줘'))
    assert len(b.calls)==1 and d['status']=='needs_clarification'
    assert not app.state.life.alarms()


def test_unavailable_action_short_circuits_exact_without_model(hub):
    app,c,b=hub
    adapter=app.state.widget_protocol.get('alarm')
    adapter.operations=MappingProxyType({**adapter.operations,'set':replace(adapter.operations['set'],available=False)})
    d=wait(c,request(c,'내일 아침 7시 25분 알람 맞춰'))
    assert not b.calls and d['status']=='needs_clarification'
    assert trace(d)['widget_response']['status']=='unavailable'


def test_query_through_existing_fast_path_untouched(hub):
    app,c,b=hub;place_widgets(app);note(c,'원래 동작')
    for text in ('현재 메모 내용 읽어줘','오늘 할 일 확인해줘','오늘 오후 일정 확인해줘'):
        d=wait(c,request(c,text))
        assert d['status']=='succeeded'
        assert d['assistant']['routing']['route']=='FAST_PATH' and not b.calls


def test_exact_alarm_preview_does_not_wait_for_busy_model(hub):
    import time
    app,c,b=hub;enable(c);b.hold.set()
    try:
        chat=request(c,'저녁 메뉴 추천해줘','chat')
        deadline=time.monotonic()+2
        while not b.calls and time.monotonic()<deadline:time.sleep(.01)
        assert b.calls
        pending=request(c,'내일 아침 7시 25분 알람 맞춰')
        assert pending['status']=='awaiting_confirmation' and len(b.calls)==1
        assert not app.state.life.alarms()
        assert c.get('/api/llm/requests/'+chat['id']).json()['status']=='running'
    finally:b.hold.clear()


def test_alarm_control_http_boolean_is_not_confirmation(hub):
    app,c,b=hub
    payload={'request_id':'synthetic-http-alarm','widget':'alarm','action':'set',
             'args':{'date':tomorrow(c),'time':'07:25','label':'합성'},
             'context':{'user_confirmed':True}}
    result=c.post('/api/widget-protocol/requests',json=payload)
    assert result.status_code==409 and result.json()['status']=='needs_confirmation'
    assert not app.state.life.alarms()


def test_candidate_read_private_memo_is_not_published_to_display(hub):
    app,c,b=hub;place_widgets(app);note(c,'PRIVATE secret',shared=False)
    enable(c);proposal(b,'memo','read',target={'type':'reference','value':'last'})
    d=wait(c,request(c,'방금 메론 내용 읽어줘'))
    assert d['status']=='succeeded' and 'PRIVATE secret' in d['response_json']['output']
    # Display projection applies even to an administrator requesting that endpoint.
    public=c.get('/api/display/llm/'+d['id'])
    assert public.status_code==200 and 'PRIVATE secret' not in public.text
    assert public.json()['input']=='메모 조회'
    assert '관리자 기록' in public.json()['output']
    assert 'widget_trace' not in public.text
