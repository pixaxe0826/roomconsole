"""Extended WidgetAdapter implementations covering all Room Hub widgets and systems.

Conforms strictly to WidgetProtocol 1.0 (WidgetAdapter, Operation, Contract,
AdapterResult, ProtocolFault, WidgetRequest, WidgetResponse, ExecutionContext)
so the Personal AI Agent interacts with every widget and subsystem through a
single unified adapter protocol:
- memo (widgets/note): read, list, write, append, clear, delete
- todo (widgets/todos, widgets/all-todos): list, get, add, update, complete, reopen, delete
- calendar (widgets/calendar): list, get, add, update, delete
- alarm (widgets/alarms): list, events, set, update, cancel, snooze, ack
- timer (widgets/timers): list, get, start, stop
- clock (widgets/clock): read, resolve
- weather (widgets/weather): read, refresh
- llm-response (widgets/llm-response): list, read
- layout (dashboard 8x6 grid & widget instances): read, update_widget, add_widget, remove_widget, get_data, set_data
- settings (hub settings): read, update
- system (hub status, devices, audit log): status, devices, audit
"""
from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from pydantic import Field, JsonValue, field_validator

from ..clock_service import clock_context, resolve_range
from ..life import AlarmUpdate, EventAction, LifeService
from .. import llm_display
from ..models import WidgetInstance
from ..store import utcnow
from ..widgets import discover
from .adapters import (
    AlarmAdapter,
    CalendarAdapter,
    MemoAdapter,
    StoredAdapter,
    TaskServices,
    TodoAdapter,
)
from .core import (
    AdapterResult,
    ExecutionContext,
    Operation,
    ProtocolFault,
    WidgetAdapter,
    WidgetRegistry,
    missing,
)
from .models import Contract
from .schemas import (
    AlarmData,
    AlarmInput,
    AlarmMutation,
    AlarmsList,
    Empty,
    MemoAppend,
    MemoData,
    MemoMutation,
    MemoWrite,
    TaskCreate,
    TaskData,
    TaskList,
    TaskPatch,
    TaskQuery,
    TasksCreated,
    TasksDeleted,
    VersionArgs,
)

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ImmediateControlOperation(Operation):
    """Read-free bounded control operation that does not require manager confirmation."""

    def capability(self, action: str):
        cap = super().capability(action)
        cap.requires_confirmation = False
        return cap


# --- Extended Memo Schemas & Adapter ---


class MemoItemData(Contract):
    id: str
    title: str
    body: str
    pinned: bool
    shared: bool
    version: int
    created_at: str
    updated_at: str


class MemoListData(Contract):
    count: int
    items: list[MemoItemData]


class MemoDeleteResult(Contract):
    id: str
    deleted: bool


class FullMemoAdapter(MemoAdapter):
    """Extends MemoAdapter with list and delete while preserving read/write/append/clear."""

    operations = MappingProxyType({
        **MemoAdapter.operations,
        'list': Operation(Empty, MemoListData, '저장된 전체 메모 목록 조회', assistant_capability='memo.list'),
        'delete': Operation(VersionArgs, MemoDeleteResult, '지정 ID/버전의 저장 메모 삭제', False, 'write', ('item_id',), True),
    })

    async def _execute(self, request, args, authority=None):
        if request.action == 'list':
            items = await asyncio.to_thread(self.life.notes)
            return AdapterResult({'count': len(items), 'items': items}, source=self.source_of_truth)
        if request.action == 'delete':
            try:
                res = await asyncio.to_thread(self.life.delete_note, request.target.value, args.version)
            except HTTPException as exc:
                if exc.status_code == 404:
                    raise ProtocolFault('not_found', 'NOT_FOUND', '저장된 메모가 없습니다.', 'target') from None
                if exc.status_code == 409:
                    raise ProtocolFault('conflict', 'VERSION_CONFLICT', '메모 버전이 다릅니다. 다시 조회하세요.', 'args.version') from None
                raise
            await self.changed('note.deleted', request.target.value)
            return AdapterResult(
                {'id': request.target.value, 'deleted': bool(res.get('deleted'))},
                changed=bool(res.get('deleted')),
                event='note.deleted',
                entity_id=request.target.value,
            )
        return await super()._execute(request, args, authority)


