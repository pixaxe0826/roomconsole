"""Real SpeechHub/API orchestration with synthetic audio and no network calls.

These tests cover the display -> speech worker -> personal agent trust boundary.
Fake providers validate routing and idempotency, not speech recognition quality.
"""
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import io
import json
import math
import struct
import sys
import threading
import time
import wave

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.life import NoteCreate
from app.main import create_app
from app.openai_gateway import OpenAIGateway
from app.speech import SpeechConfig


def synthetic_wav(seconds=0.5):
    data = io.BytesIO()
    with wave.open(data, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b''.join(
            struct.pack('<h', int(4000 * math.sin(i * 0.1)))
            for i in range(int(seconds * 16000))
        ))
    return data.getvalue()


class SyntheticRunner:
    def __init__(self):
        self.text = '오늘 할 일 확인해줘'
        self.hold = threading.Event()
        self.calls = []

    async def transcribe(self, source, cfg, cancel, temp_root):
        self.calls.append(source)
        while self.hold.is_set():
            if cancel.is_set():
                raise asyncio.CancelledError
            await asyncio.sleep(0.01)
        return self.text, 0.5


class ForbiddenLegacyModel:
    async def generate(self, *args, **kwargs):
        raise AssertionError('This integration test must never call a legacy model.')


@dataclass
class Harness:
    app: object
    client: TestClient
    runner: SyntheticRunner
    admin: dict
    device_id: str
    provider_calls: list

    @property
    def agent(self):
        return self.app.state.personal_agent


def pair_client(client, admin, name='synthetic-display'):
    client.headers['X-Room-Request'] = '1'
    invitation = client.post('/api/devices/pair', json={'name': name}, headers=admin)
    assert invitation.status_code == 200, invitation.text
    claimed = client.post('/api/devices/claim', json={'code': invitation.json()['path'].split('=')[1]})
    assert claimed.status_code == 200, claimed.text
    return claimed.json()['device_id']


@pytest.fixture
def speech_app(tmp_path, monkeypatch):
    # Never inherit credentials, opt-ins, model endpoints, or production paths.
    for key in ('OPENAI_API_KEY', 'HUB_OPENAI_API_KEY', 'HUB_LLM_API_KEY'):
        monkeypatch.delenv(key, raising=False)
    for key in ('HUB_PERSONAL_AGENT_ENABLED', 'HUB_OPENAI_ENABLED',
                'HUB_OPENAI_AUDIO_ENABLED', 'HUB_OPENAI_ALLOW_SHARED_NOTES',
                'HUB_RUN_LIVE_OPENAI_TESTS', 'HUB_LLM_ENABLED', 'HUB_SECURE_COOKIE'):
        monkeypatch.setenv(key, 'false')
    instances = []

    @contextmanager
    def make(*, agent_enabled=True, cloud_enabled=False, audio_cloud_enabled=False,
             local_enabled=True, transport=None):
        root = tmp_path / f'instance-{len(instances)}'
        root.mkdir()
        instances.append(root)
        model = root / 'synthetic-model.bin'
        model.write_bytes(b'synthetic-model-not-loadable' * 80)
        cfg = SpeechConfig(enabled=local_enabled, binary=sys.executable,
                           model=str(model), ffmpeg=sys.executable)
        runner = SyntheticRunner()
        app = create_app(root / 'data', weather_enabled=False, speech_config=cfg,
                         speech_runner=runner, llm_backend=ForbiddenLegacyModel())
        agent = app.state.personal_agent
        calls = []

        async def fake_provider(method, url, headers, payload):
            calls.append({'method': method, 'url': url})
            if transport is None:
                raise AssertionError('Cloud traffic is forbidden in this synthetic scenario.')
            return await transport(method, url, headers, payload)

        agent.gateway = OpenAIGateway(agent.governor, api_key='sk-synthetic-no-network',
                                      transport=fake_provider)
        agent.governor.set_policy({
            'agent_enabled': agent_enabled, 'cloud_enabled': cloud_enabled,
            'audio_cloud_enabled': audio_cloud_enabled, 'allow_shared_notes_in_cloud': False,
            'fallback_to_local': False,
        })
        with TestClient(app) as client:
            admin = {'Authorization': 'Bearer ' + app.state.admin_token, 'X-Room-Request': '1'}
            did = pair_client(client, admin)
            try:
                yield Harness(app, client, runner, admin, did, calls)
            finally:
                runner.hold.clear()

    return make


def upload(h, request_id, audio=None):
    return h.client.post('/api/speech/jobs', data={'request_id': request_id},
                         files={'file': ('voice.wav', audio or synthetic_wav(), 'audio/wav')})


