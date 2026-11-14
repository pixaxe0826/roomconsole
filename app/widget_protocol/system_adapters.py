"""Extended WidgetAdapter implementations covering all Room Hub widgets, indoor sensors, and systems.

Conforms strictly to WidgetProtocol 1.0 (WidgetAdapter, Operation, Contract,
AdapterResult, ProtocolFault, WidgetRequest, WidgetResponse, ExecutionContext)
so the Personal AI Agent interacts with every widget and subsystem through a
single unified adapter protocol:
- memo (widgets/note): read, list, write, append, clear, delete, pin, share
- todo (widgets/todos, widgets/all-todos): list, get, add, update, complete, reopen, delete
- calendar (widgets/calendar): list, get, add, update, delete
- alarm (widgets/alarms): list, events, set, update, cancel, delete, snooze, ack
- timer (widgets/timers): list, get, start, stop
- clock (widgets/clock): read, resolve
- weather (widgets/weather): read, refresh
- indoor (widgets/indoor-temperature, widgets/indoor-air-quality): read, air, devices
- llm-response (widgets/llm-response): list, read, latest, get
- layout (dashboard 8x6 grid & widget instances): read, update_widget, add_widget, remove_widget, get_data, set_data, focus, select_date
- settings (hub settings): read, update
- system (hub status, devices, audit log): status, overview, devices, audit
"""
from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from pydantic import Field, JsonValue, field_validator

from ..clock_service import clock_context, resolve_range
from ..indoor_temperature import TemperatureError, TemperatureStore, device_id_valid
from ..life import AlarmCreate, AlarmUpdate, EventAction, LifeService, NoteCreate, NoteUpdate
from .. import llm_display
from ..models import HubSettings, Layout, RemoteCommand, WidgetInstance
from ..store import Store, uid, utcnow
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


async def _call_sync(store: Store | None, fn: Callable, *args, **kwargs):
    """Run `fn` synchronously on the calling thread when inside `Store.unit_of_work()`, else via `asyncio.to_thread`."""
    if store is not None and store.in_unit_of_work():
        return fn(*args, **kwargs)
    return await asyncio.to_thread(fn, *args, **kwargs)


async def _notify_if_committed(store: Store | None, changed: Callable | None, event: str, detail: str = ''):
    """Suppress live WebSocket broadcasts while inside an uncommitted `unit_of_work()`."""
    if changed is None:
        return
    if store is not None and store.in_unit_of_work():
        return
    res = changed(event, detail)
    if inspect.isawaitable(res):
        await res


@dataclass(frozen=True)
class ImmediateControlOperation(Operation):
    """Bounded control operation that can execute without a separate manager confirmation step."""

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
    created_at: str | None = None
    updated_at: str


class MemoListInput(Contract):
    limit: int = Field(default=50, ge=1, le=200)
    include_private: bool = True


class MemoListData(Contract):
    count: int
    items: list[MemoItemData]


class MemoDeleteResult(Contract):
    id: str
    deleted: bool


class MemoFlagInput(Contract):
    version: int = Field(ge=1)
    pinned: bool | None = None
    shared: bool | None = None


class FullMemoAdapter(MemoAdapter):
    """Extends MemoAdapter with list, delete, pin, and share while preserving read/write/append/clear."""

    operations = MappingProxyType({
        **MemoAdapter.operations,
        'list': Operation(MemoListInput, MemoListData, '저장된 전체 메모 목록 조회', assistant_capability='memo.list'),
        'delete': Operation(VersionArgs, MemoDeleteResult, '지정 ID/버전의 저장 메모 삭제', False, 'write', ('item_id',), True),
        'pin': Operation(MemoFlagInput, MemoMutation, '지정 메모의 고정(pinned) 상태 변경', False, 'write', ('item_id',), True),
        'share': Operation(MemoFlagInput, MemoMutation, '지정 메모의 표시 기기 공유(shared) 상태 변경', False, 'write', ('item_id',), True),
    })

    async def _execute(self, request, args, authority=None):
        if request.action == 'list':
            items = await _call_sync(self.store, self.life.notes)
            if not getattr(args, 'include_private', True):
                items = [n for n in items if n.get('shared') or n.get('pinned')]
            limit = int(getattr(args, 'limit', 50) or 50)
            sliced = items[:limit]
            return AdapterResult({'count': len(sliced), 'items': sliced}, source=self.source_of_truth)
        if request.action == 'delete':
            try:
                res = await _call_sync(self.store, self.life.delete_note, request.target.value, args.version)
            except HTTPException as exc:
                if exc.status_code == 404:
                    raise ProtocolFault('not_found', 'NOT_FOUND', '저장된 메모가 없습니다.', 'target') from None
                if exc.status_code == 409:
                    raise ProtocolFault('conflict', 'VERSION_CONFLICT', '메모 버전이 다릅니다. 다시 조회하세요.', 'args.version') from None
                raise
            await _notify_if_committed(self.store, self.changed, 'note.deleted', request.target.value)
            return AdapterResult(
                {'id': request.target.value, 'deleted': bool(res.get('deleted'))},
                changed=bool(res.get('deleted')),
                event='note.deleted',
                entity_id=request.target.value,
            )
        if request.action in {'pin', 'share'}:
            note = await _call_sync(self.store, self._note, request.target.value)
            if note['version'] != args.version:
                raise ProtocolFault('conflict', 'VERSION_CONFLICT', '메모 버전이 다릅니다. 다시 조회하세요.', 'args.version')
            pinned = args.pinned if args.pinned is not None else (not note['pinned'] if request.action == 'pin' else note['pinned'])
            shared = args.shared if args.shared is not None else (not note['shared'] if request.action == 'share' else note['shared'])
            upd = NoteUpdate(
                version=args.version,
                title=note['title'],
                body=note['body'],
                pinned=bool(pinned),
                shared=bool(shared),
            )
            res = await _call_sync(self.store, self.life.update_note, note['id'], upd)
            await _notify_if_committed(self.store, self.changed, 'note.updated', note['id'])
            return AdapterResult(res, changed=True, event='note.updated', entity_id=note['id'])
        if request.action == 'read':
            return await _call_sync(self.store, self._read, request)
        target = request.target
        if request.action == 'write' and target is None:
            key = hashlib.sha256((authority.principal + ':' + (request.idempotency_key or request.request_id)).encode()).hexdigest()
            body = NoteCreate(
                request_id='wp_memo_' + key,
                title=args.title,
                body=args.body or '',
                shared=args.shared if args.shared is not None else False,
                pinned=args.pinned if args.pinned is not None else False,
            )
            result = await _call_sync(self.store, self.life.create_note, body)
            changed = not result['duplicate']
        else:
            note = await _call_sync(self.store, self._note, target.value)
            if note['version'] != args.version:
                raise ProtocolFault('conflict', 'VERSION_CONFLICT', '메모가 변경되었습니다. 다시 조회하세요.', 'args.version')
            values = {k: note[k] for k in ('title', 'body', 'pinned', 'shared')}
            if request.action == 'append':
                values['body'] += args.text
                if len(values['body']) > 8000:
                    raise ProtocolFault('invalid_request', 'CONTENT_TOO_LONG', '메모 본문은 최대 8,000자입니다.', 'args.text')
            elif request.action == 'clear':
                values['body'] = ''
            else:
                values.update({k: v for k, v in args.model_dump(exclude={'version'}).items() if v is not None})
            result = await _call_sync(self.store, self.life.update_note, note['id'], NoteUpdate(version=args.version, **values))
            changed = True
        event = 'note.created' if target is None else 'note.updated'
        if changed:
            await _notify_if_committed(self.store, self.changed, event, result['id'])
        return AdapterResult(result, changed=changed, event=event, entity_id=result['id'])