# --- Extended Todo Adapter ---


class FullTodoAdapter(TodoAdapter):
    """Extends TodoAdapter with update (matching CalendarAdapter.update)."""

    operations = MappingProxyType({
        **TodoAdapter.operations,
        'update': Operation(TaskPatch, TaskData, '할 일 회차 수정; 버전 필요', False, 'write', ('item_id',), True),
    })


# --- Extended Alarm Schemas & Adapter ---


class AlarmEventItem(Contract):
    id: str
    alarm_id: str
    label: str
    timezone: str
    scheduled_at: str | None
    due_at: str | None
    state: str
    version: int
    snoozes: int
    ring_until: str | None
    updated_at: str


class AlarmEventsList(Contract):
    items: list[AlarmEventItem]


class AlarmUpdateResult(Contract):
    id: str
    version: int


class AlarmEventActionResult(Contract):
    id: str
    action: str
    duplicate: bool = False


class FullAlarmAdapter(AlarmAdapter):
    """Extends AlarmAdapter with events, update, snooze, and ack."""

    operations = MappingProxyType({
        **AlarmAdapter.operations,
        'events': Operation(Empty, AlarmEventsList, '최근/울림 중 알람 이벤트 목록 조회'),
        'update': Operation(AlarmUpdate, AlarmUpdateResult, '기존 알람 시각·제목·요일·활성 상태 수정', False, 'control', ('item_id',), True),
        'snooze': Operation(VersionArgs, AlarmEventActionResult, '울리는 알람 5분 미루기', False, 'control', ('item_id',), True),
        'ack': Operation(VersionArgs, AlarmEventActionResult, '울리는/놓친 알람 확인 완료 처리', False, 'control', ('item_id',), True),
    })

    def validate_slots(self, request, args):
        super().validate_slots(request, args)
        if request.action == 'update':
            self.life._validated_alarm(args)

    async def _execute(self, request, args, authority=None):
        if request.action == 'events':
            items = await asyncio.to_thread(self.life.events, 50)
            return AdapterResult({'items': items}, source='room_hub_sqlite.hub_alarm_events')
        if request.action == 'update':
            try:
                res = await asyncio.to_thread(self.life.update_alarm, request.target.value, args)
            except HTTPException as exc:
                if exc.status_code == 404:
                    raise ProtocolFault('not_found', 'NOT_FOUND', '해당 알람이 없습니다.', 'target') from None
                if exc.status_code == 409:
                    raise ProtocolFault('conflict', 'VERSION_CONFLICT', '알람 버전이 다릅니다.', 'args.version') from None
                raise
            await self.changed('alarm.updated', request.target.value)
            return AdapterResult(res, changed=True, event='alarm.updated', entity_id=request.target.value)
        if request.action in {'snooze', 'ack'}:
            key = f'wp_alarm_ev_{request.idempotency_key or request.request_id}'[:78]
            try:
                res = await asyncio.to_thread(
                    self.life.act,
                    request.target.value,
                    request.action,
                    EventAction(request_id=key, version=args.version),
                )
            except HTTPException as exc:
                if exc.status_code == 404:
                    raise ProtocolFault('not_found', 'NOT_FOUND', '알람 이벤트가 없습니다.', 'target') from None
                if exc.status_code == 409:
                    raise ProtocolFault('conflict', 'INVALID_ALARM_STATE', str(exc.detail), 'target') from None
                raise
            await self.changed('alarm.' + request.action, request.target.value)
            return AdapterResult(res, changed=not res.get('duplicate'), event='alarm.' + request.action, entity_id=request.target.value)
        return await super()._execute(request, args, authority)


# --- Clock Adapter (widgets/clock & app/clock_service.py) ---


class ClockStateData(Contract):
    reference_at: str
    local_at: str
    timezone: str
    today: str
    tomorrow: str
    weekday: str
    this_week: list[str]
    clock_source: str
    clock_sync_verified: bool


