"""Odyssey topology enforcement using synthetic records and mock providers only.

No real PC, SSH tunnel, API key, model, recording, or paid inference is used.
"""
from contextlib import contextmanager
from dataclasses import asdict
import asyncio
import json
import sys
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from app.ai_profile import AIProfile
from app.llm import LLMConfig, sha
from app.main import create_app
from app.openai_gateway import OpenAIGateway, OpenAIGatewayError
from app.speech import SpeechConfig
from app.store import utcnow
from test_agent_speech_safety import synthetic_wav, pair_client


class ForbiddenPC:
    def __init__(self):
        self.calls = []

    async def _forbidden(self, *args, **kwargs):
        self.calls.append('unexpected PC call')
        raise AssertionError('Odyssey must never contact or launch a legacy model.')

    generate = probe = transcribe = transcribe_detailed = _forbidden


@pytest.fixture
def odyssey(tmp_path, monkeypatch):
    for name in ('OPENAI_API_KEY', 'HUB_OPENAI_API_KEY', 'HUB_LLM_API_KEY', 'HUB_SPEECH_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    for name in ('HUB_PERSONAL_AGENT_ENABLED', 'HUB_OPENAI_ENABLED', 'HUB_OPENAI_AUDIO_ENABLED',
                 'HUB_OPENAI_ALLOW_SHARED_NOTES', 'HUB_RUN_LIVE_OPENAI_TESTS', 'HUB_SECURE_COOKIE'):
        monkeypatch.setenv(name, 'false')
    monkeypatch.setenv('HUB_AI_PROFILE', 'odyssey_openai')
    # Metadata-only readiness fixture. Synthetic WAV avoids invoking this file.
    monkeypatch.setenv('HUB_FFMPEG', sys.executable)
    instances = []

    @contextmanager
    def make(*, flags=None, key='synthetic-offline-key', provider_error=False, before_start=None):
        root = tmp_path / str(len(instances))
        root.mkdir()
        instances.append(root)
        # Retired fields are intentionally invalid on Odyssey. Only valid codec
        # limits/language should matter, and the original bytes must survive.
        old_cfg = asdict(SpeechConfig(enabled=True, backend='remote_http',
            binary='C:\\RoomHubAI\\whisper-server.exe', model='C:\\RoomHubAI\\model.bin',
            remote_url='http://retired-pc.invalid:8178/inference',
            remote_model='old/model/path', remote_device='cuda', threads=6,
            language='ko', max_seconds=25))
        path = root / 'speech-config.json'
        path.write_text(json.dumps(old_cfg), encoding='utf-8')
        before = path.read_bytes()
        legacy = ForbiddenPC()
        app = create_app(root, weather_enabled=False, speech_runner=legacy, llm_backend=legacy)
        app.state.speech.remote_runner = legacy
        app.state.store.set('llm_config', LLMConfig(enabled=True).model_dump())
        calls = []

        async def provider(method, url, headers, payload):
            calls.append((method, url))
            if provider_error:
                raise OpenAIGatewayError('Synthetic provider failure.', 'connection_failed', 503)
            assert url == 'https://api.openai.com/v1/audio/transcriptions'
            return {'text': '오늘 할 일 확인해줘',
                    'usage': {'input_tokens': 16, 'output_tokens': 8, 'total_tokens': 24}}

        agent = app.state.personal_agent
        agent.gateway = OpenAIGateway(agent.governor, api_key=key, transport=provider,
                                      profile=app.state.speech.profile)
        policy = {'agent_enabled': True, 'cloud_enabled': True, 'audio_cloud_enabled': True,
                  # Even a legacy policy permitting fallback must not override
                  # this server's process-level PC-free execution boundary.
                  'fallback_to_local': True}
        policy.update(flags or {})
        agent.governor.set_policy(policy)
        if before_start:
            before_start(app)
        with TestClient(app) as client:
            admin = {'Authorization': 'Bearer ' + app.state.admin_token, 'X-Room-Request': '1'}
            device = pair_client(client, admin)
            yield SimpleNamespace(app=app, client=client, admin=admin, device=device,
                                  legacy=legacy, calls=calls, root=root, before=before, cfg_path=path)

    return make


def upload(h, request_id='odyssey-audio'):
    return h.client.post('/api/speech/jobs', data={'request_id': request_id},
        files={'file': ('synthetic.wav', synthetic_wav(), 'audio/wav')})


def wait_job(h, job_id, *, completed_agent=False):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = h.client.get('/api/speech/jobs/' + job_id)
        assert response.status_code == 200, response.text
        row = response.json()
        if row['status'] in {'failed', 'cancelled'}:
            return row
        if row['status'] == 'succeeded' and (not completed_agent or
                (row.get('agent_run') or {}).get('status') not in {None, 'pending', 'running'}):
            return row
        time.sleep(.015)
    raise AssertionError('Synthetic speech work did not settle.')


