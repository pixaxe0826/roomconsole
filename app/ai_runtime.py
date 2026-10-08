"""Manager-only, on-demand transport diagnostics. No dependency of core uptime.

The diagnostic cache is separate from the LLM execution engine: no prompt,
queue, retry, confirmation, or model configuration behavior is changed here.
"""
import asyncio
import time

from fastapi import Depends, HTTPException

RUNTIME_PATCH = 'room-hub-m3.4-0'


class AIRuntime:
    def __init__(self, speech, llm):
        self.speech, self.llm = speech, llm
        self.lock = asyncio.Lock()
        self.llm_probe = None
        self.llm_config = None
        self.llm_probe_time = None

    async def probe_llm(self):
        cfg = self.llm.config()
        result = await self.llm.probe()
        if cfg != self.llm.config():
            raise HTTPException(409, '확인 중 LLM 설정이 바뀌었습니다. 다시 확인하세요.')
        # /models is transport evidence, not model weights or GPU attestation.
        self.llm_probe, self.llm_config = result, cfg
        self.llm_probe_time = time.monotonic()

    def llm_status(self):
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
        def safe(read):
            try:
                return read()
            except (ValueError, TypeError, OSError):
                return {'state': 'config_error', 'stale': True, 'message': '서버 설정을 확인하세요.'}
        return {'schema': 1, 'runtime_patch': RUNTIME_PATCH,
                'speech': safe(self.speech.connection_status), 'llm': safe(self.llm_status),
                'scope': 'AI transport only; core health is /healthz',
                'automatic_inference': False}

    async def probe(self):
        if self.lock.locked():
            raise HTTPException(409, 'AI 연결 확인이 진행 중입니다.')
        async with self.lock:
            checks = await asyncio.gather(self.speech.probe(), self.probe_llm(), return_exceptions=True)
            result = self.snapshot()
            for name, check in zip(('speech', 'llm'), checks):
                if isinstance(check, BaseException):
                    # No upstream response bodies, credentials or traceback.
                    result[name] = {'state': 'probe_error', 'stale': True,
                                    'message': '설정을 확인하거나 진행 중인 연결 확인 후 다시 시도하세요.'}
            return result


def register_routes(app, speech, llm, admin):
    runtime = AIRuntime(speech, llm)
    app.state.ai_runtime = runtime

    @app.get('/api/ai/status')
    async def status(_=Depends(admin)):
        # Ordinary UI polling never creates outbound network/model work.
        return runtime.snapshot()

    @app.post('/api/ai/probe')
    async def probe(_=Depends(admin)):
        return await runtime.probe()