class ClockResolveInput(Contract):
    expression: str = Field(min_length=1, max_length=80)


class ClockRangeData(Contract):
    expression: str
    start: str
    end: str
    timezone: str


class ClockAdapter(WidgetAdapter):
    name = 'clock'
    source_of_truth = 'room_hub_clock.os_clock'
    state_action = 'read'
    operations = MappingProxyType({
        'read': Operation(Empty, ClockStateData, '서버 기준 시각, 오늘/내일 날짜, 요일, 시간대 조회'),
        'resolve': Operation(ClockResolveInput, ClockRangeData, '자연어 날짜 표현(오늘, 내일, 이번 주 등)을 ISO 날짜 구간으로 변환'),
    })

    def __init__(self, store):
        self.store = store

    async def _execute(self, request, args, authority=None):
        settings = self.store.get('settings') or {'timezone': 'Asia/Seoul'}
        tz_name = settings.get('timezone', 'Asia/Seoul')
        now_iso = utcnow()
        if request.action == 'read':
            ctx = clock_context(now_iso, tz_name)
            return AdapterResult({k: ctx[k] for k in ClockStateData.model_fields}, source=self.source_of_truth)
        try:
            r = resolve_range(args.expression, now_iso, tz_name)
        except ValueError as exc:
            raise ProtocolFault('invalid_request', 'INVALID_DATE_EXPRESSION', str(exc), 'args.expression') from None
        return AdapterResult(
            {'expression': args.expression, 'start': r.start, 'end': r.end, 'timezone': tz_name},
            source=self.source_of_truth,
        )

    async def health(self, authority: ExecutionContext) -> dict:
        if not authority.principal or authority.role != 'admin' or 'read' not in authority.permissions:
            raise ProtocolFault('permission_denied', 'PERMISSION_DENIED', '관리자 읽기 권한이 필요합니다.')
        return {'available': True, 'source_of_truth': self.source_of_truth}


# --- Weather Adapter (widgets/weather & app/weather.py) ---


class WeatherStateData(Contract):
    configured: bool
    location_name: str
    latitude: float | None = None
    longitude: float | None = None
    temperature: float | None = None
    weather_code: int | None = None
    fetched_at: str | None = None
    daily_count: int = 0
    raw: dict[str, JsonValue] | None = None


class WeatherAdapter(WidgetAdapter):
    name = 'weather'
    source_of_truth = 'room_hub_sqlite.kv.weather'
    state_action = 'read'
    operations = MappingProxyType({
        'read': Operation(Empty, WeatherStateData, '현재 저장된 날씨 및 7일 예보 데이터 조회'),
        'refresh': ImmediateControlOperation(Empty, WeatherStateData, '날씨 공급자에서 최신 날씨 즉시 갱신', False, 'control'),
    })

    def __init__(self, store, refresh_fn: Callable | None = None):
        self.store = store
        self.refresh_fn = refresh_fn

    @staticmethod
    def authorize(request, authority: ExecutionContext, spec: Operation):
        if not authority.principal or authority.role != 'admin' or spec.permission not in authority.permissions:
            raise ProtocolFault('permission_denied', 'PERMISSION_DENIED', '신뢰된 관리자 실행 권한이 필요합니다.')

    def _snapshot(self) -> dict:
        settings = self.store.get('settings') or {}
        wx = self.store.get('weather')
        lat = settings.get('latitude')
        lon = settings.get('longitude')
        loc = settings.get('location_name') or ''
        if not wx:
            return {
                'configured': lat is not None and lon is not None,
                'location_name': loc,
                'latitude': lat,
                'longitude': lon,
                'temperature': None,
                'weather_code': None,
                'fetched_at': None,
                'daily_count': 0,
                'raw': None,
            }
        current = wx.get('current') if isinstance(wx.get('current'), dict) else {}
        daily = wx.get('daily') if isinstance(wx.get('daily'), list) else []
        temp = wx.get('temperature', current.get('temperature_2m'))
        code = wx.get('weather_code', current.get('weather_code'))
        return {
            'configured': lat is not None and lon is not None,
            'location_name': loc or str(wx.get('location_name') or ''),
            'latitude': lat,
            'longitude': lon,
            'temperature': float(temp) if isinstance(temp, (int, float)) else None,
            'weather_code': int(code) if isinstance(code, int) else None,
            'fetched_at': wx.get('fetched_at') or wx.get('updated_at'),
            'daily_count': len(daily),
            'raw': wx,
        }

    async def _execute(self, request, args, authority=None):
        if request.action == 'refresh' and self.refresh_fn is not None:
            await self.refresh_fn()
            return AdapterResult(self._snapshot(), changed=True, source=self.source_of_truth, event='weather.refreshed')
        return AdapterResult(self._snapshot(), source=self.source_of_truth)

    async def health(self, authority: ExecutionContext) -> dict:
        if not authority.principal or authority.role != 'admin' or 'read' not in authority.permissions:
            raise ProtocolFault('permission_denied', 'PERMISSION_DENIED', '관리자 읽기 권한이 필요합니다.')
        return {'available': True, 'source_of_truth': self.source_of_truth}


