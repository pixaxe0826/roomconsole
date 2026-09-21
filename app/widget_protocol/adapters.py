"""Thin service bindings. No LLM import, UI text scraping, or new business store."""
from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import hashlib
from types import MappingProxyType
from typing import Callable

from ..life import LifeService, NoteCreate, NoteUpdate, AlarmCreate, memo_snapshot
from ..models import TaskCompletion
from .core import (AdapterResult, ExecutionContext, Operation, ProtocolFault,
                   WidgetAdapter, WidgetRegistry, missing)
from .models import WidgetRequest
from .schemas import (AlarmInput, AlarmsList, AlarmMutation, Empty, MemoAppend, MemoData,
                      MemoMutation, MemoWrite, TaskCreate, TaskData, TaskList,
                      TaskPatch, TaskQuery, TasksCreated, TasksDeleted, VersionArgs)


class StoredAdapter(WidgetAdapter):
    def __init__(self, store):
        self.store = store

    async def health(self, authority: ExecutionContext) -> dict:
        if not authority.principal or authority.role != 'admin' or 'read' not in authority.permissions:
            raise ProtocolFault('permission_denied', 'PERMISSION_DENIED', '관리자 읽기 권한이 필요합니다.')
        def check():
            with closing(self.store.connect()) as db:
                db.execute('SELECT 1 FROM ' + self.health_table + ' LIMIT 1').fetchone()
        try:
            await asyncio.to_thread(check)
            return {'available': True, 'source_of_truth': self.source_of_truth}
        except Exception:
            return {'available': False, 'source_of_truth': self.source_of_truth}


class MemoAdapter(StoredAdapter):
    name = 'memo'
    source_of_truth = 'room_hub_sqlite.hub_notes'
    health_table = 'hub_notes'
    state_action = 'read'
    operations = MappingProxyType({
        'read': Operation(Empty, MemoData, '실제 메모 또는 저장된 메모 위젯의 기본 카드 읽기',
                          target_types=('item_id', 'widget_id', 'reference'), assistant_capability='memo.read'),
        'write': Operation(MemoWrite, MemoMutation, 'target 없음: 저장 메모 생성; item_id: 본문 교체',
                           False, 'write', ('item_id',)),
        'append': Operation(MemoAppend, MemoMutation, '저장 메모 본문 끝에 text를 그대로 추가',
                            False, 'write', ('item_id',), True),
        'clear': Operation(VersionArgs, MemoMutation, '메모 항목을 삭제하지 않고 본문만 비움',
                           False, 'write', ('item_id',), True),
    })

    def __init__(self, store, life: LifeService, changed):
        super().__init__(store)
        self.life, self.changed = life, changed

    def validate_slots(self, request, args):
        if request.action == 'write':
            if args.body is None:
                missing('args.body')
            if request.target is None:
                if args.title is None:
                    missing('args.title')
                if args.version is not None:
                    raise ProtocolFault('invalid_request', 'TARGET_REQUIRED', '수정할 메모 ID를 명시하세요.', 'target')
            elif args.version is None:
                missing('args.version')

    def _note(self, identity):
        # Reuse the existing bounded repository (at most 200 notes).
        found = next((n for n in self.life.notes() if n['id'] == identity), None)
        if found is None:
            raise ProtocolFault('not_found', 'NOT_FOUND', '저장된 메모가 없습니다.', 'target')
        return found

    def _read(self, request):
        target = request.target
        if target and target.type == 'item_id':
            return AdapterResult(self._note(target.value) | {'selection_policy': 'item_id'})
        selector, widget_id = 'current', None
        if target:
            if target.type == 'widget_id':
                widget_id = target.value
            elif target.value in {'current', 'last', 'last_modified'}:
                selector = 'current' if target.value == 'current' else 'last_modified'
            else:
                raise ProtocolFault('needs_clarification', 'CONTEXT_NOT_RESOLVED',
                                    'active/selected 등은 canonical item_id 또는 widget_id로 해석해 주세요.', 'target')
        try:
            result = memo_snapshot(self.store, selector, widget_id=widget_id)
        except LookupError:
            raise ProtocolFault('not_found', 'NOT_FOUND', '배치된 메모 위젯이 없습니다.', 'target') from None
        except ValueError:
            raise ProtocolFault('needs_clarification', 'CONTEXT_NOT_RESOLVED',
                                '메모를 특정할 수 없습니다. 실제 item_id 또는 배치된 widget_id를 지정하세요.', 'target') from None
        if selector == 'last_modified' and result['selection_policy'] != 'updated_at':
            raise ProtocolFault('not_found', 'NOT_FOUND', '최근 수정된 저장 메모가 없습니다.', 'target')
        if not result['count']:
            raise ProtocolFault('not_found', 'NOT_FOUND', '읽을 수 있는 메모가 없습니다.', 'target')
        return AdapterResult({
            'id': result['note_id'], 'widget_id': result['widget_id'], 'title': result['title'],
            'body': result['body'], 'shared': result['shared'], 'version': result['version'],
            'updated_at': result['updated_at'], 'selection_policy': result['selection_policy'],
        }, source=result['source'])

    async def _execute(self, request, args, authority=None):
        if request.action == 'read':
            return await asyncio.to_thread(self._read, request)
        target = request.target
        if request.action == 'write' and target is None:
            # Reuse LifeService's durable create-note receipt in addition to process replay protection.
            key = hashlib.sha256((authority.principal + ':' + (request.idempotency_key or request.request_id)).encode()).hexdigest()
            body = NoteCreate(request_id='wp_memo_' + key, title=args.title, body=args.body,
                              shared=args.shared if args.shared is not None else False,
                              pinned=args.pinned if args.pinned is not None else False)
            result = await asyncio.to_thread(self.life.create_note, body)
            changed = not result['duplicate']
        else:
            note = await asyncio.to_thread(self._note, target.value)
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
            result = await asyncio.to_thread(self.life.update_note, note['id'], NoteUpdate(version=args.version, **values))
            changed = True
        event = 'note.created' if target is None else 'note.updated'
        if changed:
            await self.changed(event, result['id'])
        return AdapterResult(result, changed=changed, event=event, entity_id=result['id'])


