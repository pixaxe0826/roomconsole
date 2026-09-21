"""Synthetic DB/HTTP protocol contracts. No V35 data or real model inference."""
import asyncio
from dataclasses import replace
from datetime import date
import json
import sqlite3
from types import MappingProxyType
import uuid

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.life import LifeService, NoteCreate
from app.models import TaskCreate
from app.store import Store
from app.widget_protocol import ExecutionContext, WidgetRequest, WidgetRegistry, request_digest
from app.widget_protocol.adapters import build_registry, TaskServices
from app.widget_protocol.core import AdapterResult, Operation, ProtocolFault, WidgetAdapter
from app.widget_protocol.models import WidgetResponse
from app.widget_protocol.schemas import Empty

ADMIN = ExecutionContext(principal='test-manager', role='admin',
                         permissions=frozenset({'read', 'write', 'control'}))
API = '/api/widget-protocol'


def req(widget, action, args=None, target=None, **kwargs):
    return WidgetRequest(request_id=uuid.uuid4().hex, widget=widget, action=action,
                         args=args or {}, target=target, **kwargs)


def item(identity):
    return {'type': 'item_id', 'value': identity}


def run(registry, request, *, confirmed=False, authority=ADMIN):
    if confirmed:
        authority = replace(authority, confirmed_digest=request_digest(request))
    return asyncio.run(registry.execute(request, authority))


@pytest.fixture
def hub(tmp_path):
    app = create_app(tmp_path / 'data', weather_enabled=False)
    with TestClient(app) as client:
        client.headers.update({'Authorization': 'Bearer ' + app.state.admin_token, 'X-Room-Request': '1'})
        yield app, client, app.state.widget_protocol


def note(client, body='합성 메모 본문', shared=False):
    response = client.post('/api/life/notes', json={'request_id': uuid.uuid4().hex,
                           'title': '합성 메모', 'body': body, 'shared': shared})
    assert response.status_code == 201, response.text
    return response.json()['id']


def task(client, title='합성 할 일', day='2026-09-21', clock=None):
    response = client.post('/api/tasks', json={'title': title, 'date': day, 'time': clock})
    assert response.status_code == 201, response.text
    return response.json()['ids'][0]


def post(client, request):
    return client.post(API + '/requests', json=request.model_dump(mode='json'))


def test_case1_memo_read_real_repository_and_private_admin(hub):
    app, client, registry = hub
    identity = note(client, '실제 저장값 그대로 <script>test-only</script>')
    result = post(client, req('memo', 'read', target=item(identity)))
    assert result.status_code == 200
    payload = result.json()
    assert payload['status'] == 'success'
    assert payload['data']['body'] == '실제 저장값 그대로 <script>test-only</script>'
    assert payload['data']['id'] == identity and not payload['data']['shared']
    assert payload['meta']['source_of_truth'] == 'room_hub_sqlite.hub_notes'
    assert not payload['meta']['changed']
    with app.state.store.connect() as db:
        assert db.execute('SELECT count(*) FROM llm_requests').fetchone()[0] == 0


def test_memo_current_and_explicit_widget_reuse_existing_snapshot(hub):
    app, client, registry = hub
    layout = {'columns': 8, 'rows': 6, 'version': 1, 'widgets': [
        {'id': 'memo-a', 'type': 'note', 'x': 0, 'y': 0, 'w': 4, 'h': 2, 'config': {'text': '첫 기본 문구'}},
        {'id': 'memo-b', 'type': 'note', 'x': 4, 'y': 0, 'w': 4, 'h': 2, 'config': {'text': '두 번째 문구'}},
    ]}
    assert client.put('/api/layout', json=layout).status_code == 200
    assert run(registry, req('memo', 'read')).data['body'] == '첫 기본 문구'
    result = run(registry, req('memo', 'read', target={'type': 'widget_id', 'value': 'memo-b'}))
    assert result.data['body'] == '두 번째 문구' and result.data['widget_id'] == 'memo-b'
    assert result.meta.source_of_truth == 'room_hub_sqlite.kv.note'
    assert not result.data['active_card_known'] and result.data['id'] is None


