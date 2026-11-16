"""Typed adapter and approval/replay checks using synthetic SQLite only."""
import asyncio
from dataclasses import replace
import uuid

from fastapi.testclient import TestClient
import pytest

from app.food import FoodCreate, FoodService
from app.main import create_app
from app.store import Store
from app.widget_protocol import ExecutionContext, FoodAdapter, WidgetRegistry, WidgetRequest, request_digest
from app.widget_protocol.core import ProtocolFault

AUTH = ExecutionContext(principal='synthetic-manager', role='admin', permissions=frozenset({'read', 'write'}))


@pytest.fixture
def inventory(tmp_path):
    store = Store(tmp_path / 'food.sqlite3')
    food = FoodService(store)

    async def changed(event, detail=''):
        store.bump(event, detail)

    def registry():
        result = WidgetRegistry()
        result.register(FoodAdapter(store, food, changed))
        return result

    return food, registry


def request(action, args=None, identity=None, **extra):
    return WidgetRequest(request_id=uuid.uuid4().hex, widget='food', action=action,
                         args=args or {}, target={'type': 'item_id', 'value': identity} if identity else None,
                         **extra)


def run(registry, value, *, confirm=False, authority=AUTH):
    if confirm:
        authority = replace(authority, confirmed_digest=request_digest(value))
    return asyncio.run(registry.execute(value, authority))


def values():
    return {'name': '합성 두부', 'quantity': 2, 'expiry_date': '2026-11-20'}


def test_typed_registry_all_actions_require_confirmation_for_writes(inventory):
    food, make_registry = inventory
    registry = make_registry()
    meta = {cap.action: cap for cap in registry.get_capabilities('food')}
    assert set(meta) == {'list', 'get', 'add', 'update', 'delete'}
    for action in ('add', 'update', 'delete'):
        assert meta[action].requires_confirmation and meta[action].permission_level == 'write'
    assert registry.immediate_actions() == []
    addition = request('add', values(), context={'user_confirmed': True, 'source': 'voice'})
    result = run(registry, addition)
    assert result.status == 'needs_confirmation' and food.items() == []
    assert result.meta.request_digest == request_digest(addition)
    assert run(registry, addition, authority=replace(AUTH, confirmed_digest='incorrect')).status == 'needs_confirmation'
    assert run(registry, addition, confirm=True, authority=replace(AUTH, role='display')).status == 'permission_denied'


def test_crud_and_partial_update_preserve_fields(inventory):
    food, make_registry = inventory
    registry = make_registry()
    added = run(registry, request('add', values()), confirm=True)
    assert added.status == 'success' and added.meta.changed
    identity = added.data['id']
    listing = run(registry, request('list'))
    assert listing.data['count'] == 1 and listing.meta.source_of_truth == 'room_hub_sqlite.hub_food_items'
    assert run(registry, request('get', identity=identity)).data['quantity'] == 2
    edit = request('update', {'version': 1, 'quantity': 4}, identity)
    assert run(registry, edit).status == 'needs_confirmation'
    edited = run(registry, edit, confirm=True)
    assert edited.data['quantity'] == 4 and edited.data['name'] == values()['name']
    assert edited.data['expiry_date'] == values()['expiry_date'] and edited.data['version'] == 2
    assert run(registry, request('update', {'version': 1, 'quantity': 3}, identity), confirm=True).status == 'conflict'
    deletion = request('delete', {'version': 2}, identity)
    assert run(registry, deletion).status == 'needs_confirmation'
    assert run(registry, deletion, confirm=True).data['deleted'] is True
    assert run(registry, request('get', identity=identity)).status == 'not_found'
    assert food.items() == []


@pytest.mark.parametrize(('action', 'args', 'expected'), [
    ('add', {'name': '합성'}, 'needs_clarification'),
    ('add', values() | {'quantity': None}, 'needs_clarification'),
    ('add', values() | {'quantity': True}, 'invalid_request'),
    ('add', values() | {'expiry_date': '2026-02-30'}, 'invalid_request'),
    ('add', values() | {'name': '  '}, 'invalid_request'),
    ('add', values() | {'unknown': 'no'}, 'invalid_request'),
    ('update', {'quantity': 2}, 'needs_clarification'),
    ('update', {'version': 1}, 'needs_clarification'),
    ('update', {'version': 1, 'name': None}, 'invalid_request'),
    ('delete', {'version': True}, 'invalid_request'),
])
def test_schema_and_missing_slots(inventory, action, args, expected):
    food, make_registry = inventory
    req = request(action, args, 'synthetic-id' if action != 'add' else None)
    assert run(make_registry(), req, confirm=True).status == expected
    assert food.items() == []


