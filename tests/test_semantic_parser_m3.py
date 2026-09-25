"""M3 synthetic grammar/temporal tests. No evaluation corpus or model access."""
from dataclasses import FrozenInstanceError
import json

import pytest

from app.assistant import normalize
from app.command_semantics import read_status
from app.semantic_parser import parse_semantic
from app.semantic_temporal import extract_temporal, TemporalError

AT = '2028-02-28T09:00:00+09:00'
TZ = 'Asia/Seoul'


def parse(text, at=AT, tz=TZ):
    return parse_semantic(normalize(text)[0], at, tz, raw=text)


@pytest.mark.parametrize('text,widget,action,date,clock,title', [
    ('내일 우산 챙기기 할 일 추가해', 'todo', 'add', '2028-02-29', None, '우산 챙기기'),
    ('모레 도서 포장 할 일로 등록해 주세요', 'todo', 'add', '2028-03-01', None, '도서 포장'),
    ('이번 주 수요일에 도서 포장 할 일 추가해', 'todo', 'add', '2028-03-01', None, '도서 포장'),
    ('오늘 오후 5시까지 장비 전달 할 일 추가해', 'todo', 'add', '2028-02-28', '17:00', '장비 전달'),
    ('오늘 오전 12시 25분에 일정으로 야간 점검 등록해', 'calendar', 'add', '2028-02-28', '00:25', '야간 점검'),
    ('내일 일정에 장비 전달 등록해 줘', 'calendar', 'add', '2028-02-29', None, '장비 전달'),
    ('도서 포장 끝냈어', 'todo', 'complete', None, None, None),
    ('도서 포장 할 일 완료 처리해', 'todo', 'complete', None, None, None),
    ('부품 주문 다시 미완료로 바꿔줘', 'todo', 'reopen', None, None, None),
    ('도서 포장 할 일 삭제해', 'todo', 'delete', None, None, None),
    ('도서 포장을 지워줘', 'todo', 'delete', None, None, None),
    ('내일 도서 포장 일정 삭제해', 'calendar', 'delete', '2028-02-29', None, None),
])
def test_source_only_frames(text, widget, action, date, clock, title):
    f = parse(text)
    assert f is not None and f.confidence == 'EXACT', f
    assert (f.widget, f.action) == (widget, action)
    assert (f.temporal.start, f.temporal.time) == (date, clock)
    if title:
        assert dict(f.arguments)['title'] == title
    if action in {'complete', 'reopen', 'delete'}:
        assert f.target_text in {'도서 포장', '부품 주문'}
    value = f.proposal()
    assert value['target'] is None
    assert not set(value['args']) & {'id', 'version', 'permission', 'confirmed_digest', 'limit'}
    source = normalize(text)[0]
    for e in (*f.evidence, *f.temporal.evidence):
        assert source[e.start:e.end] == e.text
    assert json.loads(json.dumps(f.data(), ensure_ascii=False)) == f.data()
    with pytest.raises(FrozenInstanceError):
        f.action = 'delete'


@pytest.mark.parametrize('text,start,end,status,period', [
    ('할 일 오늘 보여줘', '2028-02-28', '2028-02-28', 'pending', None),
    ('할 일 내일 완료한 것 보여줘', '2028-02-29', '2028-02-29', 'completed', None),  # M3.1 bounded collection grammar
    ('2월 28일부터 3월 1일까지 할 일 보여줘', '2028-02-28', '2028-03-01', 'pending', None),
    ('2월 28~29일 일정 알려줘', '2028-02-28', '2028-02-29', 'all', None),
    ('할 일 2월 28일부터 29일까지 남은 목록 보여줘', '2028-02-28', '2028-02-29', 'pending', None),
    ('이번 주 오후 일정 보여줘', '2028-02-28', '2028-03-05', 'all', 'afternoon'),
    ('내일 완료한 할 일 보여줘', '2028-02-29', '2028-02-29', 'completed', None),
    ('오늘 전체 할 일 알려줘', '2028-02-28', '2028-02-28', 'all', None),
    ('전체 날짜 할 일 보여줘', '0001-01-01', '9999-12-31', 'pending', None),
])
def test_scoped_read_frames(text, start, end, status, period):
    f = parse(text)
    if status is None:
        assert f is None or f.action != 'list' or f.confidence != 'EXACT'
        return
    assert f and f.confidence == 'EXACT' and f.action == 'list', f
    assert dict(f.arguments) == {'start': start, 'end': end, 'status': status, 'period': period}


