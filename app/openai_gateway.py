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
import hashlib
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

from .openai_budget import ALLOWED_CHAT_MODELS, ALLOWED_STT_MODELS, BudgetGovernor, UsageCategory

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
    return isinstance(body, dict) and extract_usage(body)['usage_kind'] != 'unknown'


def extract_usage(body: dict[str, Any]) -> dict[str, Any]:
    """Accept a complete provider usage pair; missing fields are never zero spend.

    Optional subset counters may be absent (provider default zero), but invalid,
    inconsistent, or partial counters leave the full reservation unsettled.
    """
    usage = body.get('usage') if isinstance(body.get('usage'), dict) else {}
    unknown = dict.fromkeys(('prompt_tokens', 'cached_tokens', 'completion_tokens',
                            'reasoning_tokens', 'cache_write_30m_tokens',
                            'cache_write_12h_tokens', 'audio_seconds'))
    unknown['usage_kind'] = 'unknown'
    zeros = {k: 0 for k in unknown if k != 'usage_kind'}
    if usage.get('type') == 'duration':
        seconds = usage.get('seconds')
        if type(seconds) in (int, float) and 0 < seconds <= MAX_AUDIO_DURATION_SECONDS and math.isfinite(seconds):
            return zeros | {'audio_seconds': float(seconds), 'usage_kind': 'duration'}
        return unknown
    if 'prompt_tokens' in usage or 'completion_tokens' in usage:
        prompt, completion = usage.get('prompt_tokens'), usage.get('completion_tokens')
    else:
        prompt, completion = usage.get('input_tokens'), usage.get('output_tokens')
    if any(type(n) is not int or n < 0 for n in (prompt, completion)) or prompt + completion == 0:
        return unknown
    total = usage.get('total_tokens', prompt + completion)
    if type(total) is not int or total != prompt + completion:
        return unknown
    details = usage.get('prompt_tokens_details', usage.get('input_tokens_details', {}))
    comp_details = usage.get('completion_tokens_details', usage.get('output_tokens_details', {}))
    if not isinstance(details, dict) or not isinstance(comp_details, dict):
        return unknown
    cached = details.get('cached_tokens', 0)
    writes = details.get('cache_write_tokens', 0)
    write_details = details.get('cache_write_tokens_details', {'ttl_30m_tokens': writes, 'ttl_12h_tokens': 0})
    reasoning = comp_details.get('reasoning_tokens', 0)
    if not isinstance(write_details, dict):
        return unknown
    writes30, writes12 = write_details.get('ttl_30m_tokens'), write_details.get('ttl_12h_tokens')
    if any(type(n) is not int or n < 0 for n in (cached, writes, writes30, writes12, reasoning)):
        return unknown
    if writes30 + writes12 != writes or cached + writes > prompt or reasoning > completion:
        return unknown
    return {
        'usage_kind': 'tokens', 'prompt_tokens': prompt, 'completion_tokens': completion,
        'cached_tokens': cached, 'reasoning_tokens': reasoning,
        'cache_write_30m_tokens': writes30, 'cache_write_12h_tokens': writes12,
        'audio_seconds': 0.0,
    }


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


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
    if riff_size + 8 != len(blob):
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
            if fmt_found or chunk_size < 16:
                raise ValueError('WAV fmt 청크가 손상되었습니다.')
            audio_fmt, channels, sample_rate, byte_rate, block_align, bits_per_sample = struct.unpack_from(
                '<HHIIHH', blob, body_start
            )
            if audio_fmt not in {1, 3} or channels not in {1, 2} or sample_rate < 8000 or sample_rate > 96000:
                raise ValueError('지원하지 않는 WAV 오디오 포맷입니다.')
            if (audio_fmt == 1 and bits_per_sample not in {8, 16, 24, 32}) or (
                audio_fmt == 3 and bits_per_sample not in {32, 64}
            ):
                raise ValueError('지원하지 않는 WAV 샘플 크기입니다.')
            expected_align = channels * (bits_per_sample // 8)
            if block_align <= 0 or expected_align != block_align or byte_rate != sample_rate * block_align:
                raise ValueError('WAV 프레임 정렬/바이트율 정보가 일치하지 않습니다.')
            fmt_found = True
        elif chunk_id == b'data':
            if data_found or not fmt_found:
                raise ValueError('WAV 데이터 청크는 fmt 이후 하나만 허용됩니다.')
            data_size = chunk_size
            data_found = True
        offset = body_end + (chunk_size % 2)

    if offset != len(blob):
        raise ValueError('WAV 청크 정렬 또는 파일 끝이 일치하지 않습니다.')
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

    def _reserve_call(self, *args, **kwargs):
        try:
            decision = self.governor.reserve_budget(*args, **kwargs)
        except ValueError as exc:
            raise OpenAIGatewayError('과금 요청 ID 또는 요청 매개변수가 일치하지 않습니다.',
                                     'request_conflict', 409) from exc
        if decision.reason == 'request_already_reserved':
            raise OpenAIGatewayError('이미 전송했거나 처리 중인 요청입니다. 자동으로 재전송하지 않습니다.',
                                     'request_already_dispatched', 409)
        if not decision.allowed_cloud:
            raise OpenAIGatewayError('월간 OpenAI API 예산 한도에 도달하여 클라우드 호출을 차단했습니다.',
                                     'budget_exceeded', 402)
        if not decision.reservation_acquired:
            raise OpenAIGatewayError('과금 예약의 전송 권한을 확보하지 못했습니다.',
                                     'reservation_not_acquired', 409)
        return decision

    def _account_response(self, body: Any, *, request_id: str, category: UsageCategory,
                          model: str, note: str, at_iso: str | None,
                          cost_source: str = 'provider_usage') -> dict[str, Any] | None:
        """Settle only priced, complete provider usage, including paid failures."""
        usage = extract_usage(body if isinstance(body, dict) else {})
        expected_kind = 'duration' if model == 'whisper-1' else 'tokens'
        actual_tier = body.get('service_tier') if isinstance(body, dict) else None
        actual_model = body.get('model') if isinstance(body, dict) else None
        if (usage['usage_kind'] != expected_kind or actual_tier not in {None, 'default'}
                or (actual_model and actual_model != model and not str(actual_model).startswith(model + '-'))):
            return None
        try:
            return self.governor.record_usage(
                request_id=request_id, category=category, model=model,
                **{k: v for k, v in usage.items() if k != 'usage_kind'},
                cost_source=cost_source, note=note, at_iso=at_iso,
            )
        except ValueError:
            # A new/unpriced billing component must not silently free the reserve.
            return None

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
            raw = json.dumps(body, ensure_ascii=False, allow_nan=False)
            if len(raw.encode('utf-8')) > MAX_RESPONSE_BYTES:
                raise OpenAIGatewayError('OpenAI 응답이 512KiB 제한을 초과했습니다.', 'response_too_large')
            if not isinstance(body, dict):
                raise OpenAIGatewayError('OpenAI 응답은 JSON 객체여야 합니다.', 'invalid_response')
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
        if self._account_response(exc.response_body, request_id=request_id,
                                  category=category, model=model,
                                  cost_source='provider_usage_on_error',
                                  note=f'{note} [error:{exc.code}]', at_iso=at_iso) is not None:
            return
        if isinstance(exc.response_body, dict) and isinstance(exc.response_body.get('usage'), dict):
            self.governor.mark_reservation_unknown(request_id, 'incomplete_error_usage')
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

        chosen_model = requested_model or (pol.deep_model if category in {'agent_deep', 'briefing'} else pol.fast_model)
        if chosen_model not in ALLOWED_CHAT_MODELS or chosen_model == 'o3-mini':
            raise OpenAIGatewayError('지원하지 않거나 종료된 플래너 모델입니다. 모델 설정을 확인하세요.',
                                     'unsupported_model', 400)
        # UTF-8 bytes plus framing/schema overhead form a conservative reservation
        # bound for byte-level tokenization. This is NOT measured provider usage.
        prompt_material = {'system': system_prompt, 'user': user_text, 'schema': schema, 'name': schema_name}
        est_input = len(json.dumps(prompt_material, ensure_ascii=False).encode('utf-8')) + 2048
        if est_input > 272_000:
            raise OpenAIGatewayError('예약 가능한 입력 크기를 초과했습니다.', 'input_too_large', 413)
        decision = self._reserve_call(
            request_id,
            category,
            requested_model,
            estimated_input_tokens=est_input,
            max_output_tokens=max_output_tokens,
            force_model=force_model,
            note=schema_name,
            at_iso=at_iso,
            request_fingerprint=_fingerprint(prompt_material | {
                'requested_model': chosen_model, 'temperature': temperature,
                'reasoning_effort': pol.reasoning_effort, 'max_output_tokens': max_output_tokens,
                'base_url': self.base_url,
            }),
        )

        is_reasoning_model = decision.selected_model.startswith(('gpt-6', 'gpt-5', 'o'))
        payload: dict[str, Any] = {
            'model': decision.selected_model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_text},
            ],
            'stream': False,
            'service_tier': 'default',
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
            if decision.selected_model in {'gpt-6.1-sol', 'gpt-6.1-terra'}:
                payload['prompt_cache_options'] = {'mode': 'implicit', 'ttl': '30m'}
        else:
            payload['max_tokens'] = decision.max_output_tokens
            payload['temperature'] = temperature

        start = time.perf_counter()
        try:
            body, raw = await asyncio.wait_for(
                self._post_json('/chat/completions', payload, timeout=timeout),
                timeout + 1.0,
            )
        except asyncio.CancelledError:
            self.governor.mark_reservation_unknown(request_id, 'cancelled_after_dispatch')
            raise
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
        ledger = self._account_response(body, request_id=request_id, category=category,
                                        model=decision.selected_model, note=f'{schema_name} ({decision.state})',
                                        at_iso=at_iso)
        if ledger is None:
            self.governor.mark_reservation_unknown(request_id, 'missing_or_incomplete_provider_usage')

        try:
            if isinstance(body.get('error'), dict):
                raise OpenAIGatewayError('OpenAI API 오류 응답입니다.', 'api_error', 400, response_body=body)
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

        return {
            'parsed': structured,
            'raw_output': content,
            'raw_response': raw,
            'model': str(body.get('model') or decision.selected_model),
            'selected_model': decision.selected_model,
            'budget_decision': decision.data(),
            'usage': usage,
            'cost_microdollars': ledger['cost_microdollars'] if ledger else None,
            'cost_usd': ledger['cost_usd'] if ledger else None,
            'cost_known': ledger is not None,
            'cost_source': 'provider_usage' if ledger else 'unknown_unsettled',
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
        if not isinstance(language, str):
            raise OpenAIGatewayError('전사 언어는 두 글자 언어 코드 또는 auto여야 합니다.',
                                     'invalid_language', 422)
        language = language.strip().lower()
        if language != 'auto' and (len(language) != 2 or not language.isascii() or not language.isalpha()):
            raise OpenAIGatewayError('전사 언어는 두 글자 언어 코드 또는 auto여야 합니다.',
                                     'invalid_language', 422)

        verified = verify_audio_payload(
            audio_bytes,
            filename=filename,
            mime_type=mime_type,
            claimed_duration_seconds=duration_seconds,
        )
        verified_seconds = verified['verified_duration_seconds']
        if verified['duration_source'] != 'wav_header':
            raise OpenAIGatewayError('클라우드 전사에는 서버에서 길이를 확인한 WAV가 필요합니다.',
                                     'unverified_audio_duration', 422)
        chosen_model = requested_model or pol.stt_model
        if chosen_model not in ALLOWED_STT_MODELS:
            raise OpenAIGatewayError('지원하지 않는 음성 전사 모델입니다.', 'unsupported_model', 400)

        key = self._api_key()
        if not key and self.base_url.startswith('https://'):
            raise OpenAIGatewayError(
                'OPENAI_API_KEY가 설정되지 않아 클라우드 음성 인식을 사용할 수 없습니다.',
                'missing_api_key',
                401,
            )

        decision = self._reserve_call(
            request_id,
            'stt',
            requested_model,
            estimated_input_tokens=0,
            max_output_tokens=0,
            audio_seconds=max(0.25, verified_seconds),
            note=f'audio_stt_reserve ({verified_seconds}s)',
            at_iso=at_iso,
            request_fingerprint=_fingerprint({
                'sha256': hashlib.sha256(audio_bytes).hexdigest(), 'language': language,
                'prompt_hint': prompt_hint[:800], 'model': chosen_model, 'base_url': self.base_url,
            }),
        )

        headers = {'Accept': 'application/json'}
        if key:
            headers['Authorization'] = f'Bearer {key}'
        data = {
            'model': decision.selected_model,
            'response_format': 'json',
        }
        # The API auto-detects only when language is omitted; "auto" is a
        # Room Hub setting, not an ISO-639-1 code accepted by the provider.
        if language != 'auto':
            data['language'] = language
        if prompt_hint:
            data['prompt'] = prompt_hint[:800]

        start = time.perf_counter()
        try:
            if callable(self.transport) and not hasattr(self.transport, 'handle_async_request'):
                body = await asyncio.wait_for(
                    self.transport('POST', self.base_url + '/audio/transcriptions', headers, audio_bytes), timeout)
                if len(json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')) > MAX_RESPONSE_BYTES:
                    raise OpenAIGatewayError('OpenAI 응답이 512KiB 제한을 초과했습니다.', 'response_too_large')
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
                        f'Content-Disposition: form-data; name="file"; filename="voice.wav"\r\n'
                        f'Content-Type: audio/wav\r\n\r\n'.encode('utf-8')
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
                        error_body = None
                        try:
                            error_raw = exc.read(MAX_RESPONSE_BYTES)
                            error_body = json.loads(error_raw.decode('utf-8'))
                        except (UnicodeError, ValueError):
                            pass
                        raise OpenAIGatewayError(f'OpenAI Audio HTTP {exc.code}', 'stt_http_error', exc.code,
                                                 response_body=error_body if isinstance(error_body, dict) else None) from None
                    except urllib.error.URLError as exc:
                        if isinstance(getattr(exc, 'reason', None), TimeoutError):
                            raise OpenAIGatewayError('OpenAI 음성 전사 제한 시간을 초과했습니다.', 'timeout') from None
                        raise OpenAIGatewayError('OpenAI 오디오 서버에 연결할 수 없습니다.', 'connection_failed') from None
                    except TimeoutError:
                        raise OpenAIGatewayError('OpenAI 음성 전사 제한 시간을 초과했습니다.', 'timeout') from None
                    if len(raw_bytes) > MAX_RESPONSE_BYTES:
                        raise OpenAIGatewayError('OpenAI 응답이 512KiB 제한을 초과했습니다.', 'response_too_large')
                    return json.loads(raw_bytes.decode('utf-8'), parse_constant=lambda _x: (_ for _ in ()).throw(ValueError('nonfinite')))

                body = await asyncio.wait_for(asyncio.to_thread(_urllib_stt), timeout + 1.0)
            else:
                import httpx
                try:
                    async with httpx.AsyncClient(
                        transport=self.transport,
                        trust_env=False,
                        follow_redirects=False,
                        timeout=httpx.Timeout(timeout, connect=min(8.0, timeout)),
                    ) as client:
                        async with client.stream(
                            'POST',
                            self.base_url + '/audio/transcriptions',
                            headers=headers,
                            data=data,
                            files={'file': ('voice.wav', audio_bytes, 'audio/wav')},
                        ) as res:
                            chunks, size = [], 0
                            async for chunk in res.aiter_bytes():
                                size += len(chunk)
                                if size > MAX_RESPONSE_BYTES:
                                    raise OpenAIGatewayError('OpenAI 응답이 512KiB 제한을 초과했습니다.', 'response_too_large')
                                chunks.append(chunk)
                            raw_bytes = b''.join(chunks)
                            if res.status_code != 200:
                                error_body = None
                                try:
                                    error_body = json.loads(raw_bytes.decode('utf-8'))
                                except (UnicodeError, ValueError):
                                    pass
                                raise OpenAIGatewayError(f'OpenAI Audio HTTP {res.status_code}', 'stt_http_error', res.status_code,
                                                         response_body=error_body if isinstance(error_body, dict) else None)
                            body = json.loads(raw_bytes.decode('utf-8'), parse_constant=lambda _x: (_ for _ in ()).throw(ValueError('nonfinite')))
                except OpenAIGatewayError:
                    raise
                except Exception as exc:
                    raise OpenAIGatewayError('OpenAI 음성 전사 요청에 실패했습니다.', 'stt_failed') from exc
        except asyncio.CancelledError:
            self.governor.mark_reservation_unknown(request_id, 'cancelled_after_dispatch')
            raise
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
        except Exception as exc:
            self.governor.mark_reservation_unknown(request_id, 'stt_transport_or_response_failure')
            raise OpenAIGatewayError('음성 전사 응답을 확인하지 못했습니다. 자동 재전송하지 않습니다.',
                                     'stt_failed') from exc

        elapsed = time.perf_counter() - start
        usage = extract_usage(body if isinstance(body, dict) else {})
        ledger = self._account_response(body, request_id=request_id, category='stt',
                                        model=decision.selected_model, note=f'audio_stt ({verified_seconds}s)',
                                        at_iso=at_iso)
        cost_source = 'provider_usage' if ledger else 'unknown_unsettled'
        if ledger is None:
            self.governor.mark_reservation_unknown(request_id, 'missing_or_incomplete_stt_usage')

        text = body.get('text') if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise OpenAIGatewayError('전사된 텍스트가 비어 있습니다.', 'empty_transcript', response_body=body)
        text = text.strip()[:16000]

        return {
            'text': text,
            'model': decision.selected_model,
            'duration_seconds': verified_seconds,
            'verified_duration_seconds': verified_seconds,
            'cost_source': cost_source,
            'latency_seconds': round(elapsed, 4),
            'usage': usage,
            'cost_microdollars': ledger['cost_microdollars'] if ledger else None,
            'cost_usd': ledger['cost_usd'] if ledger else None,
            'cost_known': ledger is not None,
            'budget_decision': decision.data(),
        }
