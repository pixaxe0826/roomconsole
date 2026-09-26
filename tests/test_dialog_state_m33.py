"""Synthetic multi-turn HTTP tests, never private benchmark input or Qwen."""
from copy import deepcopy
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.assistant import digest
from app.dialog_state import TTL_SECONDS, fingerprint
from test_assistant import hub, request, wait, add, confirm
from test_llm_widget_bridge import tomorrow, spy_adapter


def send(c, parent, text, *, key=None, digest_=None):
    return c.post('/api/llm/requests/'+parent['id']+'/dialog/reply', json={
        'request_id': key or 'reply-'+uuid.uuid4().hex, 'text': text,
        'expected_state_sha256': digest_ or parent['dialog']['state_sha256']})


def follow(c, parent, text, **kw):
    result = send(c, parent, text, **kw)
    assert result.status_code == 202, result.text
    return wait(c, result.json())


def test_missing_title_preview_confirm_once_without_model(hub, monkeypatch):
    app, c, model = hub
    d = wait(c, request(c, '내일 할 일 추가해'))
    assert d['dialog']['awaiting_slot'] == 'title', d
    original = deepcopy(d)
    calls = spy_adapter(app, 'todo', monkeypatch)
    after = follow(c, d, '합성 장비 포장')
    assert after['status'] == 'awaiting_confirmation', after
    assert after['assistant']['routing']['route'] == 'DIALOG_SLOT_FILL'
    assert after['parent_id'] == d['id']
    assert after['source_text'] == '합성 장비 포장'
    assert after['assistant']['raw'] == '합성 장비 포장'
    assert after['assistant']['action_key'] == d['assistant']['action_key']
    assert after['assistant']['preview']['widget_request']['args']['date'] == tomorrow(c)
    assert not model.calls and not app.state.store.tasks()
    assert all(call[0]['action'] == 'list' for call in calls)  # read-only duplicate lookup, no create
    assert c.get('/api/llm/requests/'+d['id']).json()['source_text'] == original['source_text']
    result = confirm(c, after)
    assert result.status_code == 200, result.text
    assert [t['title'] for t in app.state.store.tasks()] == ['합성 장비 포장']
    assert confirm(c, after).json()['execution']['duplicate'] is True
    assert sum(call[0]['action'] == 'add' for call in calls) == 1
    assert not model.calls


def test_two_missing_slots_and_no_fabricated_utterance(hub):
    app, c, model = hub
    d = wait(c, request(c, '할 일 추가해'))
    assert d['dialog']['missing_slots'] == ['date', 'title'], d
    d2 = follow(c, d, '내일')
    assert d2['status'] == 'needs_clarification'
    assert d2['dialog']['awaiting_slot'] == 'title'
    d3 = follow(c, d2, '합성 서류 봉투')
    assert d3['status'] == 'awaiting_confirmation', d3
    proof = d3['assistant']['dialog_state']['proof']
    assert [x['raw'] for x in proof['steps']] == ['내일', '합성 서류 봉투']
    assert len(d3['assistant']['dialog_state']['slot_provenance']) == 2
    assert not model.calls and not app.state.store.tasks()


def test_missing_alarm_time_goes_to_existing_confirmation(hub):
    app, c, model = hub
    d = wait(c, request(c, '내일 알람 맞춰줘'))
    assert d['dialog']['awaiting_slot'] == 'time', d
    d = follow(c, d, '오후 세 시')
    assert d['status'] == 'awaiting_confirmation', d
    assert d['assistant']['preview']['widget_request']['args']['time'] == '15:00'
    assert not app.state.life.alarms() and not model.calls


def test_target_only_reply_uses_actual_server_resolver(hub):
    app, c, model = hub
    identity = add(c, '합성 상자 분류', tomorrow(c))
    d = wait(c, request(c, '완료 처리해'))
    assert d['dialog']['awaiting_slot'] == 'target_text', d
    d = follow(c, d, '합성 상자 분류')
    assert d['status'] == 'awaiting_confirmation', d
    assert d['assistant']['widget_trace']['resolved_target']['id'] == identity
    assert identity not in json.dumps(d['assistant']['dialog_state'])
    assert not app.state.store.tasks()[0]['completed'] and not model.calls