# --- LLM Response Widget Adapter (widgets/llm-response & app/llm_display.py) ---


class LLMDisplayItemSummary(Contract):
    id: str
    status: str
    source_text: str
    summary_text: str
    created_at: str


class LLMDisplayListData(Contract):
    widget_placed: bool
    llm_total: int
    agent_run_count: int
    recent_agent_runs: list[LLMDisplayItemSummary]


class LLMDisplayAdapter(WidgetAdapter):
    name = 'llm-response'
    source_of_truth = 'room_hub_sqlite.hub_agent_runs'
    state_action = 'list'
    operations = MappingProxyType({
        'list': Operation(Empty, LLMDisplayListData, 'LLM 응답 위젯 배치 여부 및 최근 에이전트/LLM 응답 목록 조회'),
        'read': Operation(Empty, LLMDisplayItemSummary, '특정 또는 가장 최근 에이전트 응답 카드 조회', target_types=('item_id', 'reference')),
    })

    def __init__(self, store):
        self.store = store

    def _list(self) -> dict:
        placed = llm_display.enabled(self.store)
        idx = llm_display.index(self.store)
        with closing(self.store.connect()) as db:
            has_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_agent_runs'"
            ).fetchone()
            runs = []
            if has_table:
                rows = db.execute(
                    'SELECT id, status, source_text, summary_text, created_at FROM hub_agent_runs ORDER BY created_at DESC LIMIT 10'
                ).fetchall()
                runs = [dict(r) for r in rows]
        return {
            'widget_placed': placed,
            'llm_total': idx.get('total', 0),
            'agent_run_count': len(runs),
            'recent_agent_runs': runs,
        }

    def _read_one(self, target) -> dict:
        data = self._list()
        runs = data['recent_agent_runs']
        if not runs:
            raise ProtocolFault('not_found', 'NOT_FOUND', '저장된 에이전트 응답 기록이 없습니다.', 'target')
        if target is None or (target.type == 'reference' and target.value in {'current', 'last', 'latest'}):
            return runs[0]
        if target.type == 'item_id':
            found = next((r for r in runs if r['id'] == target.value), None)
            if not found:
                raise ProtocolFault('not_found', 'NOT_FOUND', '해당 ID의 응답 기록이 없습니다.', 'target')
            return found
        raise ProtocolFault('needs_clarification', 'CONTEXT_NOT_RESOLVED', 'current/last 또는 실제 ID를 지정하세요.', 'target')

    async def _execute(self, request, args, authority=None):
        if request.action == 'list':
            return AdapterResult(await asyncio.to_thread(self._list), source=self.source_of_truth)
        return AdapterResult(await asyncio.to_thread(self._read_one, request.target), source=self.source_of_truth)

    async def health(self, authority: ExecutionContext) -> dict:
        if not authority.principal or authority.role != 'admin' or 'read' not in authority.permissions:
            raise ProtocolFault('permission_denied', 'PERMISSION_DENIED', '관리자 읽기 권한이 필요합니다.')
        return {'available': True, 'source_of_truth': self.source_of_truth}


