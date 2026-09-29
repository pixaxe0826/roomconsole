"""Independent synthetic examples + semantic invariants, not private suite gold."""
from dataclasses import replace
from itertools import product
import json
import re

import pytest

from app.memo_language import (FAMILIES, COMPILED, GRAMMAR_VERSION, LanguageMatch,
                               SentenceFamily, recognize, validate_families, grammar_hash)
from app.memo_grounding import parse_memo, has_unhandled_name
from app.fast_reads import match_read
from app.widget_protocol.core import ProtocolFault
from test_assistant import hub, request, wait, enable, confirm
from test_fast_reads import note, place_widgets
from test_llm_widget_bridge import proposal, spy_adapter
from test_benchmark_m3 import run

AT = '2028-02-28T09:00:00+09:00'
TZ = 'Asia/Seoul'


def test_sentence_data_is_immutable_and_validated():
    from dataclasses import FrozenInstanceError
    validate_families()
    assert len(grammar_hash()) == 64
    with pytest.raises(FrozenInstanceError):
        FAMILIES[0].stage = 99
    with pytest.raises(ValueError):
        validate_families([FAMILIES[0], FAMILIES[0]])
    with pytest.raises(ValueError):
        validate_families([SentenceFamily('bad', 1, 'write', 'named', '(?P<title>.*)')])


def test_same_stage_semantic_conflict_refuses_instead_of_first_match():
    first = SentenceFamily('a', 1, 'read', 'named', '(?P<title>.+)')
    second = replace(first, name='b', action='clear')
    decision = recognize('synthetic', compiled=((first, re.compile(first.pattern)),
                                               (second, re.compile(second.pattern))))
    assert decision.family.issue == 'MEMO_GRAMMAR_AMBIGUOUS'
    assert decision.family.disposition == 'block'


def test_family_order_is_not_hidden_precedence():
    cases = ['합성 통관이라는 노트 내용 읽어 줘',
             '합성 문자열이라고 현재 메모에 한 줄 추가해 줘',
             '가장 최근에 편집한 노트 본문 보여줘',
             '그 노트 내용 알려줘', '합성 표식이 포함된 메모 찾아줘']
    for text in cases:
        assert recognize(text) == recognize(text, compiled=tuple(reversed(COMPILED)))


@pytest.mark.parametrize('title', ['합성 통관', '실험 캘리퍼', 'USB 2.5G 계측', '메모리 분류'])
@pytest.mark.parametrize('wrapper', ['라고 해둔', '이라고 해 둔', '라는', '이라는', '라고 이름 붙인'])
@pytest.mark.parametrize('noun', ['메모', '노트'])
def test_named_wrappers_extract_only_raw_title_span(title, wrapper, noun):
    raw = f'{title}{wrapper} {noun} 내용 알려줘'
    p = parse_memo(raw)
    assert p and not p.issue and p.target_text == title, p
    assert p.family.startswith('named.wrapper.')
    assert p.grammar_sha256 == grammar_hash()
    assert p.proposal()['target'] is None
    for field in p.data()['evidence']:
        assert raw[field['start']:field['end']] == field['text']
    assert next(e['text'] for e in p.data()['evidence'] if e['field']=='title') == title


@pytest.mark.parametrize('prefix', ['가장 최근에', '제일 최근', '마지막으로'])
@pytest.mark.parametrize('verb', ['편집한', '건드린', '저장한'])
def test_latest_family_uses_existing_read_selector(prefix, verb):
    text = f'{prefix} {verb} 노트 본문을 읽어 주세요'
    p = match_read(text, AT, TZ)
    assert p and p.memo_ref == 'last_modified'
    assert parse_memo(text) is None
    assert recognize(text).family.selector == 'last_modified'


@pytest.mark.parametrize('text', ['합성 자료라고 써둔 메모 읽어줘',
                                 '합성 자료라고 적어둔 메모 읽어줘'])
def test_body_report_is_not_silently_declared_note_name(text):
    p = parse_memo(text)
    assert p and p.issue == 'MEMO_NAMING_AMBIGUOUS'


