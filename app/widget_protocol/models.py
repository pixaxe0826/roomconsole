"""Versioned wire contracts. Caller-supplied context is never execution authority."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, JsonValue

PROTOCOL_VERSION = '1.0'
Permission = Literal['read', 'write', 'control', 'dangerous']
Status = Literal['success', 'needs_confirmation', 'needs_clarification', 'not_found',
                 'invalid_request', 'permission_denied', 'conflict', 'unavailable',
                 'error', 'accepted', 'running']


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class WidgetTarget(Contract):
    type: Literal['item_id', 'widget_id', 'reference']
    value: str | None = Field(default=None, min_length=1, max_length=128)


class WidgetContext(Contract):
    session_id: str | None = Field(default=None, max_length=128)
    source: str = Field(default='api', pattern=r'^[A-Za-z0-9_.:-]{1,64}$')
    user_confirmed: bool = False  # A claim, not authorization; intentionally ignored.
    active_widget: str | None = Field(default=None, max_length=128)
    selected_item: str | None = Field(default=None, max_length=128)
    last_action: str | None = Field(default=None, max_length=128)
    date_ref: Literal['today', 'tomorrow'] | None = None
    period: Literal['morning', 'afternoon'] | None = None


class WidgetRequest(Contract):
    protocol_version: Literal['1.0'] = PROTOCOL_VERSION
    request_id: str = Field(pattern=r'^[A-Za-z0-9_.:-]{1,128}$')
    idempotency_key: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_.:-]{1,128}$')
    widget: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    action: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    target: WidgetTarget | None = None
    args: dict[str, JsonValue] = Field(default_factory=dict)
    context: WidgetContext = Field(default_factory=WidgetContext)
    extensions: dict[str, JsonValue] = Field(default_factory=dict)


class WidgetError(Contract):
    code: str
    message: str
    field: str | None = None
    retryable: bool = False


class WidgetEvent(Contract):
    protocol_version: Literal['1.0'] = PROTOCOL_VERSION
    event: str
    widget: str
    entity_id: str | None = None
    request_id: str
    data: dict[str, JsonValue] = Field(default_factory=dict)
    timestamp: str


class ResultMeta(Contract):
    adapter: str | None = None
    source_of_truth: str | None = None
    changed: bool | None = False  # None only when a write's outcome is uncertain.
    executed_at: str | None = None
    latency_ms: float = 0.0
    duplicate: bool = False
    idempotency_scope: Literal['none', 'process'] = 'none'
    outcome_uncertain: bool = False
    request_digest: str | None = None
    events: list[WidgetEvent] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class WidgetResponse(Contract):
    protocol_version: Literal['1.0'] = PROTOCOL_VERSION
    request_id: str | None = None  # Missing/invalid envelope IDs are not invented.
    status: Status
    widget: str | None = None
    action: str | None = None
    data: dict[str, JsonValue] | None = None
    error: WidgetError | None = None
    meta: ResultMeta = Field(default_factory=ResultMeta)


class Capability(Contract):
    action: str
    description: str
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]
    read_only: bool
    requires_confirmation: bool
    permission_level: Permission
    idempotent: bool
    idempotency_scope: Literal['read', 'process', 'unavailable']
    availability: Literal['implemented', 'unavailable'] = 'implemented'
    target_types: list[str] = Field(default_factory=list)
    target_required: bool = False
    assistant_capability: str | None = None
