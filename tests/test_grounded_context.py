"""0.1.6: synthetic source text and temporary SQLite; never V35/model data."""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from app.assistant import detect, grounded, payload, resolve_date, PROTOCOL, digest
from app.clock_service import clock_context, resolve_range, aware
from app.llm import LLMConfig
from app.capabilities import manifest
from app.response_quality import assess
from test_assistant import hub, request, wait, add, enable, confirm

AT='2026-09-20T05:10:01+09:00'
TZ='Asia/Seoul'

@pytest.mark.parametrize('text,status',[
 ('오늘, 남은 할 일 확인해','pending'),('오늘，남은 할 일 확인해 줘.','pending'),
 ('오늘: 남은 할 일 확인해서 보고해줘!','pending'),('오늘 남은 일 알려줘','pending'),
 ('오늘 할 일 뭐 남아있어?','pending'),('오늘 할 일 뭐 남았어?','pending'),
 ('오늘 미완료 할 일 보여줘','pending'),('오늘 해야 할 것 알려줘','pending'),
 ('오늘 하지 않은 할 일 보여줘','pending'),('오늘 완료하지 않은 할 일 보여줘','pending'),
 ('오늘 안 끝낸 할 일 알려줘','pending'),('오늘 완료된 할 일 보여줘','completed'),
 ('오늘 완료한 할 일 알려줘','completed'),('오늘 전체 할 일 알려줘','all'),
 ('오늘 할 일 브리핑해 줘.','all'),('오늘 할 일 간단히 요약해 줘','all'),
 ('오늘 일정 브리핑해 줘','all'),('오늘 할 일 목록 정리해 줘','all'),
 ('내일 뭐 해야 돼?','pending'),('내일 내가 뭐 해야 돼?','pending'),
 ('내일 무엇을 해야 하나요?','pending'),('내일 남은 게 뭐야?','pending'),
 ('9월 21에 남은 일 알려줘.','pending'),('9월 21일에 남은 일 알려줘.','pending'),
 ('2026-09-21 남은 할 일 알려줘','pending'),('2026년 9월 21일 할 일 보여줘','all'),
 ('다음 주 남은 할 일 알려줘','pending'),('다음 달 일정 보여줘','all'),
 ('전체 날짜 할 일 보여줘','all'),('오늘， 남은， 할 일 알려줘','pending'),
])
def test_aliases_local_and_grounded(text,status):
 r=detect(text,AT,TZ)
 assert r['route']=='rule',r
 assert r['proposal']['status']==status
 assert grounded(r['proposal'],r).status==status
 assert r['raw']==text
 assert payload(r,LLMConfig()) is None

@pytest.mark.parametrize('text',[
 '네, 뭐 해야 돼?', '9월 21에 남은 애 말려줘', '내일 남은 게 있으면 알려줘',
 '오늘부터 내일까지 할 일 전부 삭제해', '오늘이나 내일 할 일 알려줘',
 '그 날 할 일 알려줘', '내일 할 일 중 택배만 보여줘', '오늘 할 일에서 완료된 것 빼고 알려줘',
])
def test_uncertain_personal_not_free_chat(text):
 r=detect(text,AT,TZ)
 assert r['route'] in {'clarify','parser'},r

@pytest.mark.parametrize('text',[
 '내일 뭐 먹어야 돼?', '저녁 메뉴 추천해 줘', '노트북 살 때 뭘 고려할까?',
 '왜 하늘은 파란색이야?',
])
def test_real_general_chat_not_task_query(text):
 r=detect(text,AT,TZ)
 assert r['route']=='chat',r

@pytest.mark.parametrize('text', ['내일 9시에 알람 설정해','메모 저장해 줘','내 메모 찾아줘'])
def test_uninstalled_features_explicit(text):
 r=detect(text,AT,TZ)
 assert r['route']=='clarify' and '아직' in r['final_text']

@pytest.mark.parametrize('ref,expected',[
 ('2026년 9월 21일',('2026-09-21','2026-09-21')),
 ('9월 21',('2026-09-21','2026-09-21')),
 ('이번 주',('2026-09-14','2026-09-20')),
 ('다음 주',('2026-09-21','2026-09-27')),
 ('이번 달',('2026-09-01','2026-09-30')),
 ('다음 달',('2026-10-01','2026-10-31')),
])
def test_resolve_ranges(ref,expected):
 r=resolve_range(ref,AT,TZ);assert (r.start,r.end)==expected