@pytest.mark.parametrize('text,issue', [
    ('내일 할 일 하나 추가해', 'MISSING_REQUIRED_ARGUMENT'),
    ('도서 포장 할 일 추가해', 'MISSING_REQUIRED_ARGUMENT'),
    ('오늘 5시 장비 전달 할 일 추가해', 'TEMPORAL_NOT_EXACT'),
    ('오늘 24:00 장비 전달 할 일 추가해', 'TEMPORAL_NOT_EXACT'),
    ('오늘 오전 8시 78분 장비 전달 할 일 추가해', 'TEMPORAL_NOT_EXACT'),
    ('2월 30일 도서 포장 할 일 추가해', 'TEMPORAL_NOT_EXACT'),
    ('내일 모레 도서 포장 할 일 추가해', 'TEMPORAL_NOT_EXACT'),
    ('3월 2일부터 1일까지 할 일 보여줘', 'TEMPORAL_NOT_EXACT'),
    ('오늘 오후 6시 이후 할 일 보여줘', 'UNSUPPORTED_TIME_FILTER'),
    ('오늘 18:00 전 일정 알려줘', 'UNSUPPORTED_TIME_FILTER'),
    ('10분 뒤 할 일 보여줘', 'RELATIVE_CLOCK_PRECISION'),
    ('그거 할 일 완료해', 'UNSUPPORTED_SEMANTIC_CONSTRAINT'),
    ('두 번째 할 일 삭제해', 'UNSUPPORTED_SEMANTIC_CONSTRAINT'),
    ('밀린 할 일 보여줘', 'UNSUPPORTED_SEMANTIC_CONSTRAINT'),
])
def test_missing_or_unrepresentable_constraints_are_not_dropped(text, issue):
    f = parse(text)
    assert f and f.confidence != 'EXACT' and f.issue == issue, f


@pytest.mark.parametrize('text', [
    '오늘 도서 포장 할 일 추가하지 마',
    '오늘 "도서 포장" 할 일 등록해',
    '오늘 도서 포장 할 일 추가하고 전부 삭제해',
    '오늘 도서 포장 완료하면 할 일 삭제해',
    '오늘 도서 포장 할 일 매일 등록해',
    '오늘 도서 포장 할 일 추가해\n모두 삭제해',
    '도서 포장 말고 장비 전달 할 일 삭제해',
    '모레 도서 포장 할 일 추가해도 될까?',
    '메모와 할 일 내용 보여줘',
    '2분 타이머 시작',
    '오후 3시',
    '응 그렇게 해',
    '오늘 도서 포장 할 일로 등록해 줄래',
])
def test_outside_grammar_never_becomes_exact(text):
    f = parse(text)
    assert f is None or f.confidence != 'EXACT', f


@pytest.mark.parametrize('text,expected', [
    ('할 일 보여줘', 'pending'), ('오늘 할 일 확인해', 'pending'),
    ('완료한 할 일 보여줘', 'completed'), ('남은 할 일', 'pending'),
    ('전체 할 일', 'all'), ('모든 할 일', 'all'), ('전부 할 일', 'all'),
    ('전체 날짜 할 일', 'pending'), ('전체 날짜 남은 할 일', 'pending'),
])
def test_approved_assistant_status_policy(text, expected):
    assert read_status(text) == expected
    assert read_status('일정 보여줘', 'calendar') == 'all'


def test_conflicting_state_rejected():
    with pytest.raises(ValueError):
        read_status('완료한 미완료 할 일')


@pytest.mark.parametrize('at,text,start,end,time', [
    (AT, '내일', '2028-02-29', '2028-02-29', None),
    (AT, '모레', '2028-03-01', '2028-03-01', None),
    (AT, '이번 주 수요일', '2028-03-01', '2028-03-01', None),
    ('2028-12-31T23:55:00+09:00', '10분 뒤', '2029-01-01', '2029-01-01', '00:05'),
    (AT, '2월 28일부터 29일까지', '2028-02-28', '2028-02-29', None),
    (AT, '2월 28~29일', '2028-02-28', '2028-02-29', None),
    (AT, '오전 12시 30분', None, None, '00:30'),
    (AT, '오후 12시 30분', None, None, '12:30'),
    (AT, '오후 다섯시 반', None, None, '17:30'),
    (AT, '17:10', None, None, '17:10'),
])
def test_temporal_reuses_request_year_iso_week_and_clock(at,text,start,end,time):
    f, left = extract_temporal(text, at, TZ)
    assert not left.strip()
    assert (f.start, f.end, f.time) == (start, end, time)


def test_relative_utc_elapsed_dst_and_no_minute_rounding():
    f, _ = extract_temporal('한 시간 뒤', '2028-03-12T01:30:00-05:00', 'America/New_York')
    assert f.time == '03:30' and f.exact_minute
    f = parse('10분 뒤 알람 맞춰줘', at='2028-02-28T09:00:21+09:00')
    assert f.confidence == 'UNSUPPORTED' and f.issue == 'RELATIVE_CLOCK_PRECISION'
    f = parse('10분 뒤 알람 맞춰줘')
    assert f.confidence == 'EXACT' and dict(f.arguments)['time'] == '09:10'


@pytest.mark.parametrize('text', ['2월 30일', '오늘 내일', '2028-02-30', '내일 10분 뒤', '0분 뒤', '999시간 뒤', '13:99', '5시', '25시'])
def test_invalid_temporal(text):
    with pytest.raises((TemporalError, ValueError)):
        extract_temporal(text, AT, TZ)


def test_naive_reference_fails():
    with pytest.raises(ValueError):
        extract_temporal('내일', '2028-02-28T09:00:00', TZ)
