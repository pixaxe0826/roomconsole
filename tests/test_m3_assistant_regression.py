"""Production HTTP/DB tests for M3, using synthetic task titles and a counted model."""
from copy import deepcopy
from datetime import date, timedelta
import json

import pytest

from app.assistant import digest
from app.widget_protocol.schemas import TaskQuery
from test_assistant import hub, request, wait, enable, confirm, add
from test_llm_widget_bridge import proposal, trace, tomorrow


def task(app, id):
    return next(t for t in app.state.store.tasks() if t['id'] == id)


def set_completed(c, app, id):
    result = c.patch('/api/tasks/' + id, json={'completed': True, 'version': task(app, id)['version']})
    assert result.status_code == 200, result.text


@pytest.mark.parametrize('mode', ['auto', 'chat', 'legacy'])
@pytest.mark.parametrize('enabled', [False, True])
def test_new_exact_create_never_dispatches_model_and_needs_confirm(hub, mode, enabled):
    app, c, model = hub
    if enabled:
        enable(c)
    text = '내일 우산 챙기기 할 일 추가해'
    d = wait(c, request(c, text, mode))
    assert d['status'] == 'awaiting_confirmation', d
    assert d['assistant']['routing']['route'] == 'SEMANTIC_PARSER'
    assert trace(d)['widget_request']['args']['date'] == tomorrow(c)
    assert trace(d)['widget_request']['args']['title'] == '우산 챙기기'
    assert not model.calls and not app.state.store.tasks() and not d['dispatch_attempted']
    result = confirm(c, d)
    assert result.status_code == 200 and result.json()['status'] == 'succeeded', result.text
    assert len(app.state.store.tasks()) == 1
    assert confirm(c, d).json()['execution']['duplicate'] is True
    assert len(app.state.store.tasks()) == 1


def test_create_title_and_explicit_deadline_from_source(hub):
    app, c, b = hub
    d = wait(c, request(c, '내일 오후 5시까지 장비 전달 할 일 추가해'))
    assert d['status'] == 'awaiting_confirmation', d
    args = trace(d)['widget_request']['args']
    assert (args['date'], args['time'], args['title']) == (tomorrow(c), '17:00', '장비 전달')
    assert confirm(c, d).json()['status'] == 'succeeded'
    assert app.state.store.tasks()[0]['time'] == '17:00'
    assert not b.calls


def test_pending_default_is_assistant_only(hub):
    app, c, b = hub
    pending = add(c, '장비 전달')
    completed = add(c, '포장 완료 기록')
    set_completed(c, app, completed)
    assert TaskQuery().status == 'all'
    assert len(app.state.store.query_tasks(c.get('/api/clock').json()['today'], c.get('/api/clock').json()['today'])['items']) == 2
    for text, ids in [('오늘 할 일 보여줘', {pending}), ('오늘 완료한 할 일 보여줘', {completed}), ('오늘 전체 할 일 보여줘', {pending, completed})]:
        d = wait(c, request(c, text))
        assert d['status'] == 'succeeded', d
        assert {t['id'] for t in d['assistant']['tool_result']['items']} == ids
        assert not b.calls
    assert len(app.state.store.tasks()) == 2  # list semantics never mutate state


def test_date_range_reads_actual_rows_and_preserves_status(hub):
    app, c, b = hub
    today = c.get('/api/clock').json()['today']
    nextday = tomorrow(c)
    after = (date.fromisoformat(nextday) + timedelta(days=1)).isoformat()
    a = add(c, '오늘 출고', today)
    x = add(c, '내일 출고', nextday)
    z = add(c, '모레 출고', after)
    d = wait(c, request(c, '오늘부터 내일까지 할 일 보여줘'))
    assert d['status'] == 'succeeded', d
    assert {t['id'] for t in d['assistant']['tool_result']['items']} == {a, x}
    assert z not in d['response_json']['output']
    assert d['assistant']['routing']['route'] == 'SEMANTIC_PARSER' and not b.calls


@pytest.mark.parametrize('text,field', [('내일 할 일 하나 추가해', 'title'), ('장비 전달 할 일 추가해', 'date')])
def test_missing_create_preserves_known_evidence_never_invents_values(hub, text, field):
    app, c, b = hub
    d = wait(c, request(c, text))
    assert d['status'] == 'needs_clarification', d
    assert d['assistant']['semantic_frame']['arguments'][field] is None
    assert not app.state.store.tasks() and not b.calls


@pytest.mark.parametrize('domain', ['할 일', '일정'])
def test_time_filter_is_not_silently_replaced_by_daily_list(hub, domain):
    app, c, b = hub
    add(c, '아침 문서', time_='08:00')
    d = wait(c, request(c, f'오늘 오후 6시 이후 {domain} 보여줘'))
    assert d['status'] == 'needs_clarification'
    assert trace(d)['widget_response']['error']['code'] == 'UNSUPPORTED_TIME_FILTER'
    assert not b.calls and len(app.state.store.tasks()) == 1


