"""Request-time semantics, never model arithmetic or hidden network clock sync.

A request freezes its civil date. A live time query uses execution time instead.
Month/day without a year means the reference calendar year, not next occurrence.
Week ranges use Monday ISO weeks; this does not change the client Sun-Sat strip.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# Boundaries prevent e.g. '내일부터' being accepted as just '내일'. The read
# router, not this extractor, decides whether omitted '일' is meaningful.
DATE_CORE = (r'(?:\d{4}년\s*\d{1,2}월\s*\d{1,2}일|\d{4}-\d{2}-\d{2}|'
             r'\d{1,2}월\s*\d{1,2}(?:일)?|오늘|내일|모레|어제|'
             r'(?:이번\s*주|다음\s*주|다다음\s*주)?\s*[월화수목금토일]요일|'
             r'이번\s*주|다음\s*주|이번\s*달|다음\s*달|전체\s*날짜|모든\s*날짜)')
DATE_PATTERN = re.compile(r'(?<![가-힣A-Za-z0-9])(' + DATE_CORE + r')(?=(?:에는|의|에)?(?:\s|[,，:：·]|$))')
PREFIX_PATTERN = re.compile(r'^(' + DATE_CORE + r')(?=(?:에는|의|에)?(?:\s|[,，:：·]|$))(?:에는|에|의)?\s*[,，:：·]?\s*')


def aware(at: str, tz: str) -> datetime:
    stamp = datetime.fromisoformat(at.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('시간대가 없는 기준 시각은 사용할 수 없습니다.')
    return stamp.astimezone(ZoneInfo(tz))


def clock_context(at: str, tz: str) -> dict:
    now = aware(at, tz)
    monday = now.date() - timedelta(days=now.weekday())
    return {
        'reference_at': at, 'local_at': now.isoformat(timespec='seconds'),
        'timezone': tz, 'today': now.date().isoformat(),
        'tomorrow': (now.date() + timedelta(days=1)).isoformat(),
        'weekday': '월화수목금토일'[now.weekday()] + '요일',
        'this_week': [monday.isoformat(), (monday+timedelta(days=6)).isoformat()],
        'clock_source': 'server_os_clock', 'clock_sync_verified': False,
        'date_basis': 'request_created_at', 'year_policy': 'reference_calendar_year',
    }


@dataclass(frozen=True)
class DateRange:
    start: str
    end: str
    evidence: str | None
    policy: str = 'source_expression'

    def data(self) -> dict:
        return asdict(self)


def resolve_range(ref: str | None, at: str, tz: str) -> DateRange:
    today = aware(at, tz).date()
    if not ref:
        return DateRange(today.isoformat(), today.isoformat(), None, 'default_reference_today')
    compact = re.sub(r'\s+', '', ref)
    offsets = {'오늘':0,'내일':1,'모레':2,'어제':-1,'today':0,'tomorrow':1}
    if compact in offsets:
        day = today+timedelta(days=offsets[compact]); start=end=day
    elif compact in {'전체','모든','전체날짜','모든날짜'}:
        start, end = date.min, date.max
    elif compact in {'이번주','다음주'}:
        start=today-timedelta(days=today.weekday())+timedelta(days=7 if compact=='다음주' else 0)
        end=start+timedelta(days=6)
    elif compact in {'이번달','다음달'}:
        y,m=today.year,today.month
        if compact=='다음달': y,m=(y+1,1) if m==12 else (y,m+1)
        start=date(y,m,1);end=date(y,m,calendar.monthrange(y,m)[1])
    elif re.fullmatch(r'\d{4}-\d{2}-\d{2}',compact):
        start=end=date.fromisoformat(compact)
    else:
        month=re.fullmatch(r'(?:(\d{4})년)?(\d{1,2})월(\d{1,2})(?:일)?',compact)
        weekday=re.fullmatch(r'(이번주|다음주|다다음주)?([월화수목금토일])요일',compact)
        if month:
            start=end=date(int(month[1]) if month[1] else today.year,int(month[2]),int(month[3]))
        elif weekday:
            start=today-timedelta(days=today.weekday())+timedelta(days='월화수목금토일'.index(weekday[2]))
            if weekday[1]: start+=timedelta(days={'이번주':0,'다음주':7,'다다음주':14}[weekday[1]])
            elif start<today: start+=timedelta(days=7)
            end=start
        else:
            raise ValueError('날짜를 해석하지 못했습니다. 오늘·내일 또는 정확한 날짜로 다시 요청하세요.')
    return DateRange(start.isoformat(), end.isoformat(), ref)


def resolve_day(ref: str | None, at: str, tz: str) -> str:
    r=resolve_range(ref,at,tz)
    if r.start != r.end: raise ValueError('이 변경에는 한 날짜만 지정하세요. 날짜 범위 변경은 지원하지 않습니다.')
    return r.start


def prefix_date(text: str):
    m=PREFIX_PATTERN.match(text)
    return (m[1].strip(),text[m.end():].strip()) if m else (None,text)


def date_evidence(text: str) -> list[str]:
    return [m[1].strip() for m in DATE_PATTERN.finditer(text)]


def resolve_source_dates(text: str, at: str, tz: str) -> list[DateRange]:
    refs=date_evidence(text)
    ranges=[resolve_range(ref,at,tz) for ref in refs]
    if len({(r.start,r.end) for r in ranges})>1:
        raise ValueError('날짜가 여러 개입니다. 한 날짜 또는 이번 주·다음 주 범위로 나누어 요청하세요.')
    return ranges