# --- Layout & Widget Instance Adapter (All 9 Widget Plugins & 8x6 Dashboard Grid) ---


class PlacedWidgetItem(Contract):
    id: str
    type: str
    title: str = ''
    x: int
    y: int
    w: int
    h: int
    config: dict[str, JsonValue] = Field(default_factory=dict)


class LayoutStateData(Contract):
    columns: int
    rows: int
    version: int
    widgets: list[PlacedWidgetItem]
    available_widget_types: list[str]


class WidgetPatchInput(Contract):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=80)
    x: int | None = Field(default=None, ge=0, le=7)
    y: int | None = Field(default=None, ge=0, le=5)
    w: int | None = Field(default=None, ge=1, le=8)
    h: int | None = Field(default=None, ge=1, le=6)
    config: dict[str, JsonValue] | None = None


class WidgetAddInput(Contract):
    version: int = Field(ge=1)
    id: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    type: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    title: str = Field(default='', max_length=80)
    x: int = Field(default=0, ge=0, le=7)
    y: int = Field(default=0, ge=0, le=5)
    w: int = Field(default=4, ge=1, le=8)
    h: int = Field(default=2, ge=1, le=6)
    config: dict[str, JsonValue] = Field(default_factory=dict)


class WidgetCustomDataInput(Contract):
    data: dict[str, JsonValue]


class WidgetCustomData(Contract):
    widget_type: str
    data: dict[str, JsonValue]