def seed_speech(app, *, backend='configured', status='queued', key='retired-job'):
    now = utcnow()
    with app.state.store.connect() as db:
        db.execute('''INSERT INTO voice(id,request_id,source,kind,text,locale,status,metadata,
            filename,media_type,digest,created_at) VALUES(?,?,?,'audio','','ko-KR',?,?,?,'audio/wav','synthetic',?)''',
            (key + '-voice', key + '-voice', 'synthetic-test', 'transcription_queued',
             json.dumps({'agent_delivery': 'pending'}), key + '.bin', now))
        db.execute('''INSERT INTO speech_jobs(id,voice_id,owner,request_id,status,digest,created_at,updated_at,stt_backend)
            VALUES(?,?,'admin',?,?,'synthetic',?,?,?)''',
            (key, key + '-voice', key, status, now, now, backend))
    return key


def seed_llm(app, *, status='queued', key='retired-llm'):
    now = utcnow()
    cfg = LLMConfig(enabled=True).model_dump()
    with app.state.store.connect() as db:
        db.execute('''INSERT INTO llm_requests(id,request_key,signature,source_voice_id,source_text,
            source_sha256,source_meta,config_json,request_body,request_sha256,endpoint,status,
            created_at,updated_at,queued_at) VALUES(?,?,?,'','synthetic',?,'{}',?,'{}',?,?,?, ?,?,?)''',
            (key, key, 'synthetic', sha('synthetic'), json.dumps(cfg), sha('{}'),
             cfg['base_url'] + '/chat/completions', status, now, now, now))


def test_pc_settings_preserved_but_not_required_for_cloud(odyssey):
    with odyssey() as h:
        state = h.client.get('/api/speech/status').json()
        assert state['ready'] and state['backend'] == 'openai'
        assert state['runtime_profile'] == 'odyssey_openai'
        assert state['legacy_inference_allowed'] is False
        assert state['max_seconds'] == 25 and state['language'] == 'ko' and state['threads'] is None
        assert h.cfg_path.read_bytes() == h.before
        assert h.app.state.store.get('llm_config')['enabled'] is True
        assert h.app.state.llm.worker is None
        assert h.legacy.calls == [] and h.calls == []


@pytest.mark.parametrize('flags,key', [
    ({'agent_enabled': False}, 'synthetic-offline-key'),
    ({'cloud_enabled': False}, 'synthetic-offline-key'),
    ({'audio_cloud_enabled': False}, 'synthetic-offline-key'),
    ({}, ''),
])
def test_cloud_unavailable_never_selects_retired_pc(odyssey, flags, key):
    with odyssey(flags=flags, key=key) as h:
        state = h.client.get('/api/speech/status').json()
        assert state['backend'] == 'openai' and not state['ready']
        assert state['pc_required'] is False and state['reason']
        assert upload(h).status_code == 503
        assert h.client.get('/healthz').status_code == 200
        assert h.client.get('/api/state').status_code == 200
        assert not h.legacy.calls and not h.calls


def test_cloud_voice_works_without_pc_and_duplicate_does_not_replay(odyssey):
    with odyssey() as h:
        submitted = upload(h)
        assert submitted.status_code == 202, submitted.text
        job = wait_job(h, submitted.json()['id'], completed_agent=True)
        assert job['status'] == 'succeeded' and job['stt_backend'] == 'openai'
        assert job['agent_run']['status'] == 'succeeded'
        assert job['agent_run']['device_id'] == h.device
        again = upload(h)
        assert again.status_code == 202 and again.json()['duplicate']
        assert again.json()['id'] == job['id']
        assert len(h.calls) == 1 and not h.legacy.calls
        assert h.app.state.llm.worker is None
        assert h.cfg_path.read_bytes() == h.before


def test_provider_failure_does_not_use_legacy_fallback(odyssey):
    with odyssey(provider_error=True) as h:
        submitted = upload(h)
        assert submitted.status_code == 202
        job = wait_job(h, submitted.json()['id'])
        assert job['status'] == 'failed'
        assert h.app.state.personal_agent.governor.policy().fallback_to_local is True
        assert len(h.calls) == 1 and not h.legacy.calls
        for _ in range(3):
            assert h.client.get('/api/speech/jobs/' + job['id']).json()['status'] == 'failed'
        assert len(h.calls) == 1