def poll_job(h, job_id, predicate, *, admin=False):
    deadline = time.monotonic() + 5
    row = None
    while time.monotonic() < deadline:
        response = h.client.get('/api/speech/jobs/' + job_id, headers=h.admin if admin else None)
        assert response.status_code == 200, response.text
        row = response.json()
        if predicate(row):
            return row
        time.sleep(0.015)
    raise AssertionError('Speech/agent job did not settle: ' + repr(row))


def settled_agent(row):
    agent = row.get('agent_run') or {}
    return row['status'] == 'succeeded' and agent.get('status') not in {None, 'running', 'pending'}


def test_display_speech_keeps_device_authority_and_cannot_read_private_memo(speech_app):
    with speech_app() as h:
        h.app.state.life.create_note(NoteCreate(request_id='speech-private-note',
            title='개인비밀금고', body='synthetic-secret-never-display', shared=False))
        h.runner.text = '개인비밀금고 메모 읽어줘'
        submitted = upload(h, 'display-private-read')
        assert submitted.status_code == 202, submitted.text
        job = poll_job(h, submitted.json()['id'], settled_agent)
        projected = job['agent_run']
        assert projected['status'] == 'needs_clarification'
        assert projected['redacted'] is True
        assert 'synthetic-secret-never-display' not in json.dumps(job, ensure_ascii=False)
        assert '개인비밀금고' not in json.dumps(projected, ensure_ascii=False)
        stored = h.agent.get_run(projected['id'])
        assert stored['principal'] == 'display:' + h.device_id
        assert stored['device_id'] == h.device_id
        with h.app.state.store.connect() as db:
            owner = db.execute('SELECT owner FROM speech_jobs WHERE id=?', (job['id'],)).fetchone()[0]
        assert owner == 'device:' + h.device_id
        assert h.provider_calls == []
        assert len(h.runner.calls) == 1


def test_owner_can_poll_safe_link_but_other_display_cannot_manage_or_read_it(speech_app):
    with speech_app() as h:
        submitted = upload(h, 'display-owned-run')
        assert submitted.status_code == 202, submitted.text
        job = poll_job(h, submitted.json()['id'], settled_agent)
        run_id = job['agent_run']['id']
        endpoint = '/api/agent/display/runs/' + run_id
        own = h.client.get(endpoint)
        assert own.status_code == 200, own.text
        assert own.json()['id'] == run_id
        assert own.json() == job['agent_run']
        forbidden = {'preview', 'preview_sha256', 'plan', 'receipt', 'principal', 'request_sha256'}
        assert not (forbidden & own.json().keys())
        assert h.client.get('/api/agent/runs/' + run_id).status_code == 401
        assert h.client.post('/api/agent/runs/' + run_id + '/confirm',
                             json={'preview_sha256': '0' * 64}).status_code == 401
        assert h.client.post('/api/agent/runs/' + run_id + '/cancel').status_code == 401
        other = TestClient(h.app)
        try:
            pair_client(other, h.admin, 'other-display')
            assert other.get(endpoint).status_code == 403
            assert other.get('/api/speech/jobs/' + job['id']).status_code == 404
        finally:
            other.close()
        for _ in range(3):
            assert h.client.get(endpoint).status_code == 200
            assert h.client.get('/api/speech/jobs/' + job['id']).status_code == 200
            assert h.client.get('/api/speech/status').status_code == 200
        duplicate = upload(h, 'display-owned-run')
        assert duplicate.status_code == 202
        assert duplicate.json()['duplicate'] is True
        assert duplicate.json()['agent_run']['id'] == run_id
        assert h.agent.list_runs()['total'] == 1
        assert len(h.runner.calls) == 1
        assert h.provider_calls == []


def test_device_revoked_during_local_stt_cannot_execute_agent(speech_app):
    with speech_app() as h:
        h.runner.text = '오늘 할 일에 음성 요청으로 추가 추가해'
        h.runner.hold.set()
        submitted = upload(h, 'revoke-during-speech')
        assert submitted.status_code == 202, submitted.text
        job_id = submitted.json()['id']
        poll_job(h, job_id, lambda row: row['status'] == 'running')
        assert h.client.delete('/api/devices/' + h.device_id, headers=h.admin).status_code == 200
        h.runner.hold.clear()
        job = poll_job(h, job_id, lambda row: (row.get('agent_run') or {}).get('status') == 'failed', admin=True)
        assert job['status'] == 'succeeded'  # Original successful STT is not rewritten.
        assert h.client.get('/api/speech/jobs/' + job_id).status_code == 401
        assert h.agent.list_runs()['total'] == 0
        assert h.app.state.store.tasks() == []
        assert h.provider_calls == []