def test_memo_last_uses_real_modified_time_and_tie_clarifies(hub):
    app, client, registry = hub
    a = note(client, '이전')
    b = note(client, '최근')
    with app.state.store.connect() as db:
        db.execute('UPDATE hub_notes SET updated_at=? WHERE id=?', ('2026-09-21T10:00:00+00:00', a))
        db.execute('UPDATE hub_notes SET updated_at=? WHERE id=?', ('2026-09-21T11:00:00+00:00', b))
    request = req('memo', 'read', target={'type': 'reference', 'value': 'last'})
    assert run(registry, request).data['body'] == '최근'
    with app.state.store.connect() as db:
        db.execute('UPDATE hub_notes SET updated_at=?', ('2026-09-21T11:00:00+00:00',))
    assert run(registry, request).status == 'needs_clarification'


@pytest.mark.parametrize('selector', ['active', 'selected', 'today', 'made-up'])
def test_unresolved_context_never_guesses(hub, selector):
    _, client, registry = hub
    note(client)
    request = req('memo', 'read', target={'type': 'reference', 'value': selector},
                  context={'selected_item': 'untrusted', 'active_widget': 'untrusted'})
    result = run(registry, request)
    assert result.status == 'needs_clarification' and result.error.code == 'CONTEXT_NOT_RESOLVED'


def test_case2_todo_list_real_source_with_explicit_today(hub):
    app, client, registry = hub
    today = client.get('/api/state').json()['today']
    identity = task(client, day=today)
    result = run(registry, req('todo', 'list', {'date': today}))
    assert result.status == 'success' and [t['id'] for t in result.data['items']] == [identity]
    assert result.data['range'] == [today, today]
    assert result.meta.source_of_truth == 'room_hub_sqlite.tasks'
    assert client.get('/api/state').json()['tasks'][0]['id'] == identity


def test_case3_calendar_range_and_period_use_shared_sql_query(hub):
    app, client, registry = hub
    task(client, clock='11:59')
    afternoon = task(client, clock='12:00')
    task(client, clock=None)
    task(client, day='2026-09-22', clock='15:00')
    result = run(registry, req('calendar', 'list', {'start': '2026-09-21', 'end': '2026-09-21', 'period': 'afternoon'}))
    assert result.status == 'success'
    assert [t['id'] for t in result.data['items']] == [afternoon]
    assert result.data['untimed_count'] == 1 and result.data['count'] == 1
    assert result.meta.source_of_truth == 'room_hub_sqlite.tasks'


def test_case4_missing_alarm_time_is_structured(hub):
    _, client, _ = hub
    result = post(client, req('alarm', 'set', {'label': '테스트', 'time': None, 'date': '2099-01-01'}))
    assert result.status_code == 422
    body = result.json()
    assert body['status'] == 'needs_clarification'
    assert body['error']['code'] == 'MISSING_REQUIRED_ARGUMENT'
    assert body['error']['field'] == 'args.time'
    assert 'input' not in json.dumps(body)


def test_case5_unknown_action(hub):
    _, client, _ = hub
    result = post(client, req('todo', 'fly'))
    assert result.status_code == 400
    assert result.json()['error']['code'] == 'UNSUPPORTED_ACTION'


def test_case6_registry_and_typed_schemas(hub):
    _, client, registry = hub
    assert registry.list_widgets() == ['memo', 'todo', 'calendar', 'alarm']
    assert registry.get('does-not-exist') is None
    manifest = client.get(API + '/widgets').json()
    assert manifest['protocol_version'] == '1.0' and manifest['http_write_execution'] is False
    for widget in manifest['widgets']:
        for cap in widget['capabilities']:
            assert 'properties' in cap['input_schema'] and 'properties' in cap['output_schema']
            assert cap['requires_confirmation'] == (not cap['read_only'])
            assert cap['permission_level'] in {'read', 'write', 'control', 'dangerous'}
    schema = client.get(API + '/schema').json()
    assert 'accepted' in schema['response']['properties']['status']['enum']
    assert 'running' in schema['response']['properties']['status']['enum']
    assert set(client.get(API + '/widgets/todo').json()['capabilities'][0]) >= {'action', 'idempotent'}
    assert client.get(API + '/widgets/missing').status_code == 404


