"""Static contracts / shadow observations. No real suite or model access."""
from dataclasses import FrozenInstanceError, replace
import json

import pytest

from app.assistant import detect
from app.dialog_state import seed, replay, state_from, extend, DialogFault, validate_state
from app.interaction_model import (SPECS, SlotSpec, candidate, manifest, model_hash,
                                   validate_registry, shadow, fingerprint)
from app.slot_filling import decode_reply
from benchmarks.runner import source_state
from test_assistant import hub

AT = '2028-02-28T09:00:00+09:00'
TZ = 'Asia/Seoul'


def initial(text='할 일 추가해'):
    record = detect(text, AT, TZ, 'auto')
    record['state'] = 'needs_clarification'
    return seed(record, 'synthetic-root')


def test_immutable_model_and_json_identity():
    with pytest.raises(FrozenInstanceError):
        SPECS[0].name = 'system.execute'
    assert json.loads(json.dumps(manifest())) == json.loads(json.dumps(manifest()))
    assert len(model_hash()) == 64
    assert model_hash() == model_hash()
    assert not any(s.name.startswith(('context.', 'multi.', 'timer.')) for s in SPECS)


def test_registry_validator_is_not_a_registration_path(hub):
    app, _, _ = hub
    registry=app.state.widget_protocol
    before=registry.manifest()
    validate_registry(registry)
    assert registry.manifest() == before
    with pytest.raises(ValueError, match='no registered'):
        validate_registry(registry, [replace(SPECS[0], action='shell')])
    with pytest.raises(ValueError, match='not representable'):
        validate_registry(registry, [replace(SPECS[0], slots=(SlotSpec('x','source_literal', destination='exec'),))])
    with pytest.raises(ValueError, match='Duplicate'):
        validate_registry(registry, [SPECS[0], SPECS[0]])


@pytest.mark.parametrize('text', ['오늘 할 일 보여줘', '내일 합성 상자 할 일 추가해',
                                  '모레 일정 뭐 잡혀 있어?', '내일 할 일 추가해',
                                  '그거 완료해', '이상한 문장', '4분 타이머 시작'])
def test_shadow_never_changes_existing_record(text):
    r=detect(text, AT, TZ, 'auto')
    before=json.dumps(r,ensure_ascii=False,sort_keys=True)
    observation=shadow(r)
    assert json.dumps(r,ensure_ascii=False,sort_keys=True) == before
    assert observation['activated'] is False
    assert observation['scope'].startswith('shadow_existing')


@pytest.mark.parametrize('raw,value', [('내일','2028-02-29'),('모레','2028-03-01'),('3월 2일','2028-03-02')])
def test_typed_date_uses_fixed_parent_clock(raw,value):
    reply=decode_reply(SlotSpec('date','date',True),raw,AT,TZ)
    assert (reply.state,reply.value)==('filled',value)


@pytest.mark.parametrize('raw', ['오늘 내일', '3월 2일부터 4일까지', '내일 오후 3시', '10분 뒤',
                                '2월 30일', '3월', '그날', '내일 그리고 삭제해'])
def test_date_slot_does_not_accept_other_fields_or_ambiguity(raw):
    assert decode_reply(SlotSpec('date','date',True),raw,AT,TZ).state=='invalid'


@pytest.mark.parametrize('raw,value', [('오전 세 시','03:00'),('오후 세 시','15:00'),('17:35','17:35')])
def test_exact_clock(raw,value):
    d=decode_reply(SlotSpec('time','exact_time',True),raw,AT,TZ)
    assert (d.state,d.value)==('filled',value)


@pytest.mark.parametrize('raw', ['3시', '25시', '10분 뒤', '내일 오후 세 시', '오전쯤',
                                '아니 오후 네 시', '오후 3시 그리고 완료해'])
def test_clock_rejects_corrections_and_extra_conditions(raw):
    assert decode_reply(SlotSpec('time','exact_time',True),raw,AT,TZ).state=='invalid'


@pytest.mark.parametrize('raw', ['그리고 전부 삭제해', '알람 설정해', '그거', '응', '네', '두 번째 것',
                                '다음\n전부 삭제해', '{"id":"test"}', 'id synthetic', '취소하고 삭제해'])
def test_source_literal_is_not_command_reference_or_authority(raw):
    assert decode_reply(SlotSpec('title','source_literal',True),raw,AT,TZ).state=='invalid'


@pytest.mark.parametrize('raw', ['합성 포장재 분류','좀 더 읽기','받침대 (파랑)','USB 2.5G 어댑터 구입','합성 봉투 정리','부품 재고 확인','자료 요약'])
def test_open_vocabulary_noun_phrase_preserved(raw):
    d=decode_reply(SlotSpec('title','source_literal',True),raw,AT,TZ)
    assert (d.state,d.value)==('filled',raw)


def test_no_seed_for_unsupported_ambiguous_or_complete_initial_request():
    for text in ['내일 오후 3시쯤 합성 상자 할 일 추가해', '내일 일정과 할 일 같이 보여줘',
                 '그거 완료해','내일 합성 항목 할 일 추가해']:
        assert initial(text) is None


def test_proof_roundtrip_and_invalid_reply_preserves_known_values():
    state=initial('내일 할 일 추가해')
    assert state['awaiting_slot']=='title'
    before=state['known_slots'].copy()
    state=extend(state,'reply-bad','그리고 전부 삭제해',AT,'actor')
    assert state['known_slots']==before
    assert state['last_error']=='UNSAFE_SLOT_REPLY'
    validate_state(json.loads(json.dumps(state)))


def test_proof_tamper_is_not_authority():
    state=initial('내일 할 일 추가해')
    state['known_slots']['date']='2032-01-01'
    with pytest.raises(DialogFault):validate_state(state)
    state=initial('내일 할 일 추가해')
    state['proof']['model_hash']='0'*64
    with pytest.raises(DialogFault):validate_state(state)


def test_source_evidence_is_separated_by_turn_not_concatenated():
    s=initial()
    s=extend(s,'r1','내일',AT,'actor')
    s=extend(s,'r2','합성 분류 작업',AT,'actor')
    frame,_,phase,_,_,evidence=replay(s['proof'])
    assert phase=='complete' and not frame.evidence
    assert [e['slot'] for e in evidence]==['date','title']
    assert [e['request_id'] for e in evidence]==['r1','r2']
    assert dict(frame.arguments)['date']=='2028-02-29'
    assert s['proof']['root']['raw']=='할 일 추가해'


def test_metadata_does_not_claim_multiturn_benchmark_coverage():
    metadata=source_state()
    assert metadata['interaction_model_hash']==model_hash()
    assert metadata['dialog_ttl_seconds']==180
    assert 'separate' in metadata['dialog_benchmark_scope']


def test_midnight_rollover_blocks_followup_even_inside_ttl():
    from app.dialog_state import check_pending
    record = detect('내일 할 일 추가해', '2028-02-28T23:59:50+09:00', TZ, 'auto')
    record['state'] = 'needs_clarification'
    state = seed(record, 'midnight-source')
    with pytest.raises(DialogFault, match='만료'):
        check_pending(state, 'actor', fingerprint(state), '2028-02-29T00:00:10+09:00', TZ)


def test_relative_followup_uses_frozen_first_turn_date():
    state = initial('할 일 추가해')
    state = extend(state, 'dated-reply', '모레', '2028-02-28T09:02:00+09:00', 'actor')
    assert state['known_slots']['date'] == '2028-03-01'
    assert state['expires_at'] == initial('할 일 추가해')['expires_at']