def test_agent_disabled_during_paired_stt_keeps_transcript_and_reports_failed_handoff(odyssey):
    with odyssey() as h:
        agent = h.app.state.personal_agent

        async def provider(method, url, headers, payload):
            h.calls.append((method, url))
            agent.governor.set_policy({'agent_enabled': False})
            return {'text': '4분 타이머 시작', 'usage': {'input_tokens': 50, 'output_tokens': 10}}

        agent.gateway.transport = provider
        submitted = upload(h, 'synthetic-disabled-handoff')
        assert submitted.status_code == 202
        job = wait_job(h, submitted.json()['id'], completed_agent=True)
        assert job['status'] == 'succeeded' and job['text'] == '4분 타이머 시작'
        assert job['agent_run']['status'] == 'failed'
        assert agent.timers.snapshot()['active_count'] == 0
        assert agent.list_runs()['items'] == []
        assert len(h.calls) == 1 and not h.legacy.calls
        # The service handoff also guards direct callers, not only main's sink.
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as error:
            asyncio.run(agent.submit_from_speech_job(job['id'], job['voice_id'], job['text']))
        assert error.value.status_code == 503


def test_legacy_probe_and_ingress_are_retired_even_with_enabled_config(odyssey):
    with odyssey() as h:
        speech_probe = asyncio.run(h.app.state.speech.probe())
        assert speech_probe['backend'] == 'openai' and speech_probe['network_checked'] is False
        for path in ('/api/llm/probe', '/api/ai/probe'):
            response = h.client.post(path, headers=h.admin)
            assert response.status_code == 200, (path, response.text)
        assert h.client.put('/api/llm/config', json=LLMConfig(enabled=True).model_dump(), headers=h.admin).status_code == 409
        assert h.client.post('/api/llm/requests', headers=h.admin,
            json={'voice_id': 'not-needed', 'request_id': 'retired-submit', 'expected_text_sha256': sha('synthetic')}).status_code == 409
        assert h.client.post('/api/llm/requests/not-needed/send', headers=h.admin).status_code == 409
        assert h.client.post('/api/llm/requests/not-needed/retry', headers=h.admin,
            json={'request_id': 'retired-retry'}).status_code == 409
        assert h.client.get('/api/llm/requests', headers=h.admin).status_code == 200
        assert h.app.state.llm.worker is None
        assert not h.legacy.calls and not h.calls


@pytest.mark.parametrize('state', ['queued', 'running'])
def test_retired_queued_audio_is_interrupted_without_cloud_consent(odyssey, state):
    def seed(app):
        seed_speech(app, status=state)
        seed_llm(app, status=state)
    with odyssey(flags={'agent_enabled': False}, before_start=seed) as h:
        response = h.client.get('/api/speech/jobs/retired-job', headers=h.admin)
        assert response.status_code == 200
        job = response.json()
        assert job['status'] == 'failed' and job['stt_backend'] == 'configured'
        assert 'OpenAI' in job['error']
        assert h.client.post('/api/speech/jobs/retired-job/retry', headers=h.admin).status_code == 409
        row = h.client.get('/api/llm/requests/retired-llm', headers=h.admin).json()
        assert row['status'] == 'interrupted' and row['error_code'] == 'legacy_backend_disabled'
        assert row['dispatch_attempted'] is False
        with h.app.state.store.connect() as db:
            raw = db.execute("SELECT metadata FROM voice WHERE id='retired-job-voice'").fetchone()[0]
        assert json.loads(raw)['agent_delivery'] == 'interrupted'
        assert h.app.state.speech.task is None and h.app.state.llm.worker is None
        assert not h.calls and not h.legacy.calls


def test_cloud_only_codec_read_does_not_weaken_legacy_validation(tmp_path):
    path = tmp_path / 'speech-config.json'
    path.write_text(json.dumps({'enabled': True, 'backend': 'remote_http',
        'remote_url': 'http://retired-pc.invalid:8178/inference',
        'model': 'C:\\RoomHubAI\\old.bin', 'remote_model': 'old/model',
        'ffmpeg': sys.executable, 'language': 'ja', 'max_seconds': 20}), encoding='utf-8')
    before = path.read_bytes()
    cfg = SpeechConfig.read(path, codec_only=True)
    assert cfg.ffmpeg == sys.executable and cfg.language == 'ja' and cfg.max_seconds == 20
    assert cfg.backend == 'local_cli' and not cfg.enabled and not cfg.remote_url
    with pytest.raises(ValueError):
        SpeechConfig.read(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize('change', [{'max_seconds': -1}, {'max_bytes': True}, {'language': 'unsupported'}, {'ffmpeg': 'relative/path'}])
def test_cloud_codec_limits_still_fail_closed(tmp_path, change):
    path = tmp_path / 'speech-config.json'
    path.write_text(json.dumps(change), encoding='utf-8')
    with pytest.raises(ValueError):
        SpeechConfig.read(path, codec_only=True)


def test_profile_selection_is_explicit_and_validated(monkeypatch):
    monkeypatch.delenv('HUB_AI_PROFILE', raising=False)
    assert AIProfile.from_env().name == 'legacy'
    monkeypatch.setenv('HUB_AI_PROFILE', 'odyssey_openai')
    assert AIProfile.from_env().pc_free
    monkeypatch.setenv('HUB_AI_PROFILE', 'odyssey_typo')
    with pytest.raises(ValueError):
        AIProfile.from_env()