@pytest.mark.parametrize('widget,action,args,target,field', [
    ('todo', 'list', {}, None, 'args.start'),
    ('calendar', 'list', {'start': '2026-09-21'}, None, 'args.end'),
    ('todo', 'get', {}, None, 'target'),
    ('todo', 'get', {}, {'type': 'item_id', 'value': None}, 'target.value'),
    ('todo', 'add', {'date': '2026-09-21'}, None, 'args.title'),
    ('todo', 'complete', {'version': None}, item('x'), 'args.version'),
    ('memo', 'write', {'body': 'a'}, None, 'args.title'),
    ('memo', 'write', {'body': 'a'}, item('x'), 'args.version'),
    ('memo', 'write', {'title': 'a'}, None, 'args.body'),
])
def test_missing_arguments_never_escape_as_validation_exceptions(hub, widget, action, args, target, field):
    _, client, _ = hub
    result = post(client, req(widget, action, args, target))
    body = result.json()
    assert body['status'] == 'needs_clarification' and body['error']['field'] == field
    assert body['meta']['changed'] is False


@pytest.mark.parametrize('args', [
    {'date': 'today'}, {'date': 'unknown'}, {'date': '2026-02-30'},
    {'start': '2026-09-22', 'end': '2026-09-21'},
    {'date': '2026-09-21', 'start': '2026-09-21'},
    {'date': '2026-09-21', 'limit': True}, {'date': '2026-09-21', 'limit': 0},
    {'date': '2026-09-21', 'status': 'invented'}, {'date': '2026-09-21', 'period': 'night'},
])
def test_invalid_canonical_slots_are_rejected(hub, args):
    _, client, registry = hub
    assert run(registry, req('todo', 'list', args)).status == 'invalid_request'


def test_nullable_time_accepted_and_unknown_rejected_without_crash(hub):
    _, client, registry = hub
    request = req('todo', 'add', {'title': '시간 없는 항목', 'date': '2026-09-21', 'time': None})
    assert run(registry, request).status == 'needs_confirmation'
    result = run(registry, request, confirmed=True)
    assert result.status == 'success'
    saved = run(registry, req('todo', 'get', target=item(result.data['ids'][0])))
    assert saved.data['time'] is None
    bad = req('todo', 'add', {'title': '금지', 'date': '2026-09-21', 'time': 'unknown'})
    assert run(registry, bad, confirmed=True).status == 'invalid_request'


@pytest.mark.parametrize('widget,action,args', [
    ('todo', 'add', {'title': '차단', 'date': '2026-09-21'}),
    ('memo', 'write', {'title': '차단', 'body': '차단'}),
    ('calendar', 'add', {'title': '차단', 'date': '2026-09-21'}),
])
def test_http_user_confirmed_is_not_write_authority(hub, widget, action, args):
    app, client, _ = hub
    request = req(widget, action, args, context={'source': 'voice', 'user_confirmed': True, 'session_id': 'admin'})
    result = post(client, request)
    assert result.status_code == 409
    assert result.json()['status'] == 'needs_confirmation'
    assert result.json()['meta']['request_digest'] == request_digest(request)
    assert app.state.store.tasks() == [] and app.state.life.notes() == []
    # A public digest cannot be supplied as an authority in the wire envelope.
    forged = request.model_dump(mode='json') | {'confirmed_digest': request_digest(request)}
    assert client.post(API + '/requests', json=forged).json()['status'] == 'invalid_request'


@pytest.mark.parametrize('credential', ['anonymous', 'display', 'ingest'])
def test_new_transport_does_not_expand_display_or_ingest_permissions(hub, credential):
    app, client, registry = hub
    identity = note(client, 'PRIVATE_SENTINEL')
    if credential == 'display':
        code = client.post('/api/devices/pair', json={'name': 'test'}).json()['path'].split('=')[1]
        client.post('/api/devices/claim', json={'code': code})
    client.headers.pop('Authorization')
    if credential == 'ingest':
        client.headers['Authorization'] = 'Bearer ' + app.state.ingest_token
    response = post(client, req('memo', 'read', target=item(identity)))
    assert response.status_code == 401
    assert response.json()['status'] == 'permission_denied' and 'PRIVATE_SENTINEL' not in response.text
    for path in ['/widgets', '/schema', '/health']:
        assert client.get(API + path).status_code == 401


