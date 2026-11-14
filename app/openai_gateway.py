"""Bounded OpenAI API gateway for Chat Completions (Structured Outputs) and Audio STT.

Credentials are read strictly from OpenAI-specific environment variables
(`OPENAI_API_KEY` or `HUB_OPENAI_API_KEY`; never `HUB_LLM_API_KEY`) and are
never persisted to SQLite, audit logs, or browser responses. Every call
reserves budget through `BudgetGovernor` before dispatch and settles exact
provider usage (even on refusal, truncation, or invalid JSON) or marks
ambiguous transport failures as `unknown`.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import struct
import time
from typing import Any
import urllib.error
from urllib.parse import urlsplit
import urllib.request
import uuid

from .openai_budget import BudgetGovernor, UsageCategory

MAX_RESPONSE_BYTES = 512 * 1024
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_AUDIO_DURATION_SECONDS = 300.0
OPENAI_DEFAULT_BASE_URL = 'https://api.openai.com/v1'


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


class OpenAIGatewayError(Exception):
    """Safe user-facing gateway error; never leaks API keys or raw headers."""

    def __init__(
        self,
        message: str,
        code: str = 'openai_error',
        http_status: int | None = None,
        *,
        response_body: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.response_body = response_body


def resolve_openai_api_key() -> str:
    """Read OpenAI API key from OpenAI-specific server environment variables only.

    Never falls back to `HUB_LLM_API_KEY` so local/LAN LLM tokens are never sent to OpenAI.
    """
    for env_name in ('OPENAI_API_KEY', 'HUB_OPENAI_API_KEY'):
        val = os.environ.get(env_name, '').strip()
        if val:
            return val
    return ''


def validate_openai_base_url(url: str) -> str:
    """Allow only official https://api.openai.com/v1 or local loopback /v1."""
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url):
        raise ValueError('유효하지 않은 OpenAI API 주소입니다.')
    u = urlsplit(url.rstrip('/'))
    if u.username is not None or u.password is not None or u.query or u.fragment:
        raise ValueError('API 주소에 인증 정보나 쿼리를 포함할 수 없습니다.')
    if u.scheme == 'https' and u.hostname == 'api.openai.com' and u.port in {None, 443} and u.path == '/v1':
        return 'https://api.openai.com/v1'
    if (
        u.scheme == 'http'
        and u.hostname in {'127.0.0.1', '::1'}
        and u.port is not None
        and 1024 <= u.port <= 65535
        and u.port not in {8080, 8088, 8443, 8022}
        and u.path == '/v1'
    ):
        return url.rstrip('/')
    raise ValueError('OpenAI API 주소는 https://api.openai.com/v1 또는 로컬 loopback(/v1)만 허용합니다.')


def has_usage(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    u = body.get('usage')
    if not isinstance(u, dict):
        return False
    return any(
        isinstance(u.get(k), (int, float)) and u.get(k, 0) > 0
        for k in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'input_tokens', 'output_tokens', 'seconds')
    )


def extract_usage(body: dict[str, Any]) -> dict[str, Any]:
    usage = body.get('usage') if isinstance(body.get('usage'), dict) else {}
    prompt_tokens = usage.get('prompt_tokens')
    if not (type(prompt_tokens) is int and prompt_tokens >= 0):
        in_tok = usage.get('input_tokens')
        prompt_tokens = in_tok if type(in_tok) is int and in_tok >= 0 else 0

    completion_tokens = usage.get('completion_tokens')
    if not (type(completion_tokens) is int and completion_tokens >= 0):
        out_tok = usage.get('output_tokens')
        completion_tokens = out_tok if type(out_tok) is int and out_tok >= 0 else 0

    details = usage.get('prompt_tokens_details') if isinstance(usage.get('prompt_tokens_details'), dict) else {}
    cached_tokens = details.get('cached_tokens')
    comp_details = (
        usage.get('completion_tokens_details')
        if isinstance(usage.get('completion_tokens_details'), dict)
        else {}
    )
    reasoning_tokens = comp_details.get('reasoning_tokens')
    seconds_val = usage.get('seconds')
    if not isinstance(seconds_val, (int, float)) or isinstance(seconds_val, bool):
        seconds_val = body.get('duration') if isinstance(body.get('duration'), (int, float)) else 0.0

    return {
        'prompt_tokens': prompt_tokens if type(prompt_tokens) is int and prompt_tokens >= 0 else 0,
        'cached_tokens': cached_tokens if type(cached_tokens) is int and cached_tokens >= 0 else 0,
        'completion_tokens': completion_tokens if type(completion_tokens) is int and completion_tokens >= 0 else 0,
        'reasoning_tokens': reasoning_tokens if type(reasoning_tokens) is int and reasoning_tokens >= 0 else 0,
        'audio_seconds': float(seconds_val) if isinstance(seconds_val, (int, float)) and seconds_val > 0 else 0.0,
    }