class LayoutAdapter(WidgetAdapter):
    name = 'layout'
    source_of_truth = 'room_hub_sqlite.kv.layout'
    state_action = 'read'
    operations = MappingProxyType({
        'read': Operation(Empty, LayoutStateData, '현재 대시보드 8x6 위젯 배치 및 사용 가능한 모든 위젯 종류 조회'),
        'update_widget': Operation(WidgetPatchInput, LayoutStateData, '배치된 위젯의 제목·위치·크기·설정 변경', False, 'write', ('widget_id',), True),
        'add_widget': Operation(WidgetAddInput, LayoutStateData, '대시보드에 새 위젯 인스턴스 배치', False, 'write'),
        'remove_widget': Operation(VersionArgs, LayoutStateData, '대시보드에서 지정 위젯 인스턴스 제거', False, 'write', ('widget_id',), True),
        'get_data': Operation(Empty, WidgetCustomData, '위젯 종류별 커스텀 저장 데이터 조회', target_types=('widget_id',), target_required=True),
        'set_data': Operation(WidgetCustomDataInput, WidgetCustomData, '위젯 종류별 커스텀 저장 데이터 변경', False, 'write', ('widget_id',), True),
    })

    def __init__(self, store, changed, *, timers=None, widget_root: Path | None = None):
        self.store = store
        self.changed = changed
        self.timers = timers
        self.widget_root = widget_root or (ROOT / 'widgets')

    def _available_types(self) -> list[str]:
        manifests, _ = discover(self.widget_root)
        return sorted(m['id'] for m in manifests)

    def _state(self) -> dict:
        layout = self.store.get('layout') or {'columns': 8, 'rows': 6, 'version': 1, 'widgets': []}
        widgets = [
            {
                'id': w['id'],
                'type': w['type'],
                'title': w.get('title', ''),
                'x': w.get('x', 0),
                'y': w.get('y', 0),
                'w': w.get('w', 4),
                'h': w.get('h', 2),
                'config': w.get('config') or {},
            }
            for w in layout.get('widgets', [])
        ]
        return {
            'columns': layout.get('columns', 8),
            'rows': layout.get('rows', 6),
            'version': layout.get('version', 1),
            'widgets': widgets,
            'available_widget_types': self._available_types(),
        }

    async def _execute(self, request, args, authority=None):
        if request.action == 'read':
            return AdapterResult(await asyncio.to_thread(self._state), source=self.source_of_truth)
        if request.action == 'get_data':
            kind = request.target.value
            if kind not in self._available_types():
                raise ProtocolFault('not_found', 'WIDGET_TYPE_NOT_FOUND', '설치되지 않은 위젯 종류입니다.', 'target')
            data = self.store.get('widget_data:' + kind) or {}
            return AdapterResult({'widget_type': kind, 'data': data}, source='room_hub_sqlite.kv.widget_data')
        if request.action == 'set_data':
            kind = request.target.value
            if kind not in self._available_types():
                raise ProtocolFault('not_found', 'WIDGET_TYPE_NOT_FOUND', '설치되지 않은 위젯 종류입니다.', 'target')
            if len(json.dumps(args.data).encode('utf-8')) > 100_000:
                raise ProtocolFault('invalid_request', 'CONTENT_TOO_LARGE', '위젯 데이터는 최대 100KB입니다.', 'args.data')
            self.store.set('widget_data:' + kind, args.data)
            await self.changed('widget.data', kind)
            return AdapterResult({'widget_type': kind, 'data': args.data}, changed=True, event='widget.data', entity_id=kind)

        current = self.store.get('layout') or {'columns': 8, 'rows': 6, 'version': 1, 'widgets': []}
        if current['version'] != args.version:
            raise ProtocolFault('conflict', 'VERSION_CONFLICT', '화면 배치 버전이 변경되었습니다. 다시 조회하세요.', 'args.version')

        widgets = [dict(w) for w in current.get('widgets', [])]
        if request.action == 'update_widget':
            wid = request.target.value
            target_w = next((w for w in widgets if w['id'] == wid), None)
            if target_w is None:
                raise ProtocolFault('not_found', 'NOT_FOUND', '배치된 해당 위젯 ID가 없습니다.', 'target')
            for k in ('title', 'x', 'y', 'w', 'h', 'config'):
                val = getattr(args, k)
                if val is not None:
                    target_w[k] = val
            # Validate via WidgetInstance
            WidgetInstance.model_validate(target_w)
        elif request.action == 'add_widget':
            if args.type not in self._available_types():
                raise ProtocolFault('invalid_request', 'INVALID_WIDGET_TYPE', '설치되지 않은 위젯 종류입니다.', 'args.type')
            if any(w['id'] == args.id for w in widgets):
                raise ProtocolFault('conflict', 'DUPLICATE_WIDGET_ID', '이미 같은 ID의 위젯이 배치되어 있습니다.', 'args.id')
            if len(widgets) >= 12:
                raise ProtocolFault('invalid_request', 'MAX_WIDGETS_EXCEEDED', '위젯은 최대 12개까지 배치할 수 있습니다.', 'args')
            inst = WidgetInstance(
                id=args.id,
                type=args.type,
                title=args.title,
                x=args.x,
                y=args.y,
                w=args.w,
                h=args.h,
                config=args.config,
            )
            widgets.append(inst.model_dump(mode='json'))
        elif request.action == 'remove_widget':
            wid = request.target.value
            before_len = len(widgets)
            widgets = [w for w in widgets if w['id'] != wid]
            if len(widgets) == before_len:
                raise ProtocolFault('not_found', 'NOT_FOUND', '제거할 위젯 ID가 배치에 없습니다.', 'target')
            if not widgets:
                raise ProtocolFault('invalid_request', 'EMPTY_LAYOUT', '최소 1개 이상의 위젯이 배치되어야 합니다.', 'target')

        updated_layout = {
            'columns': current.get('columns', 8),
            'rows': current.get('rows', 6),
            'version': current['version'] + 1,
            'widgets': widgets,
        }
        self.store.set('layout', updated_layout)
        if self.timers is not None:
            await asyncio.to_thread(self.timers.reconcile_widgets)
        await self.changed('layout.updated')
        return AdapterResult(self._state(), changed=True, source=self.source_of_truth, event='layout.updated')

    async def health(self, authority: ExecutionContext) -> dict:
        if not authority.principal or authority.role != 'admin' or 'read' not in authority.permissions:
            raise ProtocolFault('permission_denied', 'PERMISSION_DENIED', '관리자 읽기 권한이 필요합니다.')
        return {'available': True, 'source_of_truth': self.source_of_truth}


# --- Settings Adapter (Room Hub Settings /api/settings) ---


