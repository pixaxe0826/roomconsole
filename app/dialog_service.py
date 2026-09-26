"""Explicit manager-only continuation, transactionally linked to one parent.

Uses existing request/assistant JSON tables. Authentication supplies owner;
request JSON cannot supply identity, capabilities, slots, or approval.
"""
from __future__ import annotations

from copy import deepcopy
import hmac
import time

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .assistant import detect
from .dialog_state import (DialogFault, check_pending, descriptor, extend, fingerprint,
                           model_hash, replay, validate_state, verify_sources)


class DialogReply(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    request_id: str = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')
    text: str = Field(min_length=1, max_length=1500)
    expected_state_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


async def reply(hub, parent_id: str, body: DialogReply, owner: str):
    from .llm import MAX_HISTORY, encoded, sha, utcnow, uid
    from .semantic_bridge import install_semantic_frame
    if not owner or hub.widget_bridge is None:
        raise HTTPException(403, '인증된 관리자 대화만 이어갈 수 있습니다.')
    signature = sha(encoded({'dialog_parent': parent_id, 'owner': owner, **body.model_dump(exclude={'request_id'})}))
    async with hub.start_lock:
        now, child_id, started = utcnow(), uid(), time.perf_counter()
        parent = hub.get(parent_id)
        previous = parent.get('assistant') or {}
        state = previous.get('dialog_state')
        if not state:
            raise HTTPException(409, '이 기록에는 이어서 채울 필수 값이 없습니다. 새 요청으로 시작하세요.')
        try:
            validate_state(state)
            if state['owner'] is not None and not hmac.compare_digest(state['owner'], owner):
                raise DialogFault('DIALOG_OWNER_MISMATCH', '다른 관리자 세션의 대화에는 답할 수 없습니다.')
            duplicate = hub._duplicate(body.request_id, signature)
            if duplicate:
                return duplicate
            check_pending(state, owner, body.expected_state_sha256, now, hub.store.get('settings')['timezone'])
            if parent['status'] != 'needs_clarification':
                raise DialogFault('DIALOG_NOT_PENDING', '현재 추가 정보 대기 상태가 아닙니다.')
            ds = extend(state, child_id, body.text, now, owner)
            frame, _, phase, _, _, _ = replay(ds['proof'])
            root = ds['proof']['root']
            agent = detect(body.text, root['reference_at'], root['timezone'], 'auto')
            # The above only initializes a source record; its standalone candidate
            # is NEVER dispatched. The explicit pending slot is the sole decoder.
            agent.update(dialog_state=ds, dialog_owner=owner, route='clarify', origin='server',
                         proposal=None, state='needs_clarification', final_text=descriptor(ds)['prompt'])
            agent.pop('fast_read', None)
            agent['interaction_model'] = {'hash': model_hash(), 'version': '1.0.0', 'activated': True,
                                          'scope': 'explicit_bound_missing_slot_followup', 'candidate': ds['intent']}
            if phase == 'complete':
                install_semantic_frame(hub.widget_bridge, agent, frame, 0.0)
                agent['semantic_parser']['scope'] = 'multi_turn_source_proof; no authority or entity IDs'
                agent['widget_trace']['source_evidence'] = {'scope': 'multi_turn_source_proof',
                    'proof_sha256': ds['proof_sha256'], 'slot_provenance': ds['slot_provenance']}
            elif phase == 'cancelled':
                agent.update(state='cancelled', final_text='이 요청의 정보 입력을 취소했습니다. 위젯 데이터는 변경하지 않았습니다.')
            elif phase == 'exhausted':
                agent.update(state='needs_clarification', final_text='대화 횟수 제한에 도달했습니다. 전체 요청을 새로 입력해 주세요.')
            elif ds['last_error']:
                agent['final_text'] = '해당 값만 명확히 말씀해 주세요. ' + agent['final_text']
            agent['routing'].update(route='DIALOG_SLOT_FILL', route_reason='explicit_parent_and_typed_slot',
                                    llm_called=False, router_seconds=time.perf_counter()-started,
                                    resolved_intent=ds['intent'])
            local_payload = encoded({'route': 'DIALOG_SLOT_FILL', 'dialog_proof_sha256': ds['proof_sha256'],
                                     'local_plan': agent.get('direct_widget')})
            status = 'running' if phase == 'complete' else agent['state']
            response = None if phase == 'complete' else encoded({'output': agent['final_text'], 'output_source': 'server',
                                                               'action_state': status, 'metrics': {}, 'warnings': []})
            cfg = hub.config().model_dump()
            with hub.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                live = hub.assistant.get(parent_id, db)
                if not live or fingerprint(live.get('dialog_state')) != fingerprint(state):
                    raise DialogFault('DIALOG_STATE_CHANGED', '다른 답변이 먼저 접수됐습니다. 최신 요청을 확인하세요.')
                verify_sources(state['proof'], db)
                if db.execute('SELECT COUNT(*) FROM llm_requests').fetchone()[0] >= MAX_HISTORY:
                    raise HTTPException(429, '요청 기록 한도에 도달했습니다. 필요한 기록을 보관한 뒤 정리하세요.')
                db.execute('''INSERT INTO llm_requests(id,request_key,signature,voice_id,source_voice_id,parent_id,
                    source_text,source_sha256,source_meta,config_json,request_body,request_sha256,endpoint,status,
                    response_json,created_at,updated_at,queued_at,finished_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (child_id, body.request_id, signature, parent['voice_id'], parent['source_voice_id'], parent_id,
                     body.text, sha(body.text), encoded({'kind': 'dialog_reply', 'source': 'manager-dialog', 'parent_id': parent_id}), encoded(cfg),
                     local_payload, sha(local_payload), 'local://room-hub', status, response, now, now,
                     now if phase == 'complete' else None, now if phase != 'complete' else None))
                hub.assistant.attach(db, child_id, agent, parent_id)
                consumed = deepcopy(state)
                consumed.update(owner=owner, phase='continued', continuation_id=child_id)
                live['dialog_state'] = consumed
                hub.assistant.put(parent_id, live, db)
        except DialogFault as exc:
            raise HTTPException(403 if exc.code == 'DIALOG_OWNER_MISMATCH' else 409,
                                {'code': exc.code, 'message': str(exc)}) from None
        except (KeyError, TypeError, ValueError):
            raise HTTPException(409, {'code': 'DIALOG_PROOF_INVALID',
                                     'message': '대화 근거 형식이 달라졌습니다. 새 요청으로 시작하세요.'}) from None
    # Only complete source plans may reach the existing, confirmation-only write
    # preview. This route never schedules a model call or installs a worker.
    if phase == 'complete':
        await hub._execute_fast_read(child_id)
    await hub.notify('assistant.dialog_reply', child_id)
    return hub.get(child_id) | {'duplicate': False}
