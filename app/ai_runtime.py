"""Manager-only, on-demand transport diagnostics. No dependency of core uptime.

The diagnostic cache is separate from the LLM execution engine: no prompt,
queue, retry, confirmation, or model configuration behavior is changed here.
"""
import asyncio
import time

from fastapi import Depends, HTTPException

RUNTIME_PATCH = 'room-hub-m3.4-0'


class AIRuntime:
    def __init__(self, speech, llm, agent=None):
        self.speech, self.llm = speech, llm
        self.agent = agent
        self.profile = getattr(speech, 'profile', getattr(agent, 'profile', None))
        self.lock = asyncio.Lock()
        self.llm_probe = None
        self.llm_config = None
        self.llm_probe_time = None

    @property
    def pc_free(self):
        return bool(self.profile is not None and self.profile.pc_free)

    def _openai_snapshot(self):
        """Check only local policy/worker prerequisites; never attest the provider.

        Retired loopback endpoints are deliberately not read or probed, including
        when the key is missing, the budget is exhausted, or the agent is off.
        """
        metadata = {'runtime_profile': 'odyssey_openai', 'orchestrator': 'odyssey',
                    'pc_required': False, 'ssh_tunnel_required': False, 'legacy_inference_allowed': False,
                    'legacy_fallback_enabled': False, 'stt_fallback_to_local': False,
                    'provider_verified': False, 'check_scope': 'local_configuration_only'}
        try:
            if self.agent is None:
                raise ValueError('agent service missing')
            agent = self.agent.status()
            speech = self.speech.status()
            policy = self.agent.governor.policy()
            cloud = agent.get('openai', {})

            def state(*, audio=False):
                if not agent.get('agent_enabled'):
                    return 'agent_disabled'
                if not agent.get('cloud_enabled'):
                    return 'cloud_disabled'
                if audio and not agent.get('audio_cloud_enabled'):
                    return 'audio_cloud_disabled'
                if cloud.get('endpoint_allowed') is False:
                    return 'profile_endpoint_blocked'
                if not cloud.get('api_key_configured'):
                    return 'api_key_missing'
                if cloud.get('budget_state') == 'HARD_CAP_REACHED':
                    return 'budget_exhausted'
                if agent.get('cloud_status') not in (None, 'ready'):
                    return agent['cloud_status']
                if not cloud.get('audio_cloud_ready' if audio else 'cloud_ready'):
                    return 'unavailable'
                if audio and not speech.get('ready'):
                    return 'speech_unavailable'
                return 'configured'

            common = {'backend': 'openai', 'provider_verified': False,
                      'last_probe': None, 'stale': False, 'reported_device': None,
                      'identity_note': '서버 설정·정책·예산만 확인합니다. API 연결·모델 접근·실제 추론 성공은 별도 요청으로 검증해야 합니다.'}
            speech_result = {**common, 'state': state(audio=True),
                             'enabled': bool(agent.get('agent_enabled') and agent.get('cloud_enabled') and agent.get('audio_cloud_enabled')),
                             'configured_model': policy.stt_model, 'reported_model': None,
                             'configured_device': 'OpenAI API · GPU/PC 불필요',
                             'preprocessing': 'Odyssey FFmpeg',
                             'ready': bool(speech.get('ready')),
                             'message': speech.get('reason') or None}
            llm_result = {**common, 'state': state(),
                          'enabled': bool(agent.get('agent_enabled') and agent.get('cloud_enabled')),
                          'configured_model': policy.fast_model, 'configured_deep_model': policy.deep_model,
                          'reasoning_effort': policy.reasoning_effort,
                          'reported_models': [], 'model_list_match': False}
        except (ValueError, TypeError, OSError, AttributeError):
            speech_result = {'backend': 'openai', 'state': 'config_error', 'provider_verified': False,
                             'message': 'Odyssey의 에이전트 정책과 전사 전처리 설정을 확인하세요.'}
            llm_result = {'backend': 'openai', 'state': 'config_error', 'provider_verified': False}
        return {'schema': 2, 'runtime_patch': RUNTIME_PATCH, **metadata,
                'speech': speech_result, 'llm': llm_result,
                'scope': 'Odyssey local prerequisites only; no PC/OpenAI probe; core health is /healthz',
                'automatic_inference': False}

    async def probe_llm(self):
        if self.pc_free:
            return self._openai_snapshot()['llm']
        cfg = self.llm.config()
        result = await self.llm.probe()
        if cfg != self.llm.config():
            raise HTTPException(409, '확인 중 LLM 설정이 바뀌었습니다. 다시 확인하세요.')
        # /models is transport evidence, not model weights or GPU attestation.
        self.llm_probe, self.llm_config = result, cfg
        self.llm_probe_time = time.monotonic()

    def llm_status(self):
        if self.pc_free:
            return self._openai_snapshot()['llm']
        cfg = self.llm.config()
        fresh = (cfg == self.llm_config and self.llm_probe_time is not None
                 and time.monotonic() - self.llm_probe_time <= 30)
        probe = self.llm_probe if cfg == self.llm_config else None
        models = [v for v in (probe or {}).get('models', []) if isinstance(v, str)][:100]
        return {'backend': 'loopback_http', 'enabled': cfg.enabled,
                'configured_model': cfg.model, 'reported_models': models,
                'model_list_match': bool(fresh and probe and probe.get('ok') and cfg.model in models),
                'reported_device': None,
                'state': ('online' if probe.get('ok') else 'model_mismatch' if probe.get('reachable') else 'offline') if fresh and probe else 'unknown',
                'last_probe': probe, 'stale': not fresh, 'stale_after_seconds': 30,
                'identity_note': '모델 목록 응답 확인은 가중치 해시/CUDA 사용 또는 실제 생성 성공의 증명이 아닙니다.'}

    def snapshot(self):
        if self.pc_free:
            return self._openai_snapshot()
        def safe(read):
            try:
                return read()
            except (ValueError, TypeError, OSError):
                return {'state': 'config_error', 'stale': True, 'message': '서버 설정을 확인하세요.'}
        return {'schema': 1, 'runtime_patch': RUNTIME_PATCH, 'runtime_profile': 'legacy',
                'speech': safe(self.speech.connection_status), 'llm': safe(self.llm_status),
                'scope': 'AI transport only; core health is /healthz',
                'automatic_inference': False}

    async def probe(self):
        if self.lock.locked():
            raise HTTPException(409, 'AI 연결 확인이 진행 중입니다.')
        async with self.lock:
            if self.pc_free:
                # Explicit refresh is still local-only: neither retired PC
                # health checks nor a paid provider test is implicit here.
                return self.snapshot()
            checks = await asyncio.gather(self.speech.probe(), self.probe_llm(), return_exceptions=True)
            result = self.snapshot()
            for name, check in zip(('speech', 'llm'), checks):
                if isinstance(check, BaseException):
                    # No upstream response bodies, credentials or traceback.
                    result[name] = {'state': 'probe_error', 'stale': True,
                                    'message': '설정을 확인하거나 진행 중인 연결 확인 후 다시 시도하세요.'}
            return result


def register_routes(app, speech, llm, admin, *, agent=None):
    runtime = AIRuntime(speech, llm, agent=agent)
    app.state.ai_runtime = runtime

    @app.get('/api/ai/status')
    async def status(_=Depends(admin)):
        # Ordinary UI polling never creates outbound network/model work.
        return runtime.snapshot()

    @app.post('/api/ai/probe')
    async def probe(_=Depends(admin)):
        return await runtime.probe()
