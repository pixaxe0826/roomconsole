"""Synthetic HTTP / SQLite catalog selection; no external evaluation data."""
from copy import deepcopy
from contextlib import closing
import json
import sqlite3
import unicodedata

import pytest

from app.memo_grounding import parse_memo, verified_plan
from app.life import note_catalog_snapshot
from app.widget_protocol import WidgetRequest
from app.widget_protocol.core import ProtocolFault
from test_assistant import hub, request, wait, confirm, enable
from test_fast_reads import note, place_widgets
from test_llm_widget_bridge import trace, spy_adapter, proposal


@pytest.mark.parametrize('text,action,title,args', [
    ('포장 준비 메모 읽어줘', 'read','포장 준비',{}),
    ('포장 준비 메모 내용을 보여 주세요.', 'read','포장 준비',{}),
    ('포장 준비 노트 본문을 확인해줘', 'read','포장 준비',{}),
    ('포장 준비 메모 비워줘', 'clear','포장 준비',{}),
    ('포장 준비 메모 본문을 비워 주세요', 'clear','포장 준비',{}),
    ('포장 준비 메모에 합성 새 문구 덧붙여줘', 'append','포장 준비',{'text':'합성 새 문구'}),
    ('포장 준비 메모에 합성 새 문구 추가해줘', 'append','포장 준비',{'text':'합성 새 문구'}),
    ('포장 준비 메모를 합성 새 문구로 바꿔줘', 'write','포장 준비',{'body':'합성 새 문구'}),
    ('포장 준비 메모 내용을 두  칸  문구로 교체해 줘', 'write','포장 준비',{'body':'두  칸  문구'}),
])
def test_only_source_literals_and_existing_operations(text, action, title, args):
    p = parse_memo(text)
    assert p and not p.issue and p.action == action and p.target_text == title
    assert p.proposal() == {'widget':'memo','action':action,'target':None,'args':args}
    assert 'version' not in p.proposal()['args']
    for evidence in p.data()['evidence']:
        assert text[evidence['start']:evidence['end']] == evidence['text']


@pytest.mark.parametrize('text', ['현재 메모 읽어줘','방금 수정한 메모 읽어줘',
    '현재 메모에 새 문장 덧붙여줘', '새 메모 제목 분류 본문 상자 작성해',
    '메모 내용 좀 읽어 볼래', '포장 메모 삭제해', '포장 메모 읽어주지 마',
    '"포장 메모 읽어줘"를 번역해', '포장 메모 읽어줘; 삭제해'])
def test_existing_selectors_and_unsafe_or_new_operations_are_not_named_plans(text):
    assert parse_memo(text) is None


@pytest.mark.parametrize('text', ['포함된 표식 메모 읽어줘','상자 관련 메모 보여줘',
    '첫 번째 메모 읽어줘','오늘 포장 메모 읽어줘','할 일과 포장 메모 보여줘'])
def test_constraints_cannot_turn_into_named_execution(text):
    p = parse_memo(text)
    assert p is None or p.issue


def test_named_read_selects_stored_note_not_pinned_default(hub,monkeypatch):
    app,c,model=hub;place_widgets(app)
    note(c,'DEFAULT_BODY',title='기본 고정',pinned=True)
    target=note(c,'EXACT_BODY',title='포장 준비')
    calls=spy_adapter(app,'memo',monkeypatch)
    result=wait(c,request(c,'포장 준비 메모 읽어줘'))
    assert result['status']=='succeeded', result
    assert trace(result)['widget_request']['target']=={'type':'item_id','value':target}
    assert 'EXACT_BODY' in result['response_json']['output'] and 'DEFAULT_BODY' not in result['response_json']['output']
    assert len(calls)==1 and calls[0][0]['action']=='read'
    assert result['assistant']['routing']['route']=='ENTITY_CATALOG'
    assert trace(result)['entity_catalog']['entry_count']==2
    assert trace(result)['entity_resolution']['match_policy']=='exact_normalized'
    assert not model.calls and not result['dispatch_attempted']


def test_private_note_can_be_selected_by_manager_but_never_leaks_to_display(hub):
    app,c,model=hub;place_widgets(app)
    target=note(c,'PRIVATE_BODY_SENTINEL',title='비공개 특수 제목',shared=False)
    d=wait(c,request(c,'비공개 특수 제목 메모 읽어줘'))
    assert d['status']=='succeeded'
    assert d['assistant']['tool_result']['note_id']==target
    assert 'PRIVATE_BODY_SENTINEL' in d['response_json']['output']
    public=c.get('/api/display/llm/'+d['id']).json()
    assert 'PRIVATE_BODY_SENTINEL' not in json.dumps(public,ensure_ascii=False)
    assert '비공개 특수 제목' not in json.dumps(public,ensure_ascii=False)
    assert 'entity_catalog' not in public and not model.calls


