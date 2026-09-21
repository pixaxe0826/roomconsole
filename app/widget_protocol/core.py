"""Trusted registration, validation and process-scoped replay protection.

This is not an LLM tool registry or a policy broker. HTTP never constructs a
confirmed write authority. Existing authenticated UI endpoints remain unchanged.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from .models import (Capability, ResultMeta, WidgetError, WidgetEvent,
                     WidgetRequest, WidgetResponse)

MAX_REQUEST_BYTES = 64 * 1024


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_digest(request: WidgetRequest) -> str:
    """Bind approval/replay to exact canonical target and arguments, not UI claims."""
    values = request.model_dump(mode='json', exclude={'request_id', 'idempotency_key', 'context'})
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class ExecutionContext:
    """Create only in trusted application code after authentication/policy checks.

    Never deserialize this object from an LLM proposal or HTTP request. Even an
    authenticated HTTP manager gets no confirmed_digest in this release.
    """
    principal: str | None = None
    role: str | None = None
    permissions: frozenset[str] = frozenset()
    confirmed_digest: str | None = None


class ProtocolFault(Exception):
    def __init__(self, status: str, code: str, message: str, field: str | None = None):
        super().__init__(code)
        self.status, self.error = status, WidgetError(code=code, message=message, field=field)


def missing(field: str):
    raise ProtocolFault('needs_clarification', 'MISSING_REQUIRED_ARGUMENT',
                        '필수 값을 명시해 주세요.', field)


def validation_fault(exc: ValidationError, prefix: str, allowed: set[str]):
    # Do not return Pydantic's input/context, model repr, private body or SQL.
    errors = exc.errors(include_input=False, include_context=False, include_url=False)
    first = errors[0]
    loc = first['loc']
    name = str(loc[0]) if loc and str(loc[0]) in allowed else None
    path = '.'.join(x for x in (prefix, name) if x) or None
    if first['type'] == 'missing':
        missing(path)
    raise ProtocolFault('invalid_request', 'INVALID_ARGUMENT', '값의 형식이나 범위를 확인해 주세요.', path)


@dataclass(frozen=True)
class Operation:
    inputs: type[BaseModel]
    outputs: type[BaseModel]
    description: str
    read_only: bool = True
    permission: str = 'read'
    target_types: tuple[str, ...] = ()
    target_required: bool = False
    available: bool = True
    assistant_capability: str | None = None

    def capability(self, action: str) -> Capability:
        return Capability(action=action, description=self.description,
                          input_schema=self.inputs.model_json_schema(),
                          output_schema=self.outputs.model_json_schema(),
                          read_only=self.read_only, requires_confirmation=not self.read_only,
                          permission_level=self.permission, idempotent=self.read_only,
                          idempotency_scope=('read' if self.read_only else 'process') if self.available else 'unavailable',
                          availability='implemented' if self.available else 'unavailable',
                          target_types=list(self.target_types), target_required=self.target_required,
                          assistant_capability=self.assistant_capability)


@dataclass
class AdapterResult:
    data: dict
    changed: bool = False
    source: str | None = None
    event: str | None = None
    entity_id: str | None = None


class WidgetAdapter:
    """Service adapter SPI; callers use WidgetRegistry.execute for the wire boundary."""
    name: str
    source_of_truth: str
    operations: Mapping[str, Operation]
    state_action = 'list'

    def capabilities(self) -> list[Capability]:
        return [spec.capability(action) for action, spec in self.operations.items()]

    def validate_request(self, request: WidgetRequest) -> BaseModel:
        spec = self.operations[request.action]
        target = request.target
        if target and target.value is None:
            missing('target.value')
        if spec.target_required and target is None:
            missing('target')
        if target and target.type not in spec.target_types:
            raise ProtocolFault('invalid_request', 'UNSUPPORTED_TARGET',
                                '이 동작에는 해당 target 형식을 사용할 수 없습니다.', 'target')
        for name, model_field in spec.inputs.model_fields.items():
            if model_field.is_required() and request.args.get(name) is None:
                missing('args.' + name)
        try:
            # Strict JSON validation accepts ISO dates, but not bool/string versions.
            args = spec.inputs.model_validate_json(json.dumps(request.args, allow_nan=False), strict=True)
        except ValidationError as exc:
            validation_fault(exc, 'args', set(spec.inputs.model_fields))
        self.validate_slots(request, args)
        return args

    def validate_slots(self, request: WidgetRequest, args: BaseModel) -> None:
        pass

    @staticmethod
    def authorize(request: WidgetRequest, authority: ExecutionContext, spec: Operation):
        if not authority.principal or authority.role != 'admin' or spec.permission not in authority.permissions:
            raise ProtocolFault('permission_denied', 'PERMISSION_DENIED', '신뢰된 관리자 실행 권한이 필요합니다.')
        if not spec.available:
            raise ProtocolFault('unavailable', 'CAPABILITY_UNAVAILABLE', '이 capability는 아직 연결하지 않았습니다.')
        if not spec.read_only and authority.confirmed_digest != request_digest(request):
            raise ProtocolFault('needs_confirmation', 'CONFIRMATION_REQUIRED',
                                '신뢰된 서버의 요청별 확인이 필요합니다. context.user_confirmed는 권한이 아닙니다.')

    async def execute(self, request: WidgetRequest, authority: ExecutionContext) -> AdapterResult:
        """Guarded SPI; registry adds uniform errors, output validation, replay and audit."""
        spec = self.operations[request.action]
        args = self.validate_request(request)
        self.authorize(request, authority, spec)
        return await self._execute(request, args, authority)

    async def _execute(self, request: WidgetRequest, args: BaseModel, authority: ExecutionContext) -> AdapterResult:
        raise NotImplementedError

    async def get_state(self, request: WidgetRequest, authority: ExecutionContext) -> AdapterResult:
        # Reuse the same read operation/guard, never a second state cache.
        if request.action != self.state_action:
            raise ProtocolFault('invalid_request', 'INVALID_STATE_ACTION', '기본 읽기 동작을 사용하세요.')
        return await self.execute(request, authority)

    async def health(self, authority: ExecutionContext) -> dict:
        raise NotImplementedError


class WidgetRegistry:
    def __init__(self, *, audit: Callable[[dict], None] | None = None, max_receipts: int = 512):
        self._adapters: dict[str, WidgetAdapter] = {}
        self._receipts: dict[tuple[str, str], tuple[str, WidgetResponse]] = {}
        self._lock = asyncio.Lock()
        self._audit = audit
        self._max_receipts = max_receipts

    def register(self, adapter: WidgetAdapter) -> None:
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', adapter.name) or adapter.name in self._adapters:
            raise ValueError('Invalid or duplicate adapter name')
        for action, spec in adapter.operations.items():
            if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', action):
                raise ValueError('Invalid action name')
            if (spec.read_only and spec.permission != 'read') or (not spec.read_only and spec.permission == 'read'):
                raise ValueError('Inconsistent read/write metadata')
            spec.capability(action)  # Validate metadata at startup, not on a user request.
        self._adapters[adapter.name] = adapter

    def get(self, name: str) -> WidgetAdapter | None:
        return self._adapters.get(name)

    def list_widgets(self) -> list[str]:
        return list(self._adapters)

    def get_capabilities(self, name: str) -> list[Capability]:
        adapter = self.get(name)
        return adapter.capabilities() if adapter else []

    def manifest(self) -> dict:
        return {'protocol_version': '1.0', 'http_write_execution': False,
                'widgets': [{'name': a.name, 'adapter': type(a).__name__, 'source_of_truth': a.source_of_truth,
                             'capabilities': [c.model_dump(mode='json') for c in a.capabilities()]}
                            for a in self._adapters.values()]}

    async def execute(self, raw: WidgetRequest | dict, authority: ExecutionContext) -> WidgetResponse:
        started = time.perf_counter()
        request = adapter = spec = None
        response = None
        # Mutation lock spans receipt reservation through outcome recording, including cancellation.
        locked = False
        key = digest = None
        try:
            if isinstance(raw, WidgetRequest):
                raw = raw.model_dump(mode='json')  # Revalidate even constructed/mutated models.
            encoded = json.dumps(raw, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode()) > MAX_REQUEST_BYTES:
                raise ProtocolFault('invalid_request', 'REQUEST_TOO_LARGE', '요청은 64KiB 이하로 제한합니다.')
            if isinstance(raw, dict) and raw.get('protocol_version', '1.0') != '1.0':
                raise ProtocolFault('invalid_request', 'UNSUPPORTED_PROTOCOL_VERSION',
                                    '지원하는 protocol_version은 1.0입니다.', 'protocol_version')
            try:
                request = WidgetRequest.model_validate_json(encoded, strict=True)
            except ValidationError as exc:
                validation_fault(exc, '', set(WidgetRequest.model_fields))
            # No request/session/source field can confer principal/permissions.
            if not authority.principal or authority.role != 'admin':
                raise ProtocolFault('permission_denied', 'PERMISSION_DENIED', '관리자 실행 권한이 필요합니다.')
            adapter = self.get(request.widget)
            if adapter is None:
                raise ProtocolFault('not_found', 'WIDGET_NOT_FOUND', '등록된 서비스가 없습니다.', 'widget')
            spec = adapter.operations.get(request.action)
            if spec is None:
                raise ProtocolFault('invalid_request', 'UNSUPPORTED_ACTION', '지원하지 않는 capability입니다.', 'action')
            adapter.validate_request(request)
            adapter.authorize(request, authority, spec)
            if not spec.read_only:
                await self._lock.acquire()
                locked = True
                key = (authority.principal, request.idempotency_key or request.request_id)
                digest = request_digest(request)
                old = self._receipts.get(key)
                if old:
                    if old[0] != digest:
                        raise ProtocolFault('conflict', 'IDEMPOTENCY_CONFLICT', '같은 요청 키의 내용이 달라졌습니다.')
                    response = old[1].model_copy(deep=True)
                    response.request_id = request.request_id
                    response.meta.duplicate = True
                    response.meta.events = []
                    if not response.meta.outcome_uncertain:
                        response.meta.changed = False
                elif len(self._receipts) >= self._max_receipts:
                    raise ProtocolFault('unavailable', 'RECEIPT_CAPACITY', '중복 방지 기록 용량을 확인하세요.')
            if response is None:
                try:
                    result = await adapter.execute(request, authority)
                    # Bad service output is NOT a caller validation error or fabricated success.
                    data = spec.outputs.model_validate(result.data).model_dump(mode='json')
                    if spec.read_only and result.changed:
                        raise RuntimeError('Read-only adapter reported a mutation')
                    response = WidgetResponse(request_id=request.request_id, widget=request.widget,
                        action=request.action, status='success', data=data,
                        meta=ResultMeta(adapter=type(adapter).__name__,
                            source_of_truth=result.source or adapter.source_of_truth,
                            changed=result.changed, executed_at=now(),
                            idempotency_scope='none' if spec.read_only else 'process'))
                    if result.event and result.changed:
                        response.meta.events.append(WidgetEvent(event=result.event, widget=request.widget,
                            entity_id=result.entity_id, request_id=request.request_id, timestamp=response.meta.executed_at))
                except (ProtocolFault, HTTPException):
                    raise
                except asyncio.CancelledError:
                    if key:
                        self._receipts[key] = (digest, self._uncertain(request, adapter))
                    raise
                except Exception:
                    response = self._uncertain(request, adapter) if key else self.failure(
                        request, 'error', 'ADAPTER_ERROR', '실제 저장소 조회에 실패했습니다. 내용을 추측하지 않았습니다.', adapter)
                if key:
                    self._receipts[key] = (digest, response.model_copy(deep=True))
        except ProtocolFault as exc:
            response = self.failure(request, exc.status, exc.error.code, exc.error.message, adapter, exc.error.field)
            if request and exc.status == 'needs_confirmation':
                response.meta.request_digest = request_digest(request)
        except HTTPException as exc:
            status, code = {404: ('not_found', 'NOT_FOUND'), 409: ('conflict', 'VERSION_CONFLICT'),
                            401: ('permission_denied', 'PERMISSION_DENIED'), 403: ('permission_denied', 'PERMISSION_DENIED'),
                            422: ('invalid_request', 'INVALID_ARGUMENT'), 503: ('unavailable', 'SERVICE_UNAVAILABLE')}.get(
                                exc.status_code, ('error', 'SERVICE_ERROR'))
            response = self.failure(request, status, code, '기존 서비스가 요청을 거부했습니다. 대상과 값을 확인하세요.', adapter)
        except (ValueError, TypeError, RecursionError):
            response = self.failure(request, 'invalid_request', 'INVALID_REQUEST', '유효한 JSON 요청을 사용하세요.', adapter)
        finally:
            if locked:
                self._lock.release()
        response.meta.latency_ms = round((time.perf_counter() - started) * 1000, 3)
        if request is None and isinstance(raw, dict):
            # Correlate malformed requests only with bounded identifier fields.
            for name in ('request_id', 'widget', 'action'):
                value = raw.get(name)
                if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value):
                    setattr(response, name, value)
        if self._audit:
            record = {'request_id': response.request_id, 'widget': response.widget, 'action': response.action,
                      'adapter': response.meta.adapter, 'source_of_truth': response.meta.source_of_truth,
                      'status': response.status, 'latency_ms': response.meta.latency_ms,
                      'changed': response.meta.changed, 'error_code': response.error.code if response.error else None,
                      'duplicate': response.meta.duplicate,
                      'args': {'fields': sorted(set(request.args) & set(spec.inputs.model_fields)) if request and spec else [],
                               'values_redacted': True}}
            try:
                self._audit(record)
            except Exception:
                response.meta.warnings.append('AUDIT_UNAVAILABLE')  # Never reexecute a committed write.
        return response

    @staticmethod
    def failure(request, status, code, message, adapter=None, field=None):
        return WidgetResponse(request_id=request.request_id if request else None,
            widget=request.widget if request else None, action=request.action if request else None,
            status=status, error=WidgetError(code=code, message=message, field=field),
            meta=ResultMeta(adapter=type(adapter).__name__ if adapter else None,
                            source_of_truth=adapter.source_of_truth if adapter else None))

    def _uncertain(self, request, adapter):
        response = self.failure(request, 'error', 'OUTCOME_UNCERTAIN',
            '쓰기 결과를 확정할 수 없습니다. 자동 재시도하지 말고 실제 상태를 확인하세요.', adapter)
        response.meta.changed = None
        response.meta.outcome_uncertain = True
        response.meta.idempotency_scope = 'process'
        return response
