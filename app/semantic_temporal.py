"""Explicit temporal spans using the existing Room Hub request clock and date policy.

This is not ASR correction. It does not round relative clocks, infer AM/PM, turn
one date into an unbounded range, or accept a clock as an unsupported filter.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta, timezone
import re

from .clock_service import DATE_CORE, aware, resolve_range
from .semantic_types import Evidence, Temporal

# Endpoint inheritance is restricted to a day number in an explicit date range.
DAY = (r'(?:\d{4}년\s*\d{1,2}월\s*\d{1,2}일|\d{4}-\d{2}-\d{2}|'
       r'\d{1,2}월\s*\d{1,2}일|오늘|내일|모레|어제|'
       r'(?:(?:이번|다음|다다음)\s*주\s*)?[월화수목금토일]요일)')
RANGE = re.compile(r'(?<![가-힣\w])(?P<a>' + DAY + r')\s*부터\s*(?P<b>' + DAY + r'|\d{1,2}일)\s*까지'
                   r'(?:에는|에|의|만)?(?=\s|[,，]|$)')
SHORT_RANGE = re.compile(r'(?<![가-힣\w])(?P<m>\d{1,2})월\s*(?P<a>\d{1,2})(?:일)?\s*[~～]\s*'
                         r'(?P<b>\d{1,2})일(?:에는|에|의|만)?(?=\s|[,，]|$)')
DATE = re.compile(r'(?<![가-힣\w])(?P<date>' + DATE_CORE + r')(?:에는|의|에|만)?(?=\s|[,，]|$)')
CLOCK = re.compile(r'(?<![가-힣\w])(?:(?:오전|오후|아침|저녁|밤|낮)\s*)?'
                   r'(?:\d{1,2}|열두|열한|열|한|두|세|네|다섯|여섯|일곱|여덟|아홉)\s*'
                   r'(?:시(?:\s*(?:\d{1,2}\s*분|반))?|:\d{2})'
                   r'\s*(?:(?P<relation>이전|이후|전|후|까지)\s*)?(?:에는|에)?(?=\s|[,，]|$)')
RELATIVE = re.compile(r'(?<![가-힣\w])(?P<n>\d{1,3}|한|두|세|네)\s*(?P<unit>분|시간)\s*(?:뒤|후)(?:에)?(?=\s|[,，]|$)')
PERIOD = re.compile(r'(?<![가-힣\w])(?P<period>오전|오후)(?:에는|에|의)?(?=\s|[,，]|$)')


class TemporalError(ValueError):
    """Carry only already proven, nonconflicting source facts on parse failure."""
    def __init__(self, message, *, partial=None):
        super().__init__(message)
        self.partial = partial or Temporal()



def _erase(text: str, start: int, end: int) -> str:
    return text[:start] + ' ' * (end - start) + text[end:]


def extract_temporal(text: str, at: str, tz: str) -> tuple[Temporal, str]:
    """Return proven values and a SAME-LENGTH residual for source-span extraction."""
    local = aware(at, tz)  # Reject a naive/invalid reference even without date words.
    residual = text
    evidence = []
    found = []
    for pattern in (RANGE, SHORT_RANGE):
        for match in list(pattern.finditer(residual)):
            try:
                if pattern is SHORT_RANGE:
                    start = date(local.year, int(match['m']), int(match['a'])).isoformat()
                    end = date(local.year, int(match['m']), int(match['b'])).isoformat()
                else:
                    first = resolve_range(match['a'], at, tz)
                    start = first.start
                    if first.start != first.end:
                        raise TemporalError('범위의 시작 날짜를 하나로 말씀해 주세요.')
                    right = match['b']
                    if re.fullmatch(r'\d{1,2}일', right):
                        base = date.fromisoformat(start)
                        end = date(base.year, base.month, int(right[:-1])).isoformat()
                    else:
                        last = resolve_range(right, at, tz)
                        if last.start != last.end:
                            raise TemporalError('범위의 끝 날짜를 하나로 말씀해 주세요.')
                        end = last.end
                if start > end:
                    raise TemporalError('시작일이 종료일보다 늦습니다. 연도와 날짜를 확인해 주세요.')
                found.append((start, end, match.group()))
                evidence.append(Evidence('date_range', match.start(), match.end(), text[match.start():match.end()],
                                         'inclusive_end; reference_year_or_explicit_year'))
                residual = _erase(residual, match.start(), match.end())
            except (ValueError, OverflowError) as exc:
                raise TemporalError('유효한 시작일과 종료일을 말씀해 주세요.') from exc
    for match in list(DATE.finditer(residual)):
        try:
            value = resolve_range(match['date'].strip(), at, tz)
        except (ValueError, OverflowError) as exc:
            raise TemporalError('존재하는 날짜를 말씀해 주세요. 날짜를 보정하지 않았습니다.') from exc
        found.append((value.start, value.end, match['date'].strip()))
        evidence.append(Evidence('date', match.start(), match.end(), text[match.start():match.end()],
                                 'existing_room_hub_date_policy'))
        residual = _erase(residual, match.start(), match.end())
    if len(found) > 1:
        raise TemporalError('날짜 표현이 여러 개입니다. 한 날짜 또는 하나의 범위로 다시 요청하세요.')
    relative = list(RELATIVE.finditer(residual))
    if len(relative) > 1 or (relative and found):
        raise TemporalError('상대 시간과 다른 날짜를 합쳐 추측하지 않습니다. 하나의 시각으로 요청하세요.')
    fact = Temporal(start=found[0][0] if found else None, end=found[0][1] if found else None,
                    date_ref=found[0][2] if found else None)
    if relative:
        match = relative[0]
        n = int(match['n']) if match['n'].isdigit() else {'한': 1, '두': 2, '세': 3, '네': 4}[match['n']]
        minutes = n * (60 if match['unit'] == '시간' else 1)
        if not 1 <= minutes <= 1440:
            raise TemporalError('상대 시각은 1분~24시간으로 명확히 요청하세요.')
        target = (local.astimezone(timezone.utc) + timedelta(minutes=minutes)).astimezone(local.tzinfo)
        fact = replace(fact, start=target.date().isoformat(), end=target.date().isoformat(),
                       date_ref=match.group(), time=target.strftime('%H:%M'), relative_minutes=minutes,
                       exact_minute=(target.second == 0 and target.microsecond == 0))
        evidence.append(Evidence('relative_minutes', match.start(), match.end(), text[match.start():match.end()],
                                 'elapsed_minutes_from_frozen_request_instant; no_rounding'))
        residual = _erase(residual, match.start(), match.end())
    def partial_error(message):
        return TemporalError(message, partial=replace(fact, evidence=tuple(evidence)))
    approximate = (r'(?:(?:오전|오후|아침|저녁|밤|낮)\s*)?'
                   r'(?:\d{1,2}|열두|열한|열|한|두|세|네|다섯|여섯|일곱|여덟|아홉)\s*'
                   r'(?:시(?:\s*(?:\d{1,2}\s*분|반))?|:\d{2})\s*(?:쯤|경|정도|무렵)')
    if (re.search(approximate, residual) or
            re.search(r'(?:아마|대략|약)\s*(?:(?:오전|오후)\s*)?(?:\d|한\s*시|두\s*시)', residual) or
            re.search(r'(?:아침|저녁|밤|오전|오후)\s*(?:쯤|무렵|정도)', residual)):
        raise partial_error('대략적인 시각은 확정할 수 없습니다. 오전·오후와 정확한 시간을 말씀해 주세요.')
    clocks = list(CLOCK.finditer(residual))
    if len(clocks) > 1 or (clocks and relative):
        raise partial_error('시각이 여러 개입니다. 한 시각으로 다시 요청하세요.')
    if clocks:
        # Lazy import avoids introducing a second civil clock parser.
        from .assistant import parse_clock
        match = clocks[0]
        raw = match.group()
        without_relation = re.sub(r'(?:이전|이후|전|후|까지)\s*(?:에는|에)?\s*$', '', raw).strip()
        clock, left, error = parse_clock(without_relation)
        if error or clock is None or left:
            raise partial_error(error or '오전·오후를 포함한 정확한 시간을 말씀해 주세요.')
        fact = replace(fact, time=clock, relation=match['relation'])
        evidence.append(Evidence('time', match.start(), match.end(), text[match.start():match.end()],
                                 'existing_room_hub_clock_parser'))
        residual = _erase(residual, match.start(), match.end())
    periods = list(PERIOD.finditer(residual))
    if periods and (len(periods) > 1 or clocks or relative):
        # Neither clock nor period is uniquely established in a conflicting pair.
        fact = replace(fact, time=None, period=None, relation=None)
        evidence = [e for e in evidence if e.field not in {'time', 'period'}]
        raise partial_error('시간대와 시각이 중복됩니다. 하나의 조건으로 다시 요청하세요.')
    if periods:
        match = periods[0]
        fact = replace(fact, period='morning' if match['period'] == '오전' else 'afternoon')
        evidence.append(Evidence('period', match.start(), match.end(), text[match.start():match.end()]))
        residual = _erase(residual, match.start(), match.end())
    # A partially parsed numeric expression must never turn into an unconditional request.
    if re.search(r'(?<![가-힣\w])(?:\d{1,4}[-/:년월]|\d+\s*(?:일|시|분|시간|초)(?:\s|에|전|후|뒤|$))|'
                 r'몇\s*시|저녁쯤|오전쯤|오후쯤|밤쯤|[~～]', residual):
        raise partial_error('남은 날짜·시간 표현을 확정할 수 없습니다. 숫자나 시간을 추측하지 않습니다.')
    return replace(fact, evidence=tuple(evidence)), residual