@pytest.mark.parametrize('bad_owner,expected_status', [
    ('', 403), ('display:untrusted', 403), ('device:', 403),
    ('device:unknown-device', 401), ('unknown-role', 403),
])
def test_speech_identity_never_defaults_missing_or_unknown_owner_to_admin(speech_app, bad_owner, expected_status):
    with speech_app() as h:
        job = poll_job(h, upload(h, 'identity-owner').json()['id'], settled_agent)
        with h.app.state.store.connect() as db:
            db.execute('UPDATE speech_jobs SET owner=? WHERE id=?', (bad_owner, job['id']))
        with pytest.raises(HTTPException) as error:
            h.client.portal.call(h.agent.submit_from_speech_job, job['id'], job['voice_id'], job['text'])
        assert error.value.status_code == expected_status
        assert h.agent.list_runs()['total'] == 1
        assert h.provider_calls == []


def test_speech_identity_rejects_missing_job_mismatched_voice_and_changed_text(speech_app):
    with speech_app() as h:
        job = poll_job(h, upload(h, 'identity-binding').json()['id'], settled_agent)
        for job_id, voice_id, text in (
            ('missing-job', job['voice_id'], job['text']),
            (job['id'], 'different-voice', job['text']),
            (job['id'], job['voice_id'], '4분 타이머 시작'),
        ):
            with pytest.raises(HTTPException) as error:
                h.client.portal.call(h.agent.submit_from_speech_job, job_id, voice_id, text)
            assert error.value.status_code == 409
        assert h.agent.list_runs()['total'] == 1
        assert h.provider_calls == []


def test_agent_disabled_keeps_legacy_transcript_fast_path(speech_app):
    with speech_app(agent_enabled=False) as h:
        submitted = upload(h, 'legacy-agent-disabled')
        assert submitted.status_code == 202, submitted.text
        job = poll_job(h, submitted.json()['id'], lambda row: row['status'] == 'succeeded')
        deadline = time.monotonic() + 5
        legacy = None
        while time.monotonic() < deadline:
            with h.app.state.store.connect() as db:
                legacy = db.execute('SELECT id,status FROM llm_requests WHERE source_voice_id=?',
                                    (job['voice_id'],)).fetchone()
            if legacy and legacy['status'] not in {'queued', 'running'}:
                break
            time.sleep(0.015)
        assert legacy is not None and legacy['status'] == 'succeeded'
        request = h.app.state.llm.get(legacy['id'])
        assert request['assistant']['routing']['route'] == 'FAST_PATH'
        assert request['assistant']['routing']['llm_called'] is False
        assert job['agent_run'] is None
        assert h.agent.list_runs()['total'] == 0
        assert len(h.runner.calls) == 1
        assert h.provider_calls == []


def test_cloud_stt_for_paired_display_is_durable_and_duplicate_upload_calls_once(speech_app):
    entered, release = threading.Event(), threading.Event()

    async def cloud_stt(method, url, headers, payload):
        assert method == 'POST' and url.endswith('/audio/transcriptions')
        assert payload[:4] == b'RIFF'
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return {'text': '오늘 할 일 확인해줘',
                'usage': {'input_tokens': 16, 'output_tokens': 8, 'total_tokens': 24}}

    with speech_app(cloud_enabled=True, audio_cloud_enabled=True,
                    local_enabled=False, transport=cloud_stt) as h:
        state = h.client.get('/api/speech/status').json()
        assert state['ready'] and state['backend'] == 'openai'
        first = upload(h, 'paired-cloud-once')
        assert first.status_code == 202, first.text
        try:
            assert entered.wait(3), 'Synthetic STT transport was not reached.'
            duplicate = upload(h, 'paired-cloud-once')
            assert duplicate.status_code == 202, duplicate.text
            assert duplicate.json()['id'] == first.json()['id']
            assert duplicate.json()['duplicate'] is True
            assert upload(h, 'paired-cloud-once', synthetic_wav(0.7)).status_code == 409
        finally:
            release.set()
        job = poll_job(h, first.json()['id'], settled_agent)
        assert job['status'] == 'succeeded'
        assert job['agent_run']['status'] == 'succeeded'
        assert job['stt_backend'] == 'openai'
        assert job['agent_run']['device_id'] == h.device_id
        assert h.agent.get_run(job['agent_run']['id'])['principal'] == 'display:' + h.device_id
        for _ in range(3):
            assert h.client.get('/api/speech/jobs/' + job['id']).status_code == 200
            assert h.client.get('/api/agent/display/runs/' + job['agent_run']['id']).status_code == 200
        assert len(h.provider_calls) == 1
        assert h.runner.calls == []
        assert h.agent.list_runs()['total'] == 1
        with h.app.state.store.connect() as db:
            audio = db.execute('SELECT principal,status FROM hub_agent_audio').fetchall()
            usage = db.execute("SELECT request_id FROM hub_ai_usage_ledger WHERE category='stt'").fetchall()
        assert [dict(row) for row in audio] == [{'principal': 'display:' + h.device_id, 'status': 'succeeded'}]
        assert len(usage) == 1