def test_year_rollover_and_leap():
 assert resolve_date('내일','2026-12-31T23:59:59+09:00',TZ)=='2027-01-01'
 assert resolve_date('2월 29일','2024-02-01T00:00:00+09:00',TZ)=='2024-02-29'
 with pytest.raises(ValueError):resolve_date('2월 29일',AT,TZ)
 assert resolve_date('1월 1일',AT,TZ)=='2026-01-01' # documented current-year policy
 r=resolve_range('다음 달','2026-12-21T00:00:00+09:00',TZ)
 assert r.start=='2027-01-01' and r.end=='2027-01-31'

def test_same_instant_different_zone():
 assert clock_context('2026-09-19T20:10:01+00:00',TZ)['today']=='2026-09-20'
 assert clock_context('2026-09-19T20:10:01+00:00','UTC')['today']=='2026-09-19'
 with pytest.raises(ValueError):clock_context('2026-09-20T00:00:00',TZ)

@pytest.mark.parametrize('text',['9월 31일 남은 할 일 알려줘','2026-02-30 할 일 보여줘'])
def test_invalid_date_not_defaulted(text):
 assert detect(text,AT,TZ)['route']=='clarify'

@pytest.mark.parametrize('text,ref',[
 ('내일 할 일 브리핑해 줘','2026-09-21'),
 ('오늘 할 일 뭐 남아있어','2026-09-20'),
 ('9월 21에 남은 일 알려줘','2026-09-21'),
])
def test_iso_proposal_matches_source_meaning(text,ref):
 r=detect(text,AT,TZ);p=dict(r['proposal']);p['date_ref']=ref
 validated=grounded(p,r)
 assert r['resolved_date']['start']==ref
 assert validated.date_ref!=ref or ref in text

@pytest.mark.parametrize('change',[
 {'date_ref':'2026-09-22'},{'status':'all'},{'status':'completed'},{'title':'택배'},
 {'time':'13:00'},{'scope':'all'},
])
def test_model_cannot_relax_query_constraints(change):
 r=detect('내일 남은 할 일 알려줘',AT,TZ);p=dict(r['proposal']);p.update(change)
 with pytest.raises(ValueError):grounded(p,r)

def test_read_missing_model_date_is_source_filled():
 r=detect('내일 남은 할 일 알려줘',AT,TZ);p=dict(r['proposal']);p['date_ref']=None
 assert grounded(p,r).date_ref=='내일'

def test_actual_pending_db_with_comma(hub):
 a,c,b=hub;one=add(c,'남은 업무');done=add(c,'완료 업무')
 c.patch('/api/tasks/'+done+'/completion',json={'version':1,'completed':True})
 d=wait(c,request(c,'오늘, 남은 할 일 확인해'))
 assert d['status']=='succeeded' and not b.calls
 result=d['assistant']['tool_result']
 assert result['source']=='room_hub_sqlite' and result['count']==result['returned']==1
 assert result['items'][0]['id']==one and not result['truncated']
 assert '완료 업무' not in d['response_json']['output']
 assert d['assistant']['capability']['handler']=='tasks.query'

def test_tomorrow_alias_brief_actual_data(hub):
 a,c,b=hub;today=c.get('/api/state').json()['today']
 tomorrow=(datetime.fromisoformat(today)+timedelta(days=1)).date().isoformat()
 add(c,'내일 A',tomorrow,'09:00');add(c,'내일 B',tomorrow);add(c,'오늘 C',today)
 for text in ['내일 뭐 해야 돼?','내일 할 일 브리핑해 줘.']:
  d=wait(c,request(c,text));out=d['response_json']['output']
  assert d['status']=='succeeded' and '내일 A' in out and '내일 B' in out and '오늘 C' not in out
  assert not b.calls and d['assistant']['tool_result']['range']==[tomorrow,tomorrow]