# --- Extended Todo & Calendar Adapters ---


class FullTodoAdapter(TodoAdapter):
    """Extends TodoAdapter with update (matching CalendarAdapter.update) and unit_of_work support."""

    operations = MappingProxyType({
        **TodoAdapter.operations,
        'update': Operation(TaskPatch, TaskData, '할 일 회차 수정; 버전 필요', False, 'write', ('item_id',), True),
    })

    async def _execute(self, request, args, authority=None):
        action, target = request.action, request.target
        if action == 'list':
            start, end = (args.date, args.date) if args.date else (args.start, args.end)
            return AdapterResult(
                await _call_sync(
                    self.store,
                    self.store.query_tasks,
                    start.isoformat(),
                    end.isoformat(),
                    args.status,
                    args.limit,
                    period=args.period,
                )
            )
        if action == 'get':
            return AdapterResult(await _call_sync(self.store, self._get, target.value))
        return await super()._execute(request, args, authority)


class FullCalendarAdapter(FullTodoAdapter):
    """Calendar adapter sharing FullTodoAdapter unit-of-work execution."""

    name = 'calendar'
    operations = CalendarAdapter.operations


# --- Extended Alarm Schemas & Adapter ---


class AlarmEventItem(Contract):
    id: str
    alarm_id: str
    label: str
    timezone: str
    scheduled_at: str | None = None
    due_at: str | None = None
    state: str
    version: int
    snoozes: int = 0
    ring_until: str | None = None
    updated_at: str


class AlarmEventsList(Contract):
    items: list[AlarmEventItem]


class AlarmUpdateResult(Contract):
    id: str
    version: int


class AlarmEventActionInput(Contract):
    version: int | None = Field(default=None, ge=1)
    request_id: str | None = Field(default=None, max_length=128)


class AlarmEventActionResult(Contract):
    id: str
    state: str | None = None
    action: str | None = None
    version: int | None = None
    due_at: str | None = None
    snoozes: int | None = None
    duplicate: bool = False


