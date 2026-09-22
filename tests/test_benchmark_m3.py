"""Small independent synthetic inputs verify the SAME production pipeline boundaries."""
import asyncio
from copy import deepcopy

import pytest
from benchmarks.runtime import Runtime, TextInput
from benchmarks.support import capture_snapshot

AT = '2028-02-28T09:00:00+09:00'
FIXTURE = {'todos': [{'id': 'synthetic-owned', 'title': '장비 포장', 'date': '2028-02-29', 'completed': False}]}


def run(text, mode, fixture=FIXTURE):
    async def go():
        rt = Runtime(deepcopy(fixture), AT, 'Asia/Seoul', {'llm': 'disabled'})
        try:
            result = await rt.run(TextInput(text, AT, {}), mode)
            return result
        finally:
            await rt.close()
    return asyncio.run(go())


@pytest.mark.parametrize('mode', ['nlu', 'decision', 'full'])
@pytest.mark.parametrize('text', ['내일 장비 포장 할 일 추가해', '장비 포장 끝냈어', '오늘부터 내일까지 할 일 보여줘'])
def test_semantic_runtime_boundaries_trace_and_no_model(mode, text):
    result = run(text, mode)
    assert result['harness_error'] is None, result
    trace = result
    assert trace['layers']['semantic_parser']['enabled'] is True
    assert trace['layers']['semantic_parser']['version'] == '1.0.0'
    assert trace['latency']['parser_ms'] >= 0
    assert trace['stages_semantic_frame'] and not trace['business_state_changed']
    assert not trace['llm']['attempts']
    calls = trace['adapter_calls']
    if mode == 'nlu':
        assert not calls and trace['boundary'] == 'nlu'
    if mode == 'decision':
        assert all(call['phase'] == 'grounding' and call['read_only'] for call in calls)
    if mode == 'full' and '끝냈어' in text:
        assert trace['stages_entity_resolution']['status'] == 'resolved'
        assert trace['request']['target']['value'] == 'synthetic-owned'


def test_fast_read_is_not_falsely_attributed_to_semantic_parser():
    result = run('내일 할 일 보여줘', 'full')
    assert result['harness_error'] is None
    assert result['layers']['semantic_parser']['enabled'] is False
    assert not result['llm']['attempts']


def test_always_same_declared_operations_and_no_live_io():
    snapshot = capture_snapshot()
    names = {(op['native'], op['source']) for op in snapshot['operations']}
    assert len(snapshot['operations']) == 31
    for forbidden in ['todo.search', 'todo.move', 'calendar.check_free', 'context.reference']:
        assert not any(n == forbidden for n, _ in names)
    assert snapshot['io'] == {'network_calls': 0, 'sqlite_connections': 0, 'app_main_imported': False}