class SettingsStateData(Contract):
    title: str
    timezone: str
    location_name: str
    latitude: float | None = None
    longitude: float | None = None
    weather_interval_minutes: int
    theme: Literal['light', 'dark']


class SettingsPatchInput(Contract):
    title: str | None = Field(default=None, min_length=1, max_length=60)
    timezone: str | None = Field(default=None, min_length=1, max_length=60)
    location_name: str | None = Field(default=None, max_length=80)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    weather_interval_minutes: int | None = Field(default=None, ge=5, le=180)
    theme: Literal['light', 'dark'] | None = None

    @field_validator('timezone')
    @classmethod
    def check_tz(cls, v):
        if v is not None:
            try:
                ZoneInfo(v)
            except ZoneInfoNotFoundError:
                raise ValueError('유효하지 않은 IANA 시간대입니다.') from None
        return v


class SettingsAdapter(WidgetAdapter):
    name = 'settings'
    source_of_truth = 'room_hub_sqlite.kv.settings'
    state_action = 'read'
    operations = MappingProxyType({
        'read': Operation(Empty, SettingsStateData, '현재 Room Hub 제목·테마(light/dark)·시간대·날씨 위치 설정 조회'),
        'update': Operation(SettingsPatchInput, SettingsStateData, 'Room Hub 제목·테마·시간대·날씨 위치 설정 변경', False, 'write'),
    })

    def __init__(self, store, changed):
        self.store = store
        self.changed = changed

    def _read_settings(self) -> dict:
        s = self.store.get('settings') or {}
        return {
            'title': s.get('title', 'My room'),
            'timezone': s.get('timezone', 'Asia/Seoul'),
            'location_name': s.get('location_name', ''),
            'latitude': s.get('latitude'),
            'longitude': s.get('longitude'),
            'weather_interval_minutes': int(s.get('weather_interval_minutes', 15)),
            'theme': s.get('theme', 'light') if s.get('theme') in {'light', 'dark'} else 'light',
        }

    async def _execute(self, request, args, authority=None):
        current = self._read_settings()
        if request.action == 'read':
            return AdapterResult(current, source=self.source_of_truth)
        patches = {k: v for k, v in args.model_dump(exclude_unset=True).items() if v is not None}
        if not patches:
            missing('args')
        updated = {**current, **patches}
        if (updated.get('latitude') is None) != (updated.get('longitude') is None):
            raise ProtocolFault('invalid_request', 'INCOMPLETE_COORDINATES', '위도와 경도를 함께 입력하세요.', 'args')
        self.store.set('settings', updated)
        await self.changed('settings.updated')
        return AdapterResult(updated, changed=True, source=self.source_of_truth, event='settings.updated')

    async def health(self, authority: ExecutionContext) -> dict:
        if not authority.principal or authority.role != 'admin' or 'read' not in authority.permissions:
            raise ProtocolFault('permission_denied', 'PERMISSION_DENIED', '관리자 읽기 권한이 필요합니다.')
        return {'available': True, 'source_of_truth': self.source_of_truth}


# --- System Adapter (Overview, Paired Devices, Audit Trail) ---


class SystemOverviewData(Contract):
    revision: int
    task_count: int
    note_count: int
    alarm_count: int
    active_timer_count: int
    paired_device_count: int
    placed_widget_count: int


class DeviceItemData(Contract):
    id: str
    name: str
    created_at: str
    revoked: bool
    last_seen: str | None = None
    viewport: str | None = None


class DeviceListData(Contract):
    count: int
    items: list[DeviceItemData]


class AuditQueryInput(Contract):
    limit: int = Field(default=20, ge=1, le=100)


class AuditItemData(Contract):
    id: int
    event: str
    detail: str
    created_at: str


class AuditListData(Contract):
    count: int
    items: list[AuditItemData]