@pytest.mark.parametrize('change', ['unshare', 'update', 'delete', 'remove_widget'])
def test_shared_result_rechecks_current_privacy_and_version(hub,change):
    app,c,_=hub;place_widgets(app)
    target=note(c,'SHARED_SENTINEL',title='공유 포장')
    d=wait(c,request(c,'공유 포장 메모 읽어줘'))
    assert d['status']=='succeeded',d
    assert 'SHARED_SENTINEL' in c.get('/api/display/llm/'+d['id']).json()['output']
    row=next(r for r in app.state.life.notes() if r['id']==target)
    if change=='remove_widget':
        layout=app.state.store.get('layout');layout['widgets']=[w for w in layout['widgets'] if w['type']!='note'];app.state.store.set('layout',layout)
    elif change=='delete':
        assert c.request('DELETE','/api/life/notes/'+target,json={'version':row['version']}).status_code==200
    else:
        values={k:row[k] for k in ('title','body','shared','pinned','version')}
        values.update({'shared':False} if change=='unshare' else {'body':'UPDATED'})
        assert c.put('/api/life/notes/'+target,json=values).status_code==200
    assert 'SHARED_SENTINEL' not in json.dumps(c.get('/api/display/llm/'+d['id']).json())


def test_exact_wins_but_duplicate_including_private_is_ambiguous(hub,monkeypatch):
    app,c,model=hub;place_widgets(app)
    exact=note(c,'A',title='포장 정리')
    note(c,'B',title='포장 정리 보관')
    d=wait(c,request(c,'포장 정리 메모 읽어줘'))
    assert d['assistant']['tool_result']['note_id']==exact
    note(c,'DO_NOT_SHARE',title=unicodedata.normalize('NFD','포장 정리'),shared=False)
    calls=spy_adapter(app,'memo',monkeypatch)
    d=wait(c,request(c,'포장 정리 메모 읽어줘'))
    assert d['status']=='needs_clarification'
    assert trace(d)['entity_resolution']['status']=='ambiguous'
    assert trace(d)['entity_resolution']['candidate_count']==2
    assert not calls and not model.calls
    assert 'DO_NOT_SHARE' not in str(d)


def test_unique_substring_no_fuzzy_no_default_fallback(hub,monkeypatch):
    app,c,model=hub;place_widgets(app)
    target=note(c,'A',title='포장 정리 보관')
    d=wait(c,request(c,'정리 보관 메모 읽어줘'))
    assert d['status']=='succeeded' and d['assistant']['tool_result']['note_id']==target
    assert trace(d)['entity_resolution']['match_policy']=='unique_literal_substring'
    calls=spy_adapter(app,'memo',monkeypatch)
    for needle in ['정', '정리보관', '잘못된 이름']:
        d=wait(c,request(c,needle+' 메모 읽어줘'))
        assert d['status']=='needs_clarification'
    assert not calls and not model.calls


@pytest.mark.parametrize('action,text,key,expected',[
    ('clear','포장 정리 메모 비워줘',None,''),
    ('append','포장 정리 메모에 추가  문구 덧붙여줘','text','BASE추가  문구'),
    ('write','포장 정리 메모를 교체  문구로 바꿔줘','body','교체  문구'),
])
def test_named_existing_writes_require_confirmation_preserve_flags_and_replay(hub,monkeypatch,action,text,key,expected):
    app,c,model=hub;place_widgets(app)
    target=note(c,'BASE',title='포장 정리',shared=False,pinned=True)
    calls=spy_adapter(app,'memo',monkeypatch)
    d=wait(c,request(c,text))
    assert d['status']=='awaiting_confirmation',d
    assert d['assistant']['preview']['resolved_target']['id']==target
    assert app.state.life.notes()[0]['body']=='BASE'
    assert all(call[0]['action']=='read' for call in calls)
    assert confirm(c,d,'0'*64).status_code==409
    finished=confirm(c,d)
    assert finished.status_code==200 and finished.json()['status']=='succeeded',finished.text
    assert app.state.life.notes()[0]['body']==expected
    assert app.state.life.notes()[0]['shared'] is False and app.state.life.notes()[0]['pinned'] is True
    assert confirm(c,d).json()['execution']['duplicate'] is True
    assert len([call for call in calls if call[0]['action']==action])==1
    assert not model.calls