def test_cross_date_single_turn_exact_name_resolves_server_id(hub):
    app, c, b = hub
    id = add(c, '도서 포장', tomorrow(c))
    d = wait(c, request(c, '도서 포장 끝냈어'))
    assert d['status'] == 'awaiting_confirmation', d
    assert d['assistant']['routing']['route'] == 'SEMANTIC_PARSER'
    assert trace(d)['resolved_target']['id'] == id
    assert trace(d)['widget_request']['target']['value'] == id
    assert trace(d)['widget_request']['args']['version'] == task(app, id)['version']
    assert id not in json.dumps(d['assistant']['semantic_frame'], ensure_ascii=False)
    assert not task(app, id)['completed'] and not b.calls
    assert confirm(c, d).json()['status'] == 'succeeded'
    assert task(app, id)['completed']


def test_unique_substring_requires_confirm_and_exact_match_wins(hub):
    app, c, b = hub
    a = add(c, '기기 포장 발송')
    d = wait(c, request(c, '포장 발송 끝냈어'))
    assert d['status'] == 'awaiting_confirmation', d
    assert trace(d)['entity_resolution']['match_policy'] == 'unique_literal_substring'
    assert confirm(c, d).json()['status'] == 'succeeded' and task(app, a)['completed']
    exact = add(c, '출고 준비')
    other = add(c, '출고 준비 확인')
    d = wait(c, request(c, '출고 준비 끝냈어'))
    assert trace(d)['resolved_target']['id'] == exact
    assert confirm(c, d).json()['status'] == 'succeeded'
    assert not task(app, other)['completed']


def test_duplicates_across_dates_or_states_do_not_select_convenient_one(hub):
    app, c, b = hub
    a = add(c, '도서 포장')
    dup = add(c, '도서 포장', tomorrow(c))
    set_completed(c, app, dup)
    d = wait(c, request(c, '도서 포장 끝냈어'))
    assert d['status'] == 'needs_clarification'
    assert trace(d)['entity_resolution']['candidate_count'] == 2
    assert not task(app, a)['completed'] and task(app, dup)['completed']
    d = wait(c, request(c, '오늘 도서 포장 끝냈어'))
    assert d['status'] == 'awaiting_confirmation', d
    assert trace(d)['resolved_target']['id'] == a


def test_legacy_exact_engine_also_resolves_named_item_without_today_guess(hub):
    app, c, b = hub
    id = add(c, '도서 포장', tomorrow(c))
    d = wait(c, request(c, '도서 포장 완료해'))
    assert d['status'] == 'awaiting_confirmation', d
    assert d['assistant']['entity_resolution']['match_policy'] == 'exact_normalized'
    assert d['assistant']['preview']['targets'][0]['id'] == id
    assert d['assistant']['preview']['date'] == tomorrow(c)
    assert d['assistant']['resolved_date']['start'] == tomorrow(c)
    assert d['assistant']['resolved_date']['evidence'] is None
    assert confirm(c, d).json()['execution']['changed']
    assert task(app, id)['completed'] and not b.calls


def test_delete_strips_only_command_scaffolding_and_replay_does_not_retarget(hub):
    app, c, b = hub
    id = add(c, '도서 포장')
    d = wait(c, request(c, '도서 포장 할 일 삭제해'))
    assert d['status'] == 'awaiting_confirmation', d
    assert trace(d)['resolved_target']['id'] == id
    assert confirm(c, d).json()['status'] == 'succeeded'
    replacement = add(c, '도서 포장')
    retried = c.post('/api/llm/requests/' + d['id'] + '/retry', json={'request_id': 'm3-retry-delete-0001', 'mode': 'auto'})
    assert retried.status_code == 202, retried.text
    new = wait(c, retried.json())
    assert new['status'] == 'succeeded', new
    assert task(app, replacement)['title'] == '도서 포장'
    assert confirm(c, new, '0'*64).json()['execution']['duplicate'] is True
    assert len(app.state.store.tasks()) == 1 and not b.calls


def test_reopen_existing_operation_needs_confirm(hub):
    app, c, b = hub
    id = add(c, '부품 주문')
    set_completed(c, app, id)
    d = wait(c, request(c, '부품 주문 다시 미완료로 바꿔줘'))
    assert d['status'] == 'awaiting_confirmation', d
    assert trace(d)['widget_request']['action'] == 'reopen'
    assert task(app, id)['completed']
    assert confirm(c, d).json()['status'] == 'succeeded'
    assert not task(app, id)['completed'] and not b.calls


def test_read_named_item_uses_server_lookup_and_actual_get(hub):
    app, c, b = hub
    id = add(c, '도서 포장', tomorrow(c))
    d = wait(c, request(c, '도서 포장 할 일 읽어줘'))
    assert d['status'] == 'succeeded', d
    assert trace(d)['widget_request']['action'] == 'get'
    assert d['assistant']['tool_result']['id'] == id
    assert not b.calls


