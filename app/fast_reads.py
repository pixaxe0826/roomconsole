"""Grounded read plans only: no backend/model calls and no business-data writes."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re

from .clock_service import prefix_date, resolve_range
from .command_semantics import known_read, READ_END, status_evidence, read_status
from .life import memo_snapshot
from .read_patterns import MEMO_PATTERNS, CALENDAR_PERIOD, DATED_BRIEF

INTENT_NAMES = {'memo.read': 'MEMO_READ', 'todo.list': 'TODO_LIST', 'calendar.query': 'CALENDAR_QUERY'}
MEMO_RULES = tuple((name, selector, re.compile(pattern)) for name, selector, pattern in MEMO_PATTERNS)
PERIOD_RULE = re.compile(CALENDAR_PERIOD + READ_END)
BRIEF_RULE = re.compile(DATED_BRIEF)


@dataclass(frozen=True)
class ReadPlan:
    intent: str
    date_ref: str | None = None
    status: str = 'all'
    period: str | None = None
    memo_ref: str | None = None
    pattern: str = ''

    def data(self):
        return asdict(self)

    def proposal(self):
        # Keep the existing compact proposal contract; read slots belong to the
        # server-only plan, never to a model's authority or a new action engine.
        return dict(intent=self.intent, date_ref=self.date_ref, title=None,
                    time=None, status=self.status, scope='one')


def match_read(text: str, at: str, tz: str) -> ReadPlan | None:
    if len(text) > 1500:
        return None
    s = re.sub(r'[?？!！.。]+$', '', text).strip()
    for name, selector, pattern in MEMO_RULES:
        if pattern.fullmatch(s):
            return ReadPlan('memo.read', memo_ref=selector, pattern=name)
    day, rest = prefix_date(s)
    period = PERIOD_RULE.fullmatch(rest)
    if period:
        # Explicit/default date uses the same frozen clock as the rest of Hub.
        resolve_range(day, at, tz)
        return ReadPlan('calendar.query', day, status_evidence(rest) or 'all',
                        'morning' if period['period'] == '오전' else 'afternoon',
                        pattern='calendar.period')
    brief = BRIEF_RULE.fullmatch(rest) if day else None
    if brief and not (brief['period'] and '할' in brief['noun']):
        resolve_range(day, at, tz)
        return ReadPlan('todo.list' if '할' in brief['noun'] else 'calendar.query', day,
                        status=read_status(rest, 'todo' if '할' in brief['noun'] else 'calendar'), period={'오전': 'morning', '오후': 'afternoon'}.get(brief['period']),
                        pattern='dated.brief')
    old = known_read(s, at, tz)
    if old:
        return ReadPlan(old['intent'], old.get('date_ref'), old['status'], pattern='existing.grounded_read')
    return None


def execute_read(store, plan: ReadPlan, at: str, tz: str):
    """Legacy direct reader; the integrated assistant now reads through Registry."""
    if plan.intent == 'memo.read':
        snapshot = memo_snapshot(store, plan.memo_ref)
    else:
        resolved = resolve_range(plan.date_ref, at, tz)
        snapshot = store.query_tasks(resolved.start, resolved.end, plan.status, limit=50, period=plan.period)
    return format_read(snapshot, plan, at, tz)


def format_read(snapshot, plan: ReadPlan, at: str, tz: str):
    """Pure deterministic formatter, shared by old readers and Adapter responses."""
    if plan.intent == 'memo.read':
        context = {k: snapshot.get(k) for k in (
            'selector', 'selection_policy', 'widget_id', 'note_id', 'version', 'updated_at', 'shared')}
        context['active_card_known'] = False  # Browser noteId is NOT backend state.
        if not snapshot['count']:
            text = '읽을 수 있는 저장 메모가 없습니다.'
        else:
            label = '가장 최근에 수정된 저장 메모' if snapshot['selection_policy'] == 'updated_at' else '기본 메모 위젯의 기본 카드'
            text = f'{label}에는 다음과 같이 적혀 있습니다:\n{snapshot["body"]}'
            if not snapshot['body']:
                text += '(본문이 비어 있습니다.)'
        return snapshot, text, context
    resolved = resolve_range(plan.date_ref, at, tz)
    context = {'date': resolved.start if resolved.start == resolved.end else None,
               'date_ref': plan.date_ref, 'range': [resolved.start, resolved.end],
               'timezone': tz, 'period': plan.period, 'status': plan.status,
               'date_policy': resolved.policy}
    noun = '일정' if plan.intent == 'calendar.query' else '할 일'
    qualifier = {'all': '', 'pending': '남은 ', 'completed': '완료한 '}[plan.status]
    label = '전체 날짜' if resolved.start == '0001-01-01' else plan.date_ref or '오늘'
    if plan.period:
        label += ' ' + {'morning': '오전', 'afternoon': '오후'}[plan.period]
        context['time_range'] = ['00:00', '12:00'] if plan.period == 'morning' else ['12:00', '24:00']
        context['time_range_end_exclusive'] = True
    if snapshot['count']:
        text = f'{label} {qualifier}{noun}: {snapshot["count"]}개입니다.'
        text += '\n' + '\n'.join(
            f'{t["date"]} {t["time"] or "시간 미지정"} · {t["title"]}' + (' [완료]' if t['completed'] else '')
            for t in snapshot['items'])
    else:
        text = f'{label} 등록된 {qualifier}{noun}이 없습니다.'
    if plan.period and snapshot.get('untimed_count'):
        text += f'\n시간 미지정 항목 {snapshot["untimed_count"]}개는 오전·오후를 판단할 수 없어 제외했습니다.'
    if snapshot['truncated']:
        text += '\n처음 50개를 표시했습니다. 전체 목록은 할 일 위젯에서 확인하세요.'
    return snapshot, text, context