class SystemAdapter(WidgetAdapter):
    name = 'system'
    source_of_truth = 'room_hub_sqlite.system'
    state_action = 'status'
    operations = MappingProxyType({
        'status': Operation(Empty, SystemOverviewData, 'Room Hub 전체 시스템 상태 요약 및 엔티티 개수 조회'),
        'devices': Operation(Empty, DeviceListData, '페어링된 디스플레이 기기(iPad 등) 목록 및 접속 상태 조회'),
        'audit': Operation(AuditQueryInput, AuditListData, '최근 시스템 감사 로그(audit) 조회'),
    })

    def __init__(self, store, life: LifeService, timers=None):
        self.store = store
        self.life = life
        self.timers = timers

    def _status(self) -> dict:
        rev = int(self.store.get('revision') or 0)
        layout = self.store.get('layout') or {'widgets': []}
        with closing(self.store.connect()) as db:
            t_count = db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
            d_count = db.execute('SELECT COUNT(*) FROM devices WHERE revoked=0').fetchone()[0]
        n_count = len(self.life.notes())
        a_count = len(self.life.alarms())
        tm_count = self.timers.snapshot()['active_count'] if self.timers else 0
        return {
            'revision': rev,
            'task_count': int(t_count),
            'note_count': n_count,
            'alarm_count': a_count,
            'active_timer_count': int(tm_count),
            'paired_device_count': int(d_count),
            'placed_widget_count': len(layout.get('widgets', [])),
        }

    def _devices(self) -> dict:
        with closing(self.store.connect()) as db:
            rows = db.execute('SELECT * FROM devices ORDER BY created_at DESC').fetchall()
        items = [
            {
                'id': r['id'],
                'name': r['name'],
                'created_at': r['created_at'],
                'revoked': bool(r['revoked']),
                'last_seen': r['last_seen'],
                'viewport': r['viewport'],
            }
            for r in rows
        ]
        return {'count': len(items), 'items': items}

    def _audit(self, limit: int) -> dict:
        with closing(self.store.connect()) as db:
            rows = db.execute('SELECT id, event, detail, created_at FROM audit ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        items = [dict(r) for r in rows]
        return {'count': len(items), 'items': items}

    async def _execute(self, request, args, authority=None):
        if request.action == 'status':
            return AdapterResult(await asyncio.to_thread(self._status), source=self.source_of_truth)
        if request.action == 'devices':
            return AdapterResult(await asyncio.to_thread(self._devices), source='room_hub_sqlite.devices')
        return AdapterResult(await asyncio.to_thread(self._audit, args.limit), source='room_hub_sqlite.audit')

    async def health(self, authority: ExecutionContext) -> dict:
        if not authority.principal or authority.role != 'admin' or 'read' not in authority.permissions:
            raise ProtocolFault('permission_denied', 'PERMISSION_DENIED', '관리자 읽기 권한이 필요합니다.')
        return {'available': True, 'source_of_truth': self.source_of_truth}


def build_full_registry(
    store,
    life: LifeService,
    changed,
    task_services: TaskServices,
    *,
    timers=None,
    refresh_weather: Callable | None = None,
    widget_root: Path | None = None,
) -> WidgetRegistry:
    """Build a WidgetRegistry containing adapters for all 11 Room Hub widgets & systems."""

    def audit(record):
        with closing(store.connect()) as db:
            with db:
                db.execute(
                    'INSERT INTO audit(event,detail,created_at) VALUES(?,?,?)',
                    ('widget.protocol', json.dumps(record, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
                )
                db.execute('DELETE FROM audit WHERE id < (SELECT COALESCE(MAX(id),0)-999 FROM audit)')

    registry = WidgetRegistry(audit=audit)
    registry.register(FullMemoAdapter(store, life, changed))
    registry.register(FullTodoAdapter(store, task_services))
    registry.register(CalendarAdapter(store, task_services))
    registry.register(FullAlarmAdapter(store, life, changed))
    if timers is not None:
        from .timers import TimerAdapter

        registry.register(TimerAdapter(timers))
    registry.register(ClockAdapter(store))
    registry.register(WeatherAdapter(store, refresh_fn=refresh_weather))
    registry.register(LLMDisplayAdapter(store))
    registry.register(LayoutAdapter(store, changed, timers=timers, widget_root=widget_root))
    registry.register(SettingsAdapter(store, changed))
    registry.register(SystemAdapter(store, life, timers=timers))
    return registry
