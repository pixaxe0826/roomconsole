"""Personal AI Agent for Schedule, To-Do, Alarms, Memos, Timers, and Room Hub Systems.

Built on top of Room Hub's Store, LifeService, TimerService, and Full WidgetRegistry
(WidgetProtocol 1.0), governed by the BudgetGovernor ($100/month production default,
configurable e.g. $5 test cap) using GPT-6.1 Sol High (`gpt-6.1-sol`,
`reasoning_effort="high"`):
- Tier 0 ($0, <5ms): Deterministic local fast reads, exact timers, semantic parser,
  and named memo catalog resolution.
- Tier 1 ($0.003-$0.006/min): OpenAI Audio STT with vocabulary hints + local Whisper fallback.
- Tier 2/3 (GPT-6.1 Sol High): `gpt-6.1-sol` (`reasoning_effort="high"`) Structured Outputs
  for multi-step plans across all 11 Room Hub widget/system adapters (`memo`, `todo`,
  `calendar`, `alarm`, `timer`, `clock`, `weather`, `llm-response`, `layout`,
  `settings`, `system`) and holistic daily briefings.

Every step is grounded into a canonical `WidgetRequest` (`protocol_version="1.0"`) and
dispatched through `WidgetRegistry.execute(request, authority)`. All non-immediate
mutations require explicit manager confirmation bound to both `request_digest(request)`
and a cryptographic batch preview digest (`preview_sha256`) with optimistic row version locks.
"""
from __future__ import annotations

import asyncio
from contextlib import closing, contextmanager
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from .clock_service import clock_context, resolve_range
from .entity_catalog import EntityCatalog
from .fast_reads import match_read
from .life import AlarmCreate, LifeService, memo_snapshot, note_catalog_snapshot
from .memo_grounding import parse_memo
from .models import RepeatRule, TaskCreate, TaskPatch
from .openai_budget import BudgetGovernor, BudgetPolicy
from .openai_gateway import OpenAIGateway, OpenAIGatewayError
from .semantic_parser import parse_semantic
from .store import uid, utcnow
from .timer_commands import exact_timer
from .widget_protocol import (
    ExecutionContext,
    TaskServices,
    WidgetRegistry,
    WidgetRequest,
    build_full_registry,
    request_digest,
)

AGENT_VERSION = 'personal-agent-2.0-gpt-6.1-sol-high'
CONFIRM_TTL_SECONDS = 600

READ_CTX = ExecutionContext(
    principal='personal-agent-reader',
    role='admin',
    permissions=frozenset({'read'}),
)
TIMER_CTX = ExecutionContext(
    principal='personal-agent-control',
    role='admin',
    permissions=frozenset({'read', 'control'}),
)
PREVIEW_CTX = ExecutionContext(
    principal='personal-agent-preview',
    role='admin',
    permissions=frozenset({'read', 'write', 'control'}),
)

ALL_ADAPTER_DOMAINS = (
    'todo',
    'calendar',
    'alarm',
    'memo',
    'timer',
    'clock',
    'weather',
    'llm-response',
    'layout',
    'settings',
    'system',
)


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _sha256(value: Any) -> str:
    raw = value if isinstance(value, str) else _json_dump(value)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


# Strict JSON Schema for OpenAI Structured Outputs (response_format: json_schema)
AGENT_STEP_SCHEMA: dict[str, Any] = {
    'type': 'object',
    'properties': {
        'domain': {
            'type': 'string',
            'enum': [
                'todo',
                'calendar',
                'alarm',
                'memo',
                'timer',
                'clock',
                'time',
                'weather',
                'llm-response',
                'layout',
                'settings',
                'system',
            ],
        },
        'action': {
            'type': 'string',
            'enum': [
                'list',
                'get',
                'add',
                'update',
                'complete',
                'reopen',
                'delete',
                'set',
                'cancel',
                'events',
                'snooze',
                'ack',
                'read',
                'write',
                'append',
                'clear',
                'start',
                'stop',
                'resolve',
                'refresh',
                'update_widget',
                'add_widget',
                'remove_widget',
                'get_data',
                'set_data',
                'status',
                'devices',
                'audit',
            ],
        },
        'target_id': {'type': ['string', 'null']},
        'target_title': {'type': ['string', 'null']},
        'date': {'type': ['string', 'null']},
        'start_date': {'type': ['string', 'null']},
        'end_date': {'type': ['string', 'null']},
        'time': {'type': ['string', 'null']},
        'title': {'type': ['string', 'null']},
        'body': {'type': ['string', 'null']},
        'category': {'type': ['string', 'null']},
        'priority': {'type': ['string', 'null'], 'enum': ['low', 'normal', 'high', None]},
        'status_filter': {'type': ['string', 'null'], 'enum': ['all', 'pending', 'completed', None]},
        'period': {'type': ['string', 'null'], 'enum': ['morning', 'afternoon', None]},
        'repeat_frequency': {
            'type': ['string', 'null'],
            'enum': ['none', 'daily', 'weekdays', 'weekly', 'monthly', None],
        },
        'weekdays': {
            'type': ['array', 'null'],
            'items': {'type': 'integer'},
        },
        'duration_seconds': {'type': ['integer', 'null']},
        'pinned': {'type': ['boolean', 'null']},
        'shared': {'type': ['boolean', 'null']},
    },
    'required': [
        'domain', 'action', 'target_id', 'target_title', 'date', 'start_date', 'end_date',
        'time', 'title', 'body', 'category', 'priority', 'status_filter', 'period',
        'repeat_frequency', 'weekdays', 'duration_seconds', 'pinned', 'shared',
    ],
    'additionalProperties': False,
}

AGENT_PLAN_SCHEMA: dict[str, Any] = {
    'type': 'object',
    'properties': {
        'intent_type': {
            'type': 'string',
            'enum': ['action_plan', 'briefing', 'clarification', 'general_answer'],
        },
        'assistant_message': {'type': 'string'},
        'follow_up_question': {'type': ['string', 'null']},
        'steps': {
            'type': 'array',
            'items': AGENT_STEP_SCHEMA,
        },
    },
    'required': ['intent_type', 'assistant_message', 'follow_up_question', 'steps'],
    'additionalProperties': False,
}

BRIEFING_SYNTHESIS_SCHEMA: dict[str, Any] = {
    'type': 'object',
    'properties': {
        'headline': {'type': 'string'},
        'schedule_summary': {'type': 'string'},
        'priority_recommendations': {
            'type': 'array',
            'items': {'type': 'string'},
        },
        'conflict_warnings': {
            'type': 'array',
            'items': {'type': 'string'},
        },
        'suggested_steps': {
            'type': 'array',
            'items': AGENT_STEP_SCHEMA,
        },
    },
    'required': [
        'headline',
        'schedule_summary',
        'priority_recommendations',
        'conflict_warnings',
        'suggested_steps',
    ],
    'additionalProperties': False,
}


class AgentCommandInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    request_id: str = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')
    text: str = Field(min_length=1, max_length=2000)
    mode: Literal['auto', 'fast', 'briefing', 'deep', 'local_only'] = 'auto'
    session_id: str | None = Field(default=None, max_length=80)


class AgentConfirmInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    preview_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