def test_authority_scope_and_request_digest_are_enforced(hub):
    _, client, registry = hub
    request = req('todo', 'add', {'title': '보존', 'date': '2026-09-21'})
    read = replace(ADMIN, permissions=frozenset({'read'}), confirmed_digest=request_digest(request))
    assert run(registry, request, authority=read).status == 'permission_denied'
    wrong = replace(ADMIN, confirmed_digest='wrong')
    assert run(registry, request, authority=wrong).status == 'needs_confirmation'
    for role in ['display', 'ingest', None]:
        denied = replace(ADMIN, role=role)
        assert run(registry, req('todo', 'list', {'date': '2026-09-21'}), authority=denied).status == 'permission_denied'


def test_memo_write_append_clear_use_existing_service_and_versions(hub):
    app, client, registry = hub
    created = run(registry, req('memo', 'write', {'title': '새 메모', 'body': 'A', 'shared': True}), confirmed=True)
    assert created.status == 'success' and created.meta.changed
    identity = created.data['id']
    appended = run(registry, req('memo', 'append', {'version': 1, 'text': '\nB'}, item(identity)), confirmed=True)
    assert appended.status == 'success' and appended.data['version'] == 2
    assert next(n for n in app.state.life.notes() if n['id'] == identity)['body'] == 'A\nB'
    stale = run(registry, req('memo', 'clear', {'version': 1}, item(identity)), confirmed=True)
    assert stale.status == 'conflict'
    cleared = run(registry, req('memo', 'clear', {'version': 2}, item(identity)), confirmed=True)
    assert cleared.status == 'success' and cleared.data['version'] == 3
    current = run(registry, req('memo', 'read', target=item(identity)))
    assert current.data['body'] == '' and current.data['shared'] is True
    replaced = run(registry, req('memo', 'write', {'body': 'C', 'version': 3}, item(identity)), confirmed=True)
    assert replaced.data['version'] == 4
    assert client.get('/api/life/notes').json()['items'][0]['body'] == 'C'
    assert len(app.state.life.notes()) == 1


def test_append_overflow_does_not_write(hub):
    app, client, registry = hub
    identity = note(client, 'x' * 8000)
    result = run(registry, req('memo', 'append', {'version': 1, 'text': 'y'}, item(identity)), confirmed=True)
    assert result.status == 'invalid_request'
    assert app.state.life.notes()[0]['version'] == 1


def test_case7_shared_todo_calendar_crud_and_existing_http_unchanged(hub):
    app, client, registry = hub
    added = run(registry, req('todo', 'add', {'title': '한 항목', 'date': '2026-09-21'}), confirmed=True)
    identity = added.data['ids'][0]
    assert run(registry, req('calendar', 'get', target=item(identity))).data['title'] == '한 항목'
    completed = run(registry, req('todo', 'complete', {'version': 1}, item(identity)), confirmed=True)
    assert completed.status == 'success' and completed.data['completed']
    no_op = run(registry, req('todo', 'complete', {'version': 2}, item(identity)), confirmed=True)
    assert no_op.status == 'success' and not no_op.meta.changed and not no_op.meta.events
    reopened = run(registry, req('todo', 'reopen', {'version': 2}, item(identity)), confirmed=True)
    assert reopened.status == 'success' and not reopened.data['completed']
    edited = run(registry, req('calendar', 'update', {'version': 3, 'time': '13:20'}, item(identity)), confirmed=True)
    assert edited.status == 'success' and edited.data['time'] == '13:20'
    assert client.get('/api/state').json()['tasks'][0]['time'] == '13:20'
    stale = run(registry, req('calendar', 'delete', {'version': 1}, item(identity)), confirmed=True)
    assert stale.status == 'conflict'
    removed = run(registry, req('calendar', 'delete', {'version': 4}, item(identity)), confirmed=True)
    assert removed.status == 'success' and removed.data == {'deleted': 1}
    assert client.get('/api/state').json()['tasks'] == []


def test_repeat_creation_and_single_occurrence_delete_preserved(hub):
    app, client, registry = hub
    added = run(registry, req('calendar', 'add', {'title': '반복', 'date': '2026-09-21',
        'repeat': {'frequency': 'daily', 'until': '2026-09-23'}}), confirmed=True)
    assert added.status == 'success' and added.data['count'] == 3
    first = added.data['ids'][0]
    deleted = run(registry, req('todo', 'delete', {'version': 1}, item(first)), confirmed=True)
    assert deleted.data['deleted'] == 1 and len(app.state.store.tasks()) == 2
    bad = run(registry, req('todo', 'delete', {'version': 1, 'scope': 'series'}, item(first)), confirmed=True)
    assert bad.status == 'invalid_request'


