"""Bounded whole-utterance semantic proposals for existing Room Hub operations.

Returns None for candidates outside this grammar: the existing constrained LLM
path remains responsible for them. An exact/missing frame is recomputed from the
stored source before use. It has no database, network, permission or ID access.
"""
from __future__ import annotations

import re

from .clock_service import resolve_range
from .command_semantics import read_status, READ_END
from .semantic_temporal import extract_temporal, TemporalError
from .semantic_types import Evidence, SemanticFrame, Temporal

ENDING = r'(?:\s*(?:해\s*(?:줘|주세요|줘요)?|해요|해주세요|처리해\s*(?:줘|주세요)?))?(?:요)?'
READ = re.compile(r'(?P<body>.*?)\s*(?P<verb>' + READ_END +
                  r'|읽어\s*(?:줘|주세요|줘요))(?:요)?$')
ADD = re.compile(r'(?P<body>.*?)\s*(?P<verb>추가|등록)' + ENDING + r'$')
MUTATIONS = (
    ('reopen', re.compile(r'(?P<body>.*?)\s*(?P<verb>완료\s*취소|다시\s*미완료(?:로)?\s*(?:바꿔|변경해)?|미완료(?:로)?\s*(?:바꿔|변경해)?)' + ENDING + r'(?:\s*(?:줘|주세요|줘요))?$')),
    ('complete', re.compile(r'(?P<body>.*?)\s*(?P<verb>완료했어|완료했어요|다\s*했어|다\s*했어요|완료(?:\s*처리)?|끝냈어|끝났어|마쳤어)' + ENDING + r'$')),
    ('delete', re.compile(r'(?P<body>.*?)\s*(?P<verb>삭제|지워|없애)(?:\s*(?:해\s*)?(?:줘|주세요|줘요)|해요|해)?(?:요)?$')),
)
ALARM = re.compile(r'(?P<body>.*?)\s*알람(?:을)?\s*(?P<verb>맞춰|설정(?:해)?|등록(?:해)?)(?:\s*(?:줘|주세요|줘요))?(?:요)?$')
DOMAIN = {'todo': r'할\s*일', 'calendar': r'일정|스케줄|달력|캘린더',
          'memo': r'메모(?!리)|노트', 'alarm': r'알람|알림', 'timer': r'타이머|timer'}
UNSUPPORTED = re.compile(r'그\s*(?:거|것|일|일정|항목)|아까|방금|마지막|첫\s*번째|두\s*번째|'
                         r'\d+\s*번째|두\s*개|여러|나머지|그중|중에서|밀린|기한\s*지난|연체|'
                         r'검색|찾아|관련|이름\s*바|미뤄|옮겨|수정|고쳐|일시\s*정지|재개')
UNSAFE = re.compile(r'["“”‘’\'`;\n\r\x00]|만약|조건|경우|라면|한다면|하면|했으면|할까|해도\s*될|할\s*수\s*있|'
                    r'하지\s*마|하지\s*말|지\s*(?:마|말|않)|안\s*해|말고|아니|제외|빼고|또는|혹은|거나|'
                    r'그리고|추가하고|삭제하고|완료하고|하고\s*(?:나서|그리고)|매일|매주|매월|반복|마다|'
                    r'공유|고정|우선순위|카테고리')


def _erase(text, start, end):
    return text[:start] + ' ' * (end-start) + text[end:]