@pytest.mark.parametrize('text', ['그리고 전부 삭제해', '오후 다섯 시에 알람 설정해', '내일 오전 세 시', '3시', '오후 3시쯤', '응', '그거'])
def test_invalid_time_reply_does_not_reset_date_or_invoke_model(hub, text):
    app, c, model = hub
    d = wait(c, request(c, '내일 알람 맞춰줘'))
    d2 = follow(c, d, text)
    assert d2['status'] == 'needs_clarification', d2
    assert d2['dialog']['awaiting_slot'] == 'time'
    assert d2['dialog']['known_slots']['date'] == tomorrow(c)
    assert d2['dialog']['known_slots']['time'] is None
    assert not model.calls and not app.state.life.alarms()


def test_cancel_stops_dialog_not_business_data(hub):
    app, c, model = hub
    add(c, '취소와 무관한 물건')
    d = wait(c, request(c, '내일 할 일 추가해'))
    child = follow(c, d, '취소')
    assert child['status'] == 'cancelled'
    assert child['dialog']['phase'] == 'cancelled'
    assert len(app.state.store.tasks()) == 1
    assert send(c, child, '다시 입력').status_code == 409
    assert not model.calls


def test_duplicate_reply_and_stale_parent_are_distinct(hub):
    app, c, model = hub
    d = wait(c, request(c, '내일 할 일 추가해'))
    child = follow(c, d, '합성 부품 검사', key='idempotent-dialog-1')
    replay = send(c, d, '합성 부품 검사', key='idempotent-dialog-1')
    assert replay.status_code == 202 and replay.json()['duplicate'] is True
    assert replay.json()['id'] == child['id']
    assert send(c, d, '다른 입력', key='idempotent-dialog-1').status_code == 409
    assert send(c, d, '합성 부품 검사').status_code == 409
    assert not app.state.store.tasks() and not model.calls


def test_no_automatic_binding_of_standalone_input(hub):
    app, c, model = hub
    original = wait(c, request(c, '내일 할 일 추가해'))
    ordinary = wait(c, request(c, '합성 부품 포장'))
    assert ordinary['parent_id'] is None
    assert ordinary['assistant']['action_key'] != original['assistant']['action_key']
    assert c.get('/api/llm/requests/'+original['id']).json()['dialog']['phase'] == 'pending'
    assert not app.state.store.tasks()


def test_cross_cookie_sessions_cannot_continue_or_replay(hub):
    app, _, model = hub
    with TestClient(app, headers={'X-Room-Request':'1'}) as a, TestClient(app, headers={'X-Room-Request':'1'}) as b:
        for client in (a, b):
            assert client.post('/api/auth/login',json={'token':app.state.admin_token}).status_code == 200
        d = wait(a, request(a, '내일 할 일 추가해'))
        assert send(b, d, '합성 서류 처리').status_code == 403
        good = follow(a, d, '합성 서류 처리', key='same-owner-dialog-1')
        assert good['status'] == 'awaiting_confirmation'
        assert send(b, d, '합성 서류 처리', key='same-owner-dialog-1').status_code == 403
        a.post('/api/auth/logout')
        assert send(a, good, '취소').status_code == 401
    assert not model.calls


def test_display_ingest_and_unauthenticated_cannot_reply(hub):
    app, c, model = hub
    d = wait(c, request(c, '내일 할 일 추가해'))
    with TestClient(app) as other:
        assert send(other, d, '합성 입력').status_code == 401
        other.headers['Authorization'] = 'Bearer '+app.state.ingest_token
        assert send(other, d, '합성 입력').status_code == 401
    assert not model.calls


