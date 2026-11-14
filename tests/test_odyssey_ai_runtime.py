"""PC-free diagnostic boundary; synthetic policy and no outbound requests."""
from dataclasses import replace
import sys

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.openai_gateway import OpenAIGateway
from app.speech import SpeechConfig


@pytest.fixture
def odyssey(tmp_path, monkeypatch):
    monkeypatch.setenv('HUB_AI_PROFILE', 'odyssey_openai')
    for name in ('OPENAI_API_KEY', 'HUB_OPENAI_API_KEY', 'HUB_FFMPEG',
                 'HUB_LLM_API_KEY', 'HUB_OPENAI_BASE_URL'):
        monkeypatch.delenv(name, raising=False)
    for name in ('HUB_PERSONAL_AGENT_ENABLED', 'HUB_OPENAI_ENABLED',
                 'HUB_OPENAI_AUDIO_ENABLED', 'HUB_RUN_LIVE_OPENAI_TESTS',
                 'HUB_LLM_ENABLED', 'HUB_SECURE_COOKIE'):
        monkeypatch.setenv(name, 'false')
    # Stale PC metadata is intentionally retained. It must never be probed or
    # used for readiness in the new process-level profile.
    config = SpeechConfig(enabled=True, backend='remote_http',
                          remote_url='http://127.0.0.1:8178/inference',
                          remote_model='retired-synthetic-model',
                          ffmpeg=sys.executable)
    app = create_app(tmp_path, weather_enabled=False, speech_config=config)
    agent = app.state.personal_agent
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append('outbound')
        raise AssertionError('Diagnostics must never probe PC or OpenAI.')

    def forbidden_legacy_status(*args, **kwargs):
        calls.append('legacy-config')
        raise AssertionError('Diagnostics must not read retired model readiness.')

    agent.gateway = OpenAIGateway(agent.governor, api_key='synthetic-not-a-secret',
                                 transport=forbidden, profile=agent.profile)
    agent.governor.set_policy({'agent_enabled': True, 'cloud_enabled': True,
                              'audio_cloud_enabled': True})
    monkeypatch.setattr(app.state.speech, 'probe', forbidden)
    monkeypatch.setattr(app.state.llm, 'probe', forbidden)
    monkeypatch.setattr(app.state.speech, 'connection_status', forbidden_legacy_status)
    with TestClient(app) as client:
        client.headers.update({'Authorization': 'Bearer ' + app.state.admin_token,
                               'X-Room-Request': '1'})
        yield app, client, calls


def test_status_and_explicit_refresh_do_not_contact_retired_pc_or_provider(odyssey):
    app, client, calls = odyssey
    for method, path in [('GET', '/api/ai/status'), ('POST', '/api/ai/probe')]:
        response = client.request(method, path)
        assert response.status_code == 200, response.text
        status = response.json()
        assert status['runtime_profile'] == 'odyssey_openai'
        assert status['orchestrator'] == 'odyssey'
        assert status['pc_required'] is False
        assert status['ssh_tunnel_required'] is False
        assert status['legacy_fallback_enabled'] is False
        assert status['stt_fallback_to_local'] is False
        assert status['check_scope'] == 'local_configuration_only'
        assert status['provider_verified'] is False
        assert status['automatic_inference'] is False
        for name in ('speech', 'llm'):
            assert status[name]['backend'] == 'openai'
            assert status[name]['state'] == 'configured'
            assert status[name]['provider_verified'] is False
            assert status[name]['last_probe'] is None
        assert status['speech']['ready'] is True
        assert status['speech']['configured_model'] == app.state.personal_agent.governor.policy().stt_model
        assert 'retired-synthetic-model' not in response.text
        assert '127.0.0.1:8178' not in response.text
        assert 'synthetic-not-a-secret' not in response.text
    assert calls == []
    assert client.get('/healthz').json()['status'] == 'ok'


@pytest.mark.parametrize(('patch', 'speech_state', 'llm_state'), [
    ({'agent_enabled': False}, 'agent_disabled', 'agent_disabled'),
    ({'cloud_enabled': False}, 'cloud_disabled', 'cloud_disabled'),
    ({'audio_cloud_enabled': False}, 'audio_cloud_disabled', 'configured'),
])
def test_disabled_policy_is_explicit_and_never_falls_back(odyssey, patch, speech_state, llm_state):
    app, client, calls = odyssey
    app.state.personal_agent.governor.update_policy(patch)
    status = client.post('/api/ai/probe').json()
    assert status['speech']['state'] == speech_state
    assert status['llm']['state'] == llm_state
    assert status['legacy_fallback_enabled'] is False
    assert calls == []


def test_missing_api_key_has_own_state_without_request(odyssey):
    app, client, calls = odyssey
    app.state.personal_agent.gateway._explicit_api_key = ''
    status = client.post('/api/ai/probe').json()
    assert status['speech']['state'] == status['llm']['state'] == 'api_key_missing'
    assert calls == []


def test_budget_exhaustion_does_not_become_pc_offline(odyssey, monkeypatch):
    app, client, calls = odyssey
    governor = app.state.personal_agent.governor
    summary = governor.summary()
    monkeypatch.setattr(governor, 'summary', lambda: {**summary, 'state': 'HARD_CAP_REACHED', 'remaining_usd': 0})
    status = client.get('/api/ai/status').json()
    assert status['speech']['state'] == status['llm']['state'] == 'budget_exhausted'
    assert calls == []


def test_missing_ffmpeg_blocks_only_speech_preprocessing(odyssey, tmp_path):
    app, client, calls = odyssey
    app.state.speech.override = replace(app.state.speech.override, ffmpeg=str(tmp_path/'not-installed'))
    status = client.post('/api/ai/probe').json()
    assert status['speech']['state'] == 'speech_unavailable'
    assert status['speech']['ready'] is False
    assert status['llm']['state'] == 'configured'
    assert calls == []


def test_invalid_retired_llm_configuration_does_not_affect_diagnostics(odyssey):
    app, client, calls = odyssey
    app.state.store.set('llm_config', {'model': [], 'base_url': 'not-an-endpoint'})
    status = client.get('/api/ai/status').json()
    assert status['speech']['state'] == status['llm']['state'] == 'configured'
    assert calls == []


def test_non_openai_endpoint_is_reported_blocked_without_probe(odyssey):
    app, client, calls = odyssey
    app.state.personal_agent.gateway.base_url = 'http://127.0.0.1:8090/v1'
    status = client.post('/api/ai/probe').json()
    assert status['speech']['state'] == status['llm']['state'] == 'profile_endpoint_blocked'
    assert calls == []


@pytest.mark.parametrize(('method', 'path'), [
    ('GET', '/api/ai/status'), ('POST', '/api/ai/probe'), ('GET', '/manager/runtime'),
])
def test_pc_free_diagnostics_remain_manager_only(odyssey, method, path):
    app, client, calls = odyssey
    client.headers.pop('Authorization')
    assert client.request(method, path).status_code == 401
    client.headers['Authorization'] = 'Bearer ' + app.state.ingest_token
    assert client.request(method, path).status_code == 401
    assert calls == []
