"""Small synthetic food utterances, never private evaluation cases."""
import pytest

from app.food_commands import parse_food_command


AT = '2026-11-16T00:00:00Z'


def parse(text, at=AT, timezone='Asia/Seoul'):
    return parse_food_command(text, at, timezone)


@pytest.mark.parametrize('text,name,quantity,expiry', [
    ('소비기한 알리미에 우유 2개, 소비기한 11월 20일로 추가해 줘', '우유', 2, '2026-11-20'),
    ('우유 두 개 소비기한은 11월 20일이야. 등록해줘', '우유', 2, '2026-11-20'),
    ('소비기한 알리미에 사과 3개 내일까지 등록해', '사과', 3, '2026-11-17'),
    ('두부 12개 소비기한 2027년 1월 2일 등록', '두부', 12, '2027-01-02'),
    ('소비기한 2026-11-30, 요구르트 열두 개 추가해 주세요', '요구르트', 12, '2026-11-30'),
    ('소비기한 알리미 저지방 우유 1개 모레로 추가', '저지방 우유', 1, '2026-11-18'),
    ('소비기한 알리미 라면 5개 오늘로 저장해', '라면', 5, '2026-11-16'),
    ('소비기한 알리미 우유 1개 어제로 등록해', '우유', 1, '2026-11-15'),
])
def test_complete_single_item_is_source_only(text, name, quantity, expiry):
    command = parse(text)
    assert not command.issue
    assert (command.action, command.name, command.quantity, command.expiry_date) == ('add', name, quantity, expiry)


@pytest.mark.parametrize('text,action,name,quantity,expiry', [
    ('소비기한 알리미 보여 줘', 'list', None, None, None),
    ('식품 목록 알려줘', 'list', None, None, None),
    ('소비기한 전체 목록 조회해', 'list', None, None, None),
    ('우유 소비기한 알려줘', 'get', '우유', None, None),
    ('소비기한 11월 20일인 우유 알려줘', 'get', '우유', None, '2026-11-20'),
    ('소비기한 알리미 우유 개수를 3개로 수정해', 'update', '우유', 3, None),
    ('소비기한 알리미 우유 소비기한을 내일로 바꿔줘', 'update', '우유', None, '2026-11-17'),
    ('소비기한 알리미 우유 4개, 소비기한 11월 30일로 변경해', 'update', '우유', 4, '2026-11-30'),
    ('소비기한 알리미에서 우유를 삭제해줘', 'delete', '우유', None, None),
    ('소비기한 11월 20일인 우유 삭제해', 'delete', '우유', None, '2026-11-20'),
])
def test_read_update_delete_grammar(text, action, name, quantity, expiry):
    command = parse(text)
    assert not command.issue
    assert (command.action, command.name, command.quantity, command.expiry_date) == (action, name, quantity, expiry)