@dataclass(frozen=True)
class TaskServices:
    """Existing main.py handlers injected as trusted business operations, no HTTP loopback."""
    add: Callable
    update: Callable
    complete: Callable
    delete: Callable


class TodoAdapter(StoredAdapter):
    name = 'todo'
    source_of_truth = 'room_hub_sqlite.tasks'
    health_table = 'tasks'
    operations = MappingProxyType({
        'list': Operation(TaskQuery, TaskList, 'Room Hub 할 일 조회', assistant_capability='todo.list'),
        'get': Operation(Empty, TaskData, 'ID로 한 회차 조회', target_types=('item_id',), target_required=True),
        'add': Operation(TaskCreate, TasksCreated, '기존 반복 규칙을 보존하는 할 일 생성', False, 'write', assistant_capability='todo.create'),
        'complete': Operation(VersionArgs, TaskData, '단일 회차 완료', False, 'write', ('item_id',), True, assistant_capability='todo.complete'),
        'reopen': Operation(VersionArgs, TaskData, '단일 회차 완료 취소', False, 'write', ('item_id',), True, assistant_capability='todo.uncomplete'),
        'delete': Operation(VersionArgs, TasksDeleted, '명시된 단일 회차만 삭제', False, 'write', ('item_id',), True, assistant_capability='todo.delete'),
    })

    def __init__(self, store, services: TaskServices):
        super().__init__(store)
        self.services = services

    def validate_slots(self, request, args):
        if request.action == 'list' and args.date is None:
            if args.start is None:
                missing('args.start')
            if args.end is None:
                missing('args.end')
        if request.action == 'update':
            fields = args.model_dump(exclude_unset=True, exclude={'version'})
            if not fields:
                missing('args')
            if any(v is None and k != 'time' for k, v in fields.items()):
                raise ProtocolFault('needs_clarification', 'MISSING_REQUIRED_ARGUMENT', '필수 필드를 비울 수 없습니다.', 'args')

    def _get(self, identity):
        with closing(self.store.connect()) as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (identity,)).fetchone()
            if row is None:
                raise ProtocolFault('not_found', 'NOT_FOUND', '작업이 없습니다.', 'target')
            return self.store.task_dict(row)

    async def _execute(self, request, args, authority=None):
        action, target = request.action, request.target
        if action == 'list':
            start, end = (args.date, args.date) if args.date else (args.start, args.end)
            return AdapterResult(await asyncio.to_thread(self.store.query_tasks, start.isoformat(), end.isoformat(),
                                                         args.status, args.limit, period=args.period))
        if action == 'get':
            return AdapterResult(await asyncio.to_thread(self._get, target.value))
        if action == 'add':
            result = await self.services.add(args)
            return AdapterResult(result, changed=True, event='task.created',
                                 entity_id=result['ids'][0] if len(result['ids']) == 1 else None)
        if action == 'delete':
            result = await self.services.delete(target.value, scope='one', version=args.version)
            return AdapterResult(result, changed=bool(result['deleted']), event='task.deleted', entity_id=target.value)
        if action == 'update':
            result = await self.services.update(target.value, args)
            return AdapterResult(result, changed=True, event='task.updated', entity_id=target.value)
        # Existing versioned, desired-value completion endpoint (not a blind toggle).
        result = await self.services.complete(target.value,
            TaskCompletion(version=args.version, completed=action == 'complete'),
            session={'role': 'admin', 'device_id': None})
        return AdapterResult(result, changed=result['version'] != args.version,
                             event='task.completion', entity_id=target.value)