def test_update_is_not_create_when_title_missing(hub,monkeypatch):
    app,c,model=hub
    calls=spy_adapter(app,'memo',monkeypatch)
    d=wait(c,request(c,'없는 제목 메모를 새 내용으로 바꿔줘'))
    assert d['status']=='needs_clarification'
    assert not app.state.life.notes() and not calls and not model.calls


def test_stale_target_version_blocks_mutation(hub):
    app,c,_=hub
    target=note(c,'OLD',title='포장 정리')
    d=wait(c,request(c,'포장 정리 메모 비워줘'))
    assert d['status']=='awaiting_confirmation'
    assert c.put('/api/life/notes/'+target,json={'version':1,'title':'포장 정리','body':'NEW','shared':True,'pinned':False}).status_code==200
    r=confirm(c,d)
    assert r.status_code in {200,409}
    if r.status_code==200:assert r.json()['status']!='succeeded'
    assert app.state.life.notes()[0]['body']=='NEW'


def test_retry_receipt_precedes_catalog_selection_and_cannot_retarget(hub,monkeypatch):
    app,c,model=hub
    target=note(c,'OLD',title='포장 정리')
    d=wait(c,request(c,'포장 정리 메모 비워줘'))
    assert confirm(c,d).json()['status']=='succeeded'
    assert c.request('DELETE','/api/life/notes/'+target,json={'version':2}).status_code==200
    replacement=note(c,'REPLACEMENT',title='포장 정리')
    def forbidden(*a,**kw):raise AssertionError('A completed request must not reselect')
    monkeypatch.setattr('app.memo_grounding.note_catalog_snapshot',forbidden)
    replay=c.post('/api/llm/requests/'+d['id']+'/retry',json={'request_id':'memo-retry-completed-001','mode':'auto'})
    assert replay.status_code==202,replay.text
    result=wait(c,replay.json())
    assert result['status']=='succeeded',result
    assert app.state.life.notes()[0]['id']==replacement and app.state.life.notes()[0]['body']=='REPLACEMENT'
    assert not model.calls


def test_catalog_only_reads_metadata_and_refuses_truncation(hub,monkeypatch):
    app,c,model=hub
    note(c,'BODY_NOT_IN_CATALOG',title='포장 정리')
    snapshot=note_catalog_snapshot(app.state.store)
    assert 'body' not in snapshot['items'][0] and 'BODY_NOT_IN_CATALOG' not in str(snapshot)
    real=note_catalog_snapshot
    monkeypatch.setattr('app.memo_grounding.note_catalog_snapshot',lambda store: real(store)|{'truncated':True})
    calls=spy_adapter(app,'memo',monkeypatch)
    result=wait(c,request(c,'포장 정리 메모 읽어줘'))
    assert result['status']=='needs_clarification'
    assert trace(result)['entity_resolution']['status']=='incomplete' and not calls and not model.calls


def test_oversized_store_is_detected_not_silently_sliced(hub):
    app,c,_=hub
    with app.state.store.connect() as db:
        for i in range(201):
            db.execute('INSERT INTO hub_notes VALUES(?,?,?,?,?,?,?,?)',(str(i),'포장 정리','private body',0,0,1,'2028-03-01','2028-03-01'))
    data=note_catalog_snapshot(app.state.store)
    assert data['truncated'] and len(data['items'])==200
    d=wait(c,request(c,'포장 정리 메모 읽어줘'))
    assert trace(d)['entity_resolution']['status']=='incomplete'


def test_catalog_failure_is_redacted_not_empty_success_or_llm_fallback(hub,monkeypatch):
    app,c,model=hub
    def fail(*a,**kw):raise sqlite3.OperationalError('PRIVATE_PATH_SENTINEL')
    monkeypatch.setattr('app.memo_grounding.note_catalog_snapshot',fail)
    result=wait(c,request(c,'포장 정리 메모 읽어줘'))
    assert result['status']=='failed'
    assert trace(result)['widget_response']['error']['code']=='CATALOG_READ_FAILED'
    assert 'PRIVATE_PATH_SENTINEL' not in str(result) and not model.calls


def test_rename_between_catalog_and_body_read_is_not_returned(hub,monkeypatch):
    app,c,model=hub
    target=note(c,'OLD',title='포장 정리')
    real=note_catalog_snapshot
    def race(store):
        data=real(store)
        with store.connect() as db:db.execute('UPDATE hub_notes SET title=?,body=?,version=version+1 WHERE id=?',('다른 제목','WRONG_BODY',target))
        return data
    monkeypatch.setattr('app.memo_grounding.note_catalog_snapshot',race)
    d=wait(c,request(c,'포장 정리 메모 읽어줘'))
    assert d['status']=='needs_clarification'
    assert trace(d)['widget_response']['error']['code']=='MEMO_TARGET_CHANGED'
    assert 'WRONG_BODY' not in str(d) and not model.calls