def test_admin_explicit_enqueue_uses_current_cloud_policy_without_local_engine(speech_app):
    async def cloud_stt(method, url, headers, payload):
        assert method == 'POST' and url.endswith('/audio/transcriptions')
        return {'text': '오늘 할 일 확인해줘',
                'usage': {'input_tokens': 16, 'output_tokens': 8, 'total_tokens': 24}}

    with speech_app(cloud_enabled=True, audio_cloud_enabled=True,
                    local_enabled=False, transport=cloud_stt) as h:
        uploaded = h.client.post('/api/voice/upload', headers=h.admin,
                                 data={'request_id': 'synthetic-admin-import'},
                                 files={'file': ('voice.wav', synthetic_wav(), 'audio/wav')})
        assert uploaded.status_code == 202
        vid = uploaded.json()['id']
        assert h.client.post('/api/speech/enqueue/' + vid).status_code == 401
        queued = h.client.post('/api/speech/enqueue/' + vid, headers=h.admin)
        assert queued.status_code == 202, queued.text
        job_id = queued.json()['id']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            row = h.client.get('/api/speech/jobs/' + job_id, headers=h.admin).json()
            if settled_agent(row):
                break
            time.sleep(0.01)
        assert row['status'] == 'succeeded' and row['stt_backend'] == 'openai'
        assert row['agent_run']['status'] == 'succeeded'
        assert row['agent_run']['principal'].startswith('admin:')
        assert len(h.provider_calls) == 1 and h.runner.calls == []


@pytest.mark.parametrize('flags', [
    {'agent_enabled': False, 'cloud_enabled': True, 'audio_cloud_enabled': True},
    {'agent_enabled': True, 'cloud_enabled': False, 'audio_cloud_enabled': True},
    {'agent_enabled': True, 'cloud_enabled': True, 'audio_cloud_enabled': False},
])
def test_cloud_audio_requires_each_explicit_policy_flag(speech_app, flags):
    with speech_app(local_enabled=False, **flags) as h:
        status = h.client.get('/api/speech/status').json()
        assert status['ready'] is False
        assert status['backend'] != 'openai'
        assert upload(h, 'requires-explicit-cloud-consent').status_code == 503
        assert h.provider_calls == []
        assert h.runner.calls == []


def test_speech_poll_exposes_safe_running_agent_id_before_planner_finishes(speech_app):
    entered, release = threading.Event(), threading.Event()

    async def planner(method, url, headers, payload):
        assert url.endswith('/chat/completions')
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        plan = {'intent_type': 'clarification', 'assistant_message': '합성 추가 질문',
                'follow_up_question': '어떤 작업인가요?', 'steps': []}
        return {'model': 'gpt-6.1-sol',
                'choices': [{'message': {'content': json.dumps(plan, ensure_ascii=False)}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 120, 'completion_tokens': 30, 'total_tokens': 150}}

    with speech_app(cloud_enabled=True, audio_cloud_enabled=False, transport=planner) as h:
        h.runner.text = '내가 좀 더 여유 있게 하루를 보낼 수 있게 도와줘'
        submitted = upload(h, 'running-agent-link')
        assert submitted.status_code == 202, submitted.text
        try:
            assert entered.wait(3), 'Synthetic planner transport was not reached.'
            row = poll_job(h, submitted.json()['id'], lambda item: (item.get('agent_run') or {}).get('status') == 'running')
            run = row['agent_run']
            assert row['status'] == 'succeeded'
            assert run['id'] and run['redacted'] is True
            assert h.runner.text not in run['source_text']
            assert 'preview' not in run and 'plan' not in run
            for _ in range(3):
                assert h.client.get('/api/speech/jobs/' + row['id']).json()['agent_run']['id'] == run['id']
            assert len(h.provider_calls) == 1
        finally:
            release.set()
        done = poll_job(h, submitted.json()['id'], settled_agent)
        assert done['agent_run']['id'] == run['id']
        assert done['agent_run']['status'] == 'needs_clarification'
        assert len(h.runner.calls) == 1
        assert len(h.provider_calls) == 1
