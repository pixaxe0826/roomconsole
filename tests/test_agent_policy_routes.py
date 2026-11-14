"""Offline regression for FastAPI's postponed route annotations and policy auth."""

from fastapi.testclient import TestClient

from app.main import create_app


def test_odyssey_policy_accepts_json_and_keeps_manager_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv('HUB_AI_PROFILE', 'odyssey_openai')
    for name in ('OPENAI_API_KEY', 'HUB_OPENAI_API_KEY', 'HUB_LLM_API_KEY',
                 'HUB_PERSONAL_AGENT_ENABLED', 'HUB_OPENAI_ENABLED',
                 'HUB_OPENAI_AUDIO_ENABLED', 'HUB_RUN_LIVE_OPENAI_TESTS'):
        monkeypatch.delenv(name, raising=False)
    app = create_app(data_dir=tmp_path / 'synthetic-hub', weather_enabled=False)
    patch = {
        'agent_enabled': True,
        'cloud_enabled': True,
        'audio_cloud_enabled': True,
        'allow_shared_notes_in_cloud': False,
        'monthly_cap_usd': 42.5,
    }
    with TestClient(app) as client:
        # Status and mutations are both admin-only; the display/session boundary
        # is not relaxed to repair a broken route annotation.
        assert client.get('/api/agent/status').status_code == 401
        assert client.put('/api/agent/budget/policy', json=patch).status_code == 401
        login = client.post('/api/auth/login', json={'token': app.state.admin_token},
                            headers={'X-Room-Request': '1'})
        assert login.status_code == 200
        assert client.put('/api/agent/budget/policy', json=patch).status_code == 403
        assert client.put('/api/agent/budget/policy', json=patch,
                          headers={'X-Room-Request': '1', 'Origin': 'https://evil.invalid'}).status_code == 403

        saved = client.put('/api/agent/budget/policy', json=patch,
                           headers={'X-Room-Request': '1'})
        assert saved.status_code == 200, saved.text
        assert saved.json()['policy']['monthly_cap_usd'] == 42.5
        assert saved.json()['budget']['monthly_cap_usd'] == 42.5
        status = client.get('/api/agent/status').json()
        assert status['runtime_profile'] == 'odyssey_openai'
        assert status['agent_enabled'] is True
        assert status['cloud_enabled'] is True
        assert status['audio_cloud_enabled'] is True
        assert status['openai']['api_key_configured'] is False

        # The UI's default integer value must also save (JSON 100, not 100.0).
        default_budget = client.put('/api/agent/budget/policy',
                                    json={'monthly_cap_usd': 100},
                                    headers={'X-Room-Request': '1'})
        assert default_budget.status_code == 200, default_budget.text
        assert default_budget.json()['budget']['monthly_cap_usd'] == 100.0

        invalid = client.put('/api/agent/budget/policy',
                             json={'monthly_cap_usd': 0},
                             headers={'X-Room-Request': '1'})
        assert invalid.status_code == 422
        assert client.get('/api/agent/status').json()['budget']['monthly_cap_usd'] == 100.0


def test_agent_audio_file_annotation_is_resolved_before_validation(tmp_path, monkeypatch):
    monkeypatch.setenv('HUB_AI_PROFILE', 'odyssey_openai')
    for name in ('OPENAI_API_KEY', 'HUB_OPENAI_API_KEY', 'HUB_RUN_LIVE_OPENAI_TESTS'):
        monkeypatch.delenv(name, raising=False)
    app = create_app(data_dir=tmp_path / 'synthetic-audio', weather_enabled=False)
    with TestClient(app) as client:
        response = client.post('/api/agent/audio',
                               headers={'Authorization': 'Bearer ' + app.state.admin_token,
                                        'X-Room-Request': '1'},
                               files={'file': ('synthetic.wav', b'not-real-audio', 'audio/wav')},
                               data={'request_id': 'synthetic-policy-binding', 'mode': 'unsupported'})
        assert response.status_code == 422
        assert response.json()['detail'] == '지원하지 않는 실행 모드입니다.'