def test_recent_exact_read_not_pinned_or_named_catalog(hub, monkeypatch):
    app, c, model = hub; place_widgets(app)
    app.state.life.clock = lambda: 1000
    note(c, 'PINNED', title='일반 고정', pinned=True)
    # Even a stored title identical to the selector text cannot steal selection.
    note(c, 'DECOY', title='가장 최근에 편집한')
    app.state.life.clock = lambda: 1001
    target = note(c, 'LATEST', title='합성 최신', shared=False)
    monkeypatch.setattr('app.memo_grounding.note_catalog_snapshot',
                        lambda *_: (_ for _ in ()).throw(AssertionError('selector is not a catalog')))
    d = wait(c, request(c, '가장 최근에 편집한 메모 내용 보여줘'))
    assert d['status'] == 'succeeded', d
    assert d['assistant']['routing']['route'] == 'FAST_PATH'
    assert d['assistant']['tool_result']['note_id'] == target
    assert d['assistant']['tool_result']['selection_policy'] == 'updated_at'
    assert not model.calls
    assert 'LATEST' not in json.dumps(c.get('/api/display/llm/'+d['id']).json())


def test_latest_tie_keeps_existing_ambiguity(hub):
    app, c, model = hub; place_widgets(app); app.state.life.clock = lambda: 1000
    note(c, 'A', title='합성 A'); note(c, 'B', title='합성 B')
    d = wait(c, request(c, '마지막으로 편집한 노트 내용 보여줘'))
    assert d['status'] == 'needs_clarification', d
    assert not model.calls


def test_wrapped_named_read_and_clear_keep_existing_authority(hub, monkeypatch):
    app, c, model = hub; place_widgets(app)
    note(c, 'DEFAULT', title='고정 화면', pinned=True)
    target = note(c, 'TARGET', title='합성 통관', shared=False)
    calls = spy_adapter(app, 'memo', monkeypatch)
    d = wait(c, request(c, '합성 통관이라고 해 둔 메모 내용 알려줘'))
    assert d['status'] == 'succeeded' and d['assistant']['tool_result']['note_id'] == target
    assert d['assistant']['memo_language']['family'] == 'named.wrapper.read'
    assert 'named.literal.read' in d['assistant']['memo_language']['shadowed_families']
    d = wait(c, request(c, '합성 통관이라는 메모 비워줘'))
    assert d['status'] == 'awaiting_confirmation', d
    assert next(n for n in app.state.life.notes() if n['id']==target)['body'] == 'TARGET'
    assert all(x[0]['action'] == 'read' for x in calls)
    assert confirm(c, d).json()['status'] == 'succeeded'
    assert confirm(c, d).json()['execution']['duplicate']
    assert len([x for x in calls if x[0]['action']=='clear']) == 1
    assert not model.calls


def test_content_first_current_append_is_delegated_not_named(hub, monkeypatch):
    app, c, model = hub; place_widgets(app); enable(c)
    target = note(c, 'BASE', title='기본 선택', pinned=True)
    # Payload includes a domain word; it must remain literal text, not a name.
    raw = '합성 메모 성능 다시 확인이라고 지금 메모에 한 줄 덧붙여줘'
    assert parse_memo(raw) is None and not has_unhandled_name(raw)
    assert recognize(raw).family.name == 'selector.content_first_append'
    proposal(model, 'memo', 'append', args={'text':'합성 메모 성능 다시 확인'})
    calls = spy_adapter(app, 'memo', monkeypatch)
    d = wait(c, request(c, raw))
    assert d['status'] == 'awaiting_confirmation', d
    assert d['assistant']['routing']['route'] == 'LLM_FALLBACK'
    preview = d['assistant']['preview']['widget_request']
    assert preview['target']['value'] == target
    assert preview['args']['text'] == '합성 메모 성능 다시 확인'
    assert all(x[0]['action']=='read' for x in calls)
    assert len(model.calls) == 1
    assert confirm(c, d).json()['status'] == 'succeeded'
    assert app.state.life.notes()[0]['body'] == 'BASE합성 메모 성능 다시 확인'


def test_delegation_never_trusts_model_invented_content(hub):
    app, c, model = hub; place_widgets(app); enable(c)
    note(c, 'BASE', title='기본 선택')
    proposal(model, 'memo', 'append', args={'text':'INVENTED_CONTENT'})
    d = wait(c, request(c, '합성 측정이라고 현재 메모에 한 줄 덧붙여줘'))
    assert d['status'] == 'needs_clarification'
    assert app.state.life.notes()[0]['body'] == 'BASE'


