"""Bounded OpenAI API gateway for Chat Completions (Structured Outputs) and Audio STT.

Credentials are read strictly from environment variables (OPENAI_API_KEY,
HUB_OPENAI_API_KEY, or HUB_LLM_API_KEY) and are never persisted to SQLite,
audit logs, or browser responses. Every call passes through BudgetGovernor
before dispatch and records exact token/audio usage in integer microdollars.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from typing import Any
import urllib.error
from urllib.parse import urlsplit
import urllib.request
import uuid

from .openai_budget import BudgetGovernor, UsageCategory, calculate_cost_microdollars

MAX_RESPONSE_BYTES = 512 * 1024
OPENAI_DEFAULT_BASE_URL = 'https://api.openai.com/v1'


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


class OpenAIGatewayError(Exception):
    """Safe user-facing gateway error; never leaks API keys or raw headers."""

    def __init__(self, message: str, code: str = 'openai_error', http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def resolve_openai_api_key() -> str:
    """Read OpenAI API key from server environment variables only."""
    for env_name in ('OPENAI_API_KEY', 'HUB_OPENAI_API_KEY', 'HUB_LLM_API_KEY'):
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


def extract_usage(body: dict[str, Any]) -> dict[str, int]:
    usage = body.get('usage') if isinstance(body.get('usage'), dict) else {}
    prompt_tokens = usage.get('prompt_tokens')
    completion_tokens = usage.get('completion_tokens')
    details = usage.get('prompt_tokens_details') if isinstance(usage.get('prompt_tokens_details'), dict) else {}
    cached_tokens = details.get('cached_tokens')
    comp_details = (
        usage.get('completion_tokens_details')
        if isinstance(usage.get('completion_tokens_details'), dict)
        else {}
    )
    reasoning_tokens = comp_details.get('reasoning_tokens')
    return {
        'prompt_tokens': prompt_tokens if type(prompt_tokens) is int and prompt_tokens >= 0 else 0,
        'cached_tokens': cached_tokens if type(cached_tokens) is int and cached_tokens >= 0 else 0,
        'completion_tokens': completion_tokens if type(completion_tokens) is int and completion_tokens >= 0 else 0,
        'reasoning_tokens': reasoning_tokens if type(reasoning_tokens) is int and reasoning_tokens >= 0 else 0,
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
        has_key = self.key_configured()
        budget = self.governor.summary()
        return {
            'api_key_configured': has_key,
            'base_url': self.base_url,
            'budget_state': budget['state'],
            'cloud_ready': has_key and budget['state'] != 'HARD_CAP_REACHED',
            'spent_usd': budget['spent_usd'],
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
                    msg = labels.get(exc.code, 'OpenAI API 응답 오류입니다.')
                    raise OpenAIGatewayError(f'OpenAI HTTP {exc.code}: {msg}', 'http_error', exc.code) from None
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
                    if res.status_code != 200:
                        msg = labels.get(res.status_code, 'OpenAI API 응답 오류입니다.')
                        raise OpenAIGatewayError(f'OpenAI HTTP {res.status_code}: {msg}', 'http_error', res.status_code)
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in res.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise OpenAIGatewayError('OpenAI 응답이 512KiB 제한을 초과했습니다.', 'response_too_large')
                        chunks.append(chunk)
            raw = b''.join(chunks).decode('utf-8')
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

    async def generate_structured(
        self,
        *,
        request_id: str,
        category: UsageCategory,
        system_prompt: str,
        user_text: str,
        schema_name: str,
        schema: dict[str, Any],
        requested_model: str | None = None,
        max_output_tokens: int = 4096,
        temperature: float = 0.0,
        timeout: float = 60.0,
        force_model: bool = False,
        at_iso: str | None = None,
    ) -> dict[str, Any]:
        """Run a budget-gated OpenAI Structured Outputs call and record exact usage."""
        est_input = max(256, (len(system_prompt) + len(user_text) + len(json.dumps(schema))) // 3)
        decision = self.governor.gate(
            category,
            requested_model,
            estimated_input_tokens=est_input,
            max_output_tokens=max_output_tokens,
            force_model=force_model,
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
        body, raw = await asyncio.wait_for(
            self._post_json('/chat/completions', payload, timeout=timeout),
            timeout + 1.0,
        )
        elapsed = time.perf_counter() - start
        choices = body.get('choices')
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise OpenAIGatewayError('OpenAI 응답에 choices가 없습니다.', 'invalid_response')
        choice = choices[0]
        message = choice.get('message') if isinstance(choice.get('message'), dict) else {}
        if message.get('refusal'):
            raise OpenAIGatewayError('모델이 요청 처리를 거부했습니다.', 'model_refusal')
        content = message.get('content')
        if not isinstance(content, str) or not content.strip():
            raise OpenAIGatewayError('OpenAI 구조화 응답 본문이 비어 있습니다.', 'empty_output')
        if choice.get('finish_reason') != 'stop':
            raise OpenAIGatewayError('응답이 토큰 한도 등으로 완전히 끝나지 않았습니다.', 'incomplete_output')

        try:
            structured = json.loads(content)
        except ValueError as exc:
            raise OpenAIGatewayError('구조화 JSON 파싱에 실패했습니다.', 'invalid_json') from exc

        usage = extract_usage(body)
        ledger = self.governor.record_usage(
            request_id=request_id,
            category=category,
            model=decision.selected_model,
            prompt_tokens=usage['prompt_tokens'],
            cached_tokens=usage['cached_tokens'],
            completion_tokens=usage['completion_tokens'],
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
        duration_seconds: float,
        language: str = 'ko',
        prompt_hint: str = '',
        requested_model: str | None = None,
        timeout: float = 30.0,
        at_iso: str | None = None,
    ) -> dict[str, Any]:
        """Transcribe validated audio via OpenAI Audio API within the $100/month budget."""
        decision = self.governor.gate(
            'stt',
            requested_model,
            estimated_input_tokens=0,
            max_output_tokens=0,
            audio_seconds=max(1.0, duration_seconds),
            at_iso=at_iso,
        )
        if not decision.allowed_cloud:
            raise OpenAIGatewayError(
                '월간 OpenAI API 예산 한도($100)에 도달하여 클라우드 음성 인식을 차단했습니다.',
                'budget_exceeded',
                402,
            )
        key = self._api_key()
        if not key and self.base_url.startswith('https://'):
            raise OpenAIGatewayError(
                'OPENAI_API_KEY가 설정되지 않아 클라우드 음성 인식을 사용할 수 없습니다.',
                'missing_api_key',
                401,
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
                except Exception as exc:
                    raise OpenAIGatewayError('OpenAI 음성 전사 요청에 실패했습니다.', 'stt_failed') from exc
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

        elapsed = time.perf_counter() - start
        text = body.get('text') if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise OpenAIGatewayError('전사된 텍스트가 비어 있습니다.', 'empty_transcript')
        text = text.strip()[:16000]
        ledger = self.governor.record_usage(
            request_id=request_id,
            category='stt',
            model=decision.selected_model,
            audio_seconds=max(0.25, duration_seconds),
            note=f'audio_stt ({round(duration_seconds, 1)}s)',
            at_iso=at_iso,
        )
        return {
            'text': text,
            'model': decision.selected_model,
            'duration_seconds': round(duration_seconds, 2),
            'latency_seconds': round(elapsed, 4),
            'cost_microdollars': ledger['cost_microdollars'],
            'cost_usd': ledger['cost_usd'],
            'budget_decision': decision.data(),
        }