def test_no_data_vs_failure(hub,monkeypatch):
 a,c,b=hub
 empty=wait(c,request(c,'오늘 남은 할 일 보여줘'))
 assert empty['assistant']['tool_result']['ok'] and empty['assistant']['tool_result']['count']==0
 def fail(*a,**kw):raise RuntimeError('fixture read error')
 monkeypatch.setattr(a.state.store,'query_tasks',fail)
 broken=wait(c,request(c,'오늘 남은 할 일 보여줘'))
 assert broken['status']=='failed' and '0개' not in (broken.get('response_json') or {}).get('output','')

def test_truncation_explicit(hub):
 a,c,b=hub
 for i in range(54):add(c,f'작업{i:02}')
 d=wait(c,request(c,'오늘 할 일 알려줘'))
 result=d['assistant']['tool_result'];assert result['count']==54 and result['returned']==50 and result['truncated']
 assert '50개' in d['response_json']['output']

def test_common_query_service_same_results_as_ui(hub):
 a,c,b=hub;add(c,'A');add(c,'B')
 today=c.get('/api/state').json()['today']
 read=c.get('/api/assistant/tasks',params={'start':today,'end':today,'status':'all'})
 assert read.status_code==200
 assert {t['id'] for t in read.json()['items']}=={t['id'] for t in c.get('/api/state').json()['tasks']}
 assert c.get('/api/assistant/tasks',params={'start':today,'end':today,'status':'pending','limit':0}).status_code==422

def test_new_endpoints_permissions_and_catalog(hub):
 a,c,b=hub;anon=TestClient(a)
 assert anon.get('/api/clock').status_code==401
 assert anon.get('/api/assistant/capabilities').status_code==401
 cats=c.get('/api/assistant/capabilities').json()
 assert cats['unavailable']==['notes.write','alarms']
 assert all(v['confirmation'] for v in cats['capabilities'] if v['access']=='write')
 state=c.get('/api/state').json()
 assert state['clock']['today']==state['today'] and state['clock']['reference_at']==state['server_time']
 assert c.get('/api/clock').json()['clock_sync_verified'] is False
 assert c.get('/api/llm/config').json()['assistant_protocol']==PROTOCOL
 anon.close()

def test_fresh_reads_not_cached_response(hub):
 a,c,b=hub;tid=add(c,'진행 중')
 first=wait(c,request(c,'오늘 남은 할 일 보여줘'))
 c.patch('/api/tasks/'+tid+'/completion',json={'version':1,'completed':True})
 second=wait(c,request(c,'오늘 남은 할 일 보여줘'))
 assert first['assistant']['tool_result']['count']==1
 assert second['assistant']['tool_result']['count']==0
 assert c.get('/api/llm/requests/'+first['id']).json()['assistant']['tool_result']['count']==1

def test_model_iso_write_still_needs_approval(hub):
 a,c,b=hub;enable(c)
 tomorrow=c.get('/api/clock').json()['tomorrow']
 b.output=json.dumps({'widget':'todo','action':'add','target':None,'args':{'date':tomorrow,'title':'택배 보내기','time':None}})
 d=wait(c,request(c,'내일 택배 보내기 할 일로 등록해 줘'))
 assert d['status']=='awaiting_confirmation' and a.state.store.tasks()==[]
 assert d['assistant']['resolved_date']['evidence']=='내일'
 assert confirm(c,d).status_code==200
 assert a.state.store.tasks()[0]['date']==tomorrow

@pytest.mark.parametrize('text',[ '일어나 '*90, '가나다라마바사 '*20 ])
def test_repeated_model_output_not_published(hub,text):
 a,c,b=hub;enable(c);b.output=text;b.finish='length'
 d=wait(c,request(c,'좋은 습관을 추천해',mode='chat'))
 assert d['status']=='needs_clarification' and 'repeated_phrase' in d['assistant']['quality']['issues']
 assert '일어나 일어나' not in d['response_json']['output']
 assert d['assistant']['calls'][0]['result']['output']==text
 assert d['response_json']['metrics']['output_tokens']==12
 layout=c.get('/api/state').json()['layout'];layout['widgets'][0]['type']='llm-response';c.put('/api/layout',json=layout)
 dto=c.get('/api/display/llm/'+d['id']).json()
 assert dto['output']==d['response_json']['output'] and 'quality' not in dto
 assert text not in json.dumps(dto,ensure_ascii=False)