@pytest.mark.parametrize('text', [
    '소비기한 알리미에 우유 추가해',
    '소비기한 알리미 우유 2개 추가해',
    '소비기한 알리미 우유 내일까지 추가해',
    '소비기한 알리미 2개 내일까지 추가해',
    '소비기한 알리미 우유 0개 내일까지 추가해',
    '소비기한 알리미 우유 -1개 내일까지 추가해',
    '소비기한 알리미 우유 +1개 내일까지 추가해',
    '소비기한 알리미 우유 1.5개 내일까지 추가해',
    '소비기한 알리미 우유 1,5개 내일까지 추가해',
    '소비기한 알리미 우유 10000개 내일까지 추가해',
    '소비기한 알리미 우유 한두 개 내일까지 추가해',
    '소비기한 알리미 우유 2~3개 내일까지 추가해',
    '소비기한 알리미 우유 2개 11월 31일까지 추가해',
    '소비기한 알리미 우유 2개 2026-02-29 추가해',
    '소비기한 알리미 우유 2개 다음 주까지 추가해',
    '소비기한 알리미 우유 2개 내일이나 모레까지 추가해',
    '소비기한 알리미 우유 2개 내일 모레 추가해',
    '소비기한 알리미 우유 2개와 치즈 3개 내일까지 추가해',
    '소비기한 알리미 우유와 치즈 2개 내일까지 추가해',
    '소비기한 알리미 우유, 치즈 2개 내일까지 추가해',
    '소비기한 알리미 우유 2개 내일까지 추가하고 타이머 시작해',
    '소비기한 알리미 우유 소비기한 바꾸지 말고 개수만 3개로 수정해',
    '소비기한 알리미 우유 추가하지 마',
    '소비기한 알리미 우유 안 삭제해',
    '소비기한 알리미에 가능하면 우유 2개 소비기한 내일 등록해 줘',
    '소비기한 알리미에 우유 2개 소비기한 내일 안등록해 줘',
    '소비기한 알리미에 우유 2개 소비기한 내일 못등록해 줘',
    '소비기한 알리미에 우유 2개 소비기한 내일 대신 추가해 줘',
    '소비기한 알리미에 우유 2개 소비기한 내일 다른 것으로 추가해 줘',
    '소비기한 알리미에 우유 2개 소비기한 내일 참고해서 추가해 줘',
    '소비기한 알리미에 우유 2개 저지방 소비기한 내일 등록해 줘',
    '내일 소비기한 알리미에 우유 2개 등록해 줘',
    '내일로 소비기한 알리미에 우유 2개 등록해 줘',
    '소비기한 알리미 내일 우유 2개 등록해 줘',
    '소비기한 알리미 우유 2개 내일 등록해 줘',
    '소비기한 알리미 우유 소비기한이 지나면 삭제해',
    '소비기한 지난 식품 삭제해',
    '소비기한 내일인 식품 목록 보여줘',
    '소비기한 알리미 전체 삭제해',
    '소비기한 알리미 우유 1개 삭제해',
    '소비기한 알리미 우유 수정해',
])
def test_incomplete_ambiguous_negative_or_compound_fails_closed(text):
    command = parse(text)
    assert command is not None and command.issue
    assert not command.action


def test_request_clock_and_calendar_year_not_host_day_or_next_occurrence():
    near_midnight = '2026-12-31T15:10:00Z'
    command = parse('소비기한 알리미 우유 2개 내일까지 추가해', near_midnight)
    assert command.expiry_date == '2027-01-02'
    same_source_utc = parse('소비기한 알리미 우유 2개 내일까지 추가해', near_midnight, 'UTC')
    assert same_source_utc.expiry_date == '2027-01-01'
    past = parse('소비기한 알리미 우유 2개 1월 2일로 추가해')
    assert past.expiry_date == '2026-01-02'


@pytest.mark.parametrize('text', ['오늘 할 일 확인해줘', '4분 타이머 시작', '우유 2개 추가해'])
def test_other_domains_are_not_reinterpreted(text):
    assert parse(text) is None


@pytest.mark.parametrize('scope', [
    '소비기한 알리미에는', '소비기한 알리미에서는',
    '식품 알리미에는', '식품 알리미에서는',
])
@pytest.mark.parametrize('name', ['우유', '오이', '떡볶이', '오메가'])
def test_complete_scope_particles_preserve_literal_food_name(scope, name):
    added = parse(f'{scope} {name} 2개 소비기한 11월 20일로 추가해줘')
    assert not added.issue
    assert (added.action, added.name, added.quantity, added.expiry_date) == (
        'add', name, 2, '2026-11-20')
    queried = parse(f'{scope} {name} 알려줘')
    assert not queried.issue
    assert (queried.action, queried.name) == ('get', name)


@pytest.mark.parametrize('scope', [
    '소비기한 알리미에게는', '소비기한 알리미에서만',
    '식품 알리미에서는요', '식품 알리미에만',
])
def test_unknown_scope_suffix_cannot_become_part_of_a_food_name(scope):
    result = parse(f'{scope} 우유 2개 소비기한 11월 20일로 추가해줘')
    assert result is not None and result.issue
    assert not result.action and result.name is None