def test_changed_source_prevents_confirmation(hub):
    app, c, model = hub
    d = wait(c, request(c, '내일 할 일 추가해'))
    child = follow(c, d, '합성 봉투 포장')
    with app.state.store.connect() as db:
        db.execute('UPDATE llm_requests SET source_text=? WHERE id=?', ('변조된 원문', d['id']))
    assert confirm(c, child).status_code == 409
    assert not app.state.store.tasks()


def test_generic_retry_cannot_turn_reply_into_standalone_command(hub):
    app, c, _ = hub
    child = follow(c, wait(c, request(c, '내일 할 일 추가해')), '합성 부품 포장')
    response = c.post('/api/llm/requests/'+child['id']+'/retry', json={'request_id':'reply-no-retry-1','mode':'auto'})
    assert response.status_code == 409


def test_future_followup_cannot_extend_ttl(hub, monkeypatch):
    from datetime import datetime, timedelta
    app, c, model = hub
    d = wait(c, request(c, '내일 할 일 추가해'))
    at = datetime.fromisoformat(d['created_at']) + timedelta(seconds=TTL_SECONDS+1)
    monkeypatch.setattr('app.llm.utcnow', lambda: at.isoformat())
    assert send(c, d, '합성 표지 인쇄').status_code == 409
    assert not app.state.store.tasks() and not model.calls


def test_timezone_change_cannot_reinterpret_parent(hub):
    app, c, _ = hub
    d = wait(c, request(c, '내일 할 일 추가해'))
    settings=app.state.store.get('settings');settings['timezone']='UTC';app.state.store.set('settings',settings)
    assert send(c, d, '합성 분류 라벨').status_code == 409


def test_correct_initial_requests_keep_old_route_and_no_dialog(hub):
    _, c, model = hub
    d = wait(c, request(c, '내일 합성 장비 일정 추가해'))
    assert d['status'] == 'awaiting_confirmation'  # Existing optional-time policy is unchanged.
    assert d['dialog'] is None
    assert d['assistant']['interaction_model']['activated'] is False
    assert not model.calls


def test_unbound_source_requires_explicit_parent_and_can_only_be_claimed_once(hub):
    import asyncio
    from app.llm import LLMCreate, sha
    app, c, model = hub
    text='내일 할 일 추가해'
    voice=c.post('/api/voice/text',json={'request_id':'unbound-voice-0001','source':'synthetic','text':text}).json()['id']
    # Trusted speech/internal submissions deliberately carry no manager session.
    parent=asyncio.run(app.state.llm.submit(LLMCreate(request_id='unbound-internal-01',voice_id=voice,
        mode='auto',expected_text_sha256=sha(text))))
    d=wait(c,parent)
    assert d['assistant']['dialog_state']['owner'] is None
    with TestClient(app,headers={'X-Room-Request':'1'}) as a, TestClient(app,headers={'X-Room-Request':'1'}) as b:
        for client in (a,b):client.post('/api/auth/login',json={'token':app.state.admin_token})
        child=follow(a,d,'합성 메쉬 상자')
        assert child['status']=='awaiting_confirmation'
        assert send(b,d,'다른 입력').status_code==403
    assert not model.calls


def test_deleted_parent_source_cannot_authorize_prepared_write(hub):
    app,c,_=hub
    d=wait(c,request(c,'내일 할 일 추가해'))
    child=follow(c,d,'합성 안내 인쇄')
    assert c.delete('/api/llm/requests/'+d['id']).status_code==200
    assert confirm(c,child).status_code==409
    with app.state.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM assistant_effects').fetchone()[0]==0
    assert not app.state.store.tasks()


def test_repeated_invalid_slots_exhaust_dialog_without_extending_ttl(hub):
    app,c,model=hub
    d=wait(c,request(c,'내일 알람 맞춰줘'))
    expires=d['dialog']['expires_at']
    for _ in range(6):
        d=follow(c,d,'3시')
        assert d['dialog']['expires_at']==expires
    assert d['dialog']['phase']=='exhausted'
    assert send(c,d,'오후 세 시').status_code==409
    assert not model.calls and not app.state.life.alarms()