class FullAlarmAdapter(AlarmAdapter):
    """Extends AlarmAdapter with events, update, delete, snooze, and ack."""

    operations = MappingProxyType({
        **AlarmAdapter.operations,
        'events': Operation(Empty, AlarmEventsList, '최근/울림 중 알람 이벤트 목록 조회'),
        'update': Operation(AlarmUpdate, AlarmUpdateResult, '기존 알람 시각·제목·요일·활성 상태 수정', False, 'control', ('item_id',), True),
        'delete': Operation(VersionArgs, AlarmMutation, '지정 ID/버전의 웹 알람 예약 삭제', False, 'control', ('item_id',), True),
        'snooze': Operation(AlarmEventActionInput, AlarmEventActionResult, '울리는 알람 5분 미루기', False, 'control', ('item_id',), True),
        'ack': Operation(AlarmEventActionInput, AlarmEventActionResult, '울리는/놓친 알람 확인 완료 처리', False, 'control', ('item_id',), True),
    })

    @staticmethod
    def authorize(request, authority: ExecutionContext, spec: Operation):
        if not authority.principal or authority.role != 'admin' or spec.permission not in authority.permissions:
            raise ProtocolFault('permission_denied', 'PERMISSION_DENIED', '신뢰된 관리자 실행 권한이 필요합니다.')
        if not spec.available:
            raise ProtocolFault('unavailable', 'CAPABILITY_UNAVAILABLE', '이 capability는 아직 연결하지 않았습니다.')
        if not spec.read_only and not isinstance(spec, ImmediateControlOperation) and authority.confirmed_digest != (
            hashlib.sha256(
                json.dumps(
                    request.model_dump(mode='json', exclude={'request_id', 'idempotency_key', 'context'}),
                    sort_keys=True,
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode()
            ).hexdigest()
        ):
            raise ProtocolFault('needs_confirmation', 'CONFIRMATION_REQUIRED', '신뢰된 서버의 요청별 확인이 필요합니다.')

    def validate_slots(self, request, args):
        super().validate_slots(request, args)
        if request.action == 'update':
            self.life._validated_alarm(args)

    async def _execute(self, request, args, authority=None):
        if request.action == 'list':
            items = await _call_sync(self.store, self.life.alarms)
            return AdapterResult({'items': items, 'delivery': 'foreground_browser_only', 'sound_confirmed': False})
        if request.action == 'set':
            key = hashlib.sha256((authority.principal + ':' + (request.idempotency_key or request.request_id)).encode()).hexdigest()
            result = await _call_sync(self.store, self.life.create_alarm, AlarmCreate(**args.model_dump(), request_id=key))
            if not result['duplicate']:
                await _notify_if_committed(self.store, self.changed, 'alarm.created', result['id'])
            return AdapterResult(result, changed=not result['duplicate'], event='alarm.created', entity_id=result['id'])
        if request.action in {'cancel', 'delete'}:
            result = await _call_sync(self.store, self.life.delete_alarm, request.target.value, args.version)
            await _notify_if_committed(self.store, self.changed, 'alarm.deleted', request.target.value)
            return AdapterResult(
                {'id': request.target.value, 'cancelled': result['deleted']},
                changed=result['deleted'],
                event='alarm.deleted',
                entity_id=request.target.value,
            )
        if request.action == 'events':
            items = await _call_sync(self.store, self.life.events)
            return AdapterResult({'items': items}, source='room_hub_sqlite.hub_alarm_events')
        if request.action == 'update':
            try:
                res = await _call_sync(self.store, self.life.update_alarm, request.target.value, args)
            except HTTPException as exc:
                if exc.status_code == 404:
                    raise ProtocolFault('not_found', 'NOT_FOUND', '해당 알람이 없습니다.', 'target') from None
                if exc.status_code == 409:
                    raise ProtocolFault('conflict', 'VERSION_CONFLICT', '알람 버전이 다릅니다.', 'args.version') from None
                raise
            await _notify_if_committed(self.store, self.changed, 'alarm.updated', request.target.value)
            return AdapterResult(res, changed=True, event='alarm.updated', entity_id=request.target.value)
        if request.action in {'snooze', 'ack'}:
            eid = request.target.value
            ver = args.version
            events = await _call_sync(self.store, self.life.events)
            matched = next(
                (
                    e
                    for e in events
                    if e['id'] == eid
                    or (e.get('alarm_id') == eid and e.get('state') in {'ringing', 'snoozed', 'missed'})
                ),
                None,
            )
            if matched is None:
                alarms = await _call_sync(self.store, self.life.alarms)
                alarm_row = next((a for a in alarms if a['id'] == eid), None)
                if alarm_row is None:
                    raise ProtocolFault('not_found', 'NOT_FOUND', '알람 이벤트가 없습니다.', 'target')

                def _materialize_event():
                    from ..life import stamp
                    now_ts = self.life.clock()
                    ev_id = f'ev_{eid}_{int(now_ts)}'
                    with self.life.db(write=True) as db:
                        cur = db.execute(
                            "SELECT * FROM hub_alarm_events WHERE alarm_id=? AND state IN ('ringing','snoozed','missed') ORDER BY scheduled_at DESC LIMIT 1",
                            (eid,),
                        ).fetchone()
                        if cur:
                            return str(cur['id']), int(cur['version'])
                        db.execute(
                            'INSERT INTO hub_alarm_events VALUES(?,?,?,?,?,?,?,1,0,?,?)',
                            (
                                ev_id,
                                eid,
                                alarm_row['label'],
                                alarm_row['timezone'],
                                now_ts,
                                now_ts,
                                'ringing',
                                now_ts + 300,
                                stamp(now_ts),
                            ),
                        )
                        return ev_id, 1

                target_ev_id, ev_ver = await _call_sync(self.store, _materialize_event)
            else:
                target_ev_id = matched['id']
                ev_ver = int(matched.get('version', 1))
            use_ver = ver if ver is not None else ev_ver
            req_token = args.request_id or request.idempotency_key or request.request_id
            key = hashlib.sha256(((authority.principal if authority else 'admin') + ':' + str(req_token)).encode()).hexdigest()
            try:
                res = await _call_sync(
                    self.store,
                    self.life.act,
                    target_ev_id,
                    request.action,
                    EventAction(request_id=key, version=use_ver),
                )
            except HTTPException as exc:
                if exc.status_code == 404:
                    raise ProtocolFault('not_found', 'NOT_FOUND', '알람 이벤트가 없습니다.', 'target') from None
                if exc.status_code == 409:
                    raise ProtocolFault('conflict', 'INVALID_ALARM_STATE', str(exc.detail), 'target') from None
                raise
            await _notify_if_committed(self.store, self.changed, 'alarm.' + request.action, target_ev_id)
            out = {
                'id': str(res.get('id') or target_ev_id),
                'state': res.get('state'),
                'action': request.action,
                'version': res.get('version'),
                'due_at': res.get('due_at'),
                'snoozes': res.get('snoozes'),
                'duplicate': bool(res.get('duplicate', False)),
            }
            return AdapterResult(out, changed=not out['duplicate'], event='alarm.' + request.action, entity_id=target_ev_id)
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
            res = self.refresh_fn()
            if inspect.isawaitable(res):
                await res
            return AdapterResult(self._snapshot(), changed=True, source=self.source_of_truth, event='weather.refreshed')
        return AdapterResult(self._snapshot(), source=self.source_of_truth)

    async def health(self, authority: ExecutionContext) -> dict:
        if not authority.principal or authority.role != 'admin' or 'read' not in authority.permissions:
            raise ProtocolFault('permission_denied', 'PERMISSION_DENIED', '관리자 읽기 권한이 필요합니다.')
        return {'available': True, 'source_of_truth': self.source_of_truth}


# --- Indoor Sensor & Air Quality Adapter (widgets/indoor-temperature & widgets/indoor-air-quality) ---


class IndoorSensorArgs(Contract):
    device_id: str | None = Field(default=None, max_length=64)


class IndoorSensorReadOut(Contract):
    device_id: str
    registered: bool
    status: str
    is_fresh: bool
    sensor_ok: bool | None = None
    received_at: str | None = None
    age_seconds: float | None = None
    stale_after_seconds: int = 30
    offline_after_seconds: int = 120
    temperature_c: float | None = None
    humidity_percent: float | None = None
    pressure_hpa: float | None = None
    dust_adc_raw: float | None = None
    dust_a0_voltage_v: float | None = None
    dust_sensor_voltage_v: float | None = None
    source_kind: str | None = None
    source_status: str | None = None
    source_error: str | None = None
    freshness_notice: str
    unit_notice: str


class IndoorSensorDeviceItem(Contract):
    device_id: str
    label: str
    enabled: bool
    received_at: str | None = None
    acquisition: str
    source_kind: str | None = None
    last_attempt_at: str | None = None
    source_error: str | None = None
    source_status: str | None = None


class IndoorSensorDevicesOut(Contract):
    count: int
    items: list[IndoorSensorDeviceItem]


class IndoorSensorAdapter(WidgetAdapter):
    """Read-only adapter for indoor temperature, humidity, pressure, and dust ADC/voltage sensors.

    Reuses `TemperatureStore` directly without triggering extra HTTP sensor polls
    and without converting raw dust ADC/voltage values into unverified PM2.5/AQI grades.
    """

    name = 'indoor'
    source_of_truth = 'room_hub_indoor_sensor.sqlite'
    state_action = 'read'
    operations = MappingProxyType({
        'read': Operation(
            IndoorSensorArgs,
            IndoorSensorReadOut,
            '실내 온도·습도·기압 및 먼지 센서 원시값(ADC·전압)과 수신 상태 조회 (추가 네트워크 폴링 없음)',
        ),
        'air': Operation(
            IndoorSensorArgs,
            IndoorSensorReadOut,
            '실내 공기 질(먼지 ADC·전압) 센서 측정값과 수신 상태 조회 (PM2.5/AQI 임의 환산 없음)',
        ),
        'devices': Operation(
            Empty,
            IndoorSensorDevicesOut,
            '등록된 실내 환경 센서 목록 및 수신 상태 조회',
        ),
    })

    def __init__(self, store: Store, temperature_store: TemperatureStore | None = None, *, data_dir: Path | None = None):
        self.store = store
        if temperature_store is not None:
            self.temperature_store = temperature_store
        else:
            resolved_dir = data_dir or store.path.parent
            self.temperature_store = TemperatureStore(resolved_dir)

    async def health(self, authority: ExecutionContext) -> dict:
        if not authority.principal or authority.role != 'admin' or 'read' not in authority.permissions:
            raise ProtocolFault('permission_denied', 'PERMISSION_DENIED', '관리자 읽기 권한이 필요합니다.')
        return {'available': True, 'source_of_truth': self.source_of_truth}

    def _resolve_device_id(self, requested_id: str | None) -> str | None:
        if requested_id:
            if not device_id_valid(requested_id):
                raise ProtocolFault('invalid_request', 'INVALID_SENSOR_ID', '센서 ID 형식이 올바르지 않습니다.', 'args.device_id')
            return requested_id
        layout = self.store.get('layout') or {}
        for w in layout.get('widgets', []):
            if w.get('type') in {'indoor-temperature', 'indoor-air-quality'}:
                cfg_id = (w.get('config') or {}).get('device_id')
                if isinstance(cfg_id, str) and device_id_valid(cfg_id):
                    return cfg_id
        devices = self.temperature_store.list_devices()
        enabled = [d['device_id'] for d in devices if d.get('enabled')]
        if enabled:
            return enabled[0]
        return None

    @staticmethod
    def _freshness_notice(snap: dict[str, Any]) -> str:
        status = snap.get('status') or 'unavailable'
        age = snap.get('age_seconds')
        rec = snap.get('received_at')
        src_err = snap.get('source_error')
        age_txt = f' (경과 {int(round(age))}초, 수신 {rec})' if age is not None and rec else ''
        if status == 'ok':
            if src_err:
                return f'정상 범위이나 최근 수집 시도 오류가 기록됨({src_err}){age_txt}'
            return f'정상 최신 측정값{age_txt}'
        if status == 'stale':
            return f'주의: 오래된 측정값(stale)입니다. 최신 측정값으로 간주하지 마세요{age_txt}.'
        if status == 'offline':
            return f'주의: 센서가 오프라인(offline) 상태입니다. 마지막 수신값은 과거 기록입니다{age_txt}.'
        if status == 'sensor_error':
            return f'주의: 센서 하드웨어/측정 오류(sensor_error) 상태입니다{age_txt}.'
        if status == 'waiting':
            return '센서가 등록되었으나 아직 첫 측정값 수신 대기(waiting) 상태입니다.'
        return '등록된 실내 센서가 없거나 비활성(unavailable) 상태입니다.'

    def _read_sensor(self, device_id: str | None, *, air_only: bool = False) -> dict[str, Any]:
        dev_id = self._resolve_device_id(device_id)
        if dev_id is None:
            return {
                'device_id': 'none',
                'registered': False,
                'status': 'unavailable',
                'is_fresh': False,
                'sensor_ok': None,
                'received_at': None,
                'age_seconds': None,
                'stale_after_seconds': 30,
                'offline_after_seconds': 120,
                'temperature_c': None,
                'humidity_percent': None,
                'pressure_hpa': None,
                'dust_adc_raw': None,
                'dust_a0_voltage_v': None,
                'dust_sensor_voltage_v': None,
                'source_kind': None,
                'source_status': None,
                'source_error': None,
                'freshness_notice': '등록된 실내 센서가 없거나 비활성(unavailable) 상태입니다.',
                'unit_notice': '먼지 측정값은 ADC 원시값(dust_adc_raw) 및 전압(V)이며, PM2.5 농도나 AQI 등급으로 환산하지 않습니다.',
            }
        try:
            temp_snap = self.temperature_store.snapshot(dev_id)
            air_snap = self.temperature_store.air_snapshot(dev_id)
        except TemperatureError as exc:
            raise ProtocolFault('invalid_request', 'INVALID_SENSOR_ID', exc.detail, 'args.device_id') from None
        primary = air_snap if air_only else temp_snap
        status = str(primary.get('status') or 'waiting')
        return {
            'device_id': dev_id,
            'registered': bool(primary.get('registered')),
            'status': status,
            'is_fresh': status == 'ok',
            'sensor_ok': primary.get('sensor_ok'),
            'received_at': primary.get('received_at'),
            'age_seconds': primary.get('age_seconds'),
            'stale_after_seconds': int(primary.get('stale_after_seconds') or 30),
            'offline_after_seconds': int(primary.get('offline_after_seconds') or 120),
            'temperature_c': None if air_only else temp_snap.get('temperature_c'),
            'humidity_percent': None if air_only else temp_snap.get('humidity_percent'),
            'pressure_hpa': None if air_only else temp_snap.get('pressure_hpa'),
            'dust_adc_raw': air_snap.get('dust_adc_raw'),
            'dust_a0_voltage_v': air_snap.get('dust_a0_voltage_v'),
            'dust_sensor_voltage_v': air_snap.get('dust_sensor_voltage_v'),
            'source_kind': primary.get('source_kind'),
            'source_status': primary.get('source_status'),
            'source_error': primary.get('source_error'),
            'freshness_notice': self._freshness_notice(primary),
            'unit_notice': '먼지 측정값은 ADC 원시값(dust_adc_raw) 및 전압(V)이며, PM2.5 농도나 AQI 등급으로 환산하지 않습니다.',
        }

    def _list_devices(self) -> dict[str, Any]:
        devices = self.temperature_store.list_devices()
        items = [
            {
                'device_id': d['device_id'],
                'label': d['label'],
                'enabled': bool(d['enabled']),
                'received_at': d.get('received_at'),
                'acquisition': str(d.get('acquisition') or 'push'),
                'source_kind': d.get('source_kind'),
                'last_attempt_at': d.get('last_attempt_at'),
                'source_error': d.get('source_error'),
                'source_status': d.get('source_status'),
            }
            for d in devices
        ]
        return {'count': len(items), 'items': items}

    async def _execute(self, request, args, authority=None):
        if request.action == 'devices':
            return AdapterResult(await _call_sync(self.store, self._list_devices), source=self.source_of_truth)
        air_only = request.action == 'air'
        data = await _call_sync(self.store, self._read_sensor, args.device_id, air_only=air_only)
        return AdapterResult(data, source=self.source_of_truth)


# --- LLM Response Widget Adapter (widgets/llm-response & app/llm_display.py) ---


class LLMDisplayItemSummary(Contract):
    id: str
    status: str
    source_text: str = ''
    summary_text: str = ''
    input_text: str = ''
    output_text: str = ''
    created_at: str | None = None
    finished_at: str | None = None
    model: str | None = None


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
        'latest': Operation(Empty, LLMDisplayItemSummary, '가장 최근 에이전트/LLM 응답 상세 조회'),
        'get': Operation(Empty, LLMDisplayItemSummary, '지정 ID의 에이전트/LLM 응답 상세 조회', target_types=('item_id',), target_required=True),
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
                cols = {r['name'] for r in db.execute('PRAGMA table_info(hub_agent_runs)').fetchall()}
                src_col = 'input_text' if 'input_text' in cols else 'source_text'
                rows = db.execute(
                    f'SELECT id, status, {src_col} AS src_text, summary_text, model_used, created_at, updated_at '
                    'FROM hub_agent_runs ORDER BY created_at DESC LIMIT 10'
                ).fetchall()
                for r in rows:
                    runs.append(
                        {
                            'id': str(r['id']),
                            'status': str(r['status']),
                            'source_text': str(r['src_text'] or ''),
                            'summary_text': str(r['summary_text'] or ''),
                            'input_text': str(r['src_text'] or ''),
                            'output_text': str(r['summary_text'] or ''),
                            'created_at': r['created_at'],
                            'finished_at': r['updated_at'],
                            'model': r['model_used'],
                        }
                    )
        return {
            'widget_placed': placed,
            'llm_total': idx.get('total', 0),
            'agent_run_count': len(runs),
            'recent_agent_runs': runs,
        }

    def _read_one(self, target) -> dict:
        data = self._list()
        runs = data['recent_agent_runs']
        if target is None or (target.type == 'reference' and target.value in {'current', 'last', 'latest'}):
            if runs:
                return runs[0]
            idx = llm_display.index(self.store)
            idx_items = idx.get('items') or []
            latest_id = idx.get('latest_id') or (idx_items[-1]['id'] if idx_items else None)
            if latest_id:
                ent = llm_display.entry(self.store, latest_id)
                in_txt = str(ent.get('input_text') or ent.get('input') or '')
                out_txt = str(ent.get('output_text') or ent.get('output') or '')
                return {
                    'id': str(ent['id']),
                    'status': str(ent['status']),
                    'source_text': in_txt,
                    'summary_text': out_txt,
                    'input_text': in_txt,
                    'output_text': out_txt,
                    'created_at': ent.get('created_at') or ent.get('sent_at'),
                    'finished_at': ent.get('finished_at'),
                    'model': ent.get('model'),
                }
            return {
                'id': 'none',
                'status': 'empty',
                'source_text': '',
                'summary_text': '저장된 응답 기록이 없습니다.',
                'input_text': '',
                'output_text': '저장된 응답 기록이 없습니다.',
                'created_at': None,
                'finished_at': None,
                'model': None,
            }
        if target.type == 'item_id':
            found = next((r for r in runs if r['id'] == target.value), None)
            if found:
                return found
            try:
                ent = llm_display.entry(self.store, target.value)
                in_txt = str(ent.get('input_text') or ent.get('input') or '')
                out_txt = str(ent.get('output_text') or ent.get('output') or '')
                return {
                    'id': str(ent['id']),
                    'status': str(ent['status']),
                    'source_text': in_txt,
                    'summary_text': out_txt,
                    'input_text': in_txt,
                    'output_text': out_txt,
                    'created_at': ent.get('created_at') or ent.get('sent_at'),
                    'finished_at': ent.get('finished_at'),
                    'model': ent.get('model'),
                }
            except HTTPException:
                raise ProtocolFault('not_found', 'NOT_FOUND', '해당 ID의 응답 기록이 없습니다.', 'target') from None
        raise ProtocolFault('needs_clarification', 'CONTEXT_NOT_RESOLVED', 'current/last 또는 실제 ID를 지정하세요.', 'target')

    async def _execute(self, request, args, authority=None):
        if request.action == 'list':
            return AdapterResult(await _call_sync(self.store, self._list), source=self.source_of_truth)
        return AdapterResult(await _call_sync(self.store, self._read_one, request.target), source=self.source_of_truth)

    async def health(self, authority: ExecutionContext) -> dict:
        if not authority.principal or authority.role != 'admin' or 'read' not in authority.permissions:
            raise ProtocolFault('permission_denied', 'PERMISSION_DENIED', '관리자 읽기 권한이 필요합니다.')
        return {'available': True, 'source_of_truth': self.source_of_truth}


LLMResponseAdapter = LLMDisplayAdapter


# --- Layout & Widget Instance Adapter (8x6 Dashboard Grid with Full Bounds & Overlap Validation) ---


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
    version: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, max_length=80)
    x: int | None = Field(default=None, ge=0, le=15)
    y: int | None = Field(default=None, ge=0, le=15)
    w: int | None = Field(default=None, ge=1, le=16)
    h: int | None = Field(default=None, ge=1, le=16)
    config: dict[str, JsonValue] | None = None


