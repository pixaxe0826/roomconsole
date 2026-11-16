"""Offline regression for FastAPI's postponed route annotations and policy auth."""

from fastapi.testclient import TestClient
import pytest

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


@pytest.mark.parametrize('profile', ['odyssey_openai', 'legacy'])
def test_stt_model_policy_updates_preserve_opt_ins_and_budget(tmp_path, monkeypatch, profile):
    monkeypatch.setenv('HUB_AI_PROFILE', profile)
    monkeypatch.setenv('HUB_OPENAI_STT_MODEL', 'gpt-4o-mini-transcribe')
    for name in ('OPENAI_API_KEY', 'HUB_OPENAI_API_KEY', 'HUB_LLM_API_KEY',
                 'HUB_PERSONAL_AGENT_ENABLED', 'HUB_OPENAI_ENABLED',
                 'HUB_OPENAI_AUDIO_ENABLED', 'HUB_RUN_LIVE_OPENAI_TESTS'):
        monkeypatch.delenv(name, raising=False)
    app = create_app(data_dir=tmp_path / 'synthetic-model-policy', weather_enabled=False)
    with TestClient(app) as client:
        headers = {'Authorization': 'Bearer ' + app.state.admin_token,
                   'X-Room-Request': '1'}
        initial = client.get('/api/agent/status', headers=headers).json()
        assert initial['stt_model'] == 'gpt-4o-mini-transcribe'

        # A model-only patch neither grants cloud/audio consent nor resets money.
        for model in ('gpt-transcribe', 'gpt-4o-transcribe', 'whisper-1',
                      'gpt-4o-mini-transcribe'):
            response = client.put('/api/agent/budget/policy',
                                  headers=headers, json={'stt_model': model})
            assert response.status_code == 200, response.text
            policy = response.json()['policy']
            assert policy['stt_model'] == model
            assert policy['monthly_cap_usd'] == initial['budget']['monthly_cap_usd']
            assert all(policy[key] is False for key in
                       ('agent_enabled', 'cloud_enabled', 'audio_cloud_enabled',
                        'allow_shared_notes_in_cloud'))
            status = client.get('/api/agent/status', headers=headers).json()
            assert status['stt_model'] == model
            assert status['tiers']['tier1_stt'] == (model if profile == 'odyssey_openai' else 'local/whisper')

        # Unrelated edits keep the stored model. Invalid model patches are atomic.
        budget = client.put('/api/agent/budget/policy', headers=headers,
                            json={'monthly_cap_usd': 73})
        assert budget.status_code == 200
        assert budget.json()['policy']['stt_model'] == 'gpt-4o-mini-transcribe'
        invalid = client.put('/api/agent/budget/policy', headers=headers,
                             json={'stt_model': 'unregistered-model', 'cloud_enabled': True})
        assert invalid.status_code == 422
        saved = client.get('/api/agent/status', headers=headers).json()
        assert saved['stt_model'] == 'gpt-4o-mini-transcribe'
        assert saved['cloud_enabled'] is False
        assert saved['budget']['monthly_cap_usd'] == 73