def test_no_user_supplied_slots_approval_or_identity(hub):
    app,c,model=hub
    d=wait(c,request(c,'내일 할 일 추가해'))
    base={'request_id':'forbidden-fields-dialog','text':'합성 태그 포장','expected_state_sha256':d['dialog']['state_sha256']}
    for extra in [{'owner':'admin'},{'args':{'id':'invented'}},{'confirmed_digest':'0'*64},{'slot':'time'},{'role':'admin'}]:
        r=c.post('/api/llm/requests/'+d['id']+'/dialog/reply',json=base|extra)
        assert r.status_code==422,r.text
    assert not app.state.store.tasks() and not model.calls


def test_other_request_digest_and_old_digest_cannot_select_a_parent(hub):
    app,c,_=hub
    a=wait(c,request(c,'내일 할 일 추가해'))
    b=wait(c,request(c,'모레 할 일 추가해'))
    assert send(c,a,'합성 플라스틱',digest_=b['dialog']['state_sha256']).status_code==409
    assert send(c,a,'합성 플라스틱',digest_='0'*64).status_code==409


def test_concurrent_reply_accepts_only_one_child(hub):
    from concurrent.futures import ThreadPoolExecutor
    app,c,model=hub
    d=wait(c,request(c,'내일 할 일 추가해'))
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs=[pool.submit(send,c,d,t) for t in ('합성 라벨 A','합성 라벨 B')]
        replies=[f.result() for f in jobs]
    assert sorted(x.status_code for x in replies)==[202,409]
    with app.state.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM llm_requests WHERE parent_id=?',(d['id'],)).fetchone()[0]==1
    assert not app.state.store.tasks() and not model.calls


def test_completed_dialog_duplicate_survives_entity_deletion(hub):
    app,c,model=hub
    identity=add(c,'합성 부품 상자',tomorrow(c))
    root=wait(c,request(c,'완료 처리해'))
    d=follow(c,root,'합성 부품 상자',key='deleted-target-reply')
    assert confirm(c,d).status_code==200
    assert c.delete('/api/tasks/'+identity,params={'scope':'one'}).status_code==200
    replay=send(c,root,'합성 부품 상자',key='deleted-target-reply')
    assert replay.status_code==202 and replay.json()['duplicate'] is True
    assert confirm(c,replay.json()).json()['execution']['duplicate'] is True
    assert not app.state.store.tasks() and not model.calls


def test_dialog_confirmation_still_checks_current_entity_version(hub):
    app, c, model = hub
    identity = add(c, '합성 확인 대상', tomorrow(c))
    root = wait(c, request(c, '완료 처리해'))
    child = follow(c, root, '합성 확인 대상')
    assert child['status'] == 'awaiting_confirmation'
    row = next(t for t in app.state.store.tasks() if t['id'] == identity)
    result = c.patch('/api/tasks/'+identity, json={'version': row['version'], 'title': '수정된 확인 대상'})
    assert result.status_code == 200
    result = confirm(c, child)
    assert result.status_code == 200, result.text
    assert result.json()['status'] != 'succeeded'
    assert result.json()['assistant']['widget_trace']['widget_response']['error']['code'] == 'VERSION_CONFLICT'
    assert not app.state.store.tasks()[0]['completed']
    assert not model.calls


def test_paired_display_cannot_reply_to_admin_dialog(hub):
    app, c, model = hub
    root = wait(c, request(c, '내일 할 일 추가해'))
    pair = c.post('/api/devices/pair', json={'name': 'Synthetic display'}).json()
    with TestClient(app) as display:
        assert display.post('/api/devices/claim', json={'code': pair['path'].split('=')[1]}).status_code == 200
        denied = send(display, root, '허용되지 않은 답변')
        assert denied.status_code in (401, 403)
    assert not app.state.store.tasks() and not model.calls