def test_concurrent_retries_deduplicate_and_changed_means_this_call(hub):
    app, client, registry = hub
    request = req('todo', 'add', {'title': '한 번', 'date': '2026-09-21'})
    authority = replace(ADMIN, confirmed_digest=request_digest(request))
    async def concurrent():
        return await asyncio.gather(*(registry.execute(request, authority) for _ in range(8)))
    results = asyncio.run(concurrent())
    assert all(r.status == 'success' for r in results)
    assert sum(r.meta.changed for r in results) == 1
    assert sum(r.meta.duplicate for r in results) == 7
    assert len(app.state.store.tasks()) == 1
    assert all(r.data == results[0].data for r in results)
    bad = request.model_copy(update={'args': {'title': '다른 내용', 'date': '2026-09-21'}})
    assert run(registry, bad, confirmed=True).status == 'conflict'


def test_dedup_key_can_correlate_new_request_id(hub):
    app, client, registry = hub
    a = req('memo', 'write', {'title': '제목', 'body': '본문'}, idempotency_key='logical-operation-1')
    b = a.model_copy(update={'request_id': uuid.uuid4().hex})
    first, second = run(registry, a, confirmed=True), run(registry, b, confirmed=True)
    assert first.status == second.status == 'success'
    assert second.request_id == b.request_id and second.meta.duplicate
    assert len(app.state.life.notes()) == 1


def test_receipt_capacity_refuses_new_write_instead_of_eviction(hub):
    app, client, registry = hub
    registry._max_receipts = 1
    a = req('todo', 'add', {'title': '첫', 'date': '2026-09-21'})
    assert run(registry, a, confirmed=True).status == 'success'
    b = req('todo', 'add', {'title': '둘', 'date': '2026-09-21'})
    result = run(registry, b, confirmed=True)
    assert result.status == 'unavailable' and result.error.code == 'RECEIPT_CAPACITY'
    assert run(registry, a, confirmed=True).meta.duplicate
    assert len(app.state.store.tasks()) == 1


def test_reads_are_never_replayed_from_stale_cache(hub):
    app, client, registry = hub
    identity = note(client, '전')
    request = req('memo', 'read', target=item(identity))
    assert run(registry, request).data['body'] == '전'
    client.put('/api/life/notes/' + identity, json={'version': 1, 'title': '제목', 'body': '후'})
    result = run(registry, request)
    assert result.data['body'] == '후' and not result.meta.duplicate
    assert client.request('DELETE', '/api/life/notes/' + identity, json={'version': 2}).status_code == 200
    assert run(registry, request).status == 'not_found'


def test_database_failure_is_not_fabricated_empty_result(hub, monkeypatch):
    app, client, registry = hub
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError('PRIVATE_DB_PATH should not appear')
    monkeypatch.setattr(app.state.store, 'query_tasks', broken)
    result = run(registry, req('todo', 'list', {'date': '2026-09-21'}))
    assert result.status == 'error' and result.data is None
    assert 'PRIVATE_DB_PATH' not in result.model_dump_json()


def test_uncertain_write_is_not_automatically_executed_twice(hub, monkeypatch):
    app, client, registry = hub
    adapter = registry.get('todo')
    calls = []
    original = adapter.services.add
    async def committed_then_failed(args):
        calls.append(1)
        await original(args)
        raise RuntimeError('PRIVATE_INTERNAL_ERROR')
    adapter.services = replace(adapter.services, add=committed_then_failed)
    request = req('todo', 'add', {'title': '한 번만', 'date': '2026-09-21'})
    result = run(registry, request, confirmed=True)
    assert result.status == 'error' and result.meta.changed is None and result.meta.outcome_uncertain
    again = run(registry, request, confirmed=True)
    assert again.meta.duplicate and again.meta.outcome_uncertain and len(calls) == 1
    assert len(app.state.store.tasks()) == 1 and 'PRIVATE_INTERNAL_ERROR' not in again.model_dump_json()