@pytest.mark.parametrize('text,issue', [
    ('합성 표식이 들어간 노트 찾아줘', 'MEMO_SEARCH_UNSUPPORTED'),
    ('합성 표식 관련된 메모 알려줘', 'MEMO_SEARCH_UNSUPPORTED'),
    ('그 노트 내용 보여줘', 'CONTEXT_REFERENCE_UNSUPPORTED'),
    ('저 메모 내용을 알려줘', 'CONTEXT_REFERENCE_UNSUPPORTED'),
])
def test_unsupported_never_becomes_current_read(hub,monkeypatch,text,issue):
    app,c,model=hub;place_widgets(app);enable(c)
    note(c,'DO_NOT_READ',title='기본 대상')
    calls=spy_adapter(app,'memo',monkeypatch)
    d=wait(c,request(c,text))
    assert d['status']=='needs_clarification',d
    assert d['assistant']['memo_plan']['issue']==issue
    assert d['assistant']['routing']['route']=='MEMO_GUARD'
    assert not calls and not model.calls and 'DO_NOT_READ' not in str(d)


def test_title_and_content_field_order_is_not_interchangeable():
    named=parse_memo('합성 통관 메모에 두  칸  문구 덧붙여줘')
    assert named.target_text=='합성 통관'
    assert dict(named.arguments)['text']=='두  칸  문구'
    delegated=recognize('두  칸  문구라고 지금 메모에 한 줄 덧붙여줘')
    assert delegated.family.disposition=='delegate' and delegated.value('text')=='두  칸  문구'
    assert delegated.value('title') is None


def test_whole_request_boundaries_and_qualifiers_are_not_removed():
    for text in ['합성 통관이라는 메모 내용 알려줘 그리고 알람 설정해',
                 '가장 최근에 편집한 노트 내용 알려주지 마',
                 '합성이라고 지금 메모에 한 줄 덧붙여줘; 전체 삭제해']:
        assert parse_memo(text) is None
    assert has_unhandled_name('다른 제목 현재 메모 내용 좀 읽어 볼래')


def test_dialog_slot_path_does_not_dispatch_sentence_families(hub):
    from test_dialog_state_m33 import follow
    app,c,model=hub
    parent=wait(c,request(c,'내일 할 일 추가해'))
    child=follow(c,parent,'마지막으로 편집한 노트 내용 보여줘')
    assert child['dialog']['awaiting_slot']=='title'
    assert child['dialog']['known_slots']['date']==parent['dialog']['known_slots']['date']
    assert child['status']=='needs_clarification' and not app.state.store.tasks() and not model.calls


@pytest.mark.parametrize('mode', ['nlu','decision','full'])
def test_wrapper_reuses_gold_free_production_runtime(mode):
    fixture={'memos':[{'id':'synthetic-one','title':'합성 통관','body':'BODY','shared':False}]}
    d=run('합성 통관이라는 노트 내용 알려줘',mode,fixture=fixture)
    assert d['harness_error'] is None, d
    assert d['record']['memo_language']['family']=='named.wrapper.read'
    assert not d['business_state_changed'] and not d['llm']['attempts']
    if mode=='nlu':
        assert not d['adapter_calls'] and d['stages_entity_catalog'] is None
    elif mode=='full':
        assert d['request']['target']['value']=='synthetic-one' and d['final_status']=='succeeded'


def test_explicit_name_can_start_with_selector_word_without_default_fallback(hub):
    app,c,model=hub;place_widgets(app)
    note(c,'DEFAULT_BODY',title='다른 기본 카드',pinned=True)
    target=note(c,'NAMED_BODY',title='현재 상황',shared=False)
    raw='현재 상황이라는 메모 내용 알려줘'
    assert has_unhandled_name(raw)
    d=wait(c,request(c,raw))
    assert d['status']=='succeeded' and d['assistant']['tool_result']['note_id']==target
    assert not model.calls


def test_named_plan_version_binds_declarative_grammar(hub):
    from app.memo_grounding import verified_plan
    app,c,_=hub
    note(c,'BODY',title='합성 통관')
    d=wait(c,request(c,'합성 통관이라는 노트 비워줘'))
    record=d['assistant']
    record['memo_plan']['grammar_sha256']='0'*64
    with pytest.raises(ProtocolFault):
        verified_plan(record)
