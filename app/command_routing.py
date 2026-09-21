"""Small source-only routing helpers; candidates are not execution authority.

EXACT is a whole-utterance rule, CANDIDATE needs the constrained parser, and
UNSAFE/MISSING is a clarification. No fuzzy correction of text or numeric slots.
"""
from __future__ import annotations

import re

from .assistant import ambiguous, parse_clock
from .clock_service import prefix_date, resolve_range

SET_END = r'(?:맞춰|설정(?:해)?|등록(?:해)?)(?:\s*(?:줘|주세요|줘요))?(?:요)?'
CANCEL_END = r'(?:취소(?:해)?|해제(?:해)?|꺼)(?:\s*(?:줘|주세요|줘요))?(?:요)?'
READ_END = r'(?:읽어\s*(?:줘|주세요)|보여\s*(?:줘|주세요)|확인(?:해)?\s*(?:줘|주세요)?|조회(?:해)?\s*(?:줘|주세요)?|브리핑\s*해\s*(?:줘|주세요))(?:요)?'
READ_HINT = re.compile(r'(?:현재|지금|방금|아까)?\s*.{1,100}(?:내용|본문|목록)\s*(?:좀\s*)?' + READ_END)


def read_candidates(text: str) -> bool:
    """An unknown noun with explicit content-read language: read-only candidates.

This does NOT replace 메론 with 메모, expose stored content to the model, or
allow writes. Unrelated general questions and plain imperatives don't qualify.
"""
    return READ_HINT.fullmatch(re.sub(r'[?？!！.。]+$', '', text).strip()) is not None


def exact_alarm(text: str, at: str, tz: str) -> dict | None:
    """Whole explicit alarm request -> existing WidgetRequest projection.

A missing/ambiguous date or clock raises ValueError for local clarification;
a misspelled domain/verb is simply not EXACT and can use the parser instead.
Cancel is deliberately explicit-ID only; never cancel an arbitrary latest alarm.
"""
    s = re.sub(r'[?？!！.。]+$', '', text).strip()
    cancel = re.fullmatch(r'알람\s+(?:ID|아이디)\s+([A-Za-z0-9_-]{1,80})(?:을|를)?\s*' + CANCEL_END, s, re.I)
    if cancel:
        return {'widget': 'alarm', 'action': 'cancel',
                'target': {'type': 'item_id', 'value': cancel[1]}, 'args': {}}
    if re.fullmatch(r'알람(?:을)?\s*' + CANCEL_END, s):
        raise ValueError('취소할 알람의 ID를 말씀해 주세요. 임의의 알람을 취소하지 않습니다.')
    if re.fullmatch(r'알람(?:\s*목록)?(?:을)?\s*' + READ_END, s):
        return {'widget': 'alarm', 'action': 'list', 'target': None, 'args': {}}
    day, rest = prefix_date(s)
    match = re.fullmatch(r'(.*?)\s*알람(?:을)?\s*' + SET_END, rest)
    if not match:
        return None
    clock_text = match[1].strip()
    tm, remaining, error = parse_clock(clock_text)
    # Do not reinterpret arbitrary labels, multiple clocks or leftover minutes.
    if tm is None and not error and clock_text:
        return None
    if error:
        raise ValueError(error)
    if remaining:
        return None
    if tm is None:
        raise ValueError('알람의 오전·오후와 시간을 말씀해 주세요. 시간을 추측하지 않습니다.')
    if not day:
        raise ValueError('알람 날짜를 오늘·내일 또는 정확한 날짜로 말씀해 주세요.')
    date = resolve_range(day, at, tz)
    if date.start != date.end:
        raise ValueError('알람은 한 날짜로 요청해 주세요.')
    return {'widget': 'alarm', 'action': 'set', 'target': None,
            'args': {'date': date.start, 'time': tm}}


def unsafe_source(text: str) -> bool:
    # Cover negative imperative conjugations missed by the legacy 하지 마 guard.
    return ambiguous(text) or bool(re.search(r'[가-힣]+지\s*(?:마|말|않)', text))