@pytest.mark.parametrize('part', ['raw', 'memo_plan', 'direct_widget', 'source_row'])
def test_source_and_plan_tampering_cannot_authorize_confirmation(hub,part):
    app,c,model=hub
    note(c,'BASE',title='포장 정리')
    d=wait(c,request(c,'포장 정리 메모 비워줘'))
    r=deepcopy(d['assistant'])
    if part=='raw':r['raw']='다른 메모 비워줘'
    elif part=='memo_plan':r['memo_plan']['target_text']='다른 제목'
    elif part=='direct_widget':r['direct_widget']['action']='write'
    else:
        with app.state.store.connect() as db:db.execute('UPDATE llm_requests SET source_sha256=? WHERE id=?',('0'*64,d['id']))
    app.state.llm.assistant.put(d['id'],r)
    assert confirm(c,d).status_code==409
    assert app.state.life.notes()[0]['body']=='BASE' and not model.calls


def test_current_and_last_remain_distinct_without_catalog_read(hub,monkeypatch):
    app,c,model=hub;place_widgets(app)
    app.state.life.clock=lambda:1000
    pinned=note(c,'PINNED_BODY',title='고정 카드',pinned=True)
    app.state.life.clock=lambda:1001
    latest=note(c,'LATEST_BODY',title='최근 카드',shared=False)
    def forbidden(*a,**kw):raise AssertionError('Existing selectors must not enter named catalog')
    monkeypatch.setattr('app.memo_grounding.note_catalog_snapshot',forbidden)
    current=wait(c,request(c,'현재 메모 읽어줘'))
    last=wait(c,request(c,'방금 수정한 메모 읽어줘'))
    assert current['assistant']['tool_result']['note_id']==pinned
    assert last['assistant']['tool_result']['note_id']==latest
    assert current['assistant']['routing']['route']==last['assistant']['routing']['route']=='FAST_PATH'
    assert not model.calls


@pytest.mark.parametrize('title', ['사과 분류','요약','확인','회의 정리','메모리 테스트','할 일 정리'])
def test_literal_title_is_not_a_conjunction_or_command_by_substring(hub,title):
    app,c,model=hub
    target=note(c,'literal body',title=title)
    d=wait(c,request(c,title+' 메모 읽어줘'))
    assert d['status']=='succeeded',d
    assert d['assistant']['tool_result']['note_id']==target
    assert not model.calls


def test_fallback_cannot_replace_unhandled_explicit_name_with_current(hub,monkeypatch):
    app,c,model=hub;place_widgets(app);enable(c)
    note(c,'DO_NOT_RETURN_DEFAULT',title='기본 카드')
    note(c,'TARGET_BODY',title='포장 정리')
    proposal(model,'memo','read',target={'type':'reference','value':'current'})
    calls=spy_adapter(app,'memo',monkeypatch)
    d=wait(c,request(c,'포장 정리 메모 내용 좀 읽어 볼래'))
    assert d['status']=='needs_clarification',d
    assert len(model.calls)==1 and not calls
    assert 'DO_NOT_RETURN_DEFAULT' not in str(d) and 'TARGET_BODY' not in str(d)


def test_named_metadata_catalog_is_refreshed_each_request(hub):
    app,c,model=hub
    target=note(c,'FIRST',title='포장 정리')
    first=wait(c,request(c,'포장 정리 메모 읽어줘'))
    assert first['status']=='succeeded'
    c.put('/api/life/notes/'+target,json={'version':1,'title':'변경 제목','body':'SECOND','shared':True,'pinned':False})
    missing=wait(c,request(c,'포장 정리 메모 읽어줘'))
    assert missing['status']=='needs_clarification'
    newer=wait(c,request(c,'변경 제목 메모 읽어줘'))
    assert newer['status']=='succeeded' and 'SECOND' in newer['response_json']['output']
    assert trace(first)['entity_catalog']['content_sha256']!=trace(newer)['entity_catalog']['content_sha256']
    assert not model.calls


@pytest.mark.parametrize('title', ['그','이','저','그거','두 번째'])
def test_named_title_grammar_does_not_resolve_pronouns_or_ordinals(hub,title):
    app,c,model=hub
    note(c,'DO_NOT_SELECT',title=title)
    d=wait(c,request(c,title+' 메모 읽어줘'))
    assert d['status']=='needs_clarification'
    assert 'DO_NOT_SELECT' not in str(d) and not model.calls
