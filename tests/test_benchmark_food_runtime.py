"""Legacy harness isolation for the additive food service, with tiny synthetic state.

This is not a new evaluation suite or a change to legacy food-language support.
"""
import asyncio
from copy import deepcopy
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app.food import FoodCreate
from app.widget_protocol import ExecutionContext, WidgetRequest, request_digest
from app.widget_protocol.food import FoodAdapter
from benchmarks.runtime import BoundaryStop, Runtime, TextInput
from benchmarks.support import SupportIndex, capture_snapshot


AT = '2028-02-28T14:59:00Z'
FIXTURE = {'todos': [{'id': 'synthetic-task', 'title': '합성 포장', 'date': '2028-02-29'}]}
READ = ExecutionContext(principal='synthetic-manager', role='admin', permissions=frozenset({'read'}))


def run(value):
    return asyncio.run(value)


@pytest.fixture
def runtime():
    rt = Runtime(deepcopy(FIXTURE), AT, 'Asia/Seoul', {'llm': 'disabled'})
    rt.trace = {'adapter_calls': []}
    try:
        yield rt
    finally:
        run(rt.close())


def seed(rt):
    return rt.app.state.food.create(FoodCreate(
        name='합성 우유', quantity=2, expiry_date='2028-02-29', request_id='synthetic-food-seed'))


def test_existing_fixture_owned_service_and_food_clock_are_preserved(runtime):
    rt = runtime
    food = rt.app.state.food
    adapter = rt.registry.get('food')
    assert type(adapter).__name__ == 'FakeFoodAdapter' and isinstance(adapter, FoodAdapter)
    assert adapter.food is food and food.store is rt.store and adapter.store is rt.store
    assert Path(food.store.path).resolve() == rt.owned_store_path
    assert rt.business_state()['tasks'][0]['id'] == FIXTURE['todos'][0]['id']
    assert rt.business_state()['hub_food_items'] == []
    assert not any(w['type'] == 'food-expiry' for w in rt.store.get('layout')['widgets'])
    item = seed(rt)
    assert item['created_at'] == rt.clock.iso()
    assert rt.business_state()['hub_food_items'][0]['id'] == item['id']
    first = food.snapshot()
    assert first['today'] == '2028-02-28' and first['items'][0]['days_remaining'] == 1
    rt.clock.set('2028-02-28T15:01:00Z')
    later = food.snapshot()
    assert later['today'] == '2028-02-29' and later['items'][0]['days_remaining'] == 0
    assert later['items'][0]['expiry_date'] == item['expiry_date']
    response = run(rt.registry.execute(WidgetRequest(
        request_id='synthetic-food-read', widget='food', action='list'), READ))
    assert response.status == 'success' and response.data['items'][0]['id'] == item['id']
    event = rt.trace['adapter_calls'][-1]
    assert event['adapter'] == 'FakeFoodAdapter' and event['production_adapter'] == 'FoodAdapter'
    assert event['changed'] is False


def test_food_writes_require_exact_trusted_confirmation_and_appear_in_state(runtime):
    rt = runtime
    request = WidgetRequest(request_id='synthetic-food-add', widget='food', action='add',
        args={'name': '합성 두유', 'quantity': 3, 'expiry_date': '2028-03-01'},
        context={'user_confirmed': True})
    before = rt.business_state()
    for role, digest, expected in (
        ('admin', None, 'needs_confirmation'),
        ('admin', 'incorrect-digest', 'needs_confirmation'),
        ('display', request_digest(request), 'permission_denied'),
    ):
        authority = ExecutionContext(principal='synthetic-manager', role=role,
            permissions=frozenset({'read', 'write'}), confirmed_digest=digest)
        assert run(rt.registry.execute(request, authority)).status == expected
        assert rt.business_state() == before
    assert rt.trace['adapter_calls'] == []
    authority = ExecutionContext(principal='synthetic-manager', role='admin',
        permissions=frozenset({'read', 'write'}), confirmed_digest=request_digest(request))
    accepted = run(rt.registry.execute(request, authority))
    assert accepted.status == 'success' and accepted.meta.changed
    after = rt.business_state()
    assert after['hub_food_items'][0]['quantity'] == 3
    assert {key for key in before if before[key] != after[key]} == {'hub_food_items'}
    repeated = run(rt.registry.execute(request, authority))
    assert repeated.meta.duplicate and not repeated.meta.changed
    assert rt.business_state() == after


