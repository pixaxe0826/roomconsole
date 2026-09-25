"""Synthetic M3.1 contracts, not private benchmark cases or empirical recovery claims."""
from copy import deepcopy

import pytest

from app.assistant import normalize
from app.semantic_parser import parse_semantic
from app.semantic_types import semantic_decision
from app.semantic_temporal import extract_temporal, TemporalError
from app.entity_resolver import resolve_target
from test_assistant import hub, request, wait, add, confirm, enable
from test_llm_widget_bridge import proposal, trace, tomorrow

AT = '2028-02-28T09:00:00+09:00'
TZ = 'Asia/Seoul'


def parse(text):
    return parse_semantic(normalize(text)[0], AT, TZ, raw=text)


@pytest.mark.parametrize('clock', ['3시쯤', '오후 세시쯤', '오후 3시경', '17:00 정도',
                                  '대략 오후 5시', '약 17:00', '저녁 무렵'])
def test_approximate_time_cannot_become_create_title(clock):
    f = parse(f'내일 {clock} 검사 도구 포장 할 일 추가해')
    assert f and f.confidence != 'EXACT'
    assert semantic_decision(f)['outcome'] == 'AMBIGUOUS'
    assert dict(f.arguments)['date'] == '2028-02-29'
    assert dict(f.arguments)['time'] is None
    assert f.temporal.evidence


@pytest.mark.parametrize('filler', ['좀', '하나', '하나 좀', '좀 하나', '한 개만', '하나만 좀'])
def test_scaffold_is_not_a_title(filler):
    f = parse(f'내일 할 일 {filler} 추가해')
    assert f.confidence == 'MISSING' and dict(f.arguments)['title'] is None
    assert dict(f.arguments)['date'] == '2028-02-29'
    assert semantic_decision(f)['outcome'] == 'PARTIAL'
    assert semantic_decision(f)['reason_code'] == 'MISSING_REQUIRED_ARGUMENT'


def test_filler_inside_real_literal_title_is_preserved():
    f = parse('내일 좀 더 읽기 할 일 추가해')
    assert f.confidence == 'EXACT'
    assert dict(f.arguments)['title'] == '좀 더 읽기'


@pytest.mark.parametrize('ending,action', [('완료했어', 'complete'), ('완료했어요', 'complete'),
                                        ('다 했어', 'complete'), ('다 했어요', 'complete'),
                                        ('없애줘', 'delete'), ('지워주세요', 'delete')])
def test_clear_action_variants_preserve_target(ending, action):
    f = parse('검사 도구 포장 ' + ending)
    assert f.confidence == 'EXACT' and f.action == action
    assert f.target_text == '검사 도구 포장'
    assert f.proposal()['target'] is None


@pytest.mark.parametrize('text,state', [
    ('할 일 내일 완료한 것 보여줘', 'completed'),
    ('할 일 내일 남은 것 보여줘', 'pending'),
    ('오늘부터 내일까지 남은 할 일 뭐 있어', 'pending'),
    ('할 일 2월 28일부터 29일까지 남은 목록 보여줘', 'pending'),
    ('할 일 내일 남은 항목들만 알려줘', 'pending'),
])
def test_collection_read_is_not_named_item_lookup(text, state):
    f = parse(text)
    assert f.confidence == 'EXACT' and f.action == 'list', f
    assert dict(f.arguments)['status'] == state and not f.target_text
    assert f.temporal.start and f.temporal.end


@pytest.mark.parametrize('text', [
    '내일 오늘 검사 도구 포장 할 일 추가해',
    '내일 검사 도구 포장 할 일 추가하지 마',
    '검사 도구 포장 다 했으면 할 일 삭제해',
    '검사 도구 포장 완료했어 그리고 장비 출고 삭제해',
    '그거 다 했어', '전체 할 일 다 했어',
    '내일 오후 5시 이후 검사 도구 포장 할 일 추가해',
    '할 일 내일 모르는 조건 전체 보여줘',
])
def test_no_new_unsafe_exact_plans(text):
    f = parse(text)
    assert f is None or f.confidence != 'EXACT', f


