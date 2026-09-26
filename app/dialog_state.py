"""Bounded source-only dialog proofs stored in existing assistant_runs JSON.

No global last-pending request, no entity IDs, no permissions, and no generated
utterance. A proof replays original source + typed, explicitly linked replies.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hmac

from .assistant import normalize
from .clock_service import aware
from .interaction_model import (candidate, fingerprint, known_slots, missing_slots,
                                model_hash, spec_for)
from .semantic_types import SEMANTIC_VERSION
from .slot_filling import decode_reply, merge_slot, prompt

DIALOG_VERSION = '1.0.0'
TTL_SECONDS = 180
MAX_TURNS = 6


class DialogFault(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _fault(code='DIALOG_PROOF_INVALID', message='대화 근거가 달라졌습니다. 새 요청으로 시작하세요.'):
    raise DialogFault(code, message)


def root_frame(root):
    if set(root) != {'request_id', 'raw', 'reference_at', 'timezone'}:
        _fault()
    frame = candidate(normalize(root['raw'])[0], root['reference_at'], root['timezone'], raw=root['raw'])
    spec = spec_for(frame.widget, frame.action) if frame else None
    if (not spec or not spec.dialog or frame.confidence != 'MISSING'
            or frame.issue not in {'MISSING_REQUIRED_ARGUMENT', 'TARGET_REQUIRED'}
            or not missing_slots(frame, spec)):
        _fault('DIALOG_NOT_FILLABLE', '확정되지 않은 필수 값만 이어서 입력할 수 있습니다. 새 요청으로 시작하세요.')
    return frame, spec


def replay(proof: dict):
    """Verify structure and recompute EVERY value from immutable raw turns."""
    if (not isinstance(proof, dict)
            or set(proof) != {'version', 'model_hash', 'semantic_version', 'root', 'steps'}
            or proof['version'] != DIALOG_VERSION or proof['model_hash'] != model_hash()
            or proof['semantic_version'] != SEMANTIC_VERSION
            or not isinstance(proof['steps'], list) or len(proof['steps']) > MAX_TURNS):
        _fault()
    root = proof['root']
    frame, spec = root_frame(root)
    reference = aware(root['reference_at'], root['timezone'])
    phase, last_error = 'pending', None
    seen, previous_time, evidence = {root['request_id']}, reference, []
    for step in proof['steps']:
        if set(step) != {'request_id', 'raw', 'at'} or step['request_id'] in seen or phase != 'pending':
            _fault()
        seen.add(step['request_id'])
        when = aware(step['at'], root['timezone'])
        if (when < previous_time or when.date() != reference.date()
                or not 0 <= (when-reference).total_seconds() <= TTL_SECONDS):
            _fault('DIALOG_EXPIRED', '대화 시간이 만료되었거나 날짜가 바뀌었습니다. 새 요청으로 시작하세요.')
        previous_time = when
        missing = missing_slots(frame, spec)
        slot = next(s for s in spec.slots if s.name == missing[0])
        decoded = decode_reply(slot, step['raw'], root['reference_at'], root['timezone'])
        evidence.append({'request_id': step['request_id'], 'slot': slot.name,
                         'normalized': decoded.normalized, 'outcome': decoded.state,
                         'value': decoded.value, 'reason': decoded.reason,
                         'span': [0, len(decoded.normalized)], 'policy': 'whole_reply_typed_slot'})
        last_error = decoded.reason
        if decoded.state == 'cancel':
            phase = 'cancelled'
        elif decoded.state == 'filled':
            frame = merge_slot(frame, slot, decoded)
            if not missing_slots(frame, spec):
                phase = 'complete'
    missing = missing_slots(frame, spec)
    if phase == 'pending' and len(proof['steps']) >= MAX_TURNS:
        phase, last_error = 'exhausted', 'DIALOG_TURN_LIMIT'
    if phase != 'complete':
        frame = replace(frame, confidence='MISSING', issue='DIALOG_SLOT_REQUIRED',
                        field=missing[0] if missing else None, message=prompt(missing[0] if missing else None))
    return frame, spec, phase, missing, last_error, evidence


def state_from(proof, owner):
    frame, spec, phase, missing, error, evidence = replay(proof)
    return {'version': DIALOG_VERSION, 'proof': deepcopy(proof), 'proof_sha256': fingerprint(proof),
            'owner': owner, 'phase': phase, 'revision': len(proof['steps']),
            'expires_at': aware(proof['root']['reference_at'], proof['root']['timezone']).timestamp() + TTL_SECONDS,
            'intent': spec.name, 'known_slots': known_slots(frame, spec),
            'missing_slots': list(missing), 'awaiting_slot': missing[0] if phase == 'pending' else None,
            'last_error': error, 'slot_provenance': evidence}


def seed(record: dict, request_id: str) -> dict | None:
    if record.get('mode') != 'auto' or record.get('state') != 'needs_clarification' or record.get('dialog_state'):
        return None
    proof = {'version': DIALOG_VERSION, 'model_hash': model_hash(), 'semantic_version': SEMANTIC_VERSION,
             'root': {'request_id': request_id, 'raw': record['raw'],
                      'reference_at': record['reference_at'], 'timezone': record['timezone']}, 'steps': []}
    try:
        return state_from(proof, record.get('dialog_owner'))
    except (ValueError, TypeError, KeyError):
        return None  # Non-fillable/ambiguous/unsupported requests keep their existing behavior.


def validate_state(state: dict) -> None:
    if not isinstance(state, dict) or state.get('phase') not in {'pending', 'complete', 'cancelled', 'exhausted', 'continued'}:
        _fault()
    expected = state_from(state['proof'], state.get('owner'))
    actual = deepcopy(state)
    if actual.get('phase') == 'continued':
        if not isinstance(actual.pop('continuation_id', None), str):
            _fault()
        actual['phase'] = expected['phase']
    if fingerprint(actual) != fingerprint(expected):
        _fault()


def check_pending(state, owner, expected_sha256, now, timezone):
    validate_state(state)
    if not owner or (state['owner'] is not None and not hmac.compare_digest(state['owner'], owner)):
        _fault('DIALOG_OWNER_MISMATCH', '이 대화는 다른 관리자 세션에 연결되어 있습니다.')
    if not hmac.compare_digest(fingerprint(state), expected_sha256):
        _fault('DIALOG_STATE_CHANGED', '대화 상태가 바뀌었습니다. 최신 요청을 확인하세요.')
    if state['phase'] != 'pending':
        _fault('DIALOG_NOT_PENDING', '이미 이어서 처리했거나 종료된 대화입니다. 최신 요청을 확인하세요.')
    root = state['proof']['root']
    stamp, ref = aware(now, timezone), aware(root['reference_at'], root['timezone'])
    if timezone != root['timezone'] or stamp.date() != ref.date() or not 0 <= (stamp-ref).total_seconds() <= TTL_SECONDS:
        _fault('DIALOG_EXPIRED', '대화 시간이 만료되었거나 날짜·시간대가 바뀌었습니다. 새 요청으로 시작하세요.')


def extend(state, request_id, raw, now, owner):
    proof = deepcopy(state['proof'])
    proof['steps'].append({'request_id': request_id, 'raw': raw, 'at': now})
    return state_from(proof, owner)


def descriptor(state):
    """Manager UI metadata; no session hash, entity ID, or execution authority."""
    if not state:
        return None
    return {'phase': state['phase'], 'intent': state['intent'], 'awaiting_slot': state['awaiting_slot'],
            'missing_slots': state['missing_slots'], 'known_slots': state['known_slots'],
            'expires_at': state['expires_at'], 'state_sha256': fingerprint(state),
            'prompt': prompt(state['awaiting_slot']), 'last_error': state['last_error'],
            'continuation_id': state.get('continuation_id'), 'version': DIALOG_VERSION,
            'binding': 'same_manager_session_or_explicit_first_claim_for_unbound_source'}


def verify_record(record, *, store=None, db=None):
    """Source rows are compared as well as proof/frame hashes before execution."""
    state = record['dialog_state']
    validate_state(state)
    if state['phase'] != 'complete':
        _fault('DIALOG_INCOMPLETE', '빠진 정보를 먼저 입력하세요.')
    frame, _, phase, _, _, _ = replay(state['proof'])
    proof = state['proof']
    last = proof['steps'][-1]
    if (record.get('raw') != last['raw'] or normalize(last['raw'])[0] != record.get('normalized')
            or record.get('reference_at') != proof['root']['reference_at']
            or record.get('timezone') != proof['root']['timezone']
            or frame.data() != record.get('semantic_frame') or frame.proposal() != record.get('direct_widget')):
        _fault()
    if db is not None:
        verify_sources(proof, db)
    elif store is not None:
        from contextlib import closing
        with closing(store.connect()) as con:
            verify_sources(proof, con)
    return frame


def verify_sources(proof, db):
    from .llm import sha
    parent_id = None
    for index, step in enumerate([proof['root'], *proof['steps']]):
        row = db.execute('SELECT source_text,source_sha256,created_at,parent_id FROM llm_requests WHERE id=?', (step['request_id'],)).fetchone()
        stamp = step.get('at', step.get('reference_at'))
        if (row is None or row['source_text'] != step['raw'] or row['source_sha256'] != sha(step['raw'])
                or row['created_at'] != stamp or (index and row['parent_id'] != parent_id)):
            _fault('DIALOG_SOURCE_CHANGED', '대화 원본이 삭제되거나 달라졌습니다. 새 요청으로 시작하세요.')

        parent_id = step['request_id']
