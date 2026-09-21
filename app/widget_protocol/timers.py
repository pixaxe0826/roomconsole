"""Timer Protocol 1.0: bounded, immediately authorized countdown controls only."""
from dataclasses import dataclass
import asyncio
from types import MappingProxyType
from typing import Literal
from pydantic import Field
from .core import Operation, WidgetAdapter, AdapterResult, ProtocolFault
from .models import Contract
from .schemas import Empty
from ..timers import TimerInput, TimerStart, TimerStop


class TimerData(Contract):
    id: str
    label: str
    duration_seconds: int = Field(ge=1, le=600)
    state: Literal['running', 'stopped', 'expired']
    started_at: str
    deadline_at: str
    ended_at: str | None
    remaining_seconds: int = Field(ge=0, le=600)
    version: int = Field(ge=1)


class TimerResult(TimerData):
    changed: bool
    duplicate: bool


class TimerList(Contract):
    revision: int
    server_time: str
    items: list[TimerData]
    current_id: str | None
    active_count: int
    max_active: int
    scheduler_error: bool
    delivery: Literal['foreground_browser_only']


class StopArgs(Contract):
    version: int | None = Field(default=None, ge=1)


@dataclass(frozen=True)
class TimerOperation(Operation):
    def capability(self, action):
        result = super().capability(action)
        # No weakening of any other adapter. Countdown operations have no external effect.
        result.requires_confirmation = False
        return result


class TimerAdapter(WidgetAdapter):
    name = 'timer'
    source_of_truth = 'room_hub_sqlite.hub_timers'
    operations = MappingProxyType({
        'list': TimerOperation(Empty, TimerList, '실제 타이머 목록과 현재 타이머 조회'),
        'get': TimerOperation(Empty, TimerData, '실제 타이머 한 개 조회', target_types=('item_id', 'reference'), target_required=True),
        'start': TimerOperation(TimerInput, TimerResult, '1~600초 타이머 즉시 시작', False, 'control'),
        'stop': TimerOperation(StopArgs, TimerResult, '특정 또는 최근 시작한 실행 중 타이머 즉시 종료', False, 'control', ('item_id', 'reference'), True),
    })

    def __init__(self, timers):
        self.timers = timers

    def validate_slots(self, request, args):
        if request.target and request.target.type == 'reference' and request.target.value != 'current':
            raise ProtocolFault('needs_clarification', 'CONTEXT_NOT_RESOLVED', 'current 또는 실제 타이머 ID를 지정하세요.', 'target')
        if request.target and request.target.type == 'item_id' and request.target.value == 'current':
            raise ProtocolFault('invalid_request', 'INVALID_TARGET', 'current는 reference 형식으로 지정하세요.', 'target')

    @staticmethod
    def authorize(request, authority, spec):
        if not authority.principal or authority.role != 'admin' or spec.permission not in authority.permissions:
            raise ProtocolFault('permission_denied', 'PERMISSION_DENIED', '신뢰된 타이머 실행 권한이 필요합니다.')
        # Timer only: existing HTTP admin auth + server policy authorize an immediate
        # bounded start/stop. context.user_confirmed never grants authority.

    async def execute(self, request, authority):
        args = self.validate_request(request)
        spec = self.operations[request.action]
        self.authorize(request, authority, spec)
        if request.action == 'list':
            return AdapterResult(await asyncio.to_thread(self.timers.snapshot), source=self.source_of_truth)
        if request.action == 'get':
            identity = request.target.value
            if request.target.type == 'reference':
                identity = (await asyncio.to_thread(self.timers.snapshot))['current_id']
                if identity is None:
                    raise ProtocolFault('not_found', 'NOT_FOUND', '실행 중인 타이머가 없습니다.', 'target')
            return AdapterResult(await asyncio.to_thread(self.timers.get, identity), source=self.source_of_truth)
        key = request.idempotency_key or request.request_id
        if request.action == 'start':
            data = await asyncio.to_thread(self.timers.start, TimerStart(request_id=key, **args.model_dump()), authority.principal)
            event = 'timer.started'
        else:
            data = await asyncio.to_thread(self.timers.stop, request.target.value,
                TimerStop(request_id=key, **args.model_dump()), authority.principal)
            event = 'timer.stopped'
        if data['changed']:
            await self.timers.announce(event, data['id'])
        return AdapterResult(data, changed=data['changed'], source=self.source_of_truth, event=event, entity_id=data['id'])

    async def get_state(self, authority):
        if not authority.principal or authority.role != 'admin' or 'read' not in authority.permissions:
            raise ProtocolFault('permission_denied', 'PERMISSION_DENIED', '읽기 권한이 필요합니다.')
        return await asyncio.to_thread(self.timers.snapshot)

    async def health(self, authority):
        await self.get_state(authority)
        return {'available': True, 'source_of_truth': self.source_of_truth,
                'scheduler_error': self.timers.scheduler_error}