_CONTAINER_RULES: dict[str, dict[str, set[str]]] = {
    'wav': {
        'mimes': {'audio/wav', 'audio/x-wav', 'audio/wave', 'audio/vnd.wave'},
        'exts': {'.wav'},
    },
    'webm': {
        'mimes': {'audio/webm', 'video/webm'},
        'exts': {'.webm'},
    },
    'ogg': {
        'mimes': {'audio/ogg', 'application/ogg'},
        'exts': {'.ogg', '.oga'},
    },
    'mp3': {
        'mimes': {'audio/mpeg', 'audio/mp3'},
        'exts': {'.mp3'},
    },
    'mp4': {
        'mimes': {'audio/mp4', 'audio/m4a', 'audio/x-m4a', 'video/mp4'},
        'exts': {'.m4a', '.mp4'},
    },
}


def _detect_audio_container(blob: bytes) -> str:
    if len(blob) >= 12 and blob[:4] == b'RIFF' and blob[8:12] == b'WAVE':
        return 'wav'
    if len(blob) >= 4 and blob[:4] == b'\x1a\x45\xdf\xa3':
        return 'webm'
    if len(blob) >= 4 and blob[:4] == b'OggS':
        return 'ogg'
    if len(blob) >= 3 and blob[:3] == b'ID3':
        return 'mp3'
    if len(blob) >= 2 and blob[0] == 0xFF and (blob[1] & 0xE0) == 0xE0:
        return 'mp3'
    if len(blob) >= 12 and blob[4:8] == b'ftyp':
        return 'mp4'
    raise ValueError('지원하지 않거나 손상된 오디오 컨테이너 헤더입니다.')


