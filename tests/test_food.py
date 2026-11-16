"""Synthetic use-by inventory/API checks; never judge food safety or send audio."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import sqlite3
import uuid

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from app.food import FoodCreate, FoodService, FoodUpdate
from app.main import create_app
from app.store import Store


def food_body(**changes):
    return {'name': '합성 우유', 'quantity': 2, 'expiry_date': '2026-11-20',
            'request_id': uuid.uuid4().hex} | changes


@pytest.fixture
def service(tmp_path):
    return FoodService(Store(tmp_path / 'inventory.sqlite3'))


@pytest.fixture
def hub(tmp_path):
    app = create_app(tmp_path / 'hub', weather_enabled=False)
    clock = [datetime.fromisoformat('2026-11-16T14:59:00+00:00').timestamp()]
    app.state.food.clock = lambda: clock[0]
    with TestClient(app) as admin:
        admin.headers.update({'Authorization': 'Bearer ' + app.state.admin_token,
                              'X-Room-Request': '1'})
        display = TestClient(app)
        display.headers['X-Room-Request'] = '1'
        pair = admin.post('/api/devices/pair', json={'name': 'synthetic food display'}).json()
        assert display.post('/api/devices/claim', json={'code': pair['path'].split('=')[1]}).status_code == 200
        yield app, admin, display, clock
        display.close()


def place(app, enabled=True):
    layout = app.state.store.get('layout')
    layout['widgets'][0]['type'] = 'food-expiry' if enabled else 'clock'
    app.state.store.set('layout', layout)


def test_manual_create_edit_delete_versions_and_export(hub):
    app, admin, display, _ = hub
    payload = food_body()
    created = admin.post('/api/food/items', json=payload)
    assert created.status_code == 201, created.text
    item = created.json()
    assert {key: item[key] for key in ('name', 'quantity', 'expiry_date')} == {
        key: payload[key] for key in ('name', 'quantity', 'expiry_date')}
    assert item['version'] == 1 and item['duplicate'] is False
    revision = app.state.store.get('revision')
    assert admin.post('/api/food/items', json=payload).json()['duplicate']
    assert app.state.store.get('revision') == revision
    edited = admin.put('/api/food/items/' + item['id'], json={
        'name': '합성 우유 수정', 'quantity': 3, 'expiry_date': '2026-11-22', 'version': 1})
    assert edited.status_code == 200 and edited.json()['version'] == 2
    assert admin.put('/api/food/items/' + item['id'], json={
        'name': '오래된 값', 'quantity': 1, 'expiry_date': '2026-11-22', 'version': 1}).status_code == 409
    exported = admin.get('/api/admin/export').json()
    assert exported['food']['schema_version'] == 1
    assert exported['food']['items'][0]['quantity'] == 3
    assert {'life', 'tasks', 'series', 'layout', 'settings'} <= exported.keys()
    assert admin.delete('/api/food/items/' + item['id'] + '?version=1').status_code == 409
    assert admin.delete('/api/food/items/' + item['id'] + '?version=2').status_code == 200
    # Replayed creates must not resurrect deleted inventory, even after restart.
    fresh = FoodService(app.state.store)
    assert fresh.create(FoodCreate(**payload))['duplicate'] is True
    assert fresh.items() == []


@pytest.mark.parametrize('bad', [
    {'name': ''}, {'name': '   '}, {'name': 'a\nb'}, {'name': 'x' * 121},
    {'quantity': 0}, {'quantity': -1}, {'quantity': 10000}, {'quantity': True},
    {'quantity': 1.5}, {'quantity': '2'}, {'expiry_date': '2026-02-30'},
    {'expiry_date': '26-11-20'}, {'expiry_date': '2026-11-20T00:00:00'},
    {'expiry_date': None}, {'unknown': 'no'}, {'request_id': 'short'},
])
def test_strict_manual_validation(hub, bad):
    _, admin, _, _ = hub
    assert admin.post('/api/food/items', json=food_body(**bad)).status_code == 422
    assert admin.get('/api/food/items').json()['items'] == []


def test_missing_fields_and_version_never_default(hub):
    _, admin, _, _ = hub
    for field in ('name', 'quantity', 'expiry_date', 'request_id'):
        payload = food_body()
        del payload[field]
        assert admin.post('/api/food/items', json=payload).status_code == 422
    item = admin.post('/api/food/items', json=food_body()).json()
    for query in ('', '?version=0', '?version=true', '?version=1.1'):
        assert admin.delete('/api/food/items/' + item['id'] + query).status_code == 422
    assert admin.put('/api/food/items/' + item['id'], json={
        'name': '합성 우유', 'quantity': 1, 'expiry_date': '2026-11-22'}).status_code == 422


def test_shared_projection_is_layout_opt_in_and_not_manager_crud(hub):
    app, admin, display, _ = hub
    body = food_body(name='Only visible with inventory opt-in')
    admin.post('/api/food/items', json=body)
    assert len(admin.get('/api/food/items').json()['items']) == 1
    assert display.get('/api/food/display').json()['items'] == []
    assert body['name'] not in display.get('/api/state').text
    place(app)
    state = display.get('/api/state').json()
    assert state['food']['enabled'] is True and state['food']['items'][0]['name'] == body['name']
    assert state['capabilities'].get('food_write', False) is False
    place(app, False)
    assert display.get('/api/food/display').json()['items'] == []
    assert body['name'] not in display.get('/api/state').text


def test_anonymous_display_and_ingest_cannot_edit_or_use_private_list(hub):
    app, admin, display, _ = hub
    place(app)
    item = admin.post('/api/food/items', json=food_body()).json()
    for method, path in (('GET', '/api/food/items'), ('POST', '/api/food/items'),
                         ('PUT', '/api/food/items/' + item['id']),
                         ('DELETE', '/api/food/items/' + item['id'] + '?version=1')):
        assert display.request(method, path, json=food_body()).status_code == 401
        with TestClient(app) as anonymous:
            assert anonymous.request(method, path, json=food_body()).status_code == 401
            anonymous.headers['Authorization'] = 'Bearer ' + app.state.ingest_token
            assert anonymous.request(method, path, json=food_body()).status_code == 401
    with TestClient(app) as anonymous:
        assert anonymous.get('/api/food/display').status_code == 401
    assert len(app.state.food.items()) == 1


def test_cookie_csrf_and_cross_origin_rejected(hub):
    app, admin, _, _ = hub
    admin.post('/api/auth/login', json={'token': app.state.admin_token})
    assert admin.post('/api/food/items', json=food_body(), headers={
        'Authorization': '', 'X-Room-Request': ''}).status_code == 403
    assert admin.post('/api/food/items', json=food_body(), headers={
        'Origin': 'https://untrusted.example'}).status_code == 403
    assert app.state.food.items() == []


def test_expiry_status_uses_server_timezone_across_midnight(hub):
    app, admin, display, clock = hub
    place(app)
    for label, date in (('과거', '2026-11-15'), ('오늘', '2026-11-16'),
                        ('내일', '2026-11-17'), ('사흘', '2026-11-19'), ('나중', '2026-11-20')):
        assert admin.post('/api/food/items', json=food_body(name=label, expiry_date=date)).status_code == 201
    snap = display.get('/api/food/display').json()
    assert snap['today'] == '2026-11-16' and snap['timezone'] == 'Asia/Seoul'
    assert [row['expiry_status'] for row in snap['items']] == ['expired', 'today', 'soon', 'soon', 'later']
    assert [row['days_remaining'] for row in snap['items']] == [-1, 0, 1, 3, 4]
    clock[0] += 120  # UTC date is unchanged; Asia/Seoul has crossed midnight.
    snap = display.get('/api/food/display').json()
    assert snap['today'] == '2026-11-17'
    assert [row['expiry_status'] for row in snap['items']] == ['expired', 'expired', 'today', 'soon', 'soon']
    settings = app.state.store.get('settings')
    settings['timezone'] = 'UTC'
    app.state.store.set('settings', settings)
    assert display.get('/api/food/display').json()['today'] == '2026-11-16'
    assert len(app.state.food.items()) == 5  # No expiry deletion or consumption claim.


def test_same_name_batches_stay_separate_and_no_hidden_limit(service):
    for n in range(320):
        service.create(FoodCreate(**food_body(name='같은 식품', expiry_date=f'2026-11-{1+n%28:02d}')))
    items = service.items()
    assert len(items) == 320 and len({row['id'] for row in items}) == 320
    assert items == sorted(items, key=lambda row: (row['expiry_date'], row['name'], row['id']))
    assert len(service.snapshot()['items']) == 320


def test_concurrent_creates_and_restart_replay(service):
    body = FoodCreate(**food_body())
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: service.create(body), range(2)))
    assert sum(result['duplicate'] for result in results) == 1
    assert len(service.items()) == 1
    restarted = FoodService(service.store)
    assert restarted.create(body)['duplicate'] is True
    with pytest.raises(HTTPException) as exc:
        restarted.create(FoodCreate(**(body.model_dump() | {'quantity': 4})))
    assert exc.value.status_code == 409


def test_concurrent_versions_allow_only_one_edit(service):
    item = service.create(FoodCreate(**food_body()))

    def edit(quantity):
        try:
            return service.update_fields(item['id'], {'quantity': quantity}, 1)
        except HTTPException as exc:
            return {'status': exc.status_code}

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(edit, (3, 4)))
    assert sum(result.get('status') == 409 for result in results) == 1
    assert service.get(item['id'])['version'] == 2


def test_item_and_receipt_rollback_together(service):
    body = FoodCreate(**food_body())
    with pytest.raises(RuntimeError):
        with service.store.unit_of_work():
            service.create(body)
            raise RuntimeError('synthetic later step failure')
    assert service.items() == []
    assert service.create(body)['duplicate'] is False
    item = service.items()[0]
    with pytest.raises(RuntimeError):
        with service.store.unit_of_work():
            service.update_fields(item['id'], {'quantity': 5}, 1, request_id='update-rollback')
            raise RuntimeError('synthetic later step failure')
    assert service.get(item['id'])['quantity'] == 2
    assert service.update_fields(item['id'], {'quantity': 5}, 1, request_id='update-rollback')['duplicate'] is False


def test_reinitializing_service_cannot_commit_outer_uow(service):
    with pytest.raises(RuntimeError):
        with service.store.unit_of_work():
            service.store.set('food_test_marker', 'rollback me')
            FoodService(service.store)
            raise RuntimeError('synthetic rollback')
    assert service.store.get('food_test_marker') is None


def test_service_cap_refuses_new_data_without_losing_existing(service, monkeypatch):
    monkeypatch.setattr('app.food.MAX_FOOD_ITEMS', 2)
    first = service.create(FoodCreate(**food_body()))
    service.create(FoodCreate(**food_body()))
    with pytest.raises(HTTPException) as exc:
        service.create(FoodCreate(**food_body()))
    assert exc.value.status_code == 422 and len(service.items()) == 2
    service.update(first['id'], FoodUpdate(name='수정', quantity=4, expiry_date='2026-11-22', version=1))
    assert service.get(first['id'])['quantity'] == 4


def test_database_read_failure_does_not_fabricate_empty_list(service):
    with service.store.connect() as db:
        db.execute('DROP TABLE hub_food_items')
    with pytest.raises(sqlite3.OperationalError):
        service.items()