def test_audit_redacts_values_and_does_not_bump_state_on_read(hub):
    app, client, registry = hub
    identity = note(client, 'PRIVATE_MEMO_SENTINEL')
    before = app.state.store.get('revision')
    result = run(registry, req('memo', 'read', target=item(identity), context={'session_id': 'PRIVATE_SESSION'}))
    assert result.status == 'success' and app.state.store.get('revision') == before
    with app.state.store.connect() as db:
        raw = db.execute("SELECT detail FROM audit WHERE event='widget.protocol' ORDER BY id DESC LIMIT 1").fetchone()[0]
    entry = json.loads(raw)
    assert entry['args']['values_redacted'] and entry['latency_ms'] >= 0
    assert 'PRIVATE_MEMO_SENTINEL' not in raw and 'PRIVATE_SESSION' not in raw
    assert entry['source_of_truth'] == 'room_hub_sqlite.hub_notes'
    assert 'widget.protocol' in str(client.get('/api/admin/overview').json()['events'])


def test_audit_error_does_not_report_committed_write_as_failure(hub):
    app, client, registry = hub
    def failed_audit(record):
        raise RuntimeError('synthetic')
    registry._audit = failed_audit
    result = run(registry, req('todo', 'add', {'title': '한 번', 'date': '2026-09-21'}), confirmed=True)
    assert result.status == 'success' and result.meta.warnings == ['AUDIT_UNAVAILABLE']
    assert len(app.state.store.tasks()) == 1


def test_alarm_actual_service_and_confirmation_guarded_controls(hub):
    app, client, registry = hub
    added = client.post('/api/life/alarms', json={'request_id': uuid.uuid4().hex, 'label': '합성 알람',
                        'time': '15:00', 'date': '2099-01-01'})
    assert added.status_code == 201, added.text
    result = run(registry, req('alarm', 'list'))
    assert result.status == 'success' and result.data['items'][0]['id'] == added.json()['id']
    assert result.data['delivery'] == 'foreground_browser_only' and not result.data['sound_confirmed']
    pending = req('alarm', 'set', {'label': '확인된 추가', 'time': '16:00', 'date': '2099-01-01'})
    assert run(registry, pending).status == 'needs_confirmation'
    assert len(app.state.life.alarms()) == 1
    created = run(registry, pending, confirmed=True)
    assert created.status == 'success' and len(app.state.life.alarms()) == 2
    assert created.data['sound_confirmed'] is False


@pytest.mark.parametrize('body', [b'{', b'null', b'[]', b'{"request_id":"a","request_id":"b"}',
                                b'{"args":{"time":NaN}}', b'\xff'])
def test_bad_http_envelope_never_crashes(hub, body):
    app, client, registry = hub
    response = client.post(API + '/requests', content=body, headers={'Content-Type': 'application/json'})
    assert response.status_code == 400
    assert response.json()['status'] == 'invalid_request'


def test_unknown_version_oversize_and_extra_fields(hub):
    _, client, registry = hub
    good = req('todo', 'list', {'date': '2026-09-21'}).model_dump(mode='json')
    version = client.post(API + '/requests', json=good | {'protocol_version': '2.0'})
    assert version.json()['error']['code'] == 'UNSUPPORTED_PROTOCOL_VERSION'
    assert version.json()['request_id'] == good['request_id']
    assert client.post(API + '/requests', json=good | {'role': 'admin'}).status_code == 400
    large = client.post(API + '/requests', json=good | {'extensions': {'too_big': 'x' * 65536}})
    assert large.status_code == 413
    assert run(registry, req('missing', 'read')).error.code == 'WIDGET_NOT_FOUND'


def test_health_and_adapter_state_interfaces(hub):
    app, client, registry = hub
    health = client.get(API + '/health').json()['widgets']
    assert all(x['available'] for x in health.values())
    request = req('todo', 'list', {'date': '2026-09-21'})
    state = asyncio.run(registry.get('todo').get_state(request, ADMIN))
    assert state.data['items'] == [] and not state.changed


