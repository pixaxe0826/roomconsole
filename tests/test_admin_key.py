"""Synthetic admin-key rotation and authority-boundary regressions."""
import os
import stat
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import create_app


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.delenv('HUB_ADMIN_TOKEN', raising=False)
    monkeypatch.delenv('HUB_INGEST_TOKEN', raising=False)
    app = create_app(tmp_path, weather_enabled=False)
    with TestClient(app) as client:
        client.headers['X-Room-Request'] = '1'
        yield app, client, tmp_path


def rotate(client, old, new, confirmation=None):
    return client.post('/api/auth/key', json={
        'current_key': old,
        'new_key': new,
        'confirm_key': new if confirmation is None else confirmation,
    })


def test_admin_key_exactly_eight_characters_persists_and_reauthenticates(server):
    app, client, directory = server
    old = app.state.admin_token
    assert client.post('/api/auth/login', json={'token': old}).status_code == 200

    new = 'a!234567'
    result = rotate(client, old, new)
    assert result.status_code == 200
    assert result.json() == {'ok': True, 'relogin_required': True}
    assert new not in result.text and old not in result.text
    assert 'room_admin=' in result.headers['set-cookie']
    assert app.state.admin_token == new
    assert (directory / 'admin-token.txt').read_text(encoding='utf-8') == new + '\n'
    if os.name != 'nt':
        assert stat.S_IMODE((directory / 'admin-token.txt').stat().st_mode) == 0o600

    assert client.get('/api/auth/me').status_code == 401
    assert client.post('/api/auth/login', json={'token': old}).status_code == 401
    assert client.get('/api/admin/overview', headers={'Authorization': 'Bearer ' + old}).status_code == 401
    assert client.post('/api/auth/login', json={'token': new}).status_code == 200
    assert client.get('/api/auth/me').json()['role'] == 'admin'

    restarted = create_app(directory, weather_enabled=False)
    assert restarted.state.admin_token == new
    with TestClient(restarted) as again:
        assert again.post('/api/auth/login', json={'token': old}).status_code == 401
        assert again.post('/api/auth/login', json={'token': new}).status_code == 200


def test_short_key_and_bad_confirmation_never_rotate(server):
    app, client, directory = server
    old = app.state.admin_token
    assert client.post('/api/auth/login', json={'token': old}).status_code == 200
    original = (directory / 'admin-token.txt').read_text(encoding='utf-8')
    for candidate, confirmation, expected in [
        ('1234567', '1234567', 422),
        ('12345678', 'not-match', 422),
        (old, old, 409),
    ]:
        result = rotate(client, old, candidate, confirmation)
        assert result.status_code == expected
        assert old not in result.text and candidate not in result.text and confirmation not in result.text
        assert app.state.admin_token == old
        assert (directory / 'admin-token.txt').read_text(encoding='utf-8') == original
    assert client.get('/api/auth/me').status_code == 200
    invalid = client.post('/api/auth/key', json={'current_key': old, 'new_key': '12345678', 'confirm_key': '12345678', 'extra': True})
    assert invalid.status_code == 422 and old not in invalid.text and '12345678' not in invalid.text


def test_current_key_required_and_wrong_key_is_rate_limited(server):
    app, client, directory = server
    old = app.state.admin_token
    assert client.post('/api/auth/login', json={'token': old}).status_code == 200
    assert rotate(client, 'wrong-current', 'new!key8').status_code == 401
    assert client.get('/api/auth/me').status_code == 200
    for _ in range(9):
        assert rotate(client, 'wrong-current', 'new!key8').status_code == 401
    assert rotate(client, old, 'new!key8').status_code == 429
    assert app.state.admin_token == old
    assert (directory / 'admin-token.txt').read_text(encoding='utf-8').strip() == old