def test_no_selector_or_extra_target_authority(inventory):
    _, make_registry = inventory
    registry = make_registry()
    assert run(registry, request('get')).status == 'needs_clarification'
    unknown = WidgetRequest(request_id='synthetic-unknown', widget='food', action='get',
                            target={'type': 'reference', 'value': 'current'})
    assert run(registry, unknown).status == 'invalid_request'
    assert run(registry, request('list', {'name': 'hidden filter'})).status == 'invalid_request'


def test_durable_replay_before_target_lookup_after_restart_and_delete(inventory):
    food, make_registry = inventory
    addition = request('add', values())
    first = run(make_registry(), addition, confirm=True)
    identity = first.data['id']
    update = request('update', {'version': 1, 'quantity': 3}, identity)
    assert run(make_registry(), update, confirm=True).data['version'] == 2
    # Another field changed after the original partial update; replay must not
    # merge that new state into the old request or overwrite it.
    second = request('update', {'version': 2, 'name': '새 이름'}, identity)
    assert run(make_registry(), second, confirm=True).data['version'] == 3
    replay = run(make_registry(), update, confirm=True)
    assert replay.status == 'success' and replay.data['duplicate'] is True and not replay.meta.changed
    assert food.get(identity)['name'] == '새 이름'
    deletion = request('delete', {'version': 3}, identity)
    assert run(make_registry(), deletion, confirm=True).data['deleted'] is True
    assert run(make_registry(), deletion, confirm=True).data['duplicate'] is True
    assert run(make_registry(), update, confirm=True).data['duplicate'] is True
    assert run(make_registry(), addition, confirm=True).data['duplicate'] is True
    assert food.items() == []


def test_request_key_scoped_to_trusted_principal_and_content(inventory):
    food, make_registry = inventory
    addition = request('add', values())
    assert run(make_registry(), addition, confirm=True).status == 'success'
    changed = addition.model_copy(update={'args': values() | {'quantity': 6}})
    assert run(make_registry(), changed, confirm=True).status == 'conflict'
    other = replace(AUTH, principal='another-manager')
    assert run(make_registry(), addition, confirm=True, authority=other).status == 'success'
    assert len(food.items()) == 2


def test_adapter_mutation_and_receipt_participate_in_uow(inventory):
    food, make_registry = inventory
    addition = request('add', values())

    async def attempt():
        with food.store.unit_of_work():
            authority = replace(AUTH, confirmed_digest=request_digest(addition))
            assert (await make_registry().execute(addition, authority)).status == 'success'
            raise RuntimeError('synthetic later step failed')

    with pytest.raises(RuntimeError):
        asyncio.run(attempt())
    assert food.items() == [] and food.store.get('revision') == 0
    assert run(make_registry(), addition, confirm=True).data['duplicate'] is False


def test_protocol_http_cannot_claim_confirmation(tmp_path):
    app = create_app(tmp_path / 'hub', weather_enabled=False)
    with TestClient(app) as manager:
        manager.headers.update({'Authorization': 'Bearer ' + app.state.admin_token, 'X-Room-Request': '1'})
        req = request('add', values(), context={'user_confirmed': True})
        result = manager.post('/api/widget-protocol/requests', json=req.model_dump(mode='json'))
        assert result.status_code == 409 and result.json()['status'] == 'needs_confirmation'
        assert app.state.food.items() == []
        food_meta = manager.get('/api/widget-protocol/widgets/food')
        assert food_meta.status_code == 200


def test_health_checks_food_table_and_permission(inventory):
    food, make_registry = inventory
    adapter = make_registry().get('food')
    assert asyncio.run(adapter.health(AUTH))['available'] is True
    with pytest.raises(ProtocolFault) as exc:
        asyncio.run(adapter.health(ExecutionContext(principal='display', role='display')))
    assert exc.value.error.code == 'PERMISSION_DENIED'
    with food.store.connect() as db:
        db.execute('DROP TABLE hub_food_items')
    assert asyncio.run(adapter.health(AUTH))['available'] is False