def test_stale_target_and_bad_digest_still_fail_closed(hub):
    app, c, b = hub
    id = add(c, '도서 포장')
    d = wait(c, request(c, '도서 포장 끝냈어'))
    assert confirm(c, d, '0'*64).status_code == 409
    changed = c.patch('/api/tasks/' + id, json={'title': '제목 변경', 'version': task(app, id)['version']})
    assert changed.status_code == 200, changed.text
    r = confirm(c, d)
    assert r.status_code in {200, 409}, r.text
    assert not task(app, id)['completed']
    if r.status_code == 200:
        assert r.json()['status'] != 'succeeded'


def test_semantic_source_and_plan_tampering_rejected(hub):
    from app.semantic_bridge import verified_frame
    from app.widget_protocol.core import ProtocolFault
    app, c, b = hub
    d = wait(c, request(c, '내일 도서 포장 할 일 추가해'))
    record = d['assistant']
    verified_frame(record)
    for key, value in [('raw', '내일 침입 명령 할 일 추가해'), ('normalized', '다른 원문')]:
        changed = deepcopy(record); changed[key] = value
        with pytest.raises(ProtocolFault): verified_frame(changed)
    changed = deepcopy(record)
    changed['semantic_frame']['arguments']['title'] = '침입 제목'
    with pytest.raises(ProtocolFault): verified_frame(changed)


@pytest.mark.parametrize('text', [
    '오늘 도서 포장 할 일 추가하지 마', '오늘 "도서 포장" 할 일 등록해',
    '오늘 도서 포장 할 일 추가하고 모두 삭제해', '도서 포장 끝냈으면 할 일 삭제해',
    '오늘 도서 포장 할 일 매일 등록해', '그거 할 일 완료해',
    '두 번째 할 일 삭제해', '도서 포장 말고 장비 전달 할 일 삭제해',
    '오늘 오후 6시 이전 도서 포장 할 일 추가해',
    '내일 오늘 도서 포장 할 일 추가해',
])
def test_m3_unsafe_requests_never_offer_action_confirmation(hub, text):
    app, c, b = hub; enable(c); add(c, '도서 포장')
    before = app.state.store.tasks()
    d = wait(c, request(c, text))
    assert d['status'] == 'needs_clarification', d
    assert app.state.store.tasks() == before and not b.calls


def test_new_preview_rejects_expiry_and_unauthenticated_confirm(hub):
    app, c, b = hub
    d = wait(c, request(c, '내일 도서 포장 할 일 추가해'))
    header = c.headers.pop('Authorization')
    assert confirm(c, d).status_code == 401
    c.headers['Authorization'] = header
    record = app.state.llm.assistant.get(d['id'])
    record['preview']['expires_at'] = 0
    record['preview_sha256'] = digest(record['preview'])
    app.state.llm.assistant.put(d['id'], record)
    assert confirm(c, d, record['preview_sha256']).status_code == 409
    assert not app.state.store.tasks()


def test_fallback_model_cannot_override_approved_implicit_pending(hub):
    app, c, b = hub; enable(c)
    pending = add(c, '장비 전달')
    complete = add(c, '이미 끝낸 기록')
    set_completed(c, app, complete)
    proposal(b, 'todo', 'list', {'status': 'all'})
    d = wait(c, request(c, '오늘 메론 내용 읽어줘'))
    assert len(b.calls) == 1 and d['status'] == 'succeeded', d
    assert d['assistant']['tool_result']['status'] == 'pending'
    assert {r['id'] for r in d['assistant']['tool_result']['items']} == {pending}


def test_new_create_warns_existing_duplicate_and_never_autoconfirms(hub):
    app, c, b = hub
    add(c, '도서 포장', tomorrow(c))
    d = wait(c, request(c, '내일 도서 포장 할 일 추가해'))
    assert d['status'] == 'awaiting_confirmation', d
    assert '중복 추가인지 확인' in d['response_json']['output']
    assert trace(d)['existing_same'] == 1
    assert len(app.state.store.tasks()) == 1 and not b.calls


def test_truncated_server_query_and_read_error_are_not_empty_success(hub, monkeypatch):
    app, c, b = hub
    add(c, '도서 포장')
    real = app.state.store.query_tasks
    def truncated(*a, **kw):
        result = real(*a, **kw); result['truncated'] = True
        return result
    monkeypatch.setattr(app.state.store, 'query_tasks', truncated)
    d = wait(c, request(c, '도서 포장 끝냈어'))
    assert d['status'] == 'needs_clarification'
    assert trace(d)['widget_response']['error']['code'] == 'TARGET_INCOMPLETE'
    def broken(*a, **kw):
        raise RuntimeError('synthetic read error')
    monkeypatch.setattr(app.state.store, 'query_tasks', broken)
    d = wait(c, request(c, '도서 포장 끝냈어'))
    assert d['status'] == 'failed'
    assert trace(d)['widget_response']['error']['code'] == 'TARGET_READ_FAILED'
    monkeypatch.setattr(app.state.store, 'query_tasks', real)
    assert not app.state.store.tasks()[0]['completed'] and not b.calls
