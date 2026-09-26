"""Gold-free production Runtime/Adapter boundary, tiny synthetic external state."""
from benchmarks.runner import source_state
from test_benchmark_m3 import run
import pytest

FIXTURE = {'memos': [
    {'id':'synthetic-note-id', 'title':'포장 분류', 'body':'SYNTHETIC_BODY', 'shared':False},
    {'id':'synthetic-default', 'title':'기본 카드', 'body':'DEFAULT_BODY', 'shared':True, 'pinned':True},
]}


@pytest.mark.parametrize('mode', ['nlu','decision','full'])
@pytest.mark.parametrize('text,action', [('포장 분류 메모 읽어줘','read'),('포장 분류 메모 비워줘','clear')])
def test_named_memo_uses_same_production_code_and_stage_boundaries(mode,text,action):
    r=run(text,mode,fixture=FIXTURE)
    assert r['harness_error'] is None,r
    assert r['record']['routing']['route']=='ENTITY_CATALOG'
    assert r['stages_memo_plan']['action']==action
    assert not r['business_state_changed'] and not r['llm']['attempts']
    if mode=='nlu':
        assert r['stages_entity_catalog'] is None
        assert r['adapter_calls']==[] and r['boundary']=='nlu'
    else:
        assert r['stages_entity_catalog']['entry_count']==2
        assert r['stages_entity_catalog']['as_of'].startswith('2028-02-28')
        assert r['stages_entity_resolution']['status']=='resolved'
        assert r['request']['target']['value']=='synthetic-note-id'
        if action=='read' and mode=='decision':
            assert r['adapter_calls']==[] and r['boundary']=='decision'
        elif action=='read':
            assert r['final_status']=='succeeded'
            assert r['adapter_calls'][0]['adapter']=='FakeMemoAdapter'
        else:
            assert r['final_status']=='awaiting_confirmation'
            assert all(c['read_only'] and c['phase']=='grounding' for c in r['adapter_calls'])


def test_catalog_read_does_not_escape_owned_sqlite(monkeypatch):
    from app import memo_grounding
    original=memo_grounding.note_catalog_snapshot
    paths=[]
    def inspect(store):
        paths.append(str(store.path))
        assert 'room-hub-benchmark-' in str(store.path)
        return original(store)
    monkeypatch.setattr(memo_grounding,'note_catalog_snapshot',inspect)
    r=run('포장 분류 메모 읽어줘','full',fixture=FIXTURE)
    assert r['harness_error'] is None and len(paths)==1


def test_version_metadata_is_explicit_not_a_claim_of_case_coverage():
    state=source_state()
    assert state['entity_catalog_version']=='1.0.0'
    assert state['memo_grounding_version']=='1.0.0'
    assert state['layer_versions']['entity_catalog']
    assert state['parser_version']=='1.2.0'
