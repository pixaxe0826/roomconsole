"""Tiny synthetic Agent benchmark fixtures; no live provider or private corpus."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from benchmarks.agent_gateway import RunBudget
from benchmarks.personal_agent_runtime import PersonalAgentRuntime
from benchmarks.runtime import TextInput


STAMP = '2028-02-09T09:00:00+09:00'
DAY = '2028-02-09'


def fixture():
    return {
        'todos': [{'id': 'synthetic-task', 'title': '합성 측정 항목', 'date': DAY}],
        'calendar': [], 'alarms': [], 'timers': [],
        'memos': [
            {'id': 'public-note', 'title': '합성 공개 제목', 'body': '공개 본문 표시 검사용',
             'shared': True, 'pinned': True},
            {'id': 'private-note', 'title': '비공개 제목 보호 검사용', 'body': '비공개 본문 보호 검사용',
             'shared': False, 'pinned': False},
        ],
        'session': {},
        'layout': {'columns': 8, 'rows': 6, 'version': 1, 'widgets': [
            {'id': 'synthetic-note', 'type': 'note', 'title': '메모',
             'x': 0, 'y': 0, 'w': 4, 'h': 2, 'config': {'text': ''}},
            {'id': 'synthetic-timer', 'type': 'timers', 'title': '타이머',
             'x': 4, 'y': 0, 'w': 2, 'h': 2, 'config': {}},
        ]},
    }


def plan(*steps):
    from app.personal_agent import PersonalAgentService
    return {'intent_type': 'action_plan', 'assistant_message': '합성 계획',
            'follow_up_question': None,
            'steps': [PersonalAgentService._blank_step(**step) for step in steps]}


def fake_provider(response_plan, requests=None):
    async def transport(method, url, headers, payload):
        assert method == 'POST'
        assert url == 'https://api.openai.com/v1/chat/completions'
        if requests is not None:
            requests.append(deepcopy(payload))
        return {
            'model': payload['model'],
            'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps(response_plan, ensure_ascii=False)}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120},
        }
    return transport


def run_text(text, *, config=None, transport=None, inspect=None, budget=None):
    async def run():
        runtime = PersonalAgentRuntime(
            fixture(), STAMP, 'Asia/Seoul', config or {},
            budget=budget or RunBudget('10', 10), transport=transport,
        )
        try:
            result = await runtime.run(TextInput(text, STAMP, {}))
            if inspect:
                inspect(runtime, result)
            return result
        finally:
            await runtime.close()
    return asyncio.run(run())


def test_tier0_calls_real_personal_agent_not_legacy_llm(monkeypatch):
    from app.llm import LLMHub
    from app.personal_agent import PersonalAgentService
    submitted = []
    submit = PersonalAgentService.submit_command

    async def observed(self, body, **kwargs):
        submitted.append(body.text)
        return await submit(self, body, **kwargs)

    async def no_legacy(*args, **kwargs):
        raise AssertionError('Legacy LLMHub must not be the benchmark path')

    monkeypatch.setattr(PersonalAgentService, 'submit_command', observed)
    monkeypatch.setattr(LLMHub, '_insert', no_legacy)
    result = run_text('오늘 할 일 알려줘')
    assert submitted == ['오늘 할 일 알려줘']
    assert not result['harness_error']
    assert result['agent_run']['status'] == 'succeeded'
    assert result['agent_run']['tier_used'] == 'tier0_local_rule'
    request = result['registry_calls'][0]['request']
    assert (request['widget'], request['action']) == ('todo', 'list')
    assert request['args']['start'] == request['args']['end'] == DAY
    assert result['adapter_calls'][0]['production_adapter'] == 'FullTodoAdapter'
    assert result['registry_calls'][0]['response']['status'] == 'success'
    assert not result['business_state_changed']
    assert not result['llm']['attempts']


def test_write_remains_preview_without_confirmation(monkeypatch):
    from app.personal_agent import PersonalAgentService

    async def forbidden_confirm(*args, **kwargs):
        raise AssertionError('The benchmark must never automatically confirm')

    monkeypatch.setattr(PersonalAgentService, 'confirm_run', forbidden_confirm)
    result = run_text('내일 할 일에 합성 포장 검수 추가해')
    assert not result['harness_error']
    assert result['agent_run']['status'] == 'awaiting_confirmation'
    assert result['agent_run']['created_at'] == '2028-02-09T00:00:00+00:00'
    preview = result['agent_run']['preview']
    assert preview['mutations'][0]['widget_request']['args']['date'] == '2028-02-10'
    assert preview['expires_at'] == 1833667800.0
    assert result['registry_calls'][0]['response']['error']['code'] == 'CONFIRMATION_REQUIRED'
    assert not result['adapter_calls']
    assert not result['business_state_changed']
    assert result['agent_run']['receipt'] is None


def test_food_write_stays_unconfirmed_in_isolated_runtime(monkeypatch):
    from app.personal_agent import PersonalAgentService

    async def forbidden_confirm(*args, **kwargs):
        raise AssertionError('A food benchmark must never automatically confirm')

    monkeypatch.setattr(PersonalAgentService, 'confirm_run', forbidden_confirm)

    def inspect(runtime, result):
        assert runtime.business_state()['hub_food_items'] == []
        assert runtime.app.state.food.snapshot()['today'] == DAY
        assert runtime.agent.food is runtime.app.state.food

    result = run_text('소비기한 알리미에 합성 두부 2개 소비기한 내일로 추가해 줘',
                      config={'agent_mode': 'local_only'}, inspect=inspect)
    assert not result['harness_error']
    assert result['agent_run']['status'] == 'awaiting_confirmation'
    mutation = result['agent_run']['preview']['mutations'][0]
    assert mutation['widget_request']['widget'] == 'food'
    assert mutation['widget_request']['args'] == {
        'name': '합성 두부', 'quantity': 2, 'expiry_date': '2028-02-10',
    }
    assert not result['adapter_calls'] and not result['business_state_changed']
    assert not result['llm']['attempts']


def test_food_adapter_is_reviewed_and_inventory_state_is_observed():
    from app.food import FoodCreate

    async def exercise():
        runtime = PersonalAgentRuntime(fixture(), STAMP, 'Asia/Seoul',
                                       {'agent_mode': 'local_only'}, budget=RunBudget('1', 1))
        try:
            before = runtime.business_state()
            item = runtime.app.state.food.create(FoodCreate(
                request_id='synthetic-food-seed', name='합성 두유', quantity=3,
                expiry_date='2028-02-11'))
            after = runtime.business_state()
            assert after['hub_food_items'] != before['hub_food_items']
            assert after['hub_food_items'][0]['id'] == item['id']
            assert 'food_items' not in after  # One state-diff entry per table.
            snapshot = runtime.app.state.food.snapshot()
            assert snapshot['today'] == DAY and snapshot['items'][0]['days_remaining'] == 2
            result = await runtime.run(TextInput('소비기한 알리미 목록 보여 줘', STAMP, {}))
            assert not result['harness_error']
            assert result['agent_run']['status'] == 'succeeded'
            assert result['adapter_calls'][0]['production_adapter'] == 'FoodAdapter'
            assert result['adapter_calls'][0]['result']['items'][0]['name'] == '합성 두유'
            assert not result['business_state_changed']
        finally:
            await runtime.close()

    asyncio.run(exercise())


def test_timer_service_entry_counted_once_not_preview_duplicates():
    result = run_text('37초 타이머 시작')
    assert not result['harness_error']
    assert result['agent_run']['status'] == 'succeeded'
    assert result['business_state_changed']
    assert len(result['adapter_calls']) == 1
    event = result['adapter_calls'][0]
    assert event['request']['widget'] == 'timer'
    assert event['request']['action'] == 'start'
    assert event['request']['args']['duration_seconds'] == 37
    assert event['entered'] and not event['read_only'] and event['changed']
    preview = result['agent_run']['preview']
    assert len(preview['reads_executed']) == len(preview['immediate_controls_executed']) == 1


def test_forced_fast_uses_production_gateway_and_structured_prompt():
    requests = []
    response = plan({'domain': 'todo', 'action': 'list', 'date': DAY})
    result = run_text('오늘 할 일 알려줘', config={'agent_mode': 'fast'},
                      transport=fake_provider(response, requests))
    assert not result['harness_error']
    assert len(requests) == 1
    payload = requests[0]
    assert payload['model'] == 'gpt-6.1-sol'
    assert payload['reasoning_effort'] == 'high'
    assert payload['response_format']['json_schema']['name'] == 'personal_agent_plan'
    assert payload['messages'][-1]['content'] == '오늘 할 일 알려줘'
    assert result['agent_run']['tier_used'] == 'tier2_openai_fast'
    assert result['agent_run']['model_used'] == 'gpt-6.1-sol'
    assert len(result['llm']['attempts']) == 1
    assert result['llm']['attempts'][0]['transport_attempted']
    assert result['llm']['attempts'][0]['cost_microdollars'] > 0
    assert result['llm']['attempts'][0]['billing_month'] == '2028-02'
    assert result['latency']['gateway_ms'] == result['latency']['llm_ms']
    assert 'not pure HTTP' in result['latency_scope']['llm_ms']
    assert result['latency']['gateway_ms'] == result['llm']['attempts'][0]['gateway_total_ms']
    measured_transport = result['llm']['attempts'][0].get('transport_ms')
    assert measured_transport is not None
    assert result['latency']['transport_ms'] == measured_transport
    assert result['latency']['gateway_ms'] >= measured_transport


@pytest.mark.parametrize('allow_shared', [False, True])
def test_cloud_catalog_obeys_note_privacy_policy(allow_shared):
    requests = []
    run_text('오늘 할 일 알려줘',
             config={'agent_mode': 'fast', 'allow_shared_notes': allow_shared},
             transport=fake_provider(plan({'domain': 'todo', 'action': 'list', 'date': DAY}), requests))
    prompt = json.dumps(requests, ensure_ascii=False)
    assert '비공개 제목 보호 검사용' not in prompt
    assert '비공개 본문 보호 검사용' not in prompt
    assert '공개 본문 표시 검사용' not in prompt
    assert ('합성 공개 제목' in prompt) is allow_shared


def test_rejected_grounding_keeps_native_plan_without_fake_adapter_entry():
    response = plan({'domain': 'todo', 'action': 'get', 'target_id': 'nonexistent-synthetic-id',
                     'target_title': '존재하지 않는 합성 대상'})
    result = run_text('존재하지 않는 합성 대상 보여줘', config={'agent_mode': 'fast'},
                      transport=fake_provider(response))
    assert not result['harness_error']
    assert result['agent_run']['plan'] == response
    assert result['agent_run']['status'] == 'needs_clarification'
    assert not result['adapter_calls']
    assert not result['registry_calls']
    assert not result['business_state_changed']


def test_multi_step_plan_preserves_every_native_action():
    response = plan({'domain': 'todo', 'action': 'list', 'date': DAY},
                    {'domain': 'clock', 'action': 'read'})
    result = run_text('오늘 할 일과 현재 시각을 확인해', config={'agent_mode': 'fast'},
                      transport=fake_provider(response))
    assert not result['harness_error']
    assert result['agent_run']['plan'] == response
    assert [(e['request']['widget'], e['request']['action']) for e in result['adapter_calls']] == [
        ('todo', 'list'), ('clock', 'read')]
    assert len(result['registry_calls']) == 2
    assert 'request' not in result  # No first-step selection in the worker.


def test_run_budget_survives_fresh_runtime_and_case_database():
    budget = RunBudget('10', 1)
    requests = []
    provider = fake_provider(plan({'domain': 'todo', 'action': 'list', 'date': DAY}), requests)
    first = run_text('오늘 할 일 알려줘', config={'agent_mode': 'fast'}, transport=provider, budget=budget)
    second = run_text('오늘 할 일 알려줘', config={'agent_mode': 'fast'}, transport=provider, budget=budget)
    assert first['llm']['attempts'][0]['transport_attempted']
    assert not second['llm']['attempts'][0]['transport_attempted']
    assert len(requests) == 1
    assert budget.snapshot()['authorized_calls'] == 1
    assert budget.stop_reason == 'max_calls_reached'


def test_non_live_runtime_strips_keys_and_ignores_production_environment(monkeypatch, tmp_path):
    outside = tmp_path / 'must-not-be-created'
    monkeypatch.setenv('HUB_DATA_DIR', str(outside))
    monkeypatch.setenv('HUB_AI_PROFILE', 'legacy')
    monkeypatch.setenv('HUB_OPENAI_MONTHLY_CAP_USD', '1')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-ambient-secret')
    monkeypatch.setenv('HUB_OPENAI_API_KEY', 'synthetic-second-secret')

    def inspect(runtime, result):
        assert 'OPENAI_API_KEY' not in os.environ
        assert 'HUB_OPENAI_API_KEY' not in os.environ
        assert runtime.agent.profile.pc_free
        policy = runtime.agent.governor.policy()
        assert policy.agent_enabled and not policy.cloud_enabled and not policy.audio_cloud_enabled
        assert policy.monthly_cap_usd == 100.0
        assert not policy.auto_downgrade and not policy.fallback_to_local
        assert not runtime.gateway.key_configured()
        assert Path(runtime.store.path).is_relative_to(runtime.root)
        assert set(runtime.registry.list_widgets()) == {
            'memo', 'todo', 'calendar', 'alarm', 'timer', 'food', 'clock',
            'weather', 'indoor', 'llm-response', 'layout', 'settings', 'system',
        }
        with pytest.raises(RuntimeError, match='outside the benchmark temporary tree'):
            sqlite3.connect(tmp_path / 'forbidden.sqlite3')
        assert 'synthetic-ambient-secret' not in json.dumps(result, ensure_ascii=False)

    run_text('오늘 할 일 알려줘', inspect=inspect)
    assert os.environ['OPENAI_API_KEY'] == 'synthetic-ambient-secret'
    assert os.environ['HUB_DATA_DIR'] == str(outside)
    assert not outside.exists()
    assert not (tmp_path / 'forbidden.sqlite3').exists()


@pytest.mark.parametrize('mode,expected_blocked', [('auto', True), ('fast', True),
                                                 ('deep', True), ('local_only', False)])
def test_cloud_denial_is_not_a_successful_clarification_in_cloud_modes(mode, expected_blocked):
    from benchmarks.agent_projection import project_agent
    result = run_text('합성 자유 질문에 답을 해 줘', config={'agent_mode': mode})
    assert not result['harness_error']
    assert result['agent_run']['status'] == 'needs_clarification'
    assert result['cloud_gate'] == [{
        'mode': mode, 'local_only': mode == 'local_only',
        'category': 'agent_deep' if mode == 'deep' else 'agent_fast',
        'allowed': False, 'reason': 'local_only_requested' if mode == 'local_only' else 'cloud_disabled',
    }]
    assert (result['error_code'] == 'benchmark_llm_unavailable') is expected_blocked
    assert project_agent(result, {})['llm_blocked'] is expected_blocked
    assert not result['llm']['attempts']
    assert result['latency']['transport_ms'] == 0


def test_settings_mutation_is_only_preview_and_kv_state_is_observed():
    response = plan({'domain': 'settings', 'action': 'update', 'title': '합성 설정 제목'})
    result = run_text('허브 제목을 합성 설정 제목으로 변경해', config={'agent_mode': 'fast'},
                      transport=fake_provider(response))
    assert not result['harness_error']
    assert result['agent_run']['status'] == 'awaiting_confirmation'
    assert result['registry_calls'][0]['request']['widget'] == 'settings'
    assert not result['adapter_calls']
    assert not result['business_state_changed']


def test_business_snapshot_catches_layout_settings_and_widget_data():
    def inspect(runtime, result):
        before = runtime.business_state()
        runtime.store.set('widget_data:synthetic-widget', {'synthetic': 1})
        after = runtime.business_state()
        assert before['workspace_kv'] != after['workspace_kv']
        assert all(row['key'] not in {'revision', 'llm_config'} for row in after['workspace_kv'])
    run_text('오늘 할 일 알려줘', inspect=inspect)


@pytest.mark.parametrize('mode', ['nlu', 'decision'])
def test_old_partial_modes_are_rejected_before_submit(mode):
    async def run():
        runtime = PersonalAgentRuntime(fixture(), STAMP, 'Asia/Seoul', {}, budget=RunBudget('10', 10))
        try:
            with pytest.raises(ValueError, match='full submission only'):
                await runtime.run(TextInput('오늘 할 일 알려줘', STAMP, {}), mode)
            assert not runtime.gateway.attempts
        finally:
            await runtime.close()
    asyncio.run(run())


def test_no_lifespan_or_automatic_confirmation_or_speech(monkeypatch):
    from app.life import LifeService
    from app.timers import TimerService
    from app.speech import SpeechHub
    from app.llm import LLMHub

    async def forbidden(*args, **kwargs):
        raise AssertionError('Background services must not start in a text benchmark')

    for cls, name in ((LifeService, 'run'), (TimerService, 'run'),
                      (SpeechHub, 'start'), (LLMHub, 'start')):
        monkeypatch.setattr(cls, name, forbidden)
    result = run_text('오늘 할 일 알려줘')
    assert not result['harness_error']
    assert result['agent_run']['status'] == 'succeeded'


def test_tampered_store_cannot_open_external_db_even_during_cleanup(tmp_path):
    outside = tmp_path / 'forbidden.sqlite3'

    async def run():
        runtime = PersonalAgentRuntime(fixture(), STAMP, 'Asia/Seoul', {}, budget=RunBudget('10', 10))
        root = runtime.root
        try:
            runtime.store.path = outside
            with pytest.raises(RuntimeError, match='non-sandbox'):
                await runtime.run(TextInput('오늘 할 일 알려줘', STAMP, {}))
        finally:
            await runtime.close()
        assert not root.exists()
    asyncio.run(run())
    assert not outside.exists()


@pytest.mark.parametrize('tamper', ['adapter_store', 'service_store', 'service', 'agent_service', 'app_service', 'path'])
def test_food_dependencies_are_rechecked_at_each_adapter_call(monkeypatch, tmp_path, tamper):
    from app.widget_protocol import ExecutionContext, WidgetRequest

    outside = tmp_path / 'forbidden-food.sqlite3'

    async def exercise():
        runtime = PersonalAgentRuntime(fixture(), STAMP, 'Asia/Seoul', {}, budget=RunBudget('10', 10))
        runtime.trace = {'adapter_calls': []}
        adapter = runtime.registry.get('food')
        try:
            if tamper == 'adapter_store':
                monkeypatch.setattr(adapter, 'store', SimpleNamespace(path=runtime.owned_store_path))
            elif tamper == 'service_store':
                monkeypatch.setattr(adapter.food, 'store', SimpleNamespace(path=runtime.owned_store_path))
            elif tamper == 'service':
                monkeypatch.setattr(adapter, 'food', SimpleNamespace(store=runtime.store))
            elif tamper == 'agent_service':
                monkeypatch.setattr(runtime.agent, 'food', SimpleNamespace(store=runtime.store))
            elif tamper == 'app_service':
                monkeypatch.setattr(runtime.app.state, 'food', SimpleNamespace(store=runtime.store))
            else:
                monkeypatch.setattr(runtime.store, 'path', outside)
            authority = ExecutionContext(principal='synthetic-manager', role='admin',
                                         permissions=frozenset({'read'}))
            with pytest.raises(RuntimeError, match='non-sandbox'):
                await adapter.execute(WidgetRequest(request_id='synthetic-food-guard', widget='food', action='list'), authority)
            assert runtime.trace['adapter_calls'] == []
        finally:
            await runtime.close()
        assert not runtime.root.exists()

    asyncio.run(exercise())
    assert not outside.exists()