def detect_schedule_conflicts(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Find multiple incomplete tasks scheduled at the exact same date and HH:MM."""
    by_slot: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for t in tasks:
        if t.get('completed') or not t.get('time') or not t.get('date'):
            continue
        by_slot.setdefault((t['date'], t['time']), []).append(t)
    conflicts: list[dict[str, Any]] = []
    for (day, tm), items in sorted(by_slot.items()):
        if len(items) > 1:
            conflicts.append({
                'date': day,
                'time': tm,
                'count': len(items),
                'items': [{'id': x['id'], 'title': x['title'], 'category': x.get('category', '')} for x in items],
            })
    return conflicts


class PersonalAgentService:
    """Coordinates Tier 0 local rules, GPT-6.1 Sol High Planner, and WidgetProtocol 1.0 execution."""

    def __init__(
        self,
        store,
        life: LifeService,
        timers,
        registry: WidgetRegistry | None,
        task_services: TaskServices,
        *,
        speech=None,
        governor: BudgetGovernor | None = None,
        gateway: OpenAIGateway | None = None,
        changed=None,
        refresh_weather: Callable | None = None,
        widget_root: Path | None = None,
    ):
        self.store = store
        self.life = life
        self.timers = timers
        self.task_services = task_services
        self.speech = speech
        self.governor = governor or BudgetGovernor(store)
        self.gateway = gateway or OpenAIGateway(self.governor)
        self.changed = changed
        self.refresh_weather = refresh_weather
        self.widget_root = widget_root
        self.confirm_lock = asyncio.Lock()

        # Ensure the Personal Agent always has the full 11-adapter WidgetProtocol 1.0 registry
        if registry is None or len(registry.list_widgets()) < len(ALL_ADAPTER_DOMAINS):
            async def _noop_changed(event: str, detail: str = '') -> None:
                store.bump(event, detail)

            self.registry = build_full_registry(
                store,
                life,
                changed or _noop_changed,
                task_services,
                timers=timers,
                refresh_weather=refresh_weather,
                widget_root=widget_root,
            )
        else:
            self.registry = registry

        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS hub_agent_runs (
                    id TEXT PRIMARY KEY,
                    request_key TEXT NOT NULL UNIQUE,
                    session_id TEXT,
                    mode TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    tier_used TEXT NOT NULL,
                    model_used TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary_text TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    preview_json TEXT,
                    preview_sha256 TEXT,
                    cost_microdollars INTEGER NOT NULL DEFAULT 0,
                    latency_ms REAL NOT NULL DEFAULT 0.0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_runs_created
                    ON hub_agent_runs(created_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS hub_agent_receipts (
                    run_id TEXT PRIMARY KEY REFERENCES hub_agent_runs(id) ON DELETE CASCADE,
                    preview_sha256 TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    executed_at TEXT NOT NULL
                );
            ''')

    @contextmanager
    def _db(self):
        with closing(self.store.connect()) as db:
            with db:
                yield db

    async def _notify(self, event: str, detail: str = '') -> None:
        if self.changed:
            await self.changed(event, detail)

    async def adapters_health(self) -> dict[str, Any]:
        """Query health across all 11 registered WidgetProtocol 1.0 adapters."""
        results: dict[str, Any] = {}
        for name in self.registry.list_widgets():
            adapter = self.registry.get(name)
            if adapter is not None:
                results[name] = await adapter.health(READ_CTX)
        return {
            'protocol_version': '1.0',
            'adapter_count': len(results),
            'adapters': results,
        }

    async def execute_adapter(
        self,
        raw_request: WidgetRequest | dict[str, Any],
        *,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Execute a canonical WidgetRequest through the 11-adapter WidgetRegistry."""
        req = (
            raw_request
            if isinstance(raw_request, WidgetRequest)
            else WidgetRequest.model_validate(raw_request)
        )
        adapter = self.registry.get(req.widget)
        spec = adapter.operations.get(req.action) if adapter else None
        if spec is not None and spec.read_only:
            ctx = READ_CTX
        elif f'{req.widget}.{req.action}' in self.registry.immediate_actions():
            ctx = TIMER_CTX
        elif confirmed:
            ctx = ExecutionContext(
                principal='personal-agent-direct',
                role='admin',
                permissions=frozenset({'read', 'write', 'control'}),
                confirmed_digest=request_digest(req),
            )
        else:
            ctx = PREVIEW_CTX
        res = await self.registry.execute(req, ctx)
        return res.model_dump(mode='json')

    def build_workspace_snapshot(self, at_iso: str | None = None) -> dict[str, Any]:
        """Gather a deterministic, privacy-bounded snapshot of the user's workspace."""
        now_iso = at_iso or utcnow()
        settings = self.store.get('settings') or {'timezone': 'Asia/Seoul', 'location_name': '', 'theme': 'light', 'title': 'My room'}
        tz_name = settings.get('timezone', 'Asia/Seoul')
        clock = clock_context(now_iso, tz_name)
        today_date = date.fromisoformat(clock['today'])
        past_7 = (today_date - timedelta(days=7)).isoformat()
        next_7 = (today_date + timedelta(days=7)).isoformat()

        today_tasks = self.store.query_tasks(clock['today'], clock['today'], 'all', limit=100)['items']
        upcoming_tasks = self.store.query_tasks(clock['today'], next_7, 'pending', limit=150)['items']
        overdue_tasks = [
            t for t in self.store.query_tasks(past_7, (today_date - timedelta(days=1)).isoformat(), 'pending', limit=50)['items']
        ]
        conflicts = detect_schedule_conflicts(upcoming_tasks)

        alarms = self.life.alarms()
        active_alarms = [a for a in alarms if a.get('enabled')]
        timers_snap = self.timers.snapshot() if self.timers else {'items': [], 'active_count': 0}
        running_timers = [t for t in timers_snap.get('items', []) if t.get('state') == 'running']

        # Note catalog metadata only (titles, IDs, versions, shared/pinned flags) — never private bodies!
        note_catalog = note_catalog_snapshot(self.store)
        shared_notes = [n for n in self.life.notes() if n.get('shared') or n.get('pinned')][:10]
        shared_note_summaries = [
            {
                'id': n['id'],
                'title': n['title'],
                'pinned': n['pinned'],
                'shared': n['shared'],
                'version': n['version'],
                'preview': (n['body'][:120] + '…' if len(n['body']) > 120 else n['body']) if n['shared'] else '(비공개 메모 · 제목만 표시)',
            }
            for n in shared_notes
        ]

        wx = self.store.get('weather') or {}
        current_wx = wx.get('current') or {}
        weather_summary = None
        if wx:
            weather_summary = {
                'location': settings.get('location_name') or '설정 지역',
                'temperature': wx.get('temperature', current_wx.get('temperature_2m')),
                'weather_code': current_wx.get('weather_code'),
                'fetched_at': wx.get('fetched_at'),
            }

        layout = self.store.get('layout') or {'columns': 8, 'rows': 6, 'version': 1, 'widgets': []}
        layout_summary = {
            'version': layout.get('version', 1),
            'widgets': [
                {'id': w['id'], 'type': w['type'], 'title': w.get('title', ''), 'x': w.get('x', 0), 'y': w.get('y', 0), 'w': w.get('w', 4), 'h': w.get('h', 2)}
                for w in layout.get('widgets', [])
            ],
        }

        return {
            'as_of': now_iso,
            'clock': clock,
            'settings': {
                'title': settings.get('title', 'My room'),
                'timezone': tz_name,
                'location_name': settings.get('location_name', ''),
                'theme': settings.get('theme', 'light'),
            },
            'layout': layout_summary,
            'weather': weather_summary,
            'today_tasks': today_tasks,
            'upcoming_pending_tasks': upcoming_tasks,
            'overdue_tasks': overdue_tasks,
            'schedule_conflicts': conflicts,
            'alarms': alarms,
            'active_alarms': active_alarms,
            'running_timers': running_timers,
            'note_catalog': note_catalog['items'],
            'shared_or_pinned_notes': shared_note_summaries,
            'registered_adapters': self.registry.list_widgets(),
        }

    def format_deterministic_briefing(self, snap: dict[str, Any]) -> str:
        """Render a clear Korean briefing directly from SQLite state ($0 cost)."""
        clock = snap['clock']
        lines = [f"[{clock['today']} ({clock['weekday']}) 개인 비서 브리핑]"]
        wx = snap.get('weather')
        if wx and wx.get('temperature') is not None:
            lines.append(f"• 날씨 ({wx['location']}): 현재 {wx['temperature']}°C")

        today_pending = [t for t in snap['today_tasks'] if not t['completed']]
        today_done = [t for t in snap['today_tasks'] if t['completed']]
        lines.append(f"• 오늘 할 일/일정: 남은 항목 {len(today_pending)}개 (완료 {len(today_done)}개)")
        for t in today_pending[:10]:
            prio = ' [중요]' if t.get('priority') == 'high' else ''
            lines.append(f"  - {t['time'] or '종일'} · {t['title']}{prio}")

        if snap['overdue_tasks']:
            lines.append(f"• 미완료 지난 할 일: {len(snap['overdue_tasks'])}개")
            for t in snap['overdue_tasks'][:5]:
                lines.append(f"  - {t['date']} · {t['title']}")

        if snap['schedule_conflicts']:
            lines.append(f"• 일정 시간 중복 경고: {len(snap['schedule_conflicts'])}건")
            for c in snap['schedule_conflicts']:
                titles = ', '.join(x['title'] for x in c['items'])
                lines.append(f"  - {c['date']} {c['time']}: {titles}")

        if snap['active_alarms']:
            alarm_str = ', '.join(f"{a['time']} ({a['label']})" for a in snap['active_alarms'][:5])
            lines.append(f"• 활성 알람 ({len(snap['active_alarms'])}개): {alarm_str}")
        else:
            lines.append('• 활성 알람: 없음')

        if snap['running_timers']:
            timer_str = ', '.join(f"{t['label']} (남은 {t['remaining_seconds']}초)" for t in snap['running_timers'])
            lines.append(f"• 실행 중인 타이머: {timer_str}")

        lines.append(f"• 저장된 메모: 총 {len(snap['note_catalog'])}개")
        return '\n'.join(lines)

    def _can_use_tier0(self, text: str, mode: str) -> bool:
        """Check if a request is a simple single-intent command suitable for Tier 0 ($0)."""
        if mode in {'briefing', 'deep', 'fast'}:
            return False
        if not self.governor.policy().prefer_tier0_rules and mode != 'local_only':
            return False
        if mode != 'local_only':
            domain_hits = sum(
                bool(re.search(pat, text))
                for pat in (
                    r'타이머',
                    r'알람',
                    r'메모',
                    r'일정|약속|회의|캘린더',
                    r'할\s*일|투두',
                    r'날씨',
                    r'레이아웃|위젯|배치',
                    r'설정|테마|다크\s*모드|라이트\s*모드',
                    r'시스템|기기|감사\s*로그',
                )
            )
            if domain_hits >= 2:
                return False
            if re.search(
                r'그리고|하고\s*나서|추가하고|잡고|켜고|맞추고|적고|해\s*주고|뒤에\s*또|동시에|같이\s*추가|한꺼번에|브리핑|우선순위|중요도|일정\s*조정|시간\s*변경|미뤄|옮겨|바꿔\s*줘',
                text,
            ):
                return False
        return True

    def _try_tier0_plan(self, text: str, at_iso: str, tz_name: str) -> dict[str, Any] | None:
        """Attempt zero-cost (<5ms) local resolution using Room Hub's deterministic parsers."""
        cleaned = re.sub(r'[?？!！.。]+$', '', text.strip()).strip()
        # 1. Exact timer command
        try:
            timer_plan = exact_timer(cleaned)
            if timer_plan:
                target = timer_plan.get('target') or {}
                args = timer_plan.get('args') or {}
                return {
                    'intent_type': 'action_plan',
                    'assistant_message': '로컬 타이머 규칙으로 즉시 처리합니다.',
                    'follow_up_question': None,
                    'steps': [
                        self._blank_step(
                            domain='timer',
                            action=timer_plan['action'],
                            target_id=target.get('value') if target.get('type') == 'item_id' else None,
                            target_title=target.get('value') if target.get('type') == 'reference' else None,
                            duration_seconds=args.get('duration_seconds'),
                            title=args.get('label'),
                        )
                    ],
                }
        except ValueError:
            pass

        # 2. Fast read (todo.list, calendar.query, memo.read)
        try:
            fast = match_read(cleaned, at_iso, tz_name)
            if fast:
                if fast.intent == 'memo.read':
                    return {
                        'intent_type': 'action_plan',
                        'assistant_message': '로컬 빠른 메모 읽기를 실행합니다.',
                        'follow_up_question': None,
                        'steps': [
                            self._blank_step(
                                domain='memo',
                                action='read',
                                target_title='last' if fast.memo_ref == 'last_modified' else 'current',
                            )
                        ],
                    }
                r = resolve_range(fast.date_ref, at_iso, tz_name)
                return {
                    'intent_type': 'action_plan',
                    'assistant_message': '로컬 빠른 일정/할 일 조회를 실행합니다.',
                    'follow_up_question': None,
                    'steps': [
                        self._blank_step(
                            domain='todo' if fast.intent == 'todo.list' else 'calendar',
                            action='list',
                            start_date=r.start,
                            end_date=r.end,
                            status_filter=fast.status,
                            period=fast.period,
                        )
                    ],
                }
        except ValueError:
            pass

        # 3. Named memo grounding (M3.3-B)
        memo_plan = parse_memo(text)
        if memo_plan and not memo_plan.issue:
            args = dict(memo_plan.arguments)
            return {
                'intent_type': 'action_plan',
                'assistant_message': '이름 기반 메모 규칙으로 대상을 확인합니다.',
                'follow_up_question': None,
                'steps': [
                    self._blank_step(
                        domain='memo',
                        action=memo_plan.action,
                        target_title=memo_plan.target_text,
                        body=args.get('body') or args.get('text'),
                    )
                ],
            }

        # 4. Semantic parser (M3.2)
        frame = parse_semantic(cleaned, at_iso, tz_name, raw=text)
        if frame and frame.confidence == 'EXACT':
            fargs = dict(frame.arguments)
            return {
                'intent_type': 'action_plan',
                'assistant_message': '시맨틱 파서(Tier 0)로 명령을 분석했습니다.',
                'follow_up_question': None,
                'steps': [
                    self._blank_step(
                        domain=frame.widget,
                        action=frame.action,
                        target_title=frame.target_text,
                        date=fargs.get('date'),
                        start_date=fargs.get('start'),
                        end_date=fargs.get('end'),
                        time=fargs.get('time'),
                        title=fargs.get('title'),
                        status_filter=fargs.get('status'),
                        period=fargs.get('period'),
                    )
                ],
            }
        return None

    @staticmethod
    def _blank_step(**overrides: Any) -> dict[str, Any]:
        base = {
            'domain': 'todo',
            'action': 'list',
            'target_id': None,
            'target_title': None,
            'date': None,
            'start_date': None,
            'end_date': None,
            'time': None,
            'title': None,
            'body': None,
            'category': None,
            'priority': None,
            'status_filter': None,
            'period': None,
            'repeat_frequency': None,
            'weekdays': None,
            'duration_seconds': None,
            'pinned': None,
            'shared': None,
        }
        base.update(overrides)
        return base

    def _build_planner_system_prompt(self, snap: dict[str, Any]) -> str:
        clock = snap['clock']
        tasks_compact = [
            {
                'id': t['id'],
                'title': t['title'],
                'date': t['date'],
                'time': t['time'],
                'completed': t['completed'],
                'priority': t.get('priority', 'normal'),
            }
            for t in (snap['today_tasks'] + snap['upcoming_pending_tasks'])[:40]
        ]
        alarms_compact = [
            {
                'id': a['id'],
                'label': a['label'],
                'time': a['time'],
                'repeat': a['repeat'],
                'date': a.get('date'),
                'weekdays': a.get('weekdays', []),
                'enabled': a['enabled'],
            }
            for a in snap['alarms'][:25]
        ]
        notes_compact = [
            {'id': n['id'], 'title': n['title'], 'shared': n['shared']}
            for n in snap['note_catalog'][:40]
        ]
        layout_widgets = snap.get('layout', {}).get('widgets', [])
        settings_info = snap.get('settings', {})
        return (
            '당신은 Room Hub의 개인 AI 비서 플래너(GPT-6.1 Sol High)입니다. 사용자의 한국어 요청을 분석해 '
            'Room Hub의 11개 위젯 및 시스템 어댑터(WidgetProtocol 1.0) 작업 계획 JSON을 생성하세요.\n'
            '지원 도메인(domain) 및 주요 동작(action):\n'
            '- todo (할 일 위젯): list, get, add, update, complete, reopen, delete\n'
            '- calendar (캘린더 위젯): list, get, add, update, delete\n'
            '- alarm (알람 위젯): list, events, set, update, cancel, snooze, ack\n'
            '- memo (노트/메모 위젯): read, list, write, append, clear, delete\n'
            '- timer (타이머 위젯): list, get, start(duration_seconds 1~600), stop\n'
            '- clock (시계 위젯): read, resolve(title 또는 body에 날짜 표현 지정)\n'
            '- weather (날씨 위젯): read, refresh\n'
            '- llm-response (AI 응답 위젯): list, read\n'
            '- layout (대시보드 레이아웃/위젯 배치): read, update_widget(target_id에 위젯 ID, title에 새 제목), add_widget, remove_widget, get_data, set_data\n'
            '- settings (허브 설정): read, update(title에 허브 제목, category에 light/dark 테마, target_title에 지역명)\n'
            '- system (시스템 상태/기기/감사로그): status, devices, audit\n'
            '규칙:\n'
            '1. 복합 명령(예: 일정 추가 + 알람 설정 + 메모 작성 + 타이머 시작 + 날씨/시스템 조회 + 테마/위젯 변경)은 steps 배열에 순서대로 담으세요.\n'
            '2. 기존 항목을 완료/수정/삭제/취소할 때는 아래 카탈로그에서 일치하는 target_id 또는 정확한 target_title을 지정하세요.\n'
            '3. 없는 날짜나 시간을 임의로 지어내지 말고, 필수 정보가 빠졌으면 intent_type="clarification"과 follow_up_question을 반환하세요.\n'
            '4. 변경 작업은 서버가 관리자 확인 후 실행하므로, assistant_message에 "미리보기를 준비했습니다"라고 안내하세요.\n'
            f"현재 기준 시각: {clock['local_at']} ({clock['timezone']}), 오늘={clock['today']} ({clock['weekday']}), 내일={clock['tomorrow']}\n"
            f'현재 허브 설정: {_json_dump(settings_info)}\n'
            f'현재 대시보드 배치 위젯: {_json_dump(layout_widgets)}\n'
            f'현재 할 일/일정 메타데이터: {_json_dump(tasks_compact)}\n'
            f'현재 알람 목록: {_json_dump(alarms_compact)}\n'
            f'현재 메모 제목 목록(본문 비공개): {_json_dump(notes_compact)}'
        )

    def _resolve_task_target(self, step: dict[str, Any], tz_name: str, at_iso: str) -> dict[str, Any]:
        tid = step.get('target_id')
        all_tasks = self.store.query_tasks('0001-01-01', '9999-12-31', 'all', limit=10000)['items']
        if tid:
            found = next((t for t in all_tasks if t['id'] == tid), None)
            if found:
                return found
        target_title = (step.get('target_title') or step.get('title') or '').strip()
        if not target_title:
            raise ValueError('대상 할 일/일정의 제목이나 ID를 확인할 수 없습니다.')
        day_filter = step.get('date')
        candidates = [t for t in all_tasks if (not day_filter or t['date'] == day_filter)]
        catalog = EntityCatalog.from_rows(
            'todo',
            candidates,
            source='room_hub_sqlite.tasks',
            as_of=at_iso,
            truncated=False,
            scope={'date': day_filter, 'completion_filter': 'all'},
        )
        res = catalog.resolve(target_title)
        if res.status != 'resolved':
            raise ValueError('일치하는 할 일/일정이 없거나 여러 개입니다. 날짜와 정확한 제목을 지정해 주세요.')
        return res.matches[0]

    def _resolve_alarm_target(self, step: dict[str, Any]) -> dict[str, Any]:
        alarms = self.life.alarms()
        tid = step.get('target_id')
        if tid:
            found = next((a for a in alarms if a['id'] == tid), None)
            if found:
                return found
        label = (step.get('target_title') or step.get('title') or '').strip()
        tm = step.get('time')
        matches = [
            a for a in alarms
            if (tm and a['time'] == tm) or (label and (a['label'] == label or label in a['label']))
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError('일치하는 알람을 찾지 못했습니다. 알람 시각이나 이름을 확인해 주세요.')
        raise ValueError('일치하는 알람이 여러 개입니다. 정확한 알람 시각과 이름을 함께 말씀해 주세요.')

    def _resolve_memo_target(self, step: dict[str, Any], at_iso: str) -> dict[str, Any]:
        notes = self.life.notes()
        tid = step.get('target_id')
        if tid:
            found = next((n for n in notes if n['id'] == tid), None)
            if found:
                return found
        ref = (step.get('target_title') or '').strip()
        if ref in {'current', 'last', 'last_modified'}:
            snap = memo_snapshot(self.store, 'last_modified' if ref in {'last', 'last_modified'} else 'current')
            if snap.get('note_id'):
                found = next((n for n in notes if n['id'] == snap['note_id']), None)
                if found:
                    return found
        if not ref:
            raise ValueError('대상 메모의 제목을 지정해 주세요.')
        snap_cat = note_catalog_snapshot(self.store)
        catalog = EntityCatalog.from_rows(
            'memo',
            snap_cat['items'],
            source=snap_cat['source'],
            as_of=at_iso,
            truncated=snap_cat['truncated'],
            scope={'selection': 'named_title'},
        )
        res = catalog.resolve(ref)
        if res.status != 'resolved':
            raise ValueError('일치하는 메모가 없거나 같은 제목이 여러 개입니다.')
        item = res.matches[0]
        found = next((n for n in notes if n['id'] == item['id']), None)
        if not found:
            raise ValueError('대상 메모를 찾을 수 없습니다.')
        return found

    def _resolve_widget_instance(self, step: dict[str, Any]) -> dict[str, Any]:
        layout = self.store.get('layout') or {'version': 1, 'widgets': []}
        widgets = layout.get('widgets', [])
        ref = (step.get('target_id') or step.get('target_title') or '').strip()
        if not ref:
            raise ValueError('대상 대시보드 위젯 ID 또는 이름을 지정해 주세요.')
        for w in widgets:
            if w['id'] == ref or w['type'] == ref or (w.get('title') and w['title'] == ref):
                return w
        for w in widgets:
            if ref in w['id'] or ref in w['type'] or (w.get('title') and ref in w['title']):
                return w
        raise ValueError(f'대시보드에서 위젯({ref})을 찾지 못했습니다.')

    async def _stage_via_protocol(
        self,
        *,
        step_idx: int,
        domain: str,
        action: str,
        req: WidgetRequest,
        summary: str,
        payload: dict[str, Any],
        target_lock: dict[str, Any] | None = None,
        conflicts: list[str] | None = None,
    ) -> dict[str, Any]:
        """Validate a mutation step through WidgetRegistry.execute(req, PREVIEW_CTX) and bind its digest."""
        preview_res = await self.registry.execute(req, PREVIEW_CTX)
        if preview_res.status != 'needs_confirmation':
            err_msg = (
                preview_res.error.message
                if preview_res.error
                else f'{domain}.{action} 요청 검증에 실패했습니다 ({preview_res.status}).'
            )
            raise ValueError(err_msg)
        req_dump = req.model_dump(mode='json')
        digest = preview_res.meta.request_digest or request_digest(req)
        entry: dict[str, Any] = {
            'step': step_idx,
            'domain': domain,
            'action': action,
            'summary': summary,
            'payload': payload,
            'target_lock': target_lock,
            'widget_request': req_dump,
            'request_digest': digest,
        }
        if conflicts is not None:
            entry['conflicts'] = conflicts
        return entry

    async def _ground_and_stage_steps(
        self,
        run_id: str,
        steps: list[dict[str, Any]],
        snap: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        """Execute read/immediate steps via WidgetRegistry; stage mutations with WidgetProtocol 1.0 digests."""
        clock = snap['clock']
        tz_name = clock['timezone']
        at_iso = snap['as_of']
        executed_reads: list[dict[str, Any]] = []
        staged_mutations: list[dict[str, Any]] = []
        output_lines: list[str] = []

        for idx, step in enumerate(steps[:12], 1):
            raw_domain = step['domain']
            domain = 'clock' if raw_domain == 'time' else raw_domain
            action = step['action']
            step_key = f'{run_id}:step:{idx}'

            # --- 1. Read operations via WidgetRegistry.execute(req, READ_CTX) ---
            if domain in {'todo', 'calendar'} and action == 'list':
                start = step.get('start_date') or step.get('date') or clock['today']
                end = step.get('end_date') or step.get('date') or start
                status_filter = step.get('status_filter') or ('pending' if domain == 'todo' else 'all')
                period = step.get('period')
                args: dict[str, Any] = {'start': start, 'end': end, 'status': status_filter, 'limit': 50}
                if period in {'morning', 'afternoon'}:
                    args['period'] = period
                req = WidgetRequest(request_id=step_key, widget=domain, action='list', args=args)
                res = await self.registry.execute(req, READ_CTX)
                if res.status != 'success':
                    raise ValueError(res.error.message if res.error else '일정/할 일 목록을 조회하지 못했습니다.')
                data = res.data
                executed_reads.append({'step': idx, 'domain': domain, 'action': 'list', 'data': data})
                label = '할 일' if domain == 'todo' else '일정'
                output_lines.append(f"• {start}{('~' + end) if end != start else ''} {label}: {data['count']}개")
                for item in data['items'][:15]:
                    done_mark = ' [완료]' if item['completed'] else ''
                    output_lines.append(f"  - {item['date']} {item['time'] or '종일'} · {item['title']}{done_mark}")

            elif domain in {'todo', 'calendar'} and action == 'get':
                target = self._resolve_task_target(step, tz_name, at_iso)
                req = WidgetRequest(
                    request_id=step_key,
                    widget=domain,
                    action='get',
                    target={'type': 'item_id', 'value': target['id']},
                )
                res = await self.registry.execute(req, READ_CTX)
                if res.status != 'success':
                    raise ValueError(res.error.message if res.error else '항목을 조회하지 못했습니다.')
                executed_reads.append({'step': idx, 'domain': domain, 'action': 'get', 'data': res.data})
                output_lines.append(f"• 항목 확인: {res.data['date']} {res.data['time'] or '종일'} · {res.data['title']}")

            elif domain == 'memo' and action == 'read':
                ref = (step.get('target_title') or 'current').strip()
                if ref in {'current', 'last', 'last_modified'} and not step.get('target_id'):
                    req = WidgetRequest(
                        request_id=step_key,
                        widget='memo',
                        action='read',
                        target={'type': 'reference', 'value': 'last' if ref in {'last', 'last_modified'} else 'current'},
                    )
                else:
                    note = self._resolve_memo_target(step, at_iso)
                    req = WidgetRequest(
                        request_id=step_key,
                        widget='memo',
                        action='read',
                        target={'type': 'item_id', 'value': note['id']},
                    )
                res = await self.registry.execute(req, READ_CTX)
                if res.status != 'success':
                    raise ValueError(res.error.message if res.error else '메모를 읽지 못했습니다.')
                executed_reads.append({'step': idx, 'domain': 'memo', 'action': 'read', 'data': res.data})
                output_lines.append(f"• 메모 [{res.data['title']}]: {res.data['body'] or '(본문 없음)'}")

            elif domain == 'memo' and action == 'list':
                req = WidgetRequest(request_id=step_key, widget='memo', action='list', args={})
                res = await self.registry.execute(req, READ_CTX)
                if res.status != 'success':
                    raise ValueError(res.error.message if res.error else '메모 목록을 조회하지 못했습니다.')
                executed_reads.append({'step': idx, 'domain': 'memo', 'action': 'list', 'data': res.data})
                output_lines.append(f"• 저장된 메모 목록: {res.data['count']}개")
                for n in res.data['items'][:10]:
                    output_lines.append(f"  - {n['title']} (v{n['version']})")

            elif domain == 'alarm' and action in {'list', 'events'}:
                req = WidgetRequest(request_id=step_key, widget='alarm', action=action, args={})
                res = await self.registry.execute(req, READ_CTX)
                if res.status != 'success':
                    raise ValueError(res.error.message if res.error else '알람 정보를 조회하지 못했습니다.')
                executed_reads.append({'step': idx, 'domain': 'alarm', 'action': action, 'data': res.data})
                if action == 'list':
                    alarms = res.data['items']
                    output_lines.append(f'• 등록된 알람: {len(alarms)}개')
                    for a in alarms[:10]:
                        output_lines.append(f"  - {a['time']} · {a['label']} ({'켜짐' if a['enabled'] else '꺼짐'})")
                else:
                    evs = res.data['items']
                    output_lines.append(f'• 최근 알람 이벤트: {len(evs)}건')

            elif domain == 'timer' and action in {'list', 'get'}:
                if action == 'list':
                    req = WidgetRequest(request_id=step_key, widget='timer', action='list', args={})
                else:
                    ref_val = step.get('target_id') or step.get('target_title') or 'current'
                    ttype = 'item_id' if step.get('target_id') else 'reference'
                    req = WidgetRequest(
                        request_id=step_key,
                        widget='timer',
                        action='get',
                        target={'type': ttype, 'value': ref_val},
                    )
                res = await self.registry.execute(req, READ_CTX)
                if res.status != 'success':
                    raise ValueError(res.error.message if res.error else '타이머를 조회하지 못했습니다.')
                executed_reads.append({'step': idx, 'domain': 'timer', 'action': action, 'data': res.data})
                if action == 'list':
                    output_lines.append(f"• 활성 타이머: {res.data['active_count']}개")
                else:
                    output_lines.append(f"• 타이머 [{res.data['label']}]: 남은 {res.data['remaining_seconds']}초")

            elif domain == 'weather' and action in {'read', 'list', 'refresh'}:
                w_action = 'refresh' if action == 'refresh' else 'read'
                req = WidgetRequest(request_id=step_key, idempotency_key=step_key, widget='weather', action=w_action, args={})
                ctx = TIMER_CTX if w_action == 'refresh' else READ_CTX
                res = await self.registry.execute(req, ctx)
                if res.status != 'success':
                    raise ValueError(res.error.message if res.error else '날씨 정보를 조회하지 못했습니다.')
                executed_reads.append({'step': idx, 'domain': 'weather', 'action': w_action, 'data': res.data})
                if res.data.get('temperature') is not None:
                    output_lines.append(f"• 현재 날씨 ({res.data.get('location_name') or '설정 지역'}): {res.data['temperature']}°C")
                else:
                    output_lines.append('• 저장된 날씨 정보가 없습니다.')

            elif domain == 'clock' and action in {'read', 'list', 'resolve'}:
                c_action = 'resolve' if action == 'resolve' else 'read'
                if c_action == 'resolve':
                    expr = (step.get('title') or step.get('target_title') or step.get('body') or '오늘').strip()
                    req = WidgetRequest(request_id=step_key, widget='clock', action='resolve', args={'expression': expr})
                else:
                    req = WidgetRequest(request_id=step_key, widget='clock', action='read', args={})
                res = await self.registry.execute(req, READ_CTX)
                if res.status != 'success':
                    raise ValueError(res.error.message if res.error else '시계 정보를 조회하지 못했습니다.')
                executed_reads.append({'step': idx, 'domain': raw_domain, 'action': c_action, 'data': res.data})
                if c_action == 'resolve':
                    output_lines.append(f"• 날짜 해석 ({res.data['expression']}): {res.data['start']} ~ {res.data['end']}")
                else:
                    output_lines.append(f"• 현재 시각: {res.data['local_at']} ({res.data['timezone']})")

            elif domain == 'llm-response' and action in {'list', 'read'}:
                if action == 'list':
                    req = WidgetRequest(request_id=step_key, widget='llm-response', action='list', args={})
                else:
                    ref_val = step.get('target_id') or 'latest'
                    ttype = 'item_id' if step.get('target_id') else 'reference'
                    req = WidgetRequest(
                        request_id=step_key,
                        widget='llm-response',
                        action='read',
                        target={'type': ttype, 'value': ref_val},
                    )
                res = await self.registry.execute(req, READ_CTX)
                if res.status != 'success':
                    raise ValueError(res.error.message if res.error else 'AI 응답 위젯 기록을 조회하지 못했습니다.')
                executed_reads.append({'step': idx, 'domain': 'llm-response', 'action': action, 'data': res.data})
                if action == 'list':
                    output_lines.append(f"• AI 응답 위젯 기록: 최근 에이전트 실행 {res.data['agent_run_count']}건")
                else:
                    output_lines.append(f"• 최근 AI 응답: {res.data['summary_text'][:120]}")

            elif domain == 'layout' and action in {'read', 'list', 'get_data'}:
                l_action = 'get_data' if action == 'get_data' else 'read'
                if l_action == 'get_data':
                    kind = (step.get('target_id') or step.get('target_title') or step.get('category') or 'note').strip()
                    req = WidgetRequest(
                        request_id=step_key,
                        widget='layout',
                        action='get_data',
                        target={'type': 'widget_id', 'value': kind},
                    )
                else:
                    req = WidgetRequest(request_id=step_key, widget='layout', action='read', args={})
                res = await self.registry.execute(req, READ_CTX)
                if res.status != 'success':
                    raise ValueError(res.error.message if res.error else '대시보드 레이아웃을 조회하지 못했습니다.')
                executed_reads.append({'step': idx, 'domain': 'layout', 'action': l_action, 'data': res.data})
                if l_action == 'read':
                    w_names = ', '.join(f"{w['id']}({w['type']})" for w in res.data['widgets'])
                    output_lines.append(f"• 대시보드 배치 위젯 ({len(res.data['widgets'])}개): {w_names}")
                else:
                    output_lines.append(f"• 위젯 데이터 [{res.data['widget_type']}] 조회 완료")

            elif domain == 'settings' and action in {'read', 'list'}:
                req = WidgetRequest(request_id=step_key, widget='settings', action='read', args={})
                res = await self.registry.execute(req, READ_CTX)
                if res.status != 'success':
                    raise ValueError(res.error.message if res.error else '허브 설정을 조회하지 못했습니다.')
                executed_reads.append({'step': idx, 'domain': 'settings', 'action': 'read', 'data': res.data})
                output_lines.append(
                    f"• 허브 설정: 제목='{res.data['title']}', 테마={res.data['theme']}, 시간대={res.data['timezone']}, 위치='{res.data['location_name']}'"
                )

            elif domain == 'system' and action in {'status', 'read', 'list', 'devices', 'audit'}:
                s_action = action if action in {'status', 'devices', 'audit'} else 'status'
                s_args = {'limit': step.get('duration_seconds') or 20} if s_action == 'audit' else {}
                req = WidgetRequest(request_id=step_key, widget='system', action=s_action, args=s_args)
                res = await self.registry.execute(req, READ_CTX)
                if res.status != 'success':
                    raise ValueError(res.error.message if res.error else '시스템 정보를 조회하지 못했습니다.')
                executed_reads.append({'step': idx, 'domain': 'system', 'action': s_action, 'data': res.data})
                if s_action == 'status':
                    output_lines.append(
                        f"• 시스템 상태 (rev {res.data['revision']}): 작업 {res.data['task_count']}개, 메모 {res.data['note_count']}개, 알람 {res.data['alarm_count']}개, 위젯 {res.data['placed_widget_count']}개"
                    )
                elif s_action == 'devices':
                    output_lines.append(f"• 등록된 디스플레이 기기: {res.data['count']}대")
                else:
                    output_lines.append(f"• 최근 감사 로그: {res.data['count']}건")

            # --- 2. Immediate timer controls via WidgetRegistry.execute(req, TIMER_CTX) ---
            elif domain == 'timer' and action in {'start', 'stop'}:
                if action == 'start':
                    dur = step.get('duration_seconds')
                    if not isinstance(dur, int) or not 1 <= dur <= 600:
                        raise ValueError('타이머 시간은 1초에서 600초(10분) 사이로 지정해 주세요.')
                    req = WidgetRequest(
                        request_id=step_key,
                        idempotency_key=step_key,
                        widget='timer',
                        action='start',
                        args={'duration_seconds': dur, 'label': step.get('title') or '타이머'},
                    )
                else:
                    ref_val = step.get('target_id') or step.get('target_title') or 'current'
                    target_type = 'item_id' if step.get('target_id') else 'reference'
                    req = WidgetRequest(
                        request_id=step_key,
                        idempotency_key=step_key,
                        widget='timer',
                        action='stop',
                        target={'type': target_type, 'value': ref_val},
                        args={},
                    )
                res = await self.registry.execute(req, TIMER_CTX)
                if res.status != 'success':
                    raise ValueError(res.error.message if res.error else '타이머 제어를 실행하지 못했습니다.')
                executed_reads.append({'step': idx, 'domain': 'timer', 'action': action, 'data': res.data})
                output_lines.append(
                    f"• 타이머 {'시작' if action == 'start' else '종료'} 완료: {res.data['label']} ({res.data['duration_seconds']}초)"
                )

            # --- 3. Confirmation-gated mutations via WidgetRegistry.execute(req, PREVIEW_CTX) ---
            elif domain in {'todo', 'calendar'} and action == 'add':
                title = (step.get('title') or step.get('target_title') or '').strip()
                day = step.get('date') or step.get('start_date') or clock['today']
                if not title:
                    raise ValueError('추가할 할 일/일정의 제목을 말씀해 주세요.')
                freq = step.get('repeat_frequency') or 'none'
                weekdays = step.get('weekdays') or []
                repeat_rule = RepeatRule(
                    frequency=freq if freq in {'none', 'daily', 'weekdays', 'weekly', 'monthly'} else 'none',
                    weekdays=weekdays,
                    until=date.fromisoformat(step['end_date']) if step.get('end_date') and freq != 'none' else None,
                )
                raw_cat = (step.get('category') or 'personal').strip()
                safe_cat = raw_cat if re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', raw_cat) else 'personal'
                task_create = TaskCreate(
                    title=title,
                    date=date.fromisoformat(day),
                    time=step.get('time'),
                    category=safe_cat,
                    priority=step.get('priority') if step.get('priority') in {'normal', 'high'} else 'normal',
                    notes=(step.get('body') or '')[:2000],
                    repeat=repeat_rule,
                )
                same_day = self.store.query_tasks(day, day, 'pending', limit=100)['items']
                slot_conflicts = [
                    t['title'] for t in same_day if task_create.time and t.get('time') == task_create.time
                ]
                payload_dict = task_create.model_dump(mode='json')
                req = WidgetRequest(
                    request_id=step_key,
                    idempotency_key=step_key,
                    widget=domain,
                    action='add',
                    args=payload_dict,
                )
                staged_mutations.append(
                    await self._stage_via_protocol(
                        step_idx=idx,
                        domain=domain,
                        action='add',
                        req=req,
                        summary=f"{day} {task_create.time or '종일'} · {task_create.title} ({'일정' if domain == 'calendar' else '할 일'} 추가)",
                        payload=payload_dict,
                        target_lock=None,
                        conflicts=slot_conflicts,
                    )
                )

            elif domain in {'todo', 'calendar'} and action in {'complete', 'reopen', 'delete', 'update'}:
                target = self._resolve_task_target(step, tz_name, at_iso)
                lock = {
                    'table': 'tasks',
                    'id': target['id'],
                    'version': target['version'],
                    'title': target['title'],
                    'date': target['date'],
                }
                adapter_widget = 'todo' if action in {'complete', 'reopen'} else domain
                if action == 'update':
                    patch_fields: dict[str, Any] = {'version': target['version']}
                    if step.get('title'):
                        patch_fields['title'] = step['title'].strip()
                    if step.get('date'):
                        patch_fields['date'] = step['date']
                    if step.get('time') is not None:
                        patch_fields['time'] = step['time']
                    if step.get('priority') in {'normal', 'high'}:
                        patch_fields['priority'] = step['priority']
                    if step.get('category'):
                        raw_cat = step['category'].strip()
                        if re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', raw_cat):
                            patch_fields['category'] = raw_cat
                    if step.get('body') is not None:
                        patch_fields['notes'] = step['body']
                    patch = TaskPatch.model_validate(patch_fields)
                    payload_dict = patch.model_dump(exclude_unset=True, mode='json')
                    req = WidgetRequest(
                        request_id=step_key,
                        idempotency_key=step_key,
                        widget=adapter_widget,
                        action='update',
                        target={'type': 'item_id', 'value': target['id']},
                        args=payload_dict,
                    )
                    staged_mutations.append(
                        await self._stage_via_protocol(
                            step_idx=idx,
                            domain=domain,
                            action='update',
                            req=req,
                            summary=f"일정/할 일 수정: {target['title']} (v{target['version']})",
                            payload=payload_dict,
                            target_lock=lock,
                        )
                    )
                else:
                    verb_ko = {'complete': '완료', 'reopen': '완료 취소', 'delete': '삭제'}[action]
                    payload_dict = {'version': target['version']}
                    req = WidgetRequest(
                        request_id=step_key,
                        idempotency_key=step_key,
                        widget=adapter_widget,
                        action=action,
                        target={'type': 'item_id', 'value': target['id']},
                        args=payload_dict,
                    )
                    staged_mutations.append(
                        await self._stage_via_protocol(
                            step_idx=idx,
                            domain=domain,
                            action=action,
                            req=req,
                            summary=f"{target['date']} · {target['title']} ({verb_ko}, v{target['version']})",
                            payload=payload_dict,
                            target_lock=lock,
                        )
                    )

            elif domain == 'alarm' and action == 'set':
                tm = step.get('time')
                if not tm:
                    raise ValueError('알람을 맞출 정확한 시각(HH:MM)을 말씀해 주세요.')
                weekdays = step.get('weekdays') or []
                repeat_mode: Literal['once', 'weekly'] = 'weekly' if (
                    step.get('repeat_frequency') in {'weekly', 'weekdays', 'daily'} or weekdays
                ) else 'once'
                if step.get('repeat_frequency') == 'weekdays' and not weekdays:
                    weekdays = [0, 1, 2, 3, 4]
                elif step.get('repeat_frequency') == 'daily' and not weekdays:
                    weekdays = [0, 1, 2, 3, 4, 5, 6]
                alarm_date = None if repeat_mode == 'weekly' else (step.get('date') or clock['today'])
                alarm_args = {
                    'label': (step.get('title') or step.get('target_title') or '알람').strip()[:120],
                    'time': tm,
                    'timezone': tz_name,
                    'repeat': repeat_mode,
                    'date': alarm_date,
                    'weekdays': weekdays if repeat_mode == 'weekly' else [],
                    'enabled': True,
                }
                req = WidgetRequest(
                    request_id=step_key,
                    idempotency_key=step_key,
                    widget='alarm',
                    action='set',
                    args=alarm_args,
                )
                staged_mutations.append(
                    await self._stage_via_protocol(
                        step_idx=idx,
                        domain='alarm',
                        action='set',
                        req=req,
                        summary=f"알람 설정: {alarm_date or ('요일반복 ' + str(weekdays))} {tm} · {alarm_args['label']}",
                        payload=alarm_args,
                        target_lock=None,
                    )
                )

            elif domain == 'alarm' and action in {'cancel', 'delete', 'update'}:
                alarm = self._resolve_alarm_target(step)
                lock = {
                    'table': 'hub_alarms',
                    'id': alarm['id'],
                    'version': alarm['version'],
                    'title': alarm['label'],
                }
                if action == 'update':
                    upd_args = {
                        'label': (step.get('title') or alarm['label']).strip()[:120],
                        'time': step.get('time') or alarm['time'],
                        'timezone': alarm.get('timezone') or tz_name,
                        'repeat': alarm['repeat'],
                        'date': step.get('date') or alarm.get('date'),
                        'weekdays': step.get('weekdays') if step.get('weekdays') is not None else alarm.get('weekdays', []),
                        'enabled': True if step.get('pinned') is None else bool(step['pinned']),
                        'version': alarm['version'],
                    }
                    req = WidgetRequest(
                        request_id=step_key,
                        idempotency_key=step_key,
                        widget='alarm',
                        action='update',
                        target={'type': 'item_id', 'value': alarm['id']},
                        args=upd_args,
                    )
                    staged_mutations.append(
                        await self._stage_via_protocol(
                            step_idx=idx,
                            domain='alarm',
                            action='update',
                            req=req,
                            summary=f"알람 수정: {upd_args['time']} · {upd_args['label']} (v{alarm['version']})",
                            payload=upd_args,
                            target_lock=lock,
                        )
                    )
                else:
                    payload_dict = {'version': alarm['version']}
                    req = WidgetRequest(
                        request_id=step_key,
                        idempotency_key=step_key,
                        widget='alarm',
                        action='cancel',
                        target={'type': 'item_id', 'value': alarm['id']},
                        args=payload_dict,
                    )
                    staged_mutations.append(
                        await self._stage_via_protocol(
                            step_idx=idx,
                            domain='alarm',
                            action='cancel',
                            req=req,
                            summary=f"알람 취소: {alarm['time']} · {alarm['label']} (v{alarm['version']})",
                            payload=payload_dict,
                            target_lock=lock,
                        )
                    )

            elif domain == 'memo' and action in {'write', 'append', 'clear', 'delete'}:
                if action == 'write' and not step.get('target_id') and not step.get('target_title'):
                    title = (step.get('title') or '').strip()
                    body = step.get('body') or ''
                    if not title:
                        raise ValueError('새 메모의 제목을 지정해 주세요.')
                    payload_dict = {
                        'title': title,
                        'body': body,
                        'pinned': bool(step.get('pinned') or False),
                        'shared': bool(step.get('shared') or False),
                    }
                    req = WidgetRequest(
                        request_id=step_key,
                        idempotency_key=step_key,
                        widget='memo',
                        action='write',
                        args=payload_dict,
                    )
                    staged_mutations.append(
                        await self._stage_via_protocol(
                            step_idx=idx,
                            domain='memo',
                            action='write_new',
                            req=req,
                            summary=f'새 메모 작성: {title}',
                            payload=payload_dict,
                            target_lock=None,
                        )
                    )
                else:
                    note = self._resolve_memo_target(step, at_iso)
                    lock = {
                        'table': 'hub_notes',
                        'id': note['id'],
                        'version': note['version'],
                        'title': note['title'],
                    }
                    if action == 'delete':
                        payload_dict = {'version': note['version']}
                        req = WidgetRequest(
                            request_id=step_key,
                            idempotency_key=step_key,
                            widget='memo',
                            action='delete',
                            target={'type': 'item_id', 'value': note['id']},
                            args=payload_dict,
                        )
                        summary = f"메모 삭제: {note['title']} (v{note['version']})"
                    elif action == 'clear':
                        payload_dict = {'version': note['version']}
                        req = WidgetRequest(
                            request_id=step_key,
                            idempotency_key=step_key,
                            widget='memo',
                            action='clear',
                            target={'type': 'item_id', 'value': note['id']},
                            args=payload_dict,
                        )
                        summary = f"메모 본문 비우기: {note['title']} (v{note['version']})"
                    elif action == 'append':
                        add_text = step.get('body') or ''
                        if not add_text.strip():
                            raise ValueError('메모에 덧붙일 내용을 말씀해 주세요.')
                        sep_text = ('\n' + add_text) if (note['body'] and not add_text.startswith('\n')) else add_text
                        payload_dict = {'text': sep_text, 'version': note['version']}
                        req = WidgetRequest(
                            request_id=step_key,
                            idempotency_key=step_key,
                            widget='memo',
                            action='append',
                            target={'type': 'item_id', 'value': note['id']},
                            args=payload_dict,
                        )
                        summary = f"메모 내용 추가: {note['title']} (v{note['version']})"
                    else:
                        new_body = step.get('body') or ''
                        payload_dict = {
                            'title': step.get('title') or note['title'],
                            'body': new_body,
                            'pinned': note['pinned'] if step.get('pinned') is None else bool(step['pinned']),
                            'shared': note['shared'] if step.get('shared') is None else bool(step['shared']),
                            'version': note['version'],
                        }
                        req = WidgetRequest(
                            request_id=step_key,
                            idempotency_key=step_key,
                            widget='memo',
                            action='write',
                            target={'type': 'item_id', 'value': note['id']},
                            args=payload_dict,
                        )
                        summary = f"메모 본문 교체: {note['title']} (v{note['version']})"
                    staged_mutations.append(
                        await self._stage_via_protocol(
                            step_idx=idx,
                            domain='memo',
                            action=action,
                            req=req,
                            summary=summary,
                            payload=payload_dict,
                            target_lock=lock,
                        )
                    )

            elif domain == 'layout' and action in {'update_widget', 'update', 'add_widget', 'add', 'remove_widget', 'delete', 'set_data'}:
                current_layout = self.store.get('layout') or {'version': 1, 'widgets': []}
                layout_ver = current_layout.get('version', 1)
                if action in {'update_widget', 'update'}:
                    w_inst = self._resolve_widget_instance(step)
                    w_args: dict[str, Any] = {'version': layout_ver}
                    if step.get('title') is not None:
                        w_args['title'] = step['title']
                    if step.get('body'):
                        try:
                            extra = json.loads(step['body'])
                            if isinstance(extra, dict):
                                for k in ('x', 'y', 'w', 'h', 'config', 'title'):
                                    if k in extra:
                                        w_args[k] = extra[k]
                        except ValueError:
                            pass
                    req = WidgetRequest(
                        request_id=step_key,
                        idempotency_key=step_key,
                        widget='layout',
                        action='update_widget',
                        target={'type': 'widget_id', 'value': w_inst['id']},
                        args=w_args,
                    )
                    staged_mutations.append(
                        await self._stage_via_protocol(
                            step_idx=idx,
                            domain='layout',
                            action='update_widget',
                            req=req,
                            summary=f"대시보드 위젯 수정: {w_inst['id']} (v{layout_ver})",
                            payload=w_args,
                            target_lock=None,
                        )
                    )
                elif action in {'add_widget', 'add'}:
                    w_type = (step.get('category') or step.get('target_title') or 'note').strip()
                    w_id = (step.get('target_id') or f"{w_type}-{uid()[:6]}").strip()
                    w_args = {
                        'version': layout_ver,
                        'id': w_id,
                        'type': w_type,
                        'title': step.get('title') or w_type,
                        'x': 0,
                        'y': 0,
                        'w': 4,
                        'h': 2,
                    }
                    if step.get('body'):
                        try:
                            extra = json.loads(step['body'])
                            if isinstance(extra, dict):
                                for k in ('x', 'y', 'w', 'h', 'config', 'title', 'id', 'type'):
                                    if k in extra:
                                        w_args[k] = extra[k]
                        except ValueError:
                            pass
                    req = WidgetRequest(
                        request_id=step_key,
                        idempotency_key=step_key,
                        widget='layout',
                        action='add_widget',
                        args=w_args,
                    )
                    staged_mutations.append(
                        await self._stage_via_protocol(
                            step_idx=idx,
                            domain='layout',
                            action='add_widget',
                            req=req,
                            summary=f"대시보드 위젯 추가: {w_args['id']} ({w_args['type']})",
                            payload=w_args,
                            target_lock=None,
                        )
                    )
                elif action in {'remove_widget', 'delete'}:
                    w_inst = self._resolve_widget_instance(step)
                    w_args = {'version': layout_ver}
                    req = WidgetRequest(
                        request_id=step_key,
                        idempotency_key=step_key,
                        widget='layout',
                        action='remove_widget',
                        target={'type': 'widget_id', 'value': w_inst['id']},
                        args=w_args,
                    )
                    staged_mutations.append(
                        await self._stage_via_protocol(
                            step_idx=idx,
                            domain='layout',
                            action='remove_widget',
                            req=req,
                            summary=f"대시보드 위젯 제거: {w_inst['id']} (v{layout_ver})",
                            payload=w_args,
                            target_lock=None,
                        )
                    )
                else:
                    kind = (step.get('target_id') or step.get('target_title') or step.get('category') or 'note').strip()
                    custom_data: dict[str, Any] = {}
                    if step.get('body'):
                        try:
                            parsed = json.loads(step['body'])
                            if isinstance(parsed, dict):
                                custom_data = parsed
                            else:
                                custom_data = {'value': parsed}
                        except ValueError:
                            custom_data = {'note': step['body']}
                    req = WidgetRequest(
                        request_id=step_key,
                        idempotency_key=step_key,
                        widget='layout',
                        action='set_data',
                        target={'type': 'widget_id', 'value': kind},
                        args={'data': custom_data},
                    )
                    staged_mutations.append(
                        await self._stage_via_protocol(
                            step_idx=idx,
                            domain='layout',
                            action='set_data',
                            req=req,
                            summary=f'위젯 커스텀 데이터 저장: {kind}',
                            payload={'data': custom_data},
                            target_lock=None,
                        )
                    )

            elif domain == 'settings' and action == 'update':
                s_args: dict[str, Any] = {}
                if step.get('title'):
                    s_args['title'] = step['title'].strip()
                if step.get('target_title'):
                    s_args['location_name'] = step['target_title'].strip()
                for candidate in (step.get('category'), step.get('body')):
                    if not candidate:
                        continue
                    c_str = candidate.strip()
                    if c_str in {'light', 'dark'}:
                        s_args['theme'] = c_str
                    elif '/' in c_str or c_str == 'UTC':
                        s_args['timezone'] = c_str
                    elif c_str.startswith('{'):
                        try:
                            extra = json.loads(c_str)
                            if isinstance(extra, dict):
                                s_args.update(extra)
                        except ValueError:
                            pass
                if step.get('duration_seconds') and 5 <= step['duration_seconds'] <= 180:
                    s_args['weather_interval_minutes'] = step['duration_seconds']
                req = WidgetRequest(
                    request_id=step_key,
                    idempotency_key=step_key,
                    widget='settings',
                    action='update',
                    args=s_args,
                )
                staged_mutations.append(
                    await self._stage_via_protocol(
                        step_idx=idx,
                        domain='settings',
                        action='update',
                        req=req,
                        summary=f"허브 설정 변경: {_json_dump(s_args)}",
                        payload=s_args,
                        target_lock=None,
                    )
                )

            else:
                raise ValueError(f'지원하지 않는 도메인/동작 조합입니다: {domain}.{action}')

        return executed_reads, staged_mutations, output_lines

    async def submit_command(self, body: AgentCommandInput) -> dict[str, Any]:
        """Process a natural-language command through the Personal AI Agent."""
        with self._db() as db:
            existing = db.execute(
                'SELECT id FROM hub_agent_runs WHERE request_key = ?',
                (body.request_id,),
            ).fetchone()
        if existing:
            return self.get_run(existing['id']) | {'duplicate': True}

        started = time.perf_counter()
        now_iso = utcnow()
        snap = self.build_workspace_snapshot(now_iso)
        tz_name = snap['clock']['timezone']
        run_id = uid()

        if body.mode == 'briefing':
            return await self._run_briefing(run_id, body, snap, started)

        tier_used = 'tier0_local_rule'
        model_used = 'local/deterministic'
        cost_micros = 0
        plan: dict[str, Any] | None = None

        if self._can_use_tier0(body.text, body.mode):
            plan = self._try_tier0_plan(body.text, now_iso, tz_name)

        if plan is None:
            if body.mode == 'local_only':
                plan = {
                    'intent_type': 'clarification',
                    'assistant_message': (
                        '로컬 전용 모드에서는 단일 일정/할 일 조회·추가, 이름 지정 메모, '
                        '정확한 알람/타이머 명령만 처리합니다. 한 문장으로 날짜와 제목을 명확히 말씀해 주세요.'
                    ),
                    'follow_up_question': '어느 날짜의 어떤 할 일/일정 또는 메모 작업인지 다시 말씀해 주시겠어요?',
                    'steps': [],
                }
            else:
                category = 'agent_deep' if body.mode == 'deep' else 'agent_fast'
                tier_used = 'tier3_openai_deep' if category == 'agent_deep' else 'tier2_openai_fast'
                sys_prompt = self._build_planner_system_prompt(snap)
                try:
                    llm_res = await self.gateway.generate_structured(
                        request_id=f'agent:{run_id}',
                        category=category,
                        system_prompt=sys_prompt,
                        user_text=body.text,
                        schema_name='personal_agent_plan',
                        schema=AGENT_PLAN_SCHEMA,
                        max_output_tokens=600 if category == 'agent_deep' else 450,
                        temperature=0.0,
                        at_iso=now_iso,
                    )
                    plan = llm_res['parsed']
                    model_used = llm_res['selected_model']
                    cost_micros = llm_res['cost_microdollars']
                except OpenAIGatewayError as exc:
                    tier_used = 'tier0_fallback'
                    model_used = 'local/fallback'
                    fallback_plan = self._try_tier0_plan(body.text, now_iso, tz_name)
                    if fallback_plan is not None:
                        plan = fallback_plan
                    else:
                        plan = {
                            'intent_type': 'clarification',
                            'assistant_message': f'{exc} (로컬 규칙으로 즉시 해석할 수 없는 복합 요청입니다.)',
                            'follow_up_question': '단일 일정·할 일·메모·타이머 명령으로 나누어 다시 요청해 주세요.',
                            'steps': [],
                        }

        if plan.get('intent_type') == 'briefing' and not plan.get('steps'):
            return await self._run_briefing(run_id, body, snap, started)

        status = 'succeeded'
        preview_obj = None
        preview_hash = None
        summary_parts = [plan.get('assistant_message') or '요청을 분석했습니다.']

        try:
            reads, mutations, read_lines = await self._ground_and_stage_steps(
                run_id,
                plan.get('steps') or [],
                snap,
            )
            if read_lines:
                summary_parts.extend(read_lines)
            if mutations:
                status = 'awaiting_confirmation'
                preview_obj = {
                    'run_id': run_id,
                    'created_at': now_iso,
                    'expires_at': time.time() + CONFIRM_TTL_SECONDS,
                    'reference_timezone': tz_name,
                    'mutations': mutations,
                    'reads_executed': reads,
                }
                preview_hash = _sha256(preview_obj)
                summary_parts.append(f'\n[관리자 확인 필요: 총 {len(mutations)}건의 변경 미리보기]')
                for m in mutations:
                    conflict_note = (
                        f" ⚠️ 시간 중복({', '.join(m['conflicts'])})" if m.get('conflicts') else ''
                    )
                    summary_parts.append(f"• {m['summary']}{conflict_note}")
                summary_parts.append('아직 변경하지 않았습니다. 확인 후 실행해 주세요.')
            elif plan.get('intent_type') == 'clarification' or plan.get('follow_up_question'):
                status = 'needs_clarification'
                if plan.get('follow_up_question'):
                    summary_parts.append(f"질문: {plan['follow_up_question']}")
            else:
                preview_obj = {'reads_executed': reads, 'mutations': []}
        except ValueError as exc:
            status = 'needs_clarification'
            summary_parts = [str(exc)]

        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        summary_text = '\n'.join(summary_parts)

        with self._db() as db:
            db.execute(
                '''INSERT INTO hub_agent_runs(
                    id, request_key, session_id, mode, source_text, tier_used,
                    model_used, status, summary_text, plan_json, preview_json,
                    preview_sha256, cost_microdollars, latency_ms, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    run_id,
                    body.request_id,
                    body.session_id,
                    body.mode,
                    body.text,
                    tier_used,
                    model_used,
                    status,
                    summary_text,
                    _json_dump(plan),
                    _json_dump(preview_obj) if preview_obj is not None else None,
                    preview_hash,
                    cost_micros,
                    latency_ms,
                    now_iso,
                    now_iso,
                ),
            )
        await self._notify('agent.run_created', run_id)
        return self.get_run(run_id) | {'duplicate': False}

    async def _run_briefing(
        self,
        run_id: str,
        body: AgentCommandInput,
        snap: dict[str, Any],
        started: float,
    ) -> dict[str, Any]:
        """Generate a holistic daily briefing (GPT-6.1 Sol High synthesized when budget permits, deterministic fallback)."""
        now_iso = snap['as_of']
        det_text = self.format_deterministic_briefing(snap)
        tier_used = 'tier0_deterministic_briefing'
        model_used = 'local/deterministic'
        cost_micros = 0
        plan_data: dict[str, Any] = {
            'deterministic_briefing': det_text,
            'snapshot_summary': {
                'today_task_count': len(snap['today_tasks']),
                'overdue_count': len(snap['overdue_tasks']),
                'conflict_count': len(snap['schedule_conflicts']),
                'active_alarm_count': len(snap['active_alarms']),
                'memo_count': len(snap['note_catalog']),
            },
        }
        summary_text = det_text

        if body.mode != 'local_only' and self.gateway.key_configured():
            try:
                sys_prompt = (
                    '당신은 사용자의 일정, 할 일, 알람, 메모, 날씨를 종합 브리핑하는 개인 AI 비서(GPT-6.1 Sol High)입니다. '
                    '제공된 서버 스냅샷 데이터만 근거로 브리핑과 우선순위 추천을 작성하세요. '
                    '스냅샷에 없는 일정이나 사실을 절대 지어내지 마세요.'
                )
                llm_res = await self.gateway.generate_structured(
                    request_id=f'briefing:{run_id}',
                    category='briefing',
                    system_prompt=sys_prompt,
                    user_text=f"사용자 요청: {body.text}\n서버 스냅샷 요약:\n{det_text}",
                    schema_name='personal_agent_briefing',
                    schema=BRIEFING_SYNTHESIS_SCHEMA,
                    max_output_tokens=512,
                    temperature=0.2,
                    at_iso=now_iso,
                )
                syn = llm_res['parsed']
                tier_used = 'tier3_openai_briefing'
                model_used = llm_res['selected_model']
                cost_micros = llm_res['cost_microdollars']
                plan_data['synthesis'] = syn
                parts = [
                    f"✨ {syn['headline']}",
                    syn['schedule_summary'],
                ]
                if syn.get('priority_recommendations'):
                    parts.append('\n[추천 우선순위]')
                    for rec in syn['priority_recommendations']:
                        parts.append(f'• {rec}')
                if syn.get('conflict_warnings'):
                    parts.append('\n[일정 충돌/주의사항]')
                    for w in syn['conflict_warnings']:
                        parts.append(f'⚠️ {w}')
                parts.append('\n--- 원본 데이터 요약 ---\n' + det_text)
                summary_text = '\n'.join(parts)
            except OpenAIGatewayError:
                pass

        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        with self._db() as db:
            db.execute(
                '''INSERT INTO hub_agent_runs(
                    id, request_key, session_id, mode, source_text, tier_used,
                    model_used, status, summary_text, plan_json, preview_json,
                    preview_sha256, cost_microdollars, latency_ms, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 'succeeded', ?, ?, NULL, NULL, ?, ?, ?, ?)''',
                (
                    run_id,
                    body.request_id,
                    body.session_id,
                    'briefing',
                    body.text,
                    tier_used,
                    model_used,
                    summary_text,
                    _json_dump(plan_data),
                    cost_micros,
                    latency_ms,
                    now_iso,
                    now_iso,
                ),
            )
        await self._notify('agent.briefing_created', run_id)
        return self.get_run(run_id) | {'duplicate': False}

    async def confirm_run(self, run_id: str, preview_sha256: str) -> dict[str, Any]:
        """Atomically verify preview digest, expiry, and target versions, then execute via WidgetRegistry."""
        from fastapi import HTTPException
        async with self.confirm_lock:
            run = self.get_run(run_id)
            with self._db() as db:
                existing = db.execute(
                    'SELECT receipt_json FROM hub_agent_receipts WHERE run_id = ?',
                    (run_id,),
                ).fetchone()
            if existing:
                return run | {'execution': json.loads(existing['receipt_json']) | {'duplicate': True}}

            if run['status'] != 'awaiting_confirmation':
                raise HTTPException(409, '확인 대기 중인 개인 비서 작업이 아닙니다.')
            preview = run.get('preview')
            if not preview or run.get('preview_sha256') != preview_sha256 or _sha256(preview) != preview_sha256:
                raise HTTPException(409, '변경 미리보기 해시가 일치하지 않습니다. 새 요청으로 다시 확인하세요.')
            if time.time() > preview.get('expires_at', 0):
                raise HTTPException(409, '미리보기 확인 유효 시간(10분)이 만료되었습니다. 다시 요청해 주세요.')
            settings = self.store.get('settings') or {'timezone': 'Asia/Seoul'}
            if settings.get('timezone') != preview.get('reference_timezone'):
                raise HTTPException(409, '시간대 설정이 변경되었습니다. 새 요청으로 다시 확인해 주세요.')

            # Pre-verify all optimistic version locks before mutating any row
            with self._db() as db:
                for m in preview.get('mutations', []):
                    lock = m.get('target_lock')
                    if not lock:
                        continue
                    table = lock['table']
                    if table not in {'tasks', 'hub_notes', 'hub_alarms'}:
                        raise HTTPException(400, '알 수 없는 대상 테이블입니다.')
                    row = db.execute(f'SELECT version FROM {table} WHERE id = ?', (lock['id'],)).fetchone()
                    if not row or row['version'] != lock['version']:
                        raise HTTPException(
                            409,
                            f"확인 전 대상 항목({lock.get('title', lock['id'])})이 다른 화면에서 수정되었거나 삭제되었습니다.",
                        )

            results: list[dict[str, Any]] = []
            for m in preview.get('mutations', []):
                req = WidgetRequest.model_validate(m['widget_request'])
                expected_digest = m['request_digest']
                if request_digest(req) != expected_digest:
                    raise HTTPException(409, '개별 위젯 요청 다이제스트가 손상되었습니다.')
                confirm_ctx = ExecutionContext(
                    principal=f'personal-agent-confirm:{run_id}',
                    role='admin',
                    permissions=frozenset({'read', 'write', 'control'}),
                    confirmed_digest=expected_digest,
                )
                res = await self.registry.execute(req, confirm_ctx)
                if res.status != 'success':
                    code = 409 if res.status == 'conflict' else 400
                    msg = res.error.message if res.error else '위젯 어댑터 실행 중 오류가 발생했습니다.'
                    raise HTTPException(code, msg)
                results.append({
                    'step': m['step'],
                    'domain': m['domain'],
                    'action': m['action'],
                    'adapter': res.meta.adapter,
                    'source_of_truth': res.meta.source_of_truth,
                    'result': res.data,
                })

            stamp = utcnow()
            receipt = {
                'run_id': run_id,
                'executed_at': stamp,
                'mutation_count': len(results),
                'results': results,
                'changed': True,
                'duplicate': False,
            }
            done_summary = (
                run['summary_text'].split('\n[관리자 확인 필요')[0].strip()
                + f'\n\n✅ 관리자 확인 완료: 총 {len(results)}건의 변경 작업을 실행했습니다.'
            )
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute(
                    'INSERT INTO hub_agent_receipts(run_id, preview_sha256, receipt_json, executed_at) VALUES(?, ?, ?, ?)',
                    (run_id, preview_sha256, _json_dump(receipt), stamp),
                )
                db.execute(
                    "UPDATE hub_agent_runs SET status = 'succeeded', summary_text = ?, updated_at = ? WHERE id = ?",
                    (done_summary, stamp, run_id),
                )
            await self._notify('agent.run_confirmed', run_id)
            return self.get_run(run_id) | {'execution': receipt}

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run['status'] == 'awaiting_confirmation':
            stamp = utcnow()
            with self._db() as db:
                db.execute(
                    "UPDATE hub_agent_runs SET status = 'cancelled', summary_text = ?, updated_at = ? WHERE id = ?",
                    ('요청한 변경 미리보기를 취소했습니다. 어떠한 데이터도 변경하지 않았습니다.', stamp, run_id),
                )
            await self._notify('agent.run_cancelled', run_id)
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        from fastapi import HTTPException
        with self._db() as db:
            row = db.execute('SELECT * FROM hub_agent_runs WHERE id = ?', (run_id,)).fetchone()
            receipt = db.execute('SELECT receipt_json FROM hub_agent_receipts WHERE run_id = ?', (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, '개인 비서 실행 기록을 찾을 수 없습니다.')
        d = dict(row)
        d['plan'] = json.loads(d.pop('plan_json'))
        preview_raw = d.pop('preview_json')
        d['preview'] = json.loads(preview_raw) if preview_raw else None
        d['receipt'] = json.loads(receipt['receipt_json']) if receipt else None
        d['cost_usd'] = round(d['cost_microdollars'] / 1_000_000.0, 6)
        return d

    def list_runs(self, limit: int = 25, offset: int = 0) -> dict[str, Any]:
        with self._db() as db:
            total = db.execute('SELECT COUNT(*) FROM hub_agent_runs').fetchone()[0]
            rows = db.execute(
                'SELECT id FROM hub_agent_runs ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?',
                (limit, offset),
            ).fetchall()
        return {
            'items': [self.get_run(r['id']) for r in rows],
            'total': int(total),
            'limit': limit,
            'offset': offset,
        }

    def status(self) -> dict[str, Any]:
        pol = self.governor.policy()
        return {
            'agent_version': AGENT_VERSION,
            'openai': self.gateway.readiness(),
            'budget': self.governor.summary(),
            'domains': list(ALL_ADAPTER_DOMAINS) + ['briefing'],
            'adapters': self.registry.list_widgets(),
            'tiers': {
                'tier0': 'Local deterministic rules ($0, <5ms)',
                'tier1_stt': pol.stt_model,
                'tier2_fast': f'{pol.fast_model} (reasoning_effort={pol.reasoning_effort})',
                'tier3_deep': f'{pol.deep_model} (reasoning_effort={pol.reasoning_effort})',
            },
        }

    async def transcribe_and_submit(
        self,
        *,
        request_id: str,
        audio_bytes: bytes,
        mime_type: str = 'audio/wav',
        duration_seconds: float = 5.0,
        language: str = 'ko',
        mode: Literal['auto', 'fast', 'briefing', 'deep', 'local_only'] = 'auto',
    ) -> dict[str, Any]:
        """Transcribe audio via OpenAI Audio API (with vocabulary hints) and route to Personal Agent."""
        from .speech import validate_audio
        from .stt_accuracy import AccuracyConfig, task_hint

        validate_audio(audio_bytes, mime_type.split(';')[0].lower())
        hint_info = task_hint(self.store, AccuracyConfig(profile='careful', include_task_titles=True))
        stt = await self.gateway.transcribe_audio(
            request_id=f'stt:{request_id}',
            audio_bytes=audio_bytes,
            filename='voice.wav',
            mime_type=mime_type.split(';')[0].lower(),
            duration_seconds=duration_seconds,
            language=language,
            prompt_hint=hint_info.get('text', ''),
        )
        cmd = AgentCommandInput(
            request_id=request_id,
            text=stt['text'],
            mode=mode,
        )
        run = await self.submit_command(cmd)
        return {
            'transcription': stt,
            'agent_run': run,
        }


def register_agent_routes(app, service: PersonalAgentService, admin):
    from fastapi import Depends, File, Form, HTTPException, Request, UploadFile

    @app.get('/api/agent/status')
    async def agent_status(_=Depends(admin)):
        return service.status()

    @app.get('/api/agent/adapters')
    async def agent_adapters(_=Depends(admin)):
        return service.registry.manifest()

    @app.get('/api/agent/adapters/health')
    async def agent_adapters_health(_=Depends(admin)):
        return await service.adapters_health()

    @app.post('/api/agent/adapters/execute')
    async def agent_adapters_execute(request: Request, _=Depends(admin)):
        body = await request.json()
        return await service.execute_adapter(body, confirmed=False)

    @app.get('/api/agent/budget')
    async def agent_budget(month: str | None = None, _=Depends(admin)):
        if month and not re.fullmatch(r'\d{4}-\d{2}', month):
            raise HTTPException(422, '청구 월 형식은 YYYY-MM이어야 합니다.')
        return service.governor.summary(month)

    @app.put('/api/agent/budget/policy')
    async def agent_budget_policy(body: BudgetPolicy, _=Depends(admin)):
        try:
            updated = service.governor.set_policy(body)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        await service._notify('agent.budget_policy_updated')
        return {'policy': updated.model_dump(), 'budget': service.governor.summary()}

    @app.get('/api/agent/snapshot')
    async def agent_snapshot(_=Depends(admin)):
        return service.build_workspace_snapshot()

    @app.post('/api/agent/command', status_code=202)
    async def agent_command(body: AgentCommandInput, _=Depends(admin)):
        return await service.submit_command(body)

    @app.post('/api/agent/briefing', status_code=202)
    async def agent_briefing(body: AgentCommandInput, _=Depends(admin)):
        briefing_req = body.model_copy(update={'mode': 'briefing'})
        return await service.submit_command(briefing_req)

    @app.post('/api/agent/audio', status_code=202)
    async def agent_audio(
        file: UploadFile = File(...),
        request_id: str = Form(...),
        duration_seconds: float = Form(15.0),
        language: str = Form('ko'),
        mode: str = Form('auto'),
        _=Depends(admin),
    ):
        blob = await file.read(10 * 1024 * 1024 + 1)
        if not blob or len(blob) > 10 * 1024 * 1024:
            raise HTTPException(413, '파일 크기를 확인하세요. 최대 10MB입니다.')
        if mode not in {'auto', 'fast', 'deep', 'briefing', 'local_only'}:
            raise HTTPException(422, '지원하지 않는 실행 모드입니다.')
        return await service.transcribe_and_submit(
            request_id=request_id,
            audio_bytes=blob,
            mime_type=file.content_type or 'audio/wav',
            duration_seconds=duration_seconds,
            language=language,
            mode=mode,  # type: ignore[arg-type]
        )

    @app.get('/api/agent/runs')
    async def agent_runs(limit: int = 25, offset: int = 0, _=Depends(admin)):
        if not 1 <= limit <= 100 or offset < 0:
            raise HTTPException(422, '조회 범위를 확인하세요.')
        return service.list_runs(limit=limit, offset=offset)

    @app.get('/api/agent/runs/{run_id}')
    async def agent_run_detail(run_id: str, _=Depends(admin)):
        return service.get_run(run_id)

    @app.post('/api/agent/runs/{run_id}/confirm')
    async def agent_run_confirm(run_id: str, body: AgentConfirmInput, _=Depends(admin)):
        return await service.confirm_run(run_id, body.preview_sha256)

    @app.post('/api/agent/runs/{run_id}/cancel')
    async def agent_run_cancel(run_id: str, _=Depends(admin)):
        return await service.cancel_run(run_id)
