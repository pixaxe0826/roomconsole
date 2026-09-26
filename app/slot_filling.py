"""One reply, one awaited typed slot. Never parse it as a new executable intent."""
from __future__ import annotations

from dataclasses import dataclass, replace
import re

from .assistant import normalize
from .command_routing import unsafe_source
from .interaction_model import IntentSpec, SlotSpec
from .semantic_parser import UNSAFE, READ
from .semantic_temporal import extract_temporal, TemporalError
from .semantic_types import SemanticFrame

CANCEL = re.compile(r'(?:취소(?:해\s*(?:줘|주세요)?)?|그만(?:할게|해)?)')


@dataclass(frozen=True, slots=True)
class SlotReply:
    state: str  # filled / invalid / cancel
    value: str | None = None
    normalized: str = ''
    reason: str | None = None


def decode_reply(slot: SlotSpec, raw: str, at: str, timezone: str) -> SlotReply:
    if not isinstance(raw, str) or not 1 <= len(raw) <= 1500:
        return SlotReply('invalid', reason='INVALID_REPLY')
    text = normalize(raw)[0]
    if CANCEL.fullmatch(text):
        return SlotReply('cancel', normalized=text, reason='DIALOG_CANCELLED')
    if (not text or UNSAFE.search(text) or unsafe_source(text)
            or re.search(r'[\r\n\x00-\x1f]', raw)):
        return SlotReply('invalid', normalized=text, reason='UNSAFE_SLOT_REPLY')
    if slot.kind == 'source_literal':
        # Bounded literals only. "yes" is not approval and an imperative is not
        # a title/target. Preserve real noun phrases; never rewrite a title.
        if (len(text) > 240 or re.fullmatch(r'네|예|응|아니|아니요|그거|그것|이거|저거|취소|좀|하나', text)
                or re.search(r'번째|한\s*번에|[{}\[\]<>]|(?:^|\s)(?:ID|아이디)\b', text, re.I)
                or READ.fullmatch(text)
                or re.search(r'(?:해|줘|주세요|해요|했어|했어요|할래|바꿔|삭제|완료|등록)$', text)):
            return SlotReply('invalid', normalized=text, reason='LITERAL_REQUIRED')
        return SlotReply('filled', text, text)
    try:
        fact, rest = extract_temporal(text, at, timezone)
    except (TemporalError, ValueError):
        return SlotReply('invalid', normalized=text, reason='EXACT_SLOT_REQUIRED')
    if rest.strip() or fact.relative_minutes is not None or fact.period or fact.relation:
        return SlotReply('invalid', normalized=text, reason='SINGLE_SLOT_ONLY')
    if slot.kind == 'date' and fact.start and fact.start == fact.end and fact.time is None:
        return SlotReply('filled', fact.start, text)
    if slot.kind == 'exact_time' and fact.time and fact.start is None and fact.exact_minute:
        return SlotReply('filled', fact.time, text)
    return SlotReply('invalid', normalized=text, reason='EXACT_SLOT_REQUIRED')


def merge_slot(frame: SemanticFrame, slot: SlotSpec, reply: SlotReply) -> SemanticFrame:
    """Missing-only assignment; final permission/IDs still come from the server."""
    if reply.state != 'filled' or reply.value is None:
        raise ValueError('Only a validated filled reply can merge')
    args, temporal, target = dict(frame.arguments), frame.temporal, frame.target_text
    if slot.name == 'target_text':
        if target is not None:
            raise ValueError('Existing target cannot be edited in slot filling')
        target = reply.value
    elif slot.name == 'date':
        if temporal.start is not None:
            raise ValueError('Existing date cannot be edited in slot filling')
        temporal = replace(temporal, start=reply.value, end=reply.value, date_ref=reply.normalized)
        if frame.action in {'add', 'set'}:
            args['date'] = reply.value
    elif slot.name == 'time':
        if temporal.time is not None:
            raise ValueError('Existing time cannot be edited in slot filling')
        temporal = replace(temporal, time=reply.value)
        args['time'] = reply.value
    elif slot.name == 'title':
        if args.get('title') is not None:
            raise ValueError('Existing title cannot be edited in slot filling')
        args['title'] = reply.value
    else:
        raise ValueError('Slot filling is not enabled for this field')
    # Composite evidence is in dialog_proof (turn ID + normalized spans), not in
    # a fictitious concatenated utterance with misleading one-turn offsets.
    return replace(frame, arguments=tuple(sorted(args.items())), temporal=replace(temporal, evidence=()),
                   target_text=target, evidence=(), confidence='EXACT', issue=None, field=None, message=None)


PROMPTS = {'date': '날짜를 오늘·내일 또는 정확한 날짜로 말씀해 주세요.',
           'time': '오전·오후를 포함한 정확한 시각을 말씀해 주세요.',
           'title': '추가할 제목만 말씀해 주세요.',
           'target_text': '대상의 정확한 제목만 말씀해 주세요.'}


def prompt(slot: str | None) -> str:
    return PROMPTS.get(slot, '요청을 다시 확인해 주세요.')