def parse_semantic(text: str, at: str, tz: str, *, raw: str | None = None) -> SemanticFrame | None:
    if not isinstance(text, str) or not 1 <= len(text) <= 1500:
        return None
    source = re.sub(r'[?？!！.。]+$', '', text).rstrip()
    names = [k for k, p in DOMAIN.items() if re.search(p, source, re.I)]
    # Timer semantics/ownership and memo selection keep their existing exact paths.
    if 'timer' in names or 'memo' in names or len(names) > 1:
        return None
    match, action, widget = None, None, names[0] if names else None
    if widget == 'alarm':
        match, action = ALARM.fullmatch(source), 'set'
    else:
        match = ADD.fullmatch(source)
        if match and widget in {'todo', 'calendar'}:
            action = 'add'
        else:
            match = None
            for candidate, pattern in MUTATIONS:
                found = pattern.fullmatch(source)
                if found:
                    match, action = found, candidate
                    widget = widget or 'todo'
                    break
            if not match:
                match = READ.fullmatch(source)
                if match and widget in {'todo', 'calendar'}:
                    action = 'list'
    if not match or action is None or widget is None:
        return None
    # We never promote existing bulk writes into a narrower single-item action.
    if action != 'list' and re.search(r'전체|전부|모두|모든|다\s*(?:삭제|완료|지워)|할\s*일.*한꺼번에', source):
        return None
    if widget == 'calendar' and action in {'complete', 'reopen'}:
        return None
    ev = (Evidence('action', match.start('verb'), match.end('verb'), match['verb']),)
    fact = Temporal()
    args = {}
    target = None
    def frame(confidence='EXACT', issue=None, field=None, message=None):
        return SemanticFrame(widget, action, tuple(sorted(args.items())), fact, target,
                             ev + fact.evidence, confidence, issue, field, message)
    # Pending READ adjectives are source constraints, not negative imperatives.
    safety = source
    if action == 'list':
        safety = re.sub(r'완료하지\s*않은|하지\s*않은|안\s*끝낸', '미완료', safety)
    if UNSAFE.search(safety) or (raw and re.search(r'[\n\r\x00]', raw)):
        return frame('UNSUPPORTED', 'UNSAFE_SEMANTIC_SOURCE', None,
                     '부정·조건·인용·반복·복합 요청은 실행하지 않습니다. 한 요청으로 다시 말씀해 주세요.')
    # Slot extraction is restricted to the body, preserving offsets to the source.
    body = source[:match.end('body')]
    if widget != 'alarm':
        noun = re.search('(?:' + DOMAIN[widget] + r')(?:\s*목록)?(?:에는|으로|에|로|을|를|이|은|는|가|만)?', body)
        if noun:
            ev += (Evidence('domain', noun.start(), noun.end(), source[noun.start():noun.end()]),)
            body = _erase(body, noun.start(), noun.end())
    try:
        fact, remaining = extract_temporal(body, at, tz)
    except TemporalError as exc:
        fact = exc.partial
        if action in {'add', 'set'}:
            args = {'date': fact.start, 'time': fact.time}
            if action == 'add':
                args['title'] = None
        return frame('MISSING', 'TEMPORAL_NOT_EXACT', 'args.time' if fact.start else 'args.date', str(exc))
    # Preserve the old safety gate except for temporal spans that this parser
    # has now proved as a complete range/clock (rather than deleting guard words).
    from .command_routing import unsafe_source
    guarded_source = source
    for evidence in fact.evidence:
        guarded_source = _erase(guarded_source, evidence.start, evidence.end)
    if action == 'list':
        guarded_source = re.sub(r'완료하지\s*않은|하지\s*않은|안\s*끝낸', '미완료', guarded_source)
    if unsafe_source(guarded_source):
        return frame('UNSUPPORTED', 'UNSAFE_SEMANTIC_SOURCE', None,
                     '부정·조건·인용·반복·복합 요청은 실행하지 않습니다. 한 요청으로 다시 말씀해 주세요.')
    if UNSUPPORTED.search(remaining):
        return frame('UNSUPPORTED', 'UNSUPPORTED_SEMANTIC_CONSTRAINT', None,
                     '이 요청의 검색·순서·문맥·추가 조건은 아직 안전하게 적용할 수 없습니다. 조건을 생략하지 않았습니다.')
    if re.search(r'부터|까지|이후|이전|뒤|전부|모두|모든', remaining) and action != 'list':
        return frame('UNSUPPORTED', 'UNCONSUMED_CONSTRAINT', None,
                     '남은 범위 조건을 생략해 변경하지 않습니다. 한 항목을 명확히 말씀해 주세요.')
    if fact.relative_minutes is not None and (action not in {'add', 'set'} or not fact.exact_minute):
        return frame('UNSUPPORTED', 'RELATIVE_CLOCK_PRECISION', 'args.time',
                     '현재 예약은 분 단위입니다. 상대 시각을 반올림하지 않고, 정확한 날짜와 오전·오후 시간을 다시 확인합니다.')
    if fact.period and action != 'list':
        return frame('MISSING', 'CLOCK_REQUIRED', 'args.time', '오전·오후와 정확한 시각을 말씀해 주세요.')
    if action == 'list':
        if fact.time is not None:
            return frame('UNSUPPORTED', 'UNSUPPORTED_TIME_FILTER', 'args.time',
                         '현재 조회 계약은 날짜 범위와 오전·오후를 지원합니다. 특정 시각 전후 조건을 생략하지 않았습니다.')
        # State words are consumed only in a list query, never rewritten inside a title.
        state_text = remaining
        try:
            selected_state = read_status(state_text, widget)
        except ValueError as exc:
            return frame('MISSING', 'STATUS_CONFLICT', 'args.status', str(exc))
        remaining = re.sub(r'(?<![가-힣])(?:아직\s*)?(?:완료하지\s*않은|하지\s*않은|안\s*끝낸|'
                           r'완료된|완료한|끝낸|미완료(?:인)?|남은|전체|모든|전부|모두)(?:\s|$)', ' ', remaining)
        remaining = re.sub(r'(?<![가-힣])(?:좀|간단히|짧게)(?=\s|$)', ' ', remaining).strip(' ,，')
        # Collection nouns after an explicit state are not a literal item title.
        # An unqualified "것" is NOT a conversation reference resolver.
        if re.fullmatch(r'(?:해야\s*)?(?:목록|것|거|항목)(?:들)?(?:을|를|만|이|은|는)?', remaining):
            if remaining.startswith('목록') or read_status(state_text, widget) != ('pending' if widget == 'todo' else 'all') or re.search(r'남은|미완료|해야|완료한|완료된|끝낸', state_text):
                remaining = ''
        if remaining == '해야':
            remaining = ''  # "해야 할 일": a pending-list noun phrase.

        if remaining:
            # A literal named item can use an existing get, but ordinal/session references cannot.
            action = 'get'
            if fact.period or (fact.start is not None and fact.start != fact.end) or selected_state != read_status(remaining, widget):
                return frame('UNSUPPORTED', 'UNSUPPORTED_TARGET_FILTER', None,
                             '대상 조회는 정확한 제목과 한 날짜로 요청하세요. 추가 조건을 생략하지 않습니다.')
            target = re.sub(r'(?:을|를)$', '', remaining).strip()
        else:
            try:
                state = read_status(state_text, widget)
            except ValueError as exc:
                return frame('MISSING', 'STATUS_CONFLICT', 'args.status', str(exc))
            day = fact.start
            end = fact.end
            if day is None:
                ref = '전체' if re.search(r'전체|모든', source) else None
                dates = resolve_range(ref, at, tz)
                day, end = dates.start, dates.end
            args = {'start': day, 'end': end, 'status': state, 'period': fact.period}
            return frame()
    elif action == 'set':
        if remaining.strip(' ,，'):
            return None  # Alarm labels/free-form paraphrases stay on the existing parser.
        args = {'date': fact.start, 'time': fact.time}
        if fact.start is None or fact.time is None:
            return frame('MISSING', 'MISSING_REQUIRED_ARGUMENT', 'args.date' if fact.start is None else 'args.time',
                         '알람의 날짜와 오전·오후를 포함한 시각을 말씀해 주세요.')
        if fact.start != fact.end or fact.relation is not None:
            return frame('UNSUPPORTED', 'UNSUPPORTED_ALARM_RANGE', None, '알람은 하나의 정확한 시각으로 요청하세요.')
        return frame()
    else:
        remaining = remaining.strip(' ,，')
        # Suffix particles are grammar, not fuzzy title editing. Only boundary spans are removed.
        target = re.sub(r'(?:을|를)$', '', remaining).strip()
        if not target or re.fullmatch(r'(?:좀|하나(?:만)?|한\s*개(?:만)?)(?:\s+(?:좀|하나(?:만)?|한\s*개(?:만)?))*', target):
            target = None
        if action == 'add':
            args = {'title': target, 'date': fact.start, 'time': fact.time}
            if fact.start is None or target is None:
                return frame('MISSING', 'MISSING_REQUIRED_ARGUMENT', 'args.date' if fact.start is None else 'args.title',
                             '추가할 날짜와 할 일·일정의 제목을 명확히 말씀해 주세요.')
            if fact.start != fact.end or fact.relation not in {None, '까지'}:
                return frame('UNSUPPORTED', 'UNSUPPORTED_MUTATION_RANGE', None,
                             '등록은 한 날짜와 시각으로 요청하세요. 범위·전후 조건을 생략하지 않습니다.')
            target = None  # A create title is not an existing entity reference.
        elif fact.time is not None or fact.period or (fact.start is not None and fact.start != fact.end):
            return frame('UNSUPPORTED', 'UNSUPPORTED_TARGET_FILTER', None,
                         '단일 대상은 정확한 제목과 필요하면 한 날짜로 지정하세요. 시간·범위를 추측하지 않습니다.')
    literal = dict(args).get('title') if action == 'add' else target
    if literal:
        start = source.find(literal)
        if start < 0 or len(literal) > 240:
            return frame('MISSING', 'NONCONTIGUOUS_TITLE', 'args.title',
                         '제목은 원문에 연속해서 있는 문구로 말씀해 주세요. 중간 조건을 지우고 새 제목을 만들지 않습니다.')
        ev += (Evidence('title' if action == 'add' else 'target_text', start, start + len(literal), literal),)
    elif action != 'add':
        return frame('MISSING', 'TARGET_REQUIRED', 'target', '대상의 정확한 제목을 말씀해 주세요.')
    return frame()
