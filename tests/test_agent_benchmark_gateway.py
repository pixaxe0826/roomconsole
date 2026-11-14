"""Run-wide meter tests: fake official HTTP only, no private corpus or API key."""
import asyncio
import json

import pytest

from app.ai_profile import AIProfile
from app.openai_budget import BudgetGovernor, BudgetPolicy
from app.openai_gateway import OpenAIGatewayError
from app.store import Store
from benchmarks.agent_gateway import BenchmarkGateway, RunBudget, cost_limit_microdollars


def gateway(tmp_path, budget, response=None, *, transport=None, checkpoints=None):
    governor = BudgetGovernor(Store(tmp_path / 'synthetic.sqlite3'))
    governor.set_policy(BudgetPolicy(agent_enabled=True, cloud_enabled=True,
        fast_model='gpt-4o-mini', deep_model='gpt-4o-mini', auto_downgrade=False))
    async def fake(_method, url, headers, body):
        assert url == 'https://api.openai.com/v1/chat/completions'
        assert headers['Authorization'] == 'Bearer benchmark-synthetic-transport'
        if checkpoints is not None:
            # Transport cannot precede acknowledged reservation + dispatch checkpoints.
            assert checkpoints[-1]['dispatched_calls'] == 1
            assert checkpoints[-1]['reserved_microdollars'] > 0
        return response or {
            'model': 'gpt-4o-mini', 'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110},
        }
    return BenchmarkGateway(governor, budget=budget, live=False, timeout=10,
        profile=AIProfile('odyssey_openai'), transport=transport or fake)


def call(gw, request_id='synthetic', at_iso='2026-09-01T00:00:00+09:00'):
    return gw.generate_structured(request_id=request_id, category='agent_fast',
        system_prompt='Synthetic test.', user_text='Synthetic unit input.', schema_name='synthetic',
        schema={'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
                'required': ['ok'], 'additionalProperties': False},
        at_iso=at_iso, max_output_tokens=32)


@pytest.mark.parametrize('bad', [True, None, 'nan', 'inf', '-1', '0', '1001', '0.0000001'])
def test_invalid_cost_cap(bad):
    with pytest.raises(ValueError):
        cost_limit_microdollars(bad)


def test_exact_decimal_and_budget_cross_case_month(tmp_path):
    checkpoints = []
    budget = RunBudget('0.123456', 2, on_change=checkpoints.append)
    assert budget.limit == 123456
    first = gateway(tmp_path / 'one', budget, checkpoints=checkpoints)
    result = asyncio.run(call(first))
    assert result['cost_microdollars'] == 21
    second = gateway(tmp_path / 'two', budget)
    asyncio.run(call(second, 'different', '2027-02-01T00:00:00+09:00'))
    assert budget.snapshot()['spent_microdollars'] == 42
    assert budget.snapshot()['dispatched_calls'] == 2
    with pytest.raises(OpenAIGatewayError, match='limit reached'):
        asyncio.run(call(second, 'third'))
    assert budget.stop_reason == 'max_calls_reached'
    assert budget.snapshot()['dispatched_calls'] == 2
    assert first.attempts[0]['transport_ms'] >= 0
    assert first.attempts[0]['gateway_total_ms'] >= first.attempts[0]['transport_ms']


def test_budget_blocks_before_transport(tmp_path):
    budget = RunBudget('0.000001', 10)
    async def forbidden(*args):
        raise AssertionError('No network should be attempted')
    gw = gateway(tmp_path, budget, transport=forbidden)
    with pytest.raises(OpenAIGatewayError):
        asyncio.run(call(gw))
    assert budget.stop_reason == 'max_cost_reached'
    assert budget.snapshot()['dispatched_calls'] == 0
    assert all(r['status'] == 'released' for r in gw.governor.list_reservations('2026-09'))


def test_missing_usage_preserves_reservation_and_stops(tmp_path):
    budget = RunBudget('1', 10)
    gw = gateway(tmp_path, budget, response={
        'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}],
    })
    result = asyncio.run(call(gw))
    assert result['cost_known'] is False
    assert budget.stop_reason == 'unknown_cost'
    assert budget.snapshot()['unknown_microdollars'] > 0
    assert budget.snapshot()['spent_microdollars'] == 0
    assert not budget.snapshot()['cost_complete']
    assert gw.attempts[0]['error'] == 'unknown_cost'
    with pytest.raises(OpenAIGatewayError):
        asyncio.run(call(gw, 'second'))
    assert budget.snapshot()['dispatched_calls'] == 1


