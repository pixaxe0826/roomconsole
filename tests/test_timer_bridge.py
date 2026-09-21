"""Counted fake LLM and real TimerAdapter/SQLite. No model installation."""
import json
import time
import pytest
from app.timer_commands import exact_timer
from app.timers import TimerStart
from test_assistant import hub,request,wait,enable,add,confirm


@pytest.mark.parametrize('text,duration',[
 ('4분 타이머 시작',240),('타이머 4분 시작해줘',240),('1초 타이머 시작',1),
 ('10분 타이머 시작',600),('600초 타이머 시작',600),('1분 30초 타이머 시작',90),
 ('네 분 타이머 시작해 줘.',240),('타이머 30초로 설정해줘',30),
])
def test_exact_start_immediate_no_llm(hub,text,duration):
    app,c,b=hub;enable(c)
    d=wait(c,request(c,text))
    assert d['status']=='succeeded',d
    assert not b.calls and not d['dispatch_attempted']
    trace=d['assistant']['widget_trace'];assert trace['adapter']=='TimerAdapter'
    assert trace['widget_request']['args']['duration_seconds']==duration
    assert '시작했습니다' in d['response_json']['output']
    assert len(app.state.timers.snapshot()['items'])==1


@pytest.mark.parametrize('mode',['auto','chat','legacy'])
def test_current_stop_voice_immediate_and_other_timer_kept(hub,mode):
    app,c,b=hub
    one=wait(c,request(c,'4분 타이머 시작',mode));two=wait(c,request(c,'5분 타이머 시작',mode))
    first=one['assistant']['tool_result']['id'];second=two['assistant']['tool_result']['id']
    stop=wait(c,request(c,'현재 타이머 종료',mode))
    assert stop['status']=='succeeded' and not b.calls
    assert stop['assistant']['tool_result']['id']==second
    assert app.state.timers.get(first)['state']=='running'
    assert app.state.timers.get(second)['state']=='stopped'
    assert stop['assistant']['routing']['resolved_context']['selection_policy']=='latest_started_running'


@pytest.mark.parametrize('text',[
 '0초 타이머 시작','601초 타이머 시작','11분 타이머 시작','-4분 타이머 시작',
 '1.5초 타이머 시작','내일 4분 타이머 시작','4분 타이머 시작하지 마',
 '4분 타이머 시작하고 5분 타이머 시작','모든 타이머 종료','타이머 시작',
 '1분 90초 타이머 시작',
])
def test_bad_or_unsafe_never_starts(hub,text):
    app,c,b=hub;enable(c)
    d=wait(c,request(c,text))
    assert d['status']=='needs_clarification',d
    assert not app.state.timers.snapshot()['items'] and not b.calls


def test_current_absent_not_success(hub):
    app,c,b=hub
    d=wait(c,request(c,'현재 타이머 종료'))
    assert d['status']=='needs_clarification' and not b.calls
    assert d['assistant']['widget_trace']['widget_response']['status']=='not_found'


def test_retry_family_never_creates_second_timer_or_stops_next(hub):
    app,c,b=hub
    d=wait(c,request(c,'4분 타이머 시작'))
    r=c.post('/api/llm/requests/'+d['id']+'/retry',json={'request_id':'timer-retry-key','mode':'auto'})
    assert r.status_code==202,r.text
    again=wait(c,r.json());assert again['status']=='succeeded'
    assert len(app.state.timers.snapshot()['items'])==1
    second=wait(c,request(c,'5분 타이머 시작'))
    stopped=wait(c,request(c,'현재 타이머 종료'))
    r=c.post('/api/llm/requests/'+stopped['id']+'/retry',json={'request_id':'timer-stop-retry','mode':'auto'})
    assert r.status_code==202,r.text
    assert wait(c,r.json())['status']=='succeeded'
    assert app.state.timers.snapshot()['active_count']==1


