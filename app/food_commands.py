"""Bounded, source-only food inventory commands.

The parser has no database, IDs, permissions, or model output. A recognized but
incomplete/ambiguous food request produces an issue instead of filling defaults.
Month/day means the request's local calendar year, never the next occurrence.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from .clock_service import resolve_day


@dataclass(frozen=True)
class FoodCommand:
    action: str = ''
    name: str | None = None
    quantity: int | None = None
    expiry_date: str | None = None
    issue: str | None = None


_MARKER = re.compile(r'소비\s*기한|식품\s*(?:알리미|목록|재고)')
_SCOPE_LABEL = r'(?:소비\s*기한\s*알리미|식품\s*알리미)'
_SCOPE = re.compile(
    _SCOPE_LABEL + r'(?:에서는|에는|에서|에|의|는|를|을)?'
    r'(?=$|\s|[,，.。!?？！:：])\s*'
)
_DATE = re.compile(
    r'(?<![\dA-Za-z])(?P<value>\d{4}-\d{2}-\d{2}|'
    r'(?:\d{4}\s*년\s*)?\d{1,2}\s*월\s*\d{1,2}\s*일|오늘|내일|모레|어제)'
    r'(?P<tail>(?:까지)?(?:으로|로)?(?:이에요|이야|입니다|이고|인|은|는|이|야|예요)?)'
    r'(?=$|\s|[,，.。!?？！:：])'
)
_NUMBERS = {
    '한': 1, '하나': 1, '두': 2, '둘': 2, '세': 3, '셋': 3, '네': 4, '넷': 4,
    '다섯': 5, '여섯': 6, '일곱': 7, '여덟': 8, '아홉': 9, '열': 10,
    '열한': 11, '열두': 12, '열세': 13, '열네': 14, '열다섯': 15,
    '열여섯': 16, '열일곱': 17, '열여덟': 18, '열아홉': 19, '스무': 20,
}
_QUANTITY = re.compile(
    r'(?<![\w.])(?P<value>\d+|' + '|'.join(sorted(_NUMBERS, key=len, reverse=True)) + r')\s*개'
    r'(?P<tail>으로|로|를|을|는|은|이야|야|이고)?(?=$|\s|[,，.。!?？！:：])'
)
_ENDINGS = r'(?:해\s*(?:주세요|줘요|줘|주라)?|해요|하세요|하기)?'
_ACTIONS = (
    ('add', re.compile(r'(?:(?:추가|등록|저장)' + _ENDINGS + r'|넣어\s*(?:주세요|줘)?)$')),
    ('update', re.compile(r'(?:(?:수정|변경)' + _ENDINGS + r'|바꿔\s*(?:주세요|줘)?)$')),
    ('delete', re.compile(r'(?:(?:삭제|제거)' + _ENDINGS + r'|지워\s*(?:주세요|줘)?)$')),
    ('read', re.compile(r'(?:(?:조회|확인)' + _ENDINGS + r'|(?:보여|알려|읽어)\s*(?:주세요|줘요|줘)?|언제(?:야|인가요)?)$')),
)
_UNSAFE = re.compile(
    r'말고|말아|말자|마세요|하지\s*마|하지\s*않|아니(?:라|고|면)|않(?:으면|고|게)|'
    r'만약|가능\s*하면|가능하다면|한다면|이면|되면|있으면|없으면|경우|대신|빼고|제외|'
    r'(?<!\w)(?:안|못)\s*(?:추가|등록|저장|수정|변경|삭제|제거|넣어|바꿔|지워)|'
    r'(?<!\w)(?:안|못)\s|그리고|동시에|한꺼번에|각각|(?<!\w)(?:및|또|또는|혹은)(?!\w)|'
    r'(?:추가|삭제|등록|저장|수정|변경)하고|하고\s|랑\s|(?<!\w)[과와](?!\w)'
)
_RESIDUE = re.compile(
    r'소비\s*기한|개수|수량|추가|등록|저장|수정|변경|삭제|제거|보여|알려|확인|조회|'
    r'까지|이후|이전|지난|지나|남은|임박|다가|전부|모두|전체|목록|내일|오늘|모레|어제|'
    r'다음\s*주|이번\s*주|\d\s*(?:월|일|개|병|팩)|[\n\r,，.。!?？！:：]'
)


def parse_food_command(text: str, at_iso: str, timezone: str) -> FoodCommand | None:
    """Return None for other domains, or a fully source-backed command/issue.

    Accepted mutations name exactly one item. Counts use explicit 개 and dates
    are ISO, Korean year/month/day, month/day, 오늘/내일/모레/어제. Updating a
    date is not a target selector; duplicate names require the manual editor.
    """
    if not isinstance(text, str) or not _MARKER.search(text):
        return None
    issue = '소비기한 알리미는 한 번에 식품 하나씩 요청해 주세요. 식품명·개수·소비기한을 명확히 말씀해 주세요.'
    if len(text) > 1000 or _UNSAFE.search(text):
        return FoodCommand(issue=issue)
    # Do not silently execute one domain from a combined food/task/etc request.
    # Manual names can contain these words; the deliberately bounded voice
    # grammar asks for manual entry instead of guessing an action boundary.
    if re.search(r'할\s*일|일정|캘린더|알람|타이머|메모|날씨|설정|시스템', text):
        return FoodCommand(issue=issue)
    source = text.strip().rstrip('.。!?？！').strip()
    action = ''
    for candidate, pattern in _ACTIONS:
        match = pattern.search(source)
        if match:
            action, source = candidate, source[:match.start()].strip()
            break
    if not action:
        return FoodCommand(issue='식품을 추가·수정·삭제하거나 소비기한 목록을 조회할지 말씀해 주세요.')
    # Preserve removed-field boundaries. Literal name fragments on opposite
    # sides of a typed field must never be concatenated into a made-up name.
    source = _SCOPE.sub(' \0 ', source)
    # Match complete scope particles only. A partial match of "에는" as "에"
    # would otherwise silently turn "우유" into the different name "는 우유".
    # An unsupported scope suffix is clarification, never part of a food name.
    if re.search(_SCOPE_LABEL, source):
        return FoodCommand(issue=issue)
    if re.search(r'[-+]?\d+[.,]\d+\s*개|[-+]\d+\s*개|\d+\s*(?:~|에서|부터)\s*\d+\s*개|한두\s*개', source):
        return FoodCommand(issue='개수는 1~9999 사이의 정확한 정수로 말씀해 주세요.')

    dates = list(_DATE.finditer(source))
    quantities = list(_QUANTITY.finditer(source))
    if len(dates) > 1 or len(quantities) > 1:
        return FoodCommand(issue=issue)
    expiry_date = None
    if dates:
        date_match = dates[0]
        before_date = source[:date_match.start()]
        explicit_label = re.search(r'소비\s*기한(?:은|는|을|를|이|가|:|：)?\s*$', before_date)
        until = date_match['tail'].startswith('까지')
        assigned_after_name = (
            date_match['tail'].startswith(('로', '으로'))
            and bool(before_date.strip(' \t\0,，.。:：'))
        )
        if not (explicit_label or until or assigned_after_name):
            return FoodCommand(issue='요청할 날짜와 소비기한을 혼동하지 않도록 "소비기한은 11월 20일"처럼 명시해 주세요.')
        try:
            expiry_date = resolve_day(date_match['value'], at_iso, timezone)
        except (ValueError, OverflowError):
            return FoodCommand(issue='유효한 소비기한을 YYYY-MM-DD 또는 월·일로 다시 말씀해 주세요.')
    quantity = None
    if quantities:
        value = quantities[0]['value']
        quantity = int(value) if value.isdecimal() else _NUMBERS[value]
        if not 1 <= quantity <= 9999:
            return FoodCommand(issue='개수는 1~9999 사이의 정확한 정수로 말씀해 주세요.')

    # Remove only typed field spans and their grammatical particles.
    for match in sorted([*dates, *quantities], key=lambda m: m.start(), reverse=True):
        source = source[:match.start()] + '\0' + source[match.end():]
    source = re.sub(r'소비\s*기한(?:은|는|을|를|이|가|:|：)?|(?:개수|수량)(?:는|은|을|를|이|가|:|：)?', '\0', source)
    name_parts = [part.strip(' \t,，.。:：') for part in source.split('\0')]
    name_parts = [re.sub(r'\s+', ' ', part).strip() for part in name_parts if part]
    if len(name_parts) > 1:
        return FoodCommand(issue=issue)
    source = name_parts[0] if name_parts else ''
    if action == 'read' and source in {'', '목록', '식품', '식품 목록', '식품 재고', '전체', '전체 목록', '전체 식품 목록'}:
        if expiry_date is not None or quantity is not None:
            return FoodCommand(issue='날짜·개수로 목록을 임의 필터링하지 않습니다. 전체 목록 또는 식품명을 지정해 주세요.')
        return FoodCommand(action='list')
    source = re.sub(r'(?:을|를|의|은|는)$', '', source).strip()
    source = re.sub(r'^(?:식품명|식품)\s*[:：]?\s+', '', source).strip()
    if not source or source in {'식품', '음식', '항목', '그것', '이것', '그거', '이거', '해당 식품'}:
        return FoodCommand(issue='등록하거나 조회할 정확한 식품명을 말씀해 주세요.')
    if (len(source) > 120 or _RESIDUE.search(source)
            or re.search(r'(?:과|와|랑)\s+\S', source)
            or any(ord(ch) < 32 for ch in source)):
        return FoodCommand(issue=issue)
    if action == 'add' and (quantity is None or expiry_date is None):
        return FoodCommand(issue='추가할 식품의 개수와 소비기한을 모두 말씀해 주세요. 예: 우유 2개, 소비기한 11월 20일로 등록해 줘.')
    if action == 'update' and quantity is None and expiry_date is None:
        return FoodCommand(issue='변경할 개수 또는 소비기한을 명시해 주세요.')
    if action in {'read', 'delete'} and quantity is not None:
        return FoodCommand(issue='수량 일부를 임의로 삭제하지 않습니다. 조회·삭제할 식품명만 지정하거나 개수 수정으로 요청해 주세요.')
    return FoodCommand(action='get' if action == 'read' else action,
                       name=source, quantity=quantity, expiry_date=expiry_date)