@pytest.mark.parametrize('text',['김치볶음밥과 카레를 추천합니다. 준비하기 쉽습니다.','월요일에는 공부하고 화요일에는 운동하세요.'])
def test_good_output_not_false_positive(text):
 assert assess(text,'저녁 메뉴 추천해','stop')['ok']

def test_chat_profile_and_exact_input_snapshot(hub):
 a,c,b=hub;enable(c);cfg=c.get('/api/llm/config').json()['config'];cfg.update(include_time_context=False,max_tokens=512,temperature=0.2);c.put('/api/llm/config',json=cfg)
 d=wait(c,request(c,'저녁 메뉴 추천해 줘',mode='chat'))
 body=b.calls[0]
 assert body['max_tokens']==128 and body['temperature']==.7 and body['presence_penalty']==1.0
 assert body['top_p']==.8 and body['top_k']==20 and body['min_p']==0
 assert '오늘=' not in body['messages'][0]['content']  # explicit chat honors include_time_context=False
 assert body==d['request_payload']==d['assistant']['calls'][0]['request_payload']
 assert d['assistant']['calls'][0]['result']['output']==b.output
 assert d['assistant']['source_received_at']

def test_time_reads_execution_clock(hub,monkeypatch):
 a,c,b=hub
 stamp='2026-09-20T03:12:15+00:00'
 monkeypatch.setattr('app.assistant.utcnow',lambda:stamp)
 d=wait(c,request(c,'지금 몇 시야?'))
 assert d['status']=='succeeded' and '12:12' in d['response_json']['output']
 assert d['assistant']['tool_result']['source']=='server_os_clock'

def test_stale_prepared_does_not_send(hub,monkeypatch):
 a,c,b=hub
 old=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
 monkeypatch.setattr('app.llm.utcnow',lambda:old)
 j=request(c,'저녁 메뉴 추천해',mode='chat');assert j['status']=='prepared'
 monkeypatch.undo();enable(c)
 assert c.post('/api/llm/requests/'+j['id']+'/send').status_code==200
 d=wait(c,j)
 assert d['status']=='needs_clarification' and d['assistant']['validation']=='stale_request' and not b.calls

def test_timezone_conflict_prevents_write(hub):
 a,c,b=hub
 d=wait(c,request(c,'내일 할 일에 택배 보내기 추가해'))
 settings=a.state.store.get('settings');settings['timezone']='UTC';a.state.store.set('settings',settings)
 assert confirm(c,d).status_code==409 and not a.state.store.tasks()

def test_midnight_confirmation_refused(hub,monkeypatch):
 a,c,b=hub;d=wait(c,request(c,'오늘 할 일에 자정 경계 추가해'))
 future=(datetime.fromisoformat(d['assistant']['reference_at'])+timedelta(days=1)).isoformat()
 monkeypatch.setattr('app.assistant.utcnow',lambda:future)
 assert confirm(c,d).status_code==409 and not a.state.store.tasks()


def test_today_time_uses_actual_clock(hub):
 a,c,b=hub
 d=wait(c,request(c,'오늘 몇 시야?'))
 assert d['status']=='succeeded' and ':' in d['response_json']['output']
 assert '서버 조회 시각' in d['response_json']['output']

def test_tomorrow_weekday_and_future_time_not_invented(hub):
 a,c,b=hub
 d=wait(c,request(c,'내일 무슨 요일이야?'))
 tomorrow=c.get('/api/clock').json()['tomorrow']
 assert d['status']=='succeeded' and tomorrow in d['response_json']['output']
 d=wait(c,request(c,'내일 몇 시야?'))
 assert d['status']=='needs_clarification' and not b.calls

def test_memory_hardware_question_not_routed_to_memo():
 assert detect('메모리 6GB 장점을 알려줘',AT,TZ)['route']=='chat'

def test_model_missing_day_cannot_change_tomorrow_to_now():
 r=detect('내일 무슨 요일이야?',AT,TZ)
 p=dict(r['proposal']);p['date_ref']=None
 assert grounded(p,r).date_ref=='내일'
