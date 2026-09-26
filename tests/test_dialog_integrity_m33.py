"""M3.3 adversarial transport/proof tests using synthetic local SQLite only."""
from copy import deepcopy
from types import MappingProxyType
from dataclasses import replace
import json

import pytest

from app.assistant import detect
from app.interaction_model import manifest
from test_assistant import hub, request, wait, confirm, add
from test_dialog_state_m33 import send, follow
from test_llm_widget_bridge import tomorrow


def test_old_parent_cannot_confirm_a_completed_child(hub):
    app,c,model=hub
    root=wait(c,request(c,'내일 할 일 추가해'))
    child=follow(c,root,'합성 커넥터 점검')
    assert confirm(c,child,'0'*64).status_code==409
    r=c.post('/api/assistant/'+root['id']+'/confirm',json={'preview_sha256':child['assistant']['preview_sha256']})
    assert r.status_code==409
    assert not app.state.store.tasks() and not model.calls


@pytest.mark.parametrize('where,column,value',[
    ('root','source_sha256','0'*64),
    ('root','created_at','2020-01-01T00:00:00+00:00'),
    ('child','parent_id','another-notebook-request'),
])
def test_confirm_revalidates_original_hash_clock_and_parent_chain(hub,where,column,value):
    app,c,_=hub
    root=wait(c,request(c,'내일 할 일 추가해'))
    child=follow(c,root,'합성 용기 분류')
    identity=root['id'] if where=='root' else child['id']
    with app.state.store.connect() as db:
        db.execute(f'UPDATE llm_requests SET {column}=? WHERE id=?',(value,identity))
    assert confirm(c,child).status_code==409
    with app.state.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM assistant_effects').fetchone()[0]==0
    assert not app.state.store.tasks()


def test_changed_dialog_contract_refuses_reply_before_creating_child(hub,monkeypatch):
    app,c,model=hub
    root=wait(c,request(c,'내일 할 일 추가해'))
    monkeypatch.setattr('app.dialog_state.model_hash',lambda:'f'*64)
    assert send(c,root,'합성 패널 분류').status_code==409
    with app.state.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM llm_requests WHERE parent_id=?',(root['id'],)).fetchone()[0]==0
    assert not model.calls


def test_expired_preview_still_refuses_a_complete_dialog(hub):
    app,c,_=hub
    child=follow(c,wait(c,request(c,'내일 할 일 추가해')),'합성 완충재 분류')
    record=app.state.llm.assistant.get(child['id'])
    record['preview']['expires_at']=1
    from app.assistant import digest
    record['preview_sha256']=digest(record['preview'])
    app.state.llm.assistant.put(child['id'],record)
    assert c.post('/api/assistant/'+child['id']+'/confirm',json={'preview_sha256':record['preview_sha256']}).status_code==409
    assert not app.state.store.tasks()


def test_ambiguous_server_target_remains_unresolved_after_slot_filled(hub):
    app,c,model=hub
    add(c,'합성 격자 프레임',tomorrow(c));add(c,'합성 격자 프레임',tomorrow(c))
    d=follow(c,wait(c,request(c,'완료 처리해')),'합성 격자 프레임')
    assert d['status']=='needs_clarification'
    assert d['assistant']['widget_trace']['entity_resolution']['status']=='ambiguous'
    assert all(not t['completed'] for t in app.state.store.tasks()) and not model.calls
    assert send(c,d,'첫 번째').status_code==409  # No ordinal/general-context route.


def test_other_pending_request_is_not_consumed(hub):
    _,c,_=hub
    first=wait(c,request(c,'내일 할 일 추가해'))
    second=wait(c,request(c,'모레 할 일 추가해'))
    child=follow(c,first,'합성 봉투 정리')
    assert child['status']=='awaiting_confirmation', child
    assert c.get('/api/llm/requests/'+second['id']).json()['dialog']['phase']=='pending'


def test_completed_dialog_proof_does_not_enable_unregistered_action(hub):
    app,c,_=hub
    child=follow(c,wait(c,request(c,'내일 할 일 추가해')),'합성 안내 부착')
    record=app.state.llm.assistant.get(child['id'])
    record['semantic_frame']['action']='delete'
    app.state.llm.assistant.put(child['id'],record)
    assert confirm(c,child).status_code==409
    assert not app.state.store.tasks()


@pytest.mark.parametrize('text', ['오늘 할 일 보여줘','내일 일정 뭐 잡혀 있어?',
    '내일 합성 종이 할 일 추가해','4분 타이머 시작','현재 메모 읽어줘',
    '내일 할 일 추가해','안녕하세요','내일 알람 맞춰줘'])
def test_shadow_does_not_change_any_authoritative_first_turn_plan(hub,text):
    app,_,_=hub
    b=app.state.llm.widget_bridge
    r=detect(text,'2028-02-28T09:00:00+09:00','Asia/Seoul','auto')
    old=b._prepare(deepcopy(r));new=b.prepare(deepcopy(r))
    new.pop('interaction_model')
    def stable(v):
        if isinstance(v,dict):return {k:stable(x) for k,x in v.items() if k not in {'parser_ms','router_seconds'}}
        if isinstance(v,list):return [stable(x) for x in v]
        return v
    assert stable(old)==stable(new)


def test_registry_unavailable_after_reply_still_blocks_confirmation(hub):
    app,c,_=hub
    child=follow(c,wait(c,request(c,'내일 할 일 추가해')),'합성 완충 포장')
    adapter=app.state.widget_protocol.get('todo')
    ops=dict(adapter.operations);ops['add']=replace(ops['add'],available=False)
    adapter.operations=MappingProxyType(ops)
    assert confirm(c,child).status_code==409
    assert not app.state.store.tasks()