def _verify_wav_chunks(blob: bytes) -> float:
    if len(blob) < 44:
        raise ValueError('WAV 헤더 길이가 너무 짧습니다.')
    riff_size = struct.unpack_from('<I', blob, 4)[0]
    if riff_size + 8 > len(blob):
        raise ValueError('WAV RIFF 청크 크기가 실제 파일 크기와 일치하지 않습니다.')

    offset = 12
    fmt_found = False
    byte_rate = 0
    block_align = 0
    data_size = 0
    data_found = False

    while offset + 8 <= len(blob):
        chunk_id = blob[offset : offset + 4]
        chunk_size = struct.unpack_from('<I', blob, offset + 4)[0]
        body_start = offset + 8
        body_end = body_start + chunk_size
        if body_end > len(blob):
            raise ValueError('WAV 내부 청크가 파일 끝을 벗어납니다.')
        if chunk_id == b'fmt ':
            if chunk_size < 16:
                raise ValueError('WAV fmt 청크가 손상되었습니다.')
            audio_fmt, channels, sample_rate, byte_rate, block_align, bits_per_sample = struct.unpack_from(
                '<HHIIHH', blob, body_start
            )
            if audio_fmt not in {1, 3} or channels not in {1, 2} or sample_rate < 8000 or sample_rate > 96000:
                raise ValueError('지원하지 않는 WAV 오디오 포맷입니다.')
            expected_align = channels * (bits_per_sample // 8)
            if block_align <= 0 or expected_align != block_align or byte_rate != sample_rate * block_align:
                raise ValueError('WAV 프레임 정렬/바이트율 정보가 일치하지 않습니다.')
            fmt_found = True
        elif chunk_id == b'data':
            data_size = chunk_size
            data_found = True
            break
        offset = body_end + (chunk_size % 2)

    if not fmt_found or not data_found or data_size <= 0:
        raise ValueError('WAV 필수 청크(fmt/data)가 없거나 비어 있습니다.')
    if data_size % block_align != 0:
        raise ValueError('WAV 데이터 길이가 프레임 단위로 정렬되어 있지 않습니다.')
    duration = data_size / float(byte_rate)
    if duration <= 0.0 or duration > MAX_AUDIO_DURATION_SECONDS:
        raise ValueError(f'WAV 오디오 길이가 허용 범위(0~{int(MAX_AUDIO_DURATION_SECONDS)}초)를 벗어납니다.')
    return duration


def verify_audio_payload(
    audio_bytes: bytes,
    *,
    filename: str = 'voice.wav',
    mime_type: str = 'audio/wav',
    claimed_duration_seconds: float | None = None,
) -> dict[str, Any]:
    """Validate audio container headers, MIME/extension match, and compute server-verified duration."""
    if not isinstance(audio_bytes, (bytes, bytearray)) or len(audio_bytes) == 0:
        raise ValueError('오디오 파일이 비어 있습니다.')
    size = len(audio_bytes)
    if size > MAX_AUDIO_BYTES:
        raise ValueError('오디오 파일이 최대 크기(10MB)를 초과했습니다.')

    if claimed_duration_seconds is not None:
        if isinstance(claimed_duration_seconds, bool) or not isinstance(claimed_duration_seconds, (int, float)):
            raise ValueError('오디오 길이 형식이 올바르지 않습니다.')
        claimed = float(claimed_duration_seconds)
        if math.isnan(claimed) or math.isinf(claimed) or claimed <= 0.0 or claimed > MAX_AUDIO_DURATION_SECONDS:
            raise ValueError('오디오 길이(duration_seconds)가 유효 범위를 벗어났습니다.')
    else:
        claimed = None

    container = _detect_audio_container(bytes(audio_bytes))
    rules = _CONTAINER_RULES[container]
    clean_mime = (mime_type or '').split(';')[0].strip().lower()
    ext = Path(filename or '').suffix.lower()

    if clean_mime and clean_mime not in rules['mimes']:
        raise ValueError(f'오디오 매직 헤더({container})와 MIME 타입({clean_mime})이 일치하지 않습니다.')
    if ext and ext not in rules['exts']:
        raise ValueError(f'오디오 매직 헤더({container})와 파일 확장자({ext})가 일치하지 않습니다.')

    if container == 'wav':
        actual_duration = _verify_wav_chunks(bytes(audio_bytes))
        verified_duration = max(0.25, actual_duration)
        return {
            'container': container,
            'mime_type': clean_mime or 'audio/wav',
            'size_bytes': size,
            'verified_duration_seconds': round(verified_duration, 3),
            'duration_source': 'wav_header',
        }

    # Non-WAV compressed containers (webm, ogg, mp3, mp4): enforce plausible bitrate bounds
    # Minimum plausible bitrate ~ 4 kbps (500 bytes/s); maximum ~ 512 kbps (64,000 bytes/s)
    min_plausible = max(0.1, size / 64000.0)
    max_plausible = min(MAX_AUDIO_DURATION_SECONDS, max(2.0, size / 500.0))
    if claimed is not None:
        if size >= 16384 and claimed < min_plausible * 0.5:
            raise ValueError('전달된 duration_seconds가 파일 크기 대비 비정상적으로 작습니다.')
        if claimed > max_plausible * 2.0 and claimed > 10.0:
            raise ValueError('전달된 duration_seconds가 파일 크기 대비 비정상적으로 큽니다.')
        verified_duration = max(claimed, size / 16000.0)
    else:
        verified_duration = max(1.0, size / 16000.0)

    if verified_duration > MAX_AUDIO_DURATION_SECONDS:
        raise ValueError('추정 오디오 길이가 최대 허용 시간(300초)을 초과했습니다.')

    return {
        'container': container,
        'mime_type': clean_mime,
        'size_bytes': size,
        'verified_duration_seconds': round(max(0.25, verified_duration), 3),
        'duration_source': 'container_bitrate_bound',
    }


class OpenAIGateway:
    """Production & injectable gateway for OpenAI Structured Outputs and Audio STT."""

    def __init__(
        self,
        governor: BudgetGovernor,
        *,
        base_url: str = OPENAI_DEFAULT_BASE_URL,
        api_key: str | None = None,
        transport: Any = None,
    ):
        self.governor = governor
        self.base_url = validate_openai_base_url(base_url)
        self._explicit_api_key = (api_key or '').strip()
        self.transport = transport

    def _api_key(self) -> str:
        return self._explicit_api_key or resolve_openai_api_key()

    def key_configured(self) -> bool:
        return bool(self._api_key())

    def readiness(self) -> dict[str, Any]:
        pol = self.governor.policy()
        has_key = self.key_configured()
        budget = self.governor.summary()
        budget_ok = budget['state'] != 'HARD_CAP_REACHED'
        cloud_ready = bool(pol.agent_enabled and pol.cloud_enabled and has_key and budget_ok)
        audio_cloud_ready = bool(cloud_ready and pol.audio_cloud_enabled)
        if not pol.agent_enabled:
            status_reason = 'agent_disabled'
        elif not pol.cloud_enabled:
            status_reason = 'cloud_disabled'
        elif not has_key:
            status_reason = 'missing_api_key'
        elif not budget_ok:
            status_reason = 'budget_hard_cap_reached'
        else:
            status_reason = 'ready'
        return {
            'agent_enabled': pol.agent_enabled,
            'cloud_enabled': pol.cloud_enabled,
            'audio_cloud_enabled': pol.audio_cloud_enabled,
            'allow_shared_notes_in_cloud': pol.allow_shared_notes_in_cloud,
            'api_key_configured': has_key,
            'base_url': self.base_url,
            'budget_state': budget['state'],
            'cloud_ready': cloud_ready,
            'audio_cloud_ready': audio_cloud_ready,
            'status_reason': status_reason,
            'spent_usd': budget['spent_usd'],
            'reserved_usd': budget.get('reserved_usd', 0.0),
            'unknown_usd': budget.get('unknown_usd', 0.0),
            'remaining_usd': budget['remaining_usd'],
            'monthly_cap_usd': budget['monthly_cap_usd'],
        }

    async def _post_json(self, path: str, payload: dict[str, Any], timeout: float = 60.0) -> tuple[dict[str, Any], str]:
        key = self._api_key()
        if not key and self.base_url.startswith('https://'):
            raise OpenAIGatewayError(
                'OPENAI_API_KEY 환경변수가 설정되지 않았습니다. 서버 환경변수를 확인하세요.',
                'missing_api_key',
                401,
            )
        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }
        if key:
            headers['Authorization'] = f'Bearer {key}'
        encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
        url = self.base_url + path

        if callable(self.transport) and not hasattr(self.transport, 'handle_async_request'):
            body = await self.transport('POST', url, headers, payload)
            raw = json.dumps(body, ensure_ascii=False)
            return body, raw

        labels = {
            400: 'OpenAI 요청 형식이나 스키마를 확인하세요.',
            401: 'OpenAI API 키 인증에 실패했습니다.',
            403: 'OpenAI 프로젝트/모델 접근 권한을 확인하세요.',
            404: '요청한 OpenAI 모델 ID를 확인하세요.',
            429: 'OpenAI 요청 한도(Rate Limit/Quota)에 도달했습니다.',
            500: 'OpenAI 서버 내부 오류입니다.',
            503: 'OpenAI 서버가 일시적으로 응답하지 않습니다.',
        }

        if self.transport is None:
            def _urllib_post() -> tuple[dict[str, Any], str]:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({}),
                    _NoRedirectHandler(),
                )
                req = urllib.request.Request(url, data=encoded, headers=headers, method='POST')
                try:
                    with opener.open(req, timeout=timeout) as resp:
                        raw_bytes = resp.read(MAX_RESPONSE_BYTES + 1)
                except urllib.error.HTTPError as exc:
                    err_body = None
                    try:
                        err_raw = exc.read(MAX_RESPONSE_BYTES)
                        err_parsed = json.loads(err_raw.decode('utf-8', errors='replace'))
                        if isinstance(err_parsed, dict):
                            err_body = err_parsed
                    except Exception:
                        err_body = None
                    msg = labels.get(exc.code, 'OpenAI API 응답 오류입니다.')
                    raise OpenAIGatewayError(
                        f'OpenAI HTTP {exc.code}: {msg}',
                        'http_error',
                        exc.code,
                        response_body=err_body,
                    ) from None
                except urllib.error.URLError as exc:
                    if isinstance(getattr(exc, 'reason', None), TimeoutError):
                        raise OpenAIGatewayError('OpenAI 응답 제한 시간을 초과했습니다.', 'timeout') from None
                    raise OpenAIGatewayError('OpenAI API 서버에 연결할 수 없습니다.', 'connection_failed') from None
                except TimeoutError:
                    raise OpenAIGatewayError('OpenAI 응답 제한 시간을 초과했습니다.', 'timeout') from None
                if len(raw_bytes) > MAX_RESPONSE_BYTES:
                    raise OpenAIGatewayError('OpenAI 응답이 512KiB 제한을 초과했습니다.', 'response_too_large')
                try:
                    raw_str = raw_bytes.decode('utf-8')
                    parsed_obj = json.loads(
                        raw_str,
                        parse_constant=lambda _x: (_ for _ in ()).throw(ValueError('nonfinite')),
                    )
                    if not isinstance(parsed_obj, dict):
                        raise ValueError('Expected JSON object')
                    return parsed_obj, raw_str
                except (UnicodeError, ValueError) as exc:
                    raise OpenAIGatewayError('OpenAI가 유효한 JSON 응답을 반환하지 않았습니다.', 'invalid_response') from exc

            return await asyncio.to_thread(_urllib_post)

        import httpx
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(timeout, connect=min(8.0, timeout)),
            ) as client:
                async with client.stream('POST', url, headers=headers, content=encoded) as res:
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in res.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise OpenAIGatewayError('OpenAI 응답이 512KiB 제한을 초과했습니다.', 'response_too_large')
                        chunks.append(chunk)
                    raw_bytes = b''.join(chunks)
                    if res.status_code != 200:
                        err_body = None
                        try:
                            parsed_err = json.loads(raw_bytes.decode('utf-8', errors='replace'))
                            if isinstance(parsed_err, dict):
                                err_body = parsed_err
                        except Exception:
                            err_body = None
                        msg = labels.get(res.status_code, 'OpenAI API 응답 오류입니다.')
                        raise OpenAIGatewayError(
                            f'OpenAI HTTP {res.status_code}: {msg}',
                            'http_error',
                            res.status_code,
                            response_body=err_body,
                        )
            raw = raw_bytes.decode('utf-8')
            parsed = json.loads(raw, parse_constant=lambda _x: (_ for _ in ()).throw(ValueError('nonfinite')))
            if not isinstance(parsed, dict):
                raise ValueError('Expected JSON object')
            return parsed, raw
        except OpenAIGatewayError:
            raise
        except (asyncio.TimeoutError, Exception) as exc:
            if exc.__class__.__name__.endswith('TimeoutException') or isinstance(exc, asyncio.TimeoutError):
                raise OpenAIGatewayError('OpenAI 응답 제한 시간을 초과했습니다.', 'timeout') from None
            if exc.__class__.__name__.endswith('RequestError'):
                raise OpenAIGatewayError('OpenAI API 서버에 연결할 수 없습니다.', 'connection_failed') from None
            if isinstance(exc, (UnicodeError, ValueError)):
                raise OpenAIGatewayError('OpenAI가 유효한 JSON 응답을 반환하지 않았습니다.', 'invalid_response') from None
            raise OpenAIGatewayError('OpenAI 요청 처리 중 오류가 발생했습니다.', 'gateway_error') from None

    def _settle_or_handle_failure(
        self,
        *,
        request_id: str,
        category: UsageCategory,
        model: str,
        note: str,
        at_iso: str | None,
        exc: OpenAIGatewayError,
    ) -> None:
        if exc.response_body and has_usage(exc.response_body):
            usage = extract_usage(exc.response_body)
            self.governor.record_usage(
                request_id=request_id,
                category=category,
                model=model,
                prompt_tokens=usage['prompt_tokens'],
                cached_tokens=usage['cached_tokens'],
                completion_tokens=usage['completion_tokens'],
                reasoning_tokens=usage['reasoning_tokens'],
                audio_seconds=usage['audio_seconds'],
                cost_source='provider_usage_on_error',
                note=f'{note} [error:{exc.code}]',
                at_iso=at_iso,
            )
            return
        if exc.code == 'missing_api_key':
            self.governor.release_reservation(request_id, 'missing_api_key')
            return
        if exc.code in {'http_error', 'stt_http_error'} and (exc.http_status or 0) in {400, 401, 403, 404, 422, 429}:
            self.governor.release_reservation(request_id, f'http_{exc.http_status}')
            return
        # Timeout, connection drop, 5xx, or truncated response after request was sent -> keep unknown reservation
        self.governor.mark_reservation_unknown(request_id, f'{exc.code}:{exc.http_status or ""}')

    async def generate_structured(
        self,
        *,
        request_id: str,
        category: UsageCategory,
        system_prompt: str,
        user_text: str | None = None,
        user_prompt: str | None = None,
        schema_name: str,
        schema: dict[str, Any],
        requested_model: str | None = None,
        max_output_tokens: int = 4096,
        temperature: float = 0.0,
        timeout: float = 60.0,
        force_model: bool = False,
        at_iso: str | None = None,
    ) -> dict[str, Any]:
        """Run a budget-reserved OpenAI Structured Outputs call and settle exact usage."""
        if user_text is None:
            user_text = user_prompt or ''
        pol = self.governor.policy()
        if not pol.agent_enabled or not pol.cloud_enabled:
            raise OpenAIGatewayError(
                'OpenAI 클라우드 호출이 비활성화되어 있습니다.',
                'cloud_disabled',
                403,
            )
        key = self._api_key()
        if not key and self.base_url.startswith('https://'):
            raise OpenAIGatewayError(
                'OPENAI_API_KEY 환경변수가 설정되지 않았습니다. 서버 환경변수를 확인하세요.',
                'missing_api_key',
                401,
            )

        est_input = max(256, (len(system_prompt) + len(user_text) + len(json.dumps(schema))) // 3)
        decision = self.governor.reserve_budget(
            request_id,
            category,
            requested_model,
            estimated_input_tokens=est_input,
            max_output_tokens=max_output_tokens,
            force_model=force_model,
            note=schema_name,
            at_iso=at_iso,
        )
        if not decision.allowed_cloud:
            raise OpenAIGatewayError(
                '월간 OpenAI API 예산 한도에 도달하여 클라우드 호출을 차단했습니다. 로컬 규칙 모드로 전환합니다.',
                'budget_exceeded',
                402,
            )

        is_reasoning_model = decision.selected_model.startswith(('gpt-6', 'gpt-5', 'o'))
        payload: dict[str, Any] = {
            'model': decision.selected_model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_text},
            ],
            'stream': False,
            'response_format': {
                'type': 'json_schema',
                'json_schema': {
                    'name': schema_name,
                    'strict': True,
                    'schema': schema,
                },
            },
        }
        if is_reasoning_model:
            payload['reasoning_effort'] = decision.reasoning_effort
            payload['max_completion_tokens'] = decision.max_output_tokens
        else:
            payload['max_tokens'] = decision.max_output_tokens
            payload['temperature'] = temperature

        start = time.perf_counter()
        try:
            body, raw = await asyncio.wait_for(
                self._post_json('/chat/completions', payload, timeout=timeout),
                timeout + 1.0,
            )
        except OpenAIGatewayError as exc:
            self._settle_or_handle_failure(
                request_id=request_id,
                category=category,
                model=decision.selected_model,
                note=f'{schema_name} ({decision.state})',
                at_iso=at_iso,
                exc=exc,
            )
            raise
        except Exception as exc:
            gw_exc = OpenAIGatewayError('OpenAI 응답 제한 시간을 초과했습니다.', 'timeout')
            self._settle_or_handle_failure(
                request_id=request_id,
                category=category,
                model=decision.selected_model,
                note=f'{schema_name} ({decision.state})',
                at_iso=at_iso,
                exc=gw_exc,
            )
            raise gw_exc from exc

        elapsed = time.perf_counter() - start
        usage = extract_usage(body)
        ledger: dict[str, Any] | None = None
        if has_usage(body):
            ledger = self.governor.record_usage(
                request_id=request_id,
                category=category,
                model=decision.selected_model,
                prompt_tokens=usage['prompt_tokens'],
                cached_tokens=usage['cached_tokens'],
                completion_tokens=usage['completion_tokens'],
                reasoning_tokens=usage['reasoning_tokens'],
                cost_source='provider_usage',
                note=f'{schema_name} ({decision.state})',
                at_iso=at_iso,
            )

        try:
            if isinstance(body.get('error'), dict):
                err_msg = str(body['error'].get('message') or 'OpenAI API 오류 응답입니다.')
                raise OpenAIGatewayError(err_msg, 'api_error', 400, response_body=body)
            choices = body.get('choices')
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise OpenAIGatewayError('OpenAI 응답에 choices가 없습니다.', 'invalid_response', response_body=body)
            choice = choices[0]
            message = choice.get('message') if isinstance(choice.get('message'), dict) else {}
            if message.get('refusal'):
                raise OpenAIGatewayError('모델이 요청 처리를 거부했습니다.', 'model_refusal', response_body=body)
            content = message.get('content')
            if not isinstance(content, str) or not content.strip():
                raise OpenAIGatewayError('OpenAI 구조화 응답 본문이 비어 있습니다.', 'empty_output', response_body=body)
            if choice.get('finish_reason') != 'stop':
                raise OpenAIGatewayError('응답이 토큰 한도 등으로 완전히 끝나지 않았습니다.', 'incomplete_output', response_body=body)
            try:
                structured = json.loads(content)
            except ValueError as exc:
                raise OpenAIGatewayError('구조화 JSON 파싱에 실패했습니다.', 'invalid_json', response_body=body) from exc
        except OpenAIGatewayError:
            if ledger is None:
                self.governor.mark_reservation_unknown(request_id, 'unsettled_invalid_response')
            raise

        if ledger is None:
            ledger = self.governor.record_usage(
                request_id=request_id,
                category=category,
                model=decision.selected_model,
                prompt_tokens=usage['prompt_tokens'],
                cached_tokens=usage['cached_tokens'],
                completion_tokens=usage['completion_tokens'],
                reasoning_tokens=usage['reasoning_tokens'],
                cost_source='provider_usage',
                note=f'{schema_name} ({decision.state})',
                at_iso=at_iso,
            )

        return {
            'parsed': structured,
            'raw_output': content,
            'raw_response': raw,
            'model': str(body.get('model') or decision.selected_model),
            'selected_model': decision.selected_model,
            'budget_decision': decision.data(),
            'usage': usage,
            'cost_microdollars': ledger['cost_microdollars'],
            'cost_usd': ledger['cost_usd'],
            'latency_seconds': round(elapsed, 4),
        }

    async def transcribe_audio(
        self,
        *,
        request_id: str,
        audio_bytes: bytes,
        filename: str = 'voice.wav',
        mime_type: str = 'audio/wav',
        duration_seconds: float | None = None,
        language: str = 'ko',
        prompt_hint: str = '',
        requested_model: str | None = None,
        timeout: float = 30.0,
        at_iso: str | None = None,
    ) -> dict[str, Any]:
        """Verify audio container & duration, reserve budget, and transcribe via OpenAI Audio API."""
        pol = self.governor.policy()
        if not pol.agent_enabled or not pol.cloud_enabled or not pol.audio_cloud_enabled:
            raise OpenAIGatewayError(
                '외부 클라우드 음성 전송(STT)이 비활성화되어 있습니다.',
                'audio_cloud_disabled',
                403,
            )

        verified = verify_audio_payload(
            audio_bytes,
            filename=filename,
            mime_type=mime_type,
            claimed_duration_seconds=duration_seconds,
        )
        verified_seconds = verified['verified_duration_seconds']

        key = self._api_key()
        if not key and self.base_url.startswith('https://'):
            raise OpenAIGatewayError(
                'OPENAI_API_KEY가 설정되지 않아 클라우드 음성 인식을 사용할 수 없습니다.',
                'missing_api_key',
                401,
            )

        decision = self.governor.reserve_budget(
            request_id,
            'stt',
            requested_model,
            estimated_input_tokens=0,
            max_output_tokens=0,
            audio_seconds=max(0.25, verified_seconds),
            note=f'audio_stt_reserve ({verified_seconds}s)',
            at_iso=at_iso,
        )
        if not decision.allowed_cloud:
            raise OpenAIGatewayError(
                '월간 OpenAI API 예산 한도에 도달하여 클라우드 음성 인식을 차단했습니다.',
                'budget_exceeded',
                402,
            )

        headers = {'Accept': 'application/json'}
        if key:
            headers['Authorization'] = f'Bearer {key}'
        data = {
            'model': decision.selected_model,
            'language': language,
            'response_format': 'json',
        }
        if prompt_hint:
            data['prompt'] = prompt_hint[:800]

        start = time.perf_counter()
        try:
            if callable(self.transport) and not hasattr(self.transport, 'handle_async_request'):
                body = await self.transport('POST', self.base_url + '/audio/transcriptions', headers, audio_bytes)
            elif self.transport is None:
                def _urllib_stt() -> dict[str, Any]:
                    boundary = f'----RoomHubBoundary{uuid.uuid4().hex}'
                    parts: list[bytes] = []
                    for field_name, field_val in data.items():
                        parts.append(
                            f'--{boundary}\r\n'
                            f'Content-Disposition: form-data; name="{field_name}"\r\n\r\n'
                            f'{field_val}\r\n'.encode('utf-8')
                        )
                    parts.append(
                        f'--{boundary}\r\n'
                        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                        f'Content-Type: {mime_type}\r\n\r\n'.encode('utf-8')
                        + audio_bytes
                        + f'\r\n--{boundary}--\r\n'.encode('utf-8')
                    )
                    multipart_body = b''.join(parts)
                    req_headers = {**headers, 'Content-Type': f'multipart/form-data; boundary={boundary}'}
                    opener = urllib.request.build_opener(
                        urllib.request.ProxyHandler({}),
                        _NoRedirectHandler(),
                    )
                    req = urllib.request.Request(
                        self.base_url + '/audio/transcriptions',
                        data=multipart_body,
                        headers=req_headers,
                        method='POST',
                    )
                    try:
                        with opener.open(req, timeout=timeout) as resp:
                            raw_bytes = resp.read(MAX_RESPONSE_BYTES + 1)
                    except urllib.error.HTTPError as exc:
                        raise OpenAIGatewayError(f'OpenAI Audio HTTP {exc.code}', 'stt_http_error', exc.code) from None
                    except urllib.error.URLError as exc:
                        if isinstance(getattr(exc, 'reason', None), TimeoutError):
                            raise OpenAIGatewayError('OpenAI 음성 전사 제한 시간을 초과했습니다.', 'timeout') from None
                        raise OpenAIGatewayError('OpenAI 오디오 서버에 연결할 수 없습니다.', 'connection_failed') from None
                    except TimeoutError:
                        raise OpenAIGatewayError('OpenAI 음성 전사 제한 시간을 초과했습니다.', 'timeout') from None
                    return json.loads(raw_bytes.decode('utf-8'))

                body = await asyncio.to_thread(_urllib_stt)
            else:
                import httpx
                try:
                    async with httpx.AsyncClient(
                        transport=self.transport,
                        trust_env=False,
                        follow_redirects=False,
                        timeout=httpx.Timeout(timeout, connect=min(8.0, timeout)),
                    ) as client:
                        res = await client.post(
                            self.base_url + '/audio/transcriptions',
                            headers=headers,
                            data=data,
                            files={'file': (filename, audio_bytes, mime_type)},
                        )
                    if res.status_code != 200:
                        raise OpenAIGatewayError(f'OpenAI Audio HTTP {res.status_code}', 'stt_http_error', res.status_code)
                    body = res.json()
                except OpenAIGatewayError:
                    raise
                except Exception as exc:
                    raise OpenAIGatewayError('OpenAI 음성 전사 요청에 실패했습니다.', 'stt_failed') from exc
        except OpenAIGatewayError as exc:
            self._settle_or_handle_failure(
                request_id=request_id,
                category='stt',
                model=decision.selected_model,
                note=f'audio_stt ({verified_seconds}s)',
                at_iso=at_iso,
                exc=exc,
            )
            raise

        elapsed = time.perf_counter() - start
        usage = extract_usage(body if isinstance(body, dict) else {})
        provider_has_usage = has_usage(body)
        billed_seconds = usage['audio_seconds'] if usage['audio_seconds'] > 0 else verified_seconds
        cost_source = 'provider_usage' if provider_has_usage else 'duration_estimate'

        ledger = self.governor.record_usage(
            request_id=request_id,
            category='stt',
            model=decision.selected_model,
            prompt_tokens=usage['prompt_tokens'],
            cached_tokens=usage['cached_tokens'],
            completion_tokens=usage['completion_tokens'],
            reasoning_tokens=usage['reasoning_tokens'],
            audio_seconds=0.0 if (usage['prompt_tokens'] or usage['completion_tokens']) else max(0.25, billed_seconds),
            cost_source=cost_source,
            note=f'audio_stt ({round(billed_seconds, 2)}s, {cost_source})',
            at_iso=at_iso,
        )

        text = body.get('text') if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise OpenAIGatewayError('전사된 텍스트가 비어 있습니다.', 'empty_transcript', response_body=body)
        text = text.strip()[:16000]

        return {
            'text': text,
            'model': decision.selected_model,
            'duration_seconds': round(billed_seconds, 3),
            'verified_duration_seconds': verified_seconds,
            'cost_source': cost_source,
            'latency_seconds': round(elapsed, 4),
            'cost_microdollars': ledger['cost_microdollars'],
            'cost_usd': ledger['cost_usd'],
            'budget_decision': decision.data(),
        }