def test_timer_list_grounded(hub):
    app,c,b=hub
    wait(c,request(c,'4분 타이머 시작'))
    d=wait(c,request(c,'현재 타이머 남은 시간 알려줘'))
    assert d['status']=='succeeded' and '1개' in d['response_json']['output'] and not b.calls


def test_unfamiliar_start_keeps_constrained_adapter_fallback(hub):
    app,c,b=hub;enable(c)
    b.output=json.dumps({'widget':'timer','action':'start','target':None,'args':{'duration_seconds':240}})
    # Deliberately outside the exact whole-sentence grammar.
    d=wait(c,request(c,'타이머를 가동해 주세요, 4분 정도'))
    assert d['status']=='succeeded',d
    assert len(b.calls)==1 and d['assistant']['widget_trace']['adapter']=='TimerAdapter'
    assert d['assistant']['routing']['llm_called']
    assert b.calls[0]['response_format']['type']=='json_schema'


def test_fallback_cannot_invent_duration(hub):
    app,c,b=hub;enable(c)
    b.output=json.dumps({'widget':'timer','action':'start','target':None,'args':{'duration_seconds':540}})
    d=wait(c,request(c,'타이머를 가동해 주세요, 4분 정도'))
    assert len(b.calls)==1 and d['status']=='needs_clarification'
    assert not app.state.timers.snapshot()['items']


def test_existing_writes_still_require_confirmation(hub):
    app,c,b=hub
    d=wait(c,request(c,'오늘 할 일에 우유 사기 추가해'))
    assert d['status']=='awaiting_confirmation' and not app.state.store.tasks()


@pytest.mark.parametrize('text', ['10분 후 타이머 종료해 주세요', '다른 타이머 종료해 줘', '타이머 하나를 종료해 주세요'])
def test_fallback_cannot_guess_current_or_schedule_stop(hub,text):
    app,c,b=hub;enable(c)
    before=app.state.timers.start(TimerStart(request_id='before-candidate',duration_seconds=240),'test')
    b.output=json.dumps({'widget':'timer','action':'stop','target':{'type':'reference','value':'current'},'args':{}})
    d=wait(c,request(c,text))
    assert d['status']=='needs_clarification',d
    assert app.state.timers.get(before['id'])['state']=='running'


def test_model_cannot_invent_read_target_id(hub):
    app,c,b=hub;enable(c)
    before=app.state.timers.start(TimerStart(request_id='private-id',duration_seconds=240),'test')
    b.output=json.dumps({'widget':'timer','action':'get','target':{'type':'item_id','value':before['id']},'args':{}})
    d=wait(c,request(c,'현재 타이머가 어떻게 되고 있는지 확인 좀 해 주세요'))
    assert d['status']=='needs_clarification',d
    assert len(b.calls)==1


# The real speech worker, submit queue and TimerAdapter run here. Only ASR is a
# fixture; this is not a measurement of Whisper's recognition quality.
from test_speech import enabled, post as post_speech, wait as wait_speech, wait_llm


def test_successful_stt_auto_starts_and_stops_timer_without_model(enabled):
    app,c,runner,admin,_=enabled
    runner.text='4분 타이머 시작'
    response=post_speech(c,'timer-voice-start');assert response.status_code==202
    job=wait_speech(c,response.json()['id']);result=wait_llm(app,job['voice_id'])
    assert result['status']=='succeeded' and not result['dispatch_attempted']
    timer=result['assistant']['tool_result'];assert timer['duration_seconds']==240
    duplicate=post_speech(c,'timer-voice-start').json();assert duplicate['duplicate']
    assert app.state.timers.snapshot()['active_count']==1
    runner.text='현재 타이머 종료'
    job=wait_speech(c,post_speech(c,'timer-voice-stop').json()['id'])
    result=wait_llm(app,job['voice_id'])
    assert result['status']=='succeeded' and not result['dispatch_attempted']
    assert app.state.timers.get(timer['id'])['state']=='stopped'