@pytest.mark.parametrize('status,unknown', [(401, False), (429, False), (503, True)])
def test_http_failure_halts_batch_no_retry(tmp_path, status, unknown):
    budget = RunBudget('1', 10)
    async def fake(*args):
        raise OpenAIGatewayError('Synthetic HTTP error', 'http_error', status,
                                 response_body={'error': {'message': 'Synthetic error'}})
    gw = gateway(tmp_path, budget, transport=fake)
    with pytest.raises(OpenAIGatewayError):
        asyncio.run(call(gw))
    assert budget.stop_reason == 'provider_http_error'
    assert bool(budget.snapshot()['unknown_microdollars']) is unknown
    assert len(gw.attempts) == 1


def test_paid_invalid_output_remains_measured_not_unavailable(tmp_path):
    budget = RunBudget('1', 10)
    gw = gateway(tmp_path, budget, response={
        'choices': [{'finish_reason': 'stop', 'message': {'content': 'not JSON'}}],
        'usage': {'prompt_tokens': 100, 'completion_tokens': 10},
    })
    with pytest.raises(OpenAIGatewayError):
        asyncio.run(call(gw))
    assert gw.attempts[0]['error'] == 'invalid_json'
    assert budget.snapshot()['spent_microdollars'] == 21
    assert budget.snapshot()['unknown_microdollars'] == 0
    assert budget.stop_reason == 'provider_invalid_json'


def test_checkpoint_failure_never_dispatches(tmp_path):
    def reject(_snapshot):
        raise RuntimeError('Synthetic parent checkpoint failed')
    budget = RunBudget('1', 10, on_change=reject)
    called = []
    async def fake(*args):
        called.append(True)
        raise AssertionError('Must not dispatch')
    gw = gateway(tmp_path, budget, transport=fake)
    with pytest.raises(RuntimeError):
        asyncio.run(call(gw))
    assert called == []


def test_duplicate_and_overrun_are_fail_closed():
    budget = RunBudget('1', 2)
    budget.reserve('one', 10, 'gpt-4o-mini')
    budget.dispatched('one')
    budget.settle('one', {'status': 'settled', 'actual_microdollars': 11})
    assert budget.stop_reason == 'actual_cost_exceeded_reservation'
    assert budget.snapshot()['spent_microdollars'] == 11
    with pytest.raises(OpenAIGatewayError):
        budget.reserve('two', 10, 'gpt-4o-mini')


def test_no_live_without_explicit_optin_even_with_environment_key(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-secret-not-used')
    governor = BudgetGovernor(Store(tmp_path / 'synthetic.sqlite3'))
    governor.set_policy(BudgetPolicy(agent_enabled=True, cloud_enabled=True))
    budget = RunBudget('1', 2)
    gw = BenchmarkGateway(governor, budget=budget, live=False, timeout=10,
                          profile=AIProfile('odyssey_openai'))
    with pytest.raises(OpenAIGatewayError):
        asyncio.run(call(gw))
    assert budget.snapshot()['dispatched_calls'] == 0
    assert 'synthetic-secret-not-used' not in json.dumps(gw.attempts)
    assert 'synthetic-secret-not-used' not in json.dumps(budget.snapshot())


def test_production_http_serialization_with_fake_official_transport(tmp_path):
    import httpx
    received = []
    def handle(request):
        assert str(request.url) == 'https://api.openai.com/v1/chat/completions'
        received.append(json.loads(request.content))
        return httpx.Response(200, json={
            'model': 'gpt-4o-mini',
            'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}],
            'usage': {'prompt_tokens': 50, 'completion_tokens': 5},
        })
    budget = RunBudget('1', 2)
    gw = gateway(tmp_path, budget, transport=httpx.MockTransport(handle))
    result = asyncio.run(call(gw))
    assert result['parsed'] == {'ok': True}
    assert received[0]['response_format']['json_schema']['strict'] is True
    assert received[0]['service_tier'] == 'default'
    assert budget.snapshot()['dispatched_calls'] == 1
