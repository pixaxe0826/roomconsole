"""Source-only authority for the personal agent's immediate timer exception.

The model may propose a timer action, but cannot supply its duration or target.
Only the existing exact countdown grammar supplies that proof. Compound support
is intentionally narrow: leading exact timer clauses, then an explicit non-timer
request. We do not search arbitrary text for a convenient timer substring.
"""
from __future__ import annotations

import re
from typing import Any

from .timer_commands import exact_timer


_BOUNDARY = re.compile(r'(?:하고|고)\s+|\s+(?:그리고|또)\s+|[,;\n]\s*')
# Reject reported/quoted, negative, conditional, uncertain, and delayed intent.
# False negatives are preferable to granting an immediate control from prose.
_UNSAFE = re.compile(
    r'''["'`“”‘’「」『』]|하지|말(?:고|아)|않|싫|싶|(?:^|\s)안(?:\s|해|켜|꺼)|'''
    r'혹시|아마|정도|가능|할까|될까|까요|인지|경우|조건|[가-힣]면|[가-힣]때|'
    r'나중|이따|라고|라는|이란|예시|문구|문장|설명|방법|가정|생각'
)
_OTHER_DOMAIN = re.compile(r'할\s*일|투두|일정|캘린더|알람|메모|날씨|실내|온도|습도|시계|대시보드')
_OTHER_REQUEST = re.compile(r'추가|등록|설정|수정|삭제|조회|확인|알려|보여|읽어|적어|작성|완료|켜|꺼')
_CLARIFY = '즉시 타이머 제어는 원문에 명확히 말한 1~600초 시작 또는 특정/현재 타이머 종료만 가능합니다.'


def _exact_control(clause: str) -> dict[str, Any] | None:
    try:
        proof = exact_timer(clause)
    except ValueError:
        return None
    if proof and proof['action'] in {'start', 'stop'}:
        return proof
    return None


def source_timer_controls(source_text: str) -> tuple[dict[str, Any], ...]:
    """Parse ordered controls from source, never from a model's repaired text."""
    if not isinstance(source_text, str) or not source_text.strip() or len(source_text) > 2000:
        raise ValueError(_CLARIFY)
    remaining = source_text.strip()
    if _UNSAFE.search(remaining):
        raise ValueError(_CLARIFY)
    controls: list[dict[str, Any]] = []
    while remaining:
        whole = _exact_control(remaining)
        if whole is not None:
            controls.append(whole)
            return tuple(controls)
        for boundary in _BOUNDARY.finditer(remaining):
            proof = _exact_control(remaining[:boundary.start()].strip())
            tail = remaining[boundary.end():].strip()
            if proof is not None and tail:
                controls.append(proof)
                remaining = tail
                break
        else:
            # Explicit schedule words belong to the following non-timer task,
            # not a timer. An unparsed later timer makes the whole request unsafe.
            if (controls and '타이머' not in remaining
                    and _OTHER_DOMAIN.search(remaining) and _OTHER_REQUEST.search(remaining)):
                return tuple(controls)
            raise ValueError(_CLARIFY)
    raise ValueError(_CLARIFY)


def _proof_at(source_text: str, ordinal: int) -> dict[str, Any]:
    controls = source_timer_controls(source_text)
    if type(ordinal) is not int or not 0 <= ordinal < len(controls):
        raise ValueError('원문의 타이머 명령 한 개로 여러 제어를 실행할 수 없습니다.')
    return controls[ordinal]


def ground_timer_step(source_text: str, step: dict[str, Any], ordinal: int) -> dict[str, Any]:
    """Require the proposed step to equal the next unconsumed source control."""
    proof = _proof_at(source_text, ordinal)
    if step.get('action') != proof['action']:
        raise ValueError('모델의 타이머 동작이 원문과 다릅니다. 실행하지 않았습니다.')
    if any(step.get(key) not in (None, '', []) for key in ('date', 'time', 'start_date', 'end_date', 'weekdays')):
        raise ValueError('예약·반복 타이머는 즉시 제어로 바꿔 실행하지 않습니다.')
    if step.get('repeat_frequency') not in (None, '', 'none'):
        raise ValueError('예약·반복 타이머는 즉시 제어로 바꿔 실행하지 않습니다.')
    if proof['action'] == 'start':
        seconds = step.get('duration_seconds')
        if (type(seconds) is not int or seconds != proof['args']['duration_seconds']
                or step.get('target_id') or step.get('target_title')):
            raise ValueError('모델의 타이머 시간이 원문과 다릅니다. 실행하지 않았습니다.')
    else:
        if step.get('duration_seconds') is not None:
            raise ValueError('타이머 종료에 예약 시간을 추가할 수 없습니다.')
        if step.get('target_id'):
            if step.get('target_title'):
                raise ValueError('타이머 종료 대상을 하나만 명확히 지정해 주세요.')
            proposed = {'type': 'item_id', 'value': step['target_id']}
        else:
            proposed = {'type': 'reference', 'value': step.get('target_title') or 'current'}
        if proposed != proof['target']:
            raise ValueError('모델의 타이머 종료 대상이 원문과 다릅니다. 실행하지 않았습니다.')
    # New dictionaries prevent a caller from modifying the retained proof.
    return {'action': proof['action'], 'target': dict(proof['target']) if proof['target'] else None,
            'args': dict(proof['args'])}


def verify_timer_requests(source_text: str, requests: list[Any]) -> None:
    """Recompute every proof at the execution boundary, including after waits."""
    controls = source_timer_controls(source_text)
    if len(requests) != len(controls):
        raise ValueError('원문의 타이머 제어와 실행 계획의 수가 다릅니다. 실행하지 않았습니다.')
    for proof, request in zip(controls, requests):
        raw = request.model_dump(mode='json') if hasattr(request, 'model_dump') else request
        if not isinstance(raw, dict) or raw.get('widget') != 'timer' or raw.get('action') != proof['action']:
            raise ValueError(_CLARIFY)
        target = raw.get('target')
        if target is not None:
            target = {key: target.get(key) for key in ('type', 'value')}
        args = raw.get('args') or {}
        if target != proof['target'] or not isinstance(args, dict):
            raise ValueError(_CLARIFY)
        if proof['action'] == 'start':
            if (type(args.get('duration_seconds')) is not int
                    or args['duration_seconds'] != proof['args']['duration_seconds']
                    or set(args) - {'duration_seconds', 'label'}):
                raise ValueError(_CLARIFY)
        elif args:
            raise ValueError(_CLARIFY)
