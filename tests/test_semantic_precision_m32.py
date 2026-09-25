"""Synthetic M3.2 precision tests; no private benchmark cases or model data."""
import pytest

from app.assistant import normalize
from app.semantic_parser import parse_semantic
from app.semantic_temporal import extract_temporal
from test_assistant import hub, request, wait, enable

AT = '2028-02-28T09:00:00+09:00'
TZ = 'Asia/Seoul'


def parse(text):
    return parse_semantic(normalize(text)[0], AT, TZ, raw=text)


@pytest.mark.parametrize('text,date', [
    ('모레 스케줄 뭐 잡혀 있어?', '2028-03-01'),
    ('내일 일정 어떤 게 잡혀 있어?', '2028-02-29'),
    ('오늘 무슨 일정 있어?', '2028-02-28'),
])
def test_collection_question_residue_stays_list(text, date):
    f = parse(text)
    assert f and f.confidence == 'EXACT' and f.widget == 'calendar' and f.action == 'list', f
    assert dict(f.arguments)['start'] == date
    assert f.target_text is None


def test_literal_named_calendar_item_still_becomes_get():
    f = parse('모레 검사 장비 일정 보여줘')
    assert f and f.confidence == 'EXACT' and f.action == 'get'
    assert f.target_text == '검사 장비'


def test_bounded_daypart_range_is_afternoon_and_collection_list():
    f = parse('모레 낮부터 저녁 전까지 스케줄 뭐 잡혀 있어?')
    assert f and f.confidence == 'EXACT' and f.action == 'list', f
    args = dict(f.arguments)
    assert args['start'] == args['end'] == '2028-03-01'
    assert args['period'] == 'afternoon'
    assert f.temporal.period == 'afternoon'
    assert any(e.policy == 'bounded_daypart_range' for e in f.temporal.evidence)


@pytest.mark.parametrize('text', [
    '내일 할 일 중 샘플표식 들어간 거 있어?',
    '내일 샘플표식 포함된 할 일 뭐 있어?',
    '내일 샘플표식 관련 할 일 알려줘',
])
def test_search_constraint_is_not_weakened_to_list_or_get(text):
    f = parse(text)
    assert f and f.confidence == 'UNSUPPORTED', f
    assert f.issue == 'UNSUPPORTED_SEARCH_FILTER'


def test_cross_domain_read_is_explicitly_blocked_not_partially_executed():
    f = parse('내일 해야 할 거하고 오후 일정 같이 알려줘')
    assert f and f.confidence == 'UNSUPPORTED', f
    assert f.issue == 'MULTI_INTENT_UNSUPPORTED'


@pytest.mark.parametrize('text', [
    '모레부터 다음 주까지 일정 뭐 있어?',
    '내일 이후 일정 뭐 있어?',
])
def test_unconsumed_read_range_cannot_turn_into_named_get(text):
    f = parse(text)
    assert f is None or f.confidence != 'EXACT' or f.action != 'get', f


def test_search_constraint_integration_never_calls_model_or_adapter(hub):
    app, c, model = hub
    enable(c)
    d = wait(c, request(c, '내일 할 일 중 합성표식 들어간 거 있어?'))
    assert d['status'] == 'needs_clarification', d
    assert not model.calls
    assert d['assistant']['semantic_frame']['issue'] == 'UNSUPPORTED_SEARCH_FILTER'
    wt = d['assistant']['widget_trace']
    assert wt['execution_eligibility']['decision'] == 'BLOCKED'
    assert wt.get('widget_request') is None


def test_multi_intent_integration_never_calls_model_or_adapter(hub):
    app, c, model = hub
    enable(c)
    d = wait(c, request(c, '내일 해야 할 거하고 오후 일정 같이 알려줘'))
    assert d['status'] == 'needs_clarification', d
    assert not model.calls
    assert d['assistant']['semantic_frame']['issue'] == 'MULTI_INTENT_UNSUPPORTED'
    wt = d['assistant']['widget_trace']
    assert wt['execution_eligibility']['decision'] == 'BLOCKED'
    assert wt.get('widget_request') is None


def test_daypart_range_extractor_does_not_leave_boundary_words():
    fact, remaining = extract_temporal('모레 낮부터 저녁 전까지', AT, TZ)
    assert (fact.start, fact.end, fact.period) == ('2028-03-01', '2028-03-01', 'afternoon')
    assert not remaining.strip()