class CalendarAdapter(TodoAdapter):
    """The existing calendar and todo widgets intentionally share tasks/series."""
    name = 'calendar'
    operations = MappingProxyType({
        'list': Operation(TaskQuery, TaskList, '기존 tasks에서 날짜 범위 일정 조회; 외부 달력 아님',
                          assistant_capability='calendar.query'),
        'get': TodoAdapter.operations['get'],
        'add': TodoAdapter.operations['add'],
        'update': Operation(TaskPatch, TaskData, '기존 회차 편집; 버전 필요', False, 'write', ('item_id',), True),
        'delete': TodoAdapter.operations['delete'],
    })


class AlarmAdapter(StoredAdapter):
    name = 'alarm'
    source_of_truth = 'room_hub_sqlite.hub_alarms'
    health_table = 'hub_alarms'
    operations = MappingProxyType({
        'list': Operation(Empty, AlarmsList, '실제 웹 알람 예약 조회; Android/푸시 알람 아님'),
        'set': Operation(AlarmInput, AlarmMutation, '확인 후 기존 웹 알람 예약 생성; 네이티브 알람 아님', False, 'control'),
        'cancel': Operation(VersionArgs, AlarmMutation, '확인 후 지정 ID/버전의 웹 알람 예약 삭제', False, 'control', ('item_id',), True),
    })

    def __init__(self, store, life, changed):
        super().__init__(store)
        self.life, self.changed = life, changed

    def validate_slots(self, request, args):
        if request.action == 'set':
            self.life._validated_alarm(args)  # Same future-time/DST validation as manager CRUD.

    async def _execute(self, request, args, authority=None):
        if request.action == 'list':
            return AdapterResult({'items': await asyncio.to_thread(self.life.alarms),
                                  'delivery': 'foreground_browser_only', 'sound_confirmed': False})
        if request.action == 'set':
            # Persistent service receipt is scoped to the trusted caller and exact key.
            key = hashlib.sha256((authority.principal + ':' + (request.idempotency_key or request.request_id)).encode()).hexdigest()
            result = await asyncio.to_thread(self.life.create_alarm,
                AlarmCreate(**args.model_dump(), request_id=key))
            if not result['duplicate']:
                await self.changed('alarm.created', result['id'])
            return AdapterResult(result, changed=not result['duplicate'], event='alarm.created', entity_id=result['id'])
        result = await asyncio.to_thread(self.life.delete_alarm, request.target.value, args.version)
        await self.changed('alarm.deleted', request.target.value)
        return AdapterResult({'id': request.target.value, 'cancelled': result['deleted']},
                             changed=result['deleted'], event='alarm.deleted', entity_id=request.target.value)


def build_registry(store, life, changed, task_services: TaskServices) -> WidgetRegistry:
    def audit(record):
        # Existing audit table and retention; reads do not bump layout/state revision.
        with closing(store.connect()) as db:
            with db:
                db.execute('INSERT INTO audit(event,detail,created_at) VALUES(?,?,?)',
                           ('widget.protocol', json.dumps(record, ensure_ascii=False),
                            datetime.now(timezone.utc).isoformat()))
                db.execute('DELETE FROM audit WHERE id < (SELECT COALESCE(MAX(id),0)-999 FROM audit)')

    registry = WidgetRegistry(audit=audit)
    for adapter in (MemoAdapter(store, life, changed), TodoAdapter(store, task_services),
                    CalendarAdapter(store, task_services), AlarmAdapter(store, life, changed)):
        registry.register(adapter)
    return registry
