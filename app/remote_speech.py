"""One bounded WAV POST to a configured loopback whisper.cpp v1.8.3 server.

A remote GPU may be reached through an operator-managed SSH tunnel. This module
never starts SSH, downloads/loads models, follows redirects, inherits proxies,
retries, or falls back to another recognizer. Backend identity is not inferred.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit

import httpx

from .speech import SpeechConfig, TranscriptionError, WhisperRunner, prepare_wav
from .store import utcnow
from .stt_accuracy import AccuracyConfig, MAX_HINT_BYTES, PATCH_ID

MAX_RESPONSE_BYTES = 512 * 1024
CONNECT_TIMEOUT = 3.0
PROBE_TIMEOUT = 5.0


def validate_endpoint(value: str) -> str:
    """No hostname/DNS, userinfo, query, fragment, alternate paths or core ports."""
    try:
        u = urlsplit(value)
        if (not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value)
                or u.scheme != 'http' or u.hostname not in {'127.0.0.1', '::1'}
                or u.username is not None or u.password is not None
                or u.port is None or not 1024 <= u.port <= 65535
                or u.port in {8080, 8088, 8443, 8022}
                or u.path != '/inference' or u.query or u.fragment
                or value != f'http://{u.netloc}/inference'):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Speech URL must be numeric loopback HTTP /inference on a dedicated port') from None
    return value


class RemoteSpeechError(TranscriptionError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _utf8_size(value: str) -> int:
    try:
        return len(value.encode('utf-8'))
    except UnicodeError:
        return MAX_RESPONSE_BYTES + 1


def _json(raw: bytes):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError('duplicate key')
            out[key] = value
        return out
    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))


async def cancellable(awaitable, cancel: asyncio.Event, timeout: float):
    """Outer wall deadline also bounds drip-feed streams; always join both tasks."""
    operation = asyncio.create_task(awaitable)
    stopped = asyncio.create_task(cancel.wait())
    try:
        done, _ = await asyncio.wait({operation, stopped}, timeout=timeout,
                                    return_when=asyncio.FIRST_COMPLETED)
        if cancel.is_set():
            raise asyncio.CancelledError
        if operation not in done:
            raise RemoteSpeechError('timeout', 'PC 전사 응답 제한 시간을 넘었습니다. 자동 재전송하지 않습니다.')
        return operation.result()
    finally:
        for task in (operation, stopped):
            if not task.done():
                task.cancel()
        await asyncio.gather(operation, stopped, return_exceptions=True)


class RemoteWhisperRunner(WhisperRunner):
    def __init__(self, transport=None):
        # Only tests inject transport; production has no browser-supplied endpoint.
        self.transport = transport

    async def _exchange(self, method, url, *, timeout, files=None, data=None):
        headers = {'Accept': 'application/json'}
        key = os.environ.get('HUB_SPEECH_API_KEY', '')
        if key:
            if any(ord(c) < 32 or ord(c) > 126 for c in key):
                raise RemoteSpeechError('auth_config', 'PC 전사 인증 설정을 확인하세요.')
            headers['Authorization'] = 'Bearer ' + key
        try:
            async with httpx.AsyncClient(transport=self.transport, trust_env=False,
                    follow_redirects=False,
                    timeout=httpx.Timeout(timeout, connect=min(CONNECT_TIMEOUT, timeout))) as client:
                async with client.stream(method, url, headers=headers, files=files, data=data) as res:
                    if res.status_code != 200:
                        code = 'loading' if res.status_code == 503 else 'http_error'
                        raise RemoteSpeechError(code, 'PC 전사 서버가 준비되지 않았거나 요청을 거부했습니다. 연결과 설정을 확인하세요.')
                    if res.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
                        raise RemoteSpeechError('invalid_response', 'PC 전사 서버가 JSON 응답을 반환하지 않았습니다.')
                    raw = bytearray()
                    async for chunk in res.aiter_bytes(65536):
                        if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise RemoteSpeechError('response_too_large', 'PC 전사 응답이 크기 제한을 넘었습니다.')
                        raw.extend(chunk)
            body = _json(bytes(raw))
            if not isinstance(body, dict) or 'error' in body:
                raise ValueError('invalid response shape or backend error')
            return body
        except httpx.TimeoutException:
            raise RemoteSpeechError('timeout', 'PC 전사 응답 제한 시간을 넘었습니다. 자동 재전송하지 않습니다.') from None
        except httpx.RequestError:
            raise RemoteSpeechError('connection_failed', 'PC 전사 서버에 연결할 수 없습니다. PC와 SSH 터널 상태를 확인하세요.') from None
        except (ValueError, UnicodeError, RecursionError):
            raise RemoteSpeechError('invalid_response', 'PC 전사 응답 형식을 확인할 수 없습니다.') from None

    async def probe(self, cfg: SpeechConfig):
        endpoint = validate_endpoint(cfg.remote_url)
        url = endpoint.rsplit('/', 1)[0] + '/health'
        try:
            body = await asyncio.wait_for(self._exchange('GET', url, timeout=PROBE_TIMEOUT), PROBE_TIMEOUT)
            if body.get('status') != 'ok':
                raise RemoteSpeechError('invalid_response', '전사 서버 health 응답을 확인하세요.')
            return {'ok': True, 'reachable': True, 'checked_at': utcnow(), 'state': 'online',
                    'message': '전사 health 응답 확인. 모델명·장치와 실제 추론 성능은 별도 확인이 필요합니다.'}
        except (RemoteSpeechError, asyncio.TimeoutError) as exc:
            code = exc.code if isinstance(exc, RemoteSpeechError) else 'timeout'
            return {'ok': False, 'reachable': code not in {'connection_failed', 'timeout'},
                    'checked_at': utcnow(), 'state': 'offline' if code == 'connection_failed' else code,
                    'message': str(exc) if isinstance(exc, RemoteSpeechError) else '전사 연결 확인 시간이 초과됐습니다.'}

    async def transcribe_detailed(self, source: Path, cfg: SpeechConfig, cancel: asyncio.Event,
                                 temp_root: Path, policy: AccuracyConfig, hint: dict):
        endpoint = validate_endpoint(cfg.remote_url)
        if cancel.is_set():
            raise asyncio.CancelledError
        prompt = hint.get('text', '') if policy.profile != 'legacy' else ''
        if not isinstance(prompt, str) or _utf8_size(prompt) > MAX_HINT_BYTES:
            raise RemoteSpeechError('hint_invalid', '전사 어휘 힌트 설정을 확인하세요.')
        started = time.monotonic()
        temp_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix='remote-', dir=temp_root) as folder:
            wav = Path(folder) / 'input.wav'
            conversion_start = time.monotonic()
            duration = await prepare_wav(source, cfg, cancel, wav)
            conversion_seconds = time.monotonic() - conversion_start
            # Explicit fields also prevent a previous client's settings leaking in.
            # no_timestamps=false matches the deployed compatibility wrapper.
            form = {'language': cfg.language, 'beam_size': str(policy.beam), 'best_of': '1',
                    'prompt': prompt, 'translate': 'false', 'detect_language': 'false',
                    'no_timestamps': 'false', 'no_language_probabilities': 'true',
                    'response_format': 'verbose_json'}
            call_start = time.monotonic()
            with wav.open('rb') as audio:
                body = await cancellable(self._exchange('POST', endpoint, timeout=cfg.timeout_seconds,
                    files={'file': ('input.wav', audio, 'audio/wav')}, data=form), cancel, cfg.timeout_seconds)
            request_seconds = time.monotonic() - call_start
            text = body.get('text')
            if (not isinstance(text, str) or not text.strip() or len(text) > 16000
                    or '\x00' in text or _utf8_size(text) > 128 * 1024):
                raise RemoteSpeechError('invalid_transcript', 'PC 전사 결과가 비어 있거나 허용 크기·형식을 벗어났습니다.')
            text = text.strip()
            segments = []
            rows = body.get('segments')
            if isinstance(rows, list):
                for row in rows[:128]:
                    if not isinstance(row, dict) or not isinstance(row.get('text'), str) or _utf8_size(row['text']) > 128 * 1024:
                        continue
                    item = {'text': row['text'][:16000]}
                    start, end = row.get('start'), row.get('end')
                    if all(type(v) in (int, float) and 0 <= v <= 86400 and math.isfinite(v) for v in (start, end)) and end >= start:
                        item['offsets_ms'] = {'from': round(start * 1000), 'to': round(end * 1000)}
                    segments.append(item)
            report = {
                'schema': 1, 'patch_id': PATCH_ID, 'runtime_patch_id': 'room-hub-m3.4-0',
                'backend': 'remote_http', 'profile': policy.profile, 'beam_size': policy.beam,
                'best_of': 1, 'temperature_fallback': 'server default (unchanged)',
                'model_file': cfg.remote_model or None, 'model_name': cfg.remote_model or cfg.model_name,
                'model_identity_source': 'operator_config_not_verified', 'reported_model': None,
                'configured_device': cfg.remote_device, 'device': None, 'cpu_only': None,
                'threads': None, 'thread_source': 'remote_server_not_reported', 'language': cfg.language,
                'hint': hint, 'raw_transcript': text, 'selected_transcript': text,
                'postprocessing': 'none; HTTP text field stripped at edges only',
                'passes': 1, 'automatic_second_pass': False, 'audio_seconds': duration,
                'conversion_seconds': conversion_seconds, 'remote_request_seconds': request_seconds,
                'whisper_seconds_including_load': None, 'model_load_seconds': None,
                'runner_total_seconds': time.monotonic() - started,
                'scores': {'available': False, 'reason': 'CLI token scores not provided by remote API',
                           'meaning': 'No calibrated accuracy/confidence claim'},
                'segments': {'available': isinstance(rows, list), 'language': body['language'] if isinstance(body.get('language'), str) and _utf8_size(body['language']) <= 80 else None,
                             'segments': segments},
                'notes': ['Remote request time includes transport, queue, inference and response; not GPU-only time.',
                          'Stock whisper.cpp does not report model/device identity; configured labels are not proof.',
                          'Cancellation closes the HTTP request; backend compute termination is not guaranteed.',
                          'No fallback, retry, model loading or action execution.'],
            }
            return text, duration, report