class WidgetAddInput(Contract):
    version: int | None = Field(default=None, ge=1)
    id: str | None = Field(default=None, pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    widget_id: str | None = Field(default=None, pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    type: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    title: str = Field(default='', max_length=80)
    x: int | None = Field(default=None, ge=0, le=15)
    y: int | None = Field(default=None, ge=0, le=15)
    w: int = Field(default=4, ge=1, le=16)
    h: int = Field(default=2, ge=1, le=16)
    config: dict[str, JsonValue] = Field(default_factory=dict)


class WidgetCustomDataInput(Contract):
    data: dict[str, JsonValue]


class WidgetCustomData(Contract):
    widget_type: str
    data: dict[str, JsonValue]


class LayoutRemoteCommandInput(Contract):
    action: Literal['home', 'expand', 'select_date', 'reload'] = 'expand'
    widget_id: str | None = Field(default=None, max_length=64)
    date: str | None = Field(default=None, max_length=20)
    device_id: str | None = Field(default=None, max_length=64)


class LayoutRemoteCommandData(Contract):
    ok: bool
    action: str
    widget_id: str | None = None
    date: str | None = None


class LayoutAdapter(WidgetAdapter):
    name = 'layout'
    source_of_truth = 'room_hub_sqlite.kv.layout'
    state_action = 'read'
    operations = MappingProxyType({
        'read': Operation(Empty, LayoutStateData, '현재 대시보드 8x6 위젯 배치 및 사용 가능한 모든 위젯 종류 조회'),
        'update_widget': Operation(WidgetPatchInput, LayoutStateData, '배치된 위젯의 제목·위치·크기·설정 변경', False, 'write', ('widget_id', 'item_id'), True),
        'add_widget': Operation(WidgetAddInput, LayoutStateData, '대시보드에 새 위젯 인스턴스 배치 (빈 슬롯 탐색 및 8x6 경계/겹침 검증)', False, 'write'),
        'remove_widget': Operation(VersionArgs, LayoutStateData, '대시보드에서 지정 위젯 인스턴스 제거', False, 'write', ('widget_id', 'item_id'), True),
        'get_data': Operation(Empty, WidgetCustomData, '위젯 종류별 커스텀 저장 데이터 조회', target_types=('widget_id',), target_required=True),
        'set_data': Operation(WidgetCustomDataInput, WidgetCustomData, '위젯 종류별 커스텀 저장 데이터 변경', False, 'write', ('widget_id',), True),
        'focus': ImmediateControlOperation(LayoutRemoteCommandInput, LayoutRemoteCommandData, '표시 기기에서 특정 위젯을 확대 또는 홈으로 전환', False, 'control'),
        'select_date': ImmediateControlOperation(LayoutRemoteCommandInput, LayoutRemoteCommandData, '표시 기기의 달력/할 일 선택 날짜 이동', False, 'control'),
    })

    def __init__(self, store, changed, *, timers=None, widget_root: Path | None = None, broadcast_fn: Callable | None = None):
        self.store = store
        self.changed = changed
        self.timers = timers
        self.widget_root = widget_root or (ROOT / 'widgets')
        self.broadcast_fn = broadcast_fn

    def _available_types(self) -> list[str]:
        manifests, _ = discover(self.widget_root)
        return sorted(m['id'] for m in manifests)

    @staticmethod
    def find_free_slot(widgets: list[dict[str, Any]], w: int, h: int, *, columns: int = 8, rows: int = 6) -> tuple[int, int] | None:
        """Find the first non-overlapping `(x, y)` slot of size `(w, h)` on the 8x6 grid."""
        occupied: set[tuple[int, int]] = set()
        for item in widgets:
            ix, iy = int(item.get('x', 0)), int(item.get('y', 0))
            iw, ih = int(item.get('w', 2)), int(item.get('h', 2))
            for cx in range(ix, ix + iw):
                for cy in range(iy, iy + ih):
                    occupied.add((cx, cy))
        for gy in range(0, rows - h + 1):
            for gx in range(0, columns - w + 1):
                cells = {(cx, cy) for cx in range(gx, gx + w) for cy in range(gy, gy + h)}
                if not (cells & occupied):
                    return gx, gy
        return None

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

    def _mutate(self, action: str, target_id: str | None, args) -> dict:
        current = self.store.get('layout') or {'columns': 8, 'rows': 6, 'version': 1, 'widgets': []}
        cur_ver = int(current.get('version', 1))
        if getattr(args, 'version', None) is not None and int(args.version) != cur_ver:
            raise ProtocolFault('conflict', 'VERSION_CONFLICT', '화면 배치 버전이 변경되었습니다. 다시 조회하세요.', 'args.version')

        columns = int(current.get('columns', 8))
        rows = int(current.get('rows', 6))
        widgets = [dict(w) for w in current.get('widgets', [])]
        avail = self._available_types()

        if action == 'update_widget':
            wid = target_id
            target_w = next((w for w in widgets if w['id'] == wid), None)
            if target_w is None:
                raise ProtocolFault('not_found', 'NOT_FOUND', '배치된 해당 위젯 ID가 없습니다.', 'target')
            for k in ('title', 'x', 'y', 'w', 'h', 'config'):
                val = getattr(args, k, None)
                if val is not None:
                    target_w[k] = val
        elif action == 'add_widget':
            if args.type not in avail:
                raise ProtocolFault('invalid_request', 'INVALID_WIDGET_TYPE', '설치되지 않은 위젯 종류입니다.', 'args.type')
            wid = args.id or args.widget_id or f'{args.type}-{uid()[:6]}'
            if any(w['id'] == wid for w in widgets):
                raise ProtocolFault('conflict', 'DUPLICATE_WIDGET_ID', '이미 같은 ID의 위젯이 배치되어 있습니다.', 'args.id')
            if len(widgets) >= 12:
                raise ProtocolFault('invalid_request', 'MAX_WIDGETS_EXCEEDED', '위젯은 최대 12개까지 배치할 수 있습니다.', 'args')
            w_size = int(args.w)
            h_size = int(args.h)
            if args.x is not None and args.y is not None:
                x_pos, y_pos = int(args.x), int(args.y)
            else:
                slot = self.find_free_slot(widgets, w_size, h_size, columns=columns, rows=rows)
                if slot is None:
                    raise ProtocolFault(
                        'needs_clarification',
                        'NO_FREE_LAYOUT_SLOT',
                        f'현재 {columns}x{rows} 대시보드에 ({w_size}x{h_size}) 크기의 빈 공간이 없습니다. 위치를 지정하거나 기존 위젯을 먼저 정리해 주세요.',
                        'args',
                    )
                x_pos, y_pos = slot
            inst = WidgetInstance(
                id=wid,
                type=args.type,
                title=args.title,
                x=x_pos,
                y=y_pos,
                w=w_size,
                h=h_size,
                config=args.config,
            )
            widgets.append(inst.model_dump(mode='json'))
        elif action == 'remove_widget':
            wid = target_id
            before_len = len(widgets)
            widgets = [w for w in widgets if w['id'] != wid]
            if len(widgets) == before_len:
                raise ProtocolFault('not_found', 'NOT_FOUND', '제거할 위젯 ID가 배치에 없습니다.', 'target')
            if not widgets:
                raise ProtocolFault('invalid_request', 'EMPTY_LAYOUT', '최소 1개 이상의 위젯이 배치되어야 합니다.', 'target')

        candidate_layout = {
            'columns': columns,
            'rows': rows,
            'version': cur_ver + 1,
            'widgets': widgets,
        }
        try:
            validated = Layout.model_validate(candidate_layout).model_dump(mode='json')
        except Exception as exc:
            raise ProtocolFault(
                'invalid_request',
                'LAYOUT_INVALID',
                f'전체 대시보드 격자(8x6 경계/겹침) 검증에 실패했습니다: {exc}',
                'args',
            ) from None
        self.store.set('layout', validated)
        return self._state()

    async def _execute(self, request, args, authority=None):
        if request.action == 'read':
            return AdapterResult(await _call_sync(self.store, self._state), source=self.source_of_truth)
        if request.action == 'get_data':
            kind = request.target.value
            if kind not in self._available_types():
                raise ProtocolFault('not_found', 'WIDGET_TYPE_NOT_FOUND', '설치되지 않은 위젯 종류입니다.', 'target')
            data = await _call_sync(self.store, self.store.get, 'widget_data:' + kind) or {}
            return AdapterResult({'widget_type': kind, 'data': data}, source='room_hub_sqlite.kv.widget_data')
        if request.action == 'set_data':
            kind = request.target.value
            if kind not in self._available_types():
                raise ProtocolFault('not_found', 'WIDGET_TYPE_NOT_FOUND', '설치되지 않은 위젯 종류입니다.', 'target')
            if len(json.dumps(args.data).encode('utf-8')) > 100_000:
                raise ProtocolFault('invalid_request', 'CONTENT_TOO_LARGE', '위젯 데이터는 최대 100KB입니다.', 'args.data')
            await _call_sync(self.store, self.store.set, 'widget_data:' + kind, args.data)
            await _notify_if_committed(self.store, self.changed, 'widget.data', kind)
            return AdapterResult({'widget_type': kind, 'data': args.data}, changed=True, event='widget.data', entity_id=kind)
        if request.action in {'focus', 'select_date'}:
            cmd_action = args.action
            if request.action == 'select_date':
                cmd_action = 'select_date'
            if self.broadcast_fn is not None and not self.store.in_unit_of_work():
                payload = {'type': 'command', 'id': uid(), 'sent_at': utcnow(), 'action': cmd_action, 'widget_id': args.widget_id, 'date': args.date, 'device_id': args.device_id}
                res = self.broadcast_fn(payload, role='display', device_id=args.device_id)
                if inspect.isawaitable(res):
                    await res
            return AdapterResult({'ok': True, 'action': cmd_action, 'widget_id': args.widget_id, 'date': args.date}, changed=False, source=self.source_of_truth)

        target_id = request.target.value if request.target else getattr(args, 'id', None) or getattr(args, 'widget_id', None)
        state = await _call_sync(self.store, self._mutate, request.action, target_id, args)
        if self.timers is not None and not self.store.in_unit_of_work():
            await asyncio.to_thread(self.timers.reconcile_widgets)
        await _notify_if_committed(self.store, self.changed, 'layout.updated')
        return AdapterResult(state, changed=True, source=self.source_of_truth, event='layout.updated')

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

    def _update_settings(self, args: SettingsPatchInput) -> dict:
        current = self._read_settings()
        patches = {k: v for k, v in args.model_dump(exclude_unset=True).items() if v is not None}
        if not patches:
            missing('args')
        updated = {**current, **patches}
        if (updated.get('latitude') is None) != (updated.get('longitude') is None):
            raise ProtocolFault('invalid_request', 'INCOMPLETE_COORDINATES', '위도와 경도를 함께 입력하세요.', 'args')
        self.store.set('settings', updated)
        return updated

    async def _execute(self, request, args, authority=None):
        if request.action == 'read':
            return AdapterResult(await _call_sync(self.store, self._read_settings), source=self.source_of_truth)
        updated = await _call_sync(self.store, self._update_settings, args)
        await _notify_if_committed(self.store, self.changed, 'settings.updated')
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
        'overview': Operation(Empty, SystemOverviewData, 'Room Hub 전체 시스템 상태 요약 및 엔티티 개수 조회'),
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
        if request.action in {'status', 'overview'}:
            return AdapterResult(await _call_sync(self.store, self._status), source=self.source_of_truth)
        if request.action == 'devices':
            return AdapterResult(await _call_sync(self.store, self._devices), source='room_hub_sqlite.devices')
        return AdapterResult(await _call_sync(self.store, self._audit, args.limit), source='room_hub_sqlite.audit')

    async def health(self, authority: ExecutionContext) -> dict:
        if not authority.principal or authority.role != 'admin' or 'read' not in authority.permissions:
            raise ProtocolFault('permission_denied', 'PERMISSION_DENIED', '관리자 읽기 권한이 필요합니다.')
        return {'available': True, 'source_of_truth': self.source_of_truth}


SystemStatusAdapter = SystemAdapter


def build_full_registry(
    store,
    life: LifeService,
    changed,
    task_services: TaskServices,
    *,
    timers=None,
    refresh_weather: Callable | None = None,
    broadcast_fn: Callable | None = None,
    widget_root: Path | None = None,
    temperature_store: TemperatureStore | None = None,
) -> WidgetRegistry:
    """Build a WidgetRegistry containing adapters for all 12 Room Hub widgets, indoor sensors, and systems."""

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
    registry.register(FullCalendarAdapter(store, task_services))
    registry.register(FullAlarmAdapter(store, life, changed))
    if timers is not None:
        from .timers import TimerAdapter

        registry.register(TimerAdapter(timers))
    registry.register(ClockAdapter(store))
    registry.register(WeatherAdapter(store, refresh_fn=refresh_weather))
    registry.register(IndoorSensorAdapter(store, temperature_store))
    registry.register(LLMDisplayAdapter(store))
    registry.register(LayoutAdapter(store, changed, timers=timers, widget_root=widget_root, broadcast_fn=broadcast_fn))
    registry.register(SettingsAdapter(store, changed))
    registry.register(SystemAdapter(store, life, timers=timers))
    return registry