def test_add_adapter_without_changing_router_and_no_model_dependency(tmp_path):
    class Example(WidgetAdapter):
        name = 'example'
        source_of_truth = 'test_fixture'
        operations = MappingProxyType({'read': Operation(Empty, Empty, 'synthetic registry extension')})
        async def _execute(self, request, args, authority):
            return AdapterResult({})
    registry = WidgetRegistry()
    registry.register(Example())
    assert run(registry, req('example', 'read')).status == 'success'
    with pytest.raises(ValueError):
        registry.register(Example())
    # Actual read adapters are usable without creating a SpeechHub or LLMHub.
    store = Store(tmp_path / 'without-model.sqlite3')
    async def changed(*args):
        pass
    life = LifeService(store, changed)
    async def forbidden(*args, **kwargs):
        raise AssertionError('read must not call write')
    pure = build_registry(store, life, changed, TaskServices(forbidden, forbidden, forbidden, forbidden))
    assert run(pure, req('todo', 'list', {'date': '2026-09-21'})).status == 'success'
    with store.connect() as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='llm_requests'").fetchone()


def test_registry_does_not_change_schema_or_assistant_tool_permissions(hub):
    app, client, registry = hub
    from app.capabilities import INTENTS, manifest
    assert 'memo.write' not in INTENTS and 'alarm.set' not in INTENTS
    before = client.get('/api/assistant/capabilities').json()
    assert before == json.loads(json.dumps(manifest()))
    with app.state.store.connect() as db:
        assert not [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%protocol%'")]
    assert client.get('/api/speech/status').json()['auto_submit_llm'] is True


def test_missing_explicit_memo_widget_is_not_found_not_default(hub):
    app, client, registry = hub
    result = run(registry, req('memo', 'read', target={'type': 'widget_id', 'value': 'missing-widget'}))
    assert result.status == 'not_found' and result.data is None


def test_last_reference_does_not_substitute_unversioned_legacy_card(hub):
    app, client, registry = hub
    app.state.store.set('layout', {'widgets': [{'id': 'memo', 'type': 'note', 'config': {'text': 'legacy text'}}]})
    current = run(registry, req('memo', 'read'))
    assert current.data['body'] == 'legacy text'
    last = run(registry, req('memo', 'read', target={'type': 'reference', 'value': 'last'}))
    assert last.status == 'not_found' and last.data is None


def test_adapter_spi_cannot_skip_authorization(hub):
    _, client, registry = hub
    request = req('memo', 'write', {'title': 'not executed', 'body': 'private'})
    with pytest.raises(ProtocolFault) as exc:
        asyncio.run(registry.get('memo').execute(request, ADMIN))
    assert exc.value.status == 'needs_confirmation'


def test_service_output_is_validated_not_reported_as_success(hub, monkeypatch):
    app, client, registry = hub
    monkeypatch.setattr(app.state.store, 'query_tasks', lambda *a, **k: {'fabricated': 'PRIVATE'})
    response = run(registry, req('todo', 'list', {'date': '2026-09-21'}))
    assert response.status == 'error' and response.error.code == 'ADAPTER_ERROR'
    assert response.data is None and 'PRIVATE' not in response.model_dump_json()


def test_cancelled_write_reserves_uncertain_receipt_without_reexecution():
    calls = []
    class CancelledWrite(WidgetAdapter):
        name = 'cancelled'
        source_of_truth = 'test_only'
        operations = {'write': Operation(Empty, Empty, 'synthetic cancelled write', False, 'write')}
        async def _execute(self, request, args, authority):
            calls.append(request.request_id)
            raise asyncio.CancelledError
    registry = WidgetRegistry()
    registry.register(CancelledWrite())
    request = req('cancelled', 'write')
    with pytest.raises(asyncio.CancelledError):
        run(registry, request, confirmed=True)
    response = run(registry, request, confirmed=True)
    assert response.status == 'error' and response.meta.outcome_uncertain
    assert response.meta.changed is None and response.meta.duplicate
    assert len(calls) == 1


def test_origin_and_csrf_enforcement_remains_on_protocol_endpoint(hub):
    app, client, registry = hub
    request = req('todo', 'list', {'date': '2026-09-21'}).model_dump(mode='json')
    foreign = client.post(API + '/requests', json=request, headers={'Origin': 'https://attacker.invalid'})
    assert foreign.status_code == 403
    client.post('/api/auth/login', json={'token': app.state.admin_token})
    client.headers.pop('Authorization')
    client.headers.pop('X-Room-Request')
    assert client.post(API + '/requests', json=request).status_code == 403
    assert client.post(API + '/requests', json=request, headers={'X-Room-Request': '1'}).status_code == 200
