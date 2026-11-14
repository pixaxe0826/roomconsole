"""PC-free agent/gateway contract; synthetic audio and in-process providers only."""
import asyncio
from dataclasses import FrozenInstanceError
import json
import sys

import pytest
from fastapi import HTTPException

from app.ai_profile import AIProfile
from app.openai_gateway import OPENAI_DEFAULT_BASE_URL, OpenAIGateway, OpenAIGatewayError
from app.personal_agent import AgentCommandInput, PersonalAgentService
from app.speech import SpeechConfig
from test_personal_agent import build_test_harness, make_step, make_wav_bytes


@pytest.fixture(autouse=True)
def isolated_profile(monkeypatch):
    for key in ('OPENAI_API_KEY', 'HUB_OPENAI_API_KEY', 'HUB_LLM_API_KEY', 'HUB_FFMPEG'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('HUB_AI_PROFILE', 'odyssey_openai')
    for key in ('HUB_PERSONAL_AGENT_ENABLED', 'HUB_OPENAI_ENABLED',
                'HUB_OPENAI_AUDIO_ENABLED', 'HUB_RUN_LIVE_OPENAI_TESTS'):
        monkeypatch.setenv(key, 'false')


class ForbiddenPC:
    def __init__(self):
        self.calls = 0

    def config(self):
        return SpeechConfig(enabled=True, backend='remote_http', remote_model='retired-model',
                            remote_url='http://127.0.0.1:8178/inference', ffmpeg=sys.executable)

    async def transcribe_bytes(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError('Odyssey must never request transcription from the PC.')


def harness(path, transport=None, *, api_key='synthetic-offline-key'):
    pc = ForbiddenPC()
    _, _, _, _, agent, _ = build_test_harness(path, transport=transport, api_key=api_key, speech=pc)
    return agent, pc


def audio(agent, request_id='synthetic-odyssey-audio', **kwargs):
    return agent.transcribe_audio(request_id=request_id, audio_bytes=make_wav_bytes(), **kwargs)


def run(coroutine):
    return asyncio.run(asyncio.wait_for(coroutine, 5))


def test_profile_is_explicit_frozen_and_not_changed_by_environment(monkeypatch):
    profile = AIProfile.from_env()
    assert profile.pc_free
    monkeypatch.setenv('HUB_AI_PROFILE', 'legacy')
    assert profile.pc_free
    assert not AIProfile.from_env().pc_free
    with pytest.raises(FrozenInstanceError):
        profile.name = 'legacy'
    monkeypatch.setenv('HUB_AI_PROFILE', 'typo-do-not-use-legacy')
    with pytest.raises(ValueError, match='HUB_AI_PROFILE'):
        AIProfile.from_env()
    monkeypatch.delenv('HUB_AI_PROFILE')
    assert AIProfile.from_env().name == 'legacy'


def test_profile_does_not_grant_cloud_consent(tmp_path):
    from app.openai_budget import BudgetGovernor
    from app.store import Store
    gov = BudgetGovernor(Store(tmp_path / 'synthetic.sqlite3'))
    gateway = OpenAIGateway(gov, api_key='synthetic-offline-key')
    assert gateway.profile.pc_free
    assert not gov.policy().agent_enabled
    assert not gov.policy().cloud_enabled
    assert not gov.policy().audio_cloud_enabled
    assert gateway.readiness()['status_reason'] == 'agent_disabled'


def test_ready_status_does_not_use_nonexistent_budget_allowed_field(tmp_path):
    agent, pc = harness(tmp_path)
    state = agent.status()
    assert state['cloud_status'] == 'ready'
    assert state['openai']['cloud_ready'] is True
    assert state['openai']['provider_verified'] is False
    assert state['runtime_profile'] == 'odyssey_openai'
    assert state['pc_required'] is False
    assert state['local_stt_available'] is False
    assert state['stt_fallback_to_local'] is False
    assert agent.governor.policy().fallback_to_local is True  # saved preference retained, not effective
    assert agent.governor.summary()['call_count'] == 0
    assert pc.calls == 0


@pytest.mark.parametrize('blocked, reason', [
    ('agent_enabled', 'agent_disabled'), ('cloud_enabled', 'cloud_disabled'),
    ('audio_cloud_enabled', 'audio_cloud_disabled'), ('key', 'missing_api_key'),
    ('budget', 'budget_hard_cap_reached'), ('local_only', 'local_only_requested'),
])
def test_audio_gate_failures_never_fall_back_to_pc(tmp_path, blocked, reason):
    calls = []

    async def forbidden_provider(*args):
        calls.append(args)
        raise AssertionError('A blocked call cannot reach OpenAI.')

    agent, pc = harness(tmp_path, forbidden_provider, api_key='' if blocked == 'key' else 'synthetic-offline-key')
    if blocked in {'agent_enabled', 'cloud_enabled', 'audio_cloud_enabled'}:
        agent.governor.set_policy({blocked: False})
    elif blocked == 'budget':
        agent.governor.set_policy({'monthly_cap_usd': 1.0})
        # Deliberate synthetic ledger entry, not real tokens or charges.
        agent.governor.record_usage('synthetic-cap', 'agent_fast', 'gpt-6.1-sol', prompt_tokens=1_000_000)
    with pytest.raises(HTTPException) as error:
        run(audio(agent, local_only=blocked == 'local_only'))
    assert error.value.status_code == 503
    assert reason in error.value.detail
    assert pc.calls == 0 and calls == []
    assert agent.status()['local_stt_available'] is False


def test_direct_local_transcriber_is_blocked_as_defense_in_depth(tmp_path):
    agent, pc = harness(tmp_path)
    with pytest.raises(HTTPException, match='Odyssey'):
        run(agent._transcribe_via_local_speech(
            request_id='synthetic-forbidden', audio_bytes=make_wav_bytes(), filename='voice.wav',
            mime_type='audio/wav', verified_duration=1.0, language='ko'))
    assert pc.calls == 0


@pytest.mark.parametrize('failure', ['provider_error', 'timeout'])
def test_failed_openai_audio_never_uses_pc_even_with_saved_fallback_true(tmp_path, failure):
    calls = []

    async def provider(*args):
        calls.append(args[1])
        if failure == 'timeout':
            raise asyncio.TimeoutError()
        raise OpenAIGatewayError('Synthetic provider unavailable.', 'http_error', 503)

    agent, pc = harness(tmp_path, provider)
    with pytest.raises(HTTPException) as error:
        run(audio(agent))
    assert error.value.status_code == 503
    assert calls == [OPENAI_DEFAULT_BASE_URL + '/audio/transcriptions']
    assert pc.calls == 0
    assert agent.governor.summary()['unknown_usd'] > 0
    with pytest.raises(HTTPException):
        run(audio(agent))
    assert len(calls) == 1  # replay does not bill or fall back again


def test_cancelled_audio_retains_unknown_reservation_and_never_calls_pc(tmp_path):
    async def scenario():
        entered = asyncio.Event()

        async def provider(*_args):
            entered.set()
            await asyncio.Event().wait()

        agent, pc = harness(tmp_path, provider)
        task = asyncio.create_task(audio(agent))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert pc.calls == 0
        assert agent.governor.summary()['unknown_usd'] > 0

    run(scenario())


def test_loopback_gateway_is_rejected_before_any_audio_or_planner_dispatch(tmp_path):
    calls = []

    async def provider(*args):
        calls.append(args)
        raise AssertionError('A retired loopback endpoint must not be contacted.')

    agent, pc = harness(tmp_path, provider)
    gateway = OpenAIGateway(agent.governor, api_key='synthetic-offline-key',
                            base_url='http://127.0.0.1:8090/v1', transport=provider)
    agent.gateway = gateway
    assert agent.status()['cloud_status'] == 'profile_endpoint_blocked'
    assert gateway.readiness()['cloud_ready'] is False
    with pytest.raises(OpenAIGatewayError) as error:
        run(gateway.transcribe_audio(request_id='synthetic-blocked-audio', audio_bytes=make_wav_bytes()))
    assert error.value.code == 'profile_endpoint_blocked'
    with pytest.raises(OpenAIGatewayError) as error:
        run(gateway.generate_structured(request_id='synthetic-blocked-text', category='agent_fast',
            system_prompt='Synthetic test.', user_text='Synthetic text.', schema_name='test', schema={}))
    assert error.value.code == 'profile_endpoint_blocked'
    assert calls == [] and pc.calls == 0
    assert agent.governor.list_reservations() == []


def test_gateway_rechecks_endpoint_at_dispatch_not_only_constructor(tmp_path):
    agent, _ = harness(tmp_path)
    agent.gateway.base_url = 'http://127.0.0.1:8090/v1'
    with pytest.raises(OpenAIGatewayError) as error:
        run(agent.gateway._post_json('/chat/completions', {}))
    assert error.value.code == 'profile_endpoint_blocked'


def test_odyssey_service_restricts_an_injected_legacy_gateway(tmp_path):
    original, pc = harness(tmp_path)
    gateway = OpenAIGateway(original.governor, api_key='synthetic-offline-key',
                            base_url='http://127.0.0.1:8090/v1', profile=AIProfile('legacy'))
    assert gateway.endpoint_allowed()
    agent = PersonalAgentService(original.store, original.life, original.timers, None,
                                 original.task_services, speech=pc, gateway=gateway,
                                 governor=original.governor, profile=AIProfile('odyssey_openai'))
    assert agent.gateway is gateway
    assert not gateway.endpoint_allowed()
    assert gateway.profile is agent.profile
    assert agent.status()['cloud_status'] == 'profile_endpoint_blocked'
    with pytest.raises(OpenAIGatewayError) as error:
        run(gateway._post_json('/chat/completions', {}))
    assert error.value.code == 'profile_endpoint_blocked'
    assert pc.calls == 0 and agent.governor.summary()['call_count'] == 0


def test_cloud_audio_then_rule_execution_needs_no_pc_and_replays_once(tmp_path):
    calls = []

    async def provider(method, url, headers, payload):
        calls.append(url)
        assert url == OPENAI_DEFAULT_BASE_URL + '/audio/transcriptions'
        return {'text': '오늘 할 일 확인해줘', 'usage': {'input_tokens': 50, 'output_tokens': 10}}

    agent, pc = harness(tmp_path, provider)
    first = run(agent.transcribe_and_submit(request_id='synthetic-voice-flow', audio_bytes=make_wav_bytes()))
    second = run(agent.transcribe_and_submit(request_id='synthetic-voice-flow', audio_bytes=make_wav_bytes()))
    assert first['agent_run']['status'] == 'succeeded'
    assert first['agent_run']['tier_used'] == 'tier0_local_rule'
    assert first['agent_run']['id'] == second['agent_run']['id']
    assert second['transcription']['duplicate']
    assert len(calls) == 1 and pc.calls == 0


def test_cloud_planner_runs_on_official_endpoint_with_no_pc(tmp_path):
    calls = []

    async def provider(method, url, headers, payload):
        calls.append(url)
        plan = {'intent_type': 'read', 'assistant_message': '합성 할 일 조회입니다.',
                'follow_up_question': None, 'steps': [make_step(domain='todo', action='list')]}
        return {'choices': [{'message': {'content': json.dumps(plan)}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 30}}

    agent, pc = harness(tmp_path, provider)
    result = run(agent.submit_command(AgentCommandInput(
        request_id='synthetic-planner', text='오늘 할 일 확인해줘', mode='fast')))
    assert result['status'] == 'succeeded'
    assert result['tier_used'] == 'tier2_openai_fast'
    assert calls == [OPENAI_DEFAULT_BASE_URL + '/chat/completions']
    assert pc.calls == 0


def test_agent_disabled_during_stt_preserves_transcript_without_starting_action(tmp_path):
    calls = []

    async def provider(method, url, headers, payload):
        calls.append(url)
        agent.governor.set_policy({'agent_enabled': False})
        return {'text': '4분 타이머 시작', 'usage': {'input_tokens': 50, 'output_tokens': 10}}

    agent, pc = harness(tmp_path, provider)
    with pytest.raises(HTTPException) as error:
        run(agent.transcribe_and_submit(request_id='synthetic-disable-during-stt',
                                       audio_bytes=make_wav_bytes()))
    assert error.value.status_code == 503
    assert '명령을 실행하지 않습니다' in error.value.detail
    assert agent.timers.snapshot()['active_count'] == 0
    assert agent.list_runs()['items'] == []
    assert agent.governor.summary()['call_count'] == 1
    # A completed transcript remains durable; reading that same input cannot
    # retranscribe it, reset the paid reservation, or launch a timer.
    cached = run(audio(agent, request_id='synthetic-disable-during-stt'))
    assert cached['duplicate'] and cached['text'] == '4분 타이머 시작'
    assert len(calls) == 1 and pc.calls == 0


def test_local_text_rules_survive_missing_key_but_local_only_audio_does_not(tmp_path):
    agent, pc = harness(tmp_path, api_key='')
    result = run(agent.submit_command(AgentCommandInput(
        request_id='synthetic-offline-text', text='오늘 할 일 확인해줘', mode='local_only')))
    assert result['status'] == 'succeeded'
    assert result['tier_used'] == 'tier0_local_rule'
    assert agent.governor.summary()['call_count'] == 0
    with pytest.raises(HTTPException):
        run(audio(agent, local_only=True))
    assert pc.calls == 0