@pytest.mark.parametrize('text', ['내일 5시', '내일 25시', '내일 13:78', '내일 오후 5시 오전'])
def test_temporal_error_retains_known_date_not_invalid_clock(text):
    with pytest.raises(TemporalError) as caught:
        extract_temporal(text, AT, TZ)
    assert caught.value.partial.start == '2028-02-29'
    assert all(e.text in text for e in caught.value.partial.evidence)


@pytest.mark.parametrize('text', ['2월 28일부터 29일까지', '오늘부터 내일까지', '2월 28~29일'])
def test_full_range_consumed_before_single_date(text):
    f, left = extract_temporal(text, AT, TZ)
    assert (f.start, f.end) == ('2028-02-28', '2028-02-29') and not left.strip()


def test_ambiguous_dates_do_not_retain_one_arbitrary_date():
    with pytest.raises(TemporalError) as caught:
        extract_temporal('오늘 내일', AT, TZ)
    assert caught.value.partial.start is None


def test_outside_grammar_is_a_candidate_not_invented_missing_user_argument():
    assert parse('내일 할 일에 검사 도구 포장 등록해 줄래') is None
    assert semantic_decision(None)['outcome'] == 'NO_MATCH'
    assert semantic_decision(None)['next_step'] == 'existing_guarded_route'


@pytest.mark.parametrize('text', ['내일 3시쯤 검사 도구 포장 할 일 추가해', '내일 할 일 좀 추가해'])
def test_unsafe_or_missing_create_does_not_offer_confirmation(hub, text):
    app, c, model = hub; enable(c)
    d = wait(c, request(c, text))
    assert d['status'] == 'needs_clarification', d
    assert not model.calls and not app.state.store.tasks()
    assert d['assistant']['semantic_frame']['arguments']['date'] == tomorrow(c)
    assert trace(d)['execution_eligibility']['decision'] == 'BLOCKED'


def test_uncertain_grammar_keeps_existing_constrained_fallback(hub):
    app, c, model = hub; enable(c)
    # Outside deterministic grammar. No raw-model authority is added.
    proposal(model, 'todo', 'add', {'date': tomorrow(c), 'title': '검사 도구 포장'})
    d = wait(c, request(c, '내일 할 일에 검사 도구 포장 등록해 줄래'))
    assert len(model.calls) == 1
    assert d['assistant']['routing']['route'] == 'LLM_FALLBACK'
    assert d['assistant']['semantic_parser_attempt']['outcome'] == 'NO_MATCH'
    assert not app.state.store.tasks()


def test_collection_read_executes_real_query_not_target_clarification(hub):
    app, c, model = hub
    identity = add(c, '검사 도구 포장', tomorrow(c))
    d = wait(c, request(c, '할 일 내일 남은 항목들만 보여줘'))
    assert d['status'] == 'succeeded', d
    assert trace(d)['widget_request']['action'] == 'list'
    assert {r['id'] for r in d['assistant']['tool_result']['items']} == {identity}
    assert not model.calls


def test_new_mutation_variant_preserves_confirmation_scope_and_receipt(hub):
    app, c, model = hub
    identity = add(c, '검사 도구 포장', tomorrow(c))
    d = wait(c, request(c, '검사 도구 포장 완료했어'))
    assert d['status'] == 'awaiting_confirmation', d
    assert not app.state.store.tasks()[0]['completed']
    assert trace(d)['entity_resolution']['selection_scope']['date_explicit'] is False
    assert trace(d)['resolved_target']['id'] == identity
    proof = trace(d)['field_provenance']
    assert proof['target_text'] == proof['resolved_title'] == '검사 도구 포장'
    assert trace(d)['execution_eligibility']['decision'] == 'READY_FOR_EXISTING_POLICY'
    assert confirm(c, d).json()['status'] == 'succeeded'
    assert confirm(c, d).json()['execution']['duplicate'] is True
    assert not model.calls


def test_duplicate_server_identity_is_not_unique_evidence():
    r = {'id': 'test1', 'title': '검사 도구 포장', 'version': 1}
    with pytest.raises(ValueError, match='Duplicate'):
        resolve_target([r, deepcopy(r)], r['title'])