def test_decision_boundary_does_not_apply_even_approved_food_request(runtime):
    rt = runtime
    request = WidgetRequest(request_id='synthetic-food-decision', widget='food', action='add',
        args={'name': '합성 사과', 'quantity': 1, 'expiry_date': '2028-03-01'})
    authority = ExecutionContext(principal='synthetic-manager', role='admin',
        permissions=frozenset({'write'}), confirmed_digest=request_digest(request))
    before = rt.business_state()
    rt.mode = 'decision'
    with pytest.raises(BoundaryStop):
        run(rt.registry.get('food').execute(request, authority))
    assert rt.trace['boundary'] == 'decision' and rt.trace['adapter_calls'] == []
    assert rt.business_state() == before


@pytest.mark.parametrize('tamper', ['adapter_store', 'service_store', 'service', 'path'])
def test_rebound_food_dependencies_fail_before_access(runtime, monkeypatch, tmp_path, tamper):
    rt = runtime
    adapter = rt.registry.get('food')
    if tamper == 'adapter_store':
        monkeypatch.setattr(adapter, 'store', SimpleNamespace(path=rt.owned_store_path))
    elif tamper == 'service_store':
        monkeypatch.setattr(adapter.food, 'store', SimpleNamespace(path=rt.owned_store_path))
    elif tamper == 'service':
        monkeypatch.setattr(adapter, 'food', SimpleNamespace(store=rt.store))
    else:
        monkeypatch.setattr(rt.store, 'path', tmp_path / 'must-not-open.sqlite3')
    with pytest.raises(RuntimeError, match='non-sandbox'):
        run(adapter.execute(WidgetRequest(request_id='synthetic-food-isolation',
            widget='food', action='list'), READ))
    assert not (tmp_path / 'must-not-open.sqlite3').exists()
    assert rt.trace['adapter_calls'] == []


def test_legacy_request_does_not_gain_food_execution_and_next_runtime_starts_empty(runtime):
    rt = runtime
    seed(rt)
    before = rt.business_state()
    result = run(rt.run(TextInput('소비기한 알리미에 합성 치즈 2개 소비기한 내일로 추가해 줘', AT, {})))
    assert result['harness_error'] is None
    assert not result['business_state_changed'] and rt.business_state() == before
    assert not any(call['request']['widget'] == 'food' for call in result['adapter_calls'])
    old_root = rt.root
    run(rt.close())
    assert not old_root.exists()
    fresh = Runtime(deepcopy(FIXTURE), AT, 'Asia/Seoul', {'llm': 'disabled'})
    try:
        assert fresh.root != old_root and fresh.business_state()['hub_food_items'] == []
        assert fresh.business_state()['tasks'][0]['id'] == FIXTURE['todos'][0]['id']
    finally:
        run(fresh.close())


def test_unknown_food_subclass_is_not_silently_approved(monkeypatch):
    from app import main
    create_app = main.create_app
    original_connect = sqlite3.connect
    roots = []

    class UnreviewedFoodAdapter(FoodAdapter):
        name = 'unreviewed-food'

    def expanded_app(*args, **kwargs):
        app = create_app(*args, **kwargs)
        roots.append(Path(app.state.store.path).resolve().parents[1])
        app.state.widget_protocol.register(UnreviewedFoodAdapter(
            app.state.store, app.state.food, lambda *args: None))
        return app

    monkeypatch.setattr(main, 'create_app', expanded_app)
    with pytest.raises(ValueError, match='No reviewed sandbox binding for UnreviewedFoodAdapter'):
        Runtime({}, AT, 'Asia/Seoul', {'llm': 'disabled'})
    assert roots and all(not root.exists() for root in roots)
    assert sqlite3.connect is original_connect


def test_metadata_probe_has_no_io_and_does_not_claim_legacy_food_support():
    snapshot = capture_snapshot()
    food = [entry for entry in snapshot['operations'] if entry['native'].startswith('food.')]
    assert {entry['native'] for entry in food} == {'food.add', 'food.get', 'food.list', 'food.update', 'food.delete'}
    assert all(entry['source'] == 'WidgetRegistry' and not entry['exposed'] for entry in food)
    assert snapshot['io'] == {'network_calls': 0, 'sqlite_connections': 0, 'app_main_imported': False}
    support = SupportIndex(snapshot)
    assert all(support.lookup(entry['native'])['status'] == 'UNSUPPORTED' for entry in food)