def test_rotation_revokes_other_admin_cookies_but_not_display_or_ingest(server):
    app, client, _ = server
    old = app.state.admin_token
    ingest = app.state.ingest_token
    assert client.post('/api/auth/login', json={'token': old}).status_code == 200
    with TestClient(app) as admin2, TestClient(app) as display:
        admin2.headers['X-Room-Request'] = '1'
        display.headers['X-Room-Request'] = '1'
        assert admin2.post('/api/auth/login', json={'token': old}).status_code == 200
        pair = client.post('/api/devices/pair', json={'name': 'synthetic-display'}).json()
        assert display.post('/api/devices/claim', json={'code': pair['path'].split('=')[1]}).status_code == 200
        assert display.get('/api/state').status_code == 200

        assert rotate(client, old, 'new!key8').status_code == 200
        assert admin2.get('/api/auth/me').status_code == 401
        assert admin2.get('/api/admin/overview').status_code == 401
        assert display.get('/api/state').status_code == 200
        assert display.post('/api/auth/key', json={'current_key': old, 'new_key': 'more!keys', 'confirm_key': 'more!keys'}).status_code == 401
        assert client.post('/api/voice/text', json={'request_id': 'after-rotation', 'text': 'test'}, headers={'Authorization': 'Bearer ' + ingest}).status_code == 202
        assert client.get('/api/admin/overview', headers={'Authorization': 'Bearer ' + ingest}).status_code == 401


def test_rotation_closes_authenticated_manager_websocket(server):
    app, client, _ = server
    old = app.state.admin_token
    assert client.post('/api/auth/login', json={'token': old}).status_code == 200
    with client.websocket_connect('/ws/manager') as ws:
        assert ws.receive_json()['type'] == 'hello'
        assert rotate(client, old, 'new!key8').status_code == 200
        with pytest.raises(WebSocketDisconnect) as disconnected:
            ws.receive_json()
        assert disconnected.value.code == 4001


def test_environment_managed_key_is_immutable_via_web(tmp_path, monkeypatch):
    monkeypatch.setenv('HUB_ADMIN_TOKEN', 'a' * 8)
    monkeypatch.delenv('HUB_INGEST_TOKEN', raising=False)
    app = create_app(tmp_path, weather_enabled=False)
    with TestClient(app) as client:
        client.headers['X-Room-Request'] = '1'
        assert client.post('/api/auth/login', json={'token': 'a' * 8}).status_code == 200
        result = rotate(client, 'a' * 8, 'new!key8')
        assert result.status_code == 409 and 'HUB_ADMIN_TOKEN' in result.json()['detail']
        assert client.get('/api/auth/me').status_code == 200
        assert app.state.admin_token == 'a' * 8
        assert not (tmp_path / 'admin-token.txt').exists()


def test_admin_only_eight_character_minimum_ingest_keeps_24(tmp_path, monkeypatch):
    monkeypatch.setenv('HUB_ADMIN_TOKEN', '12345678')
    monkeypatch.setenv('HUB_INGEST_TOKEN', 'q' * 24)
    assert create_app(tmp_path / 'eight-admin', weather_enabled=False).state.admin_token == '12345678'
    monkeypatch.setenv('HUB_ADMIN_TOKEN', '1234567')
    with pytest.raises(RuntimeError, match='HUB_ADMIN_TOKEN.*8'):
        create_app(tmp_path / 'seven-admin', weather_enabled=False)
    monkeypatch.setenv('HUB_ADMIN_TOKEN', '12345678')
    monkeypatch.setenv('HUB_INGEST_TOKEN', 'q' * 23)
    with pytest.raises(RuntimeError, match='HUB_INGEST_TOKEN.*24'):
        create_app(tmp_path / 'short-ingest', weather_enabled=False)


def test_write_error_keeps_previous_key_and_does_not_leave_temp_files(server):
    app, client, directory = server
    old = app.state.admin_token
    assert client.post('/api/auth/login', json={'token': old}).status_code == 200
    with patch('app.main.os.replace', side_effect=OSError('synthetic storage failure')):
        result = rotate(client, old, 'new!key8')
    assert result.status_code == 503
    assert app.state.admin_token == old
    assert (directory / 'admin-token.txt').read_text(encoding='utf-8').strip() == old
    assert not list(directory.glob('.admin-token-*'))
    assert client.post('/api/auth/login', json={'token': old}).status_code == 200
