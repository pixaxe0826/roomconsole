"""Another caller of the unmodified production LLMHub, not another interpreter.

This module runs only in a benchmark subprocess. Its test-clock patches and
instance-level observation hooks are never installed in the running web server.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import importlib
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from .storage import owned_sqlite


@dataclass(frozen=True, slots=True)
class TextInput:
    text: str
    reference_datetime: str
    session: dict


class BoundaryStop(BaseException):
    """Not a service failure. Bypasses production's intentionally broad error guards."""


class FrozenClock:
    def __init__(self, stamp): self.set(stamp)
    def set(self, stamp):
        self.value = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        if self.value.tzinfo is None:
            raise ValueError('A timezone-aware clock is required')
    def iso(self): return self.value.astimezone(timezone.utc).isoformat()
    def epoch(self): return self.value.timestamp()


class Runtime:
    """Owns exactly one temporary DB; can retain it for an explicit test session."""
    def __init__(self, fixtures: dict, reference_datetime: str, timezone_name: str, config: dict):
        self.tmp = tempfile.TemporaryDirectory(prefix='room-hub-benchmark-')
        self.root = Path(self.tmp.name).resolve()
        self.stack = ExitStack()
        try:
            self._initialize(fixtures, reference_datetime, timezone_name, config)
        except BaseException:
            self.stack.close()
            self.tmp.cleanup()
            raise

    def _initialize(self, fixtures, reference_datetime, timezone_name, config):
        self.clock = FrozenClock(reference_datetime)
        self.config = deepcopy(config)
        self.fixture_session = deepcopy(fixtures.get('session', {}))
        self.trace = {}
        self.phase = 'action'
        self.mode = 'full'
        self._closed = False
        # app.main has an import-time default app. Even that app gets a disposable
        # directory. No ambient HUB_DATA_DIR, tokens, models or speech config is read.
        clean = {k: v for k, v in os.environ.items() if not k.startswith('HUB_')}
        if config.get('llm') == 'local' and os.environ.get('HUB_LLM_API_KEY'):
            clean['HUB_LLM_API_KEY'] = os.environ['HUB_LLM_API_KEY']
        clean['HUB_DATA_DIR'] = str(self.root / 'import-only')
        self.stack.enter_context(patch.dict(os.environ, clean, clear=True))
        self.stack.enter_context(owned_sqlite(self.root))
        from app.main import create_app
        from app.llm import ChatBackend, LLMConfig, LLMFailure
        self.LLMFailure = LLMFailure
        backend = ChatBackend()
        owner = self
        class MeasuredBackend:
            async def generate(self, endpoint, body, timeout):
                call = {'request_payload': json.loads(body), 'transport_attempted': False,
                        'response_raw': None, 'latency_ms': None, 'error': None}
                owner.trace['llm']['attempts'].append(call)
                if config.get('llm', 'disabled') == 'disabled':
                    call['error'] = 'benchmark_llm_unavailable'
                    raise LLMFailure('Benchmark: local LLM was not enabled.', 'benchmark_llm_unavailable')
                call['transport_attempted'] = True
                started = time.perf_counter()
                try:
                    result, raw = await backend.generate(endpoint, body, timeout)
                    call['response_raw'] = raw
                    call['response_model'] = result.get('model')
                    return result, raw
                except LLMFailure as exc:
                    call['error'] = exc.code
                    raise
                finally:
                    call['latency_ms'] = (time.perf_counter() - started) * 1000
        self.app = create_app(self.root / 'case', weather_enabled=False, llm_backend=MeasuredBackend())
        self.store = self.app.state.store
        self.owned_store_path = Path(self.store.path).resolve()
        connect = self.store.connect
        def sandbox_connect():
            if Path(self.store.path).resolve() != self.owned_store_path:
                raise RuntimeError('Refusing non-sandbox storage, including lifecycle access')
            return connect()
        self.store.connect = sandbox_connect
        self.hub = self.app.state.llm
        self.hub._ensure_worker = lambda: None  # Run the same consumer inline, never a server/scheduler.
        cfg = LLMConfig(enabled=True, base_url=config.get('endpoint', 'http://127.0.0.1:8090/v1'),
                        model=config.get('model', 'Qwen3-0.6B-Q5_K_M.gguf'),
                        max_tokens=config.get('max_tokens', 256), temperature=0,
                        timeout_seconds=config.get('timeout', 180), non_thinking=True)
        self.store.set('llm_config', cfg.model_dump())
        settings = self.store.get('settings'); settings['timezone'] = timezone_name
        self.store.set('settings', settings)
        self._clock_patches()
        self.app.state.life.clock = self.clock.epoch
        self.app.state.timers.clock = self.clock.epoch
        self._fixtures(deepcopy(fixtures))
        self._adapters()
        self._observe()

    def _clock_patches(self):
        owner = self
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = owner.clock.value
                return value.astimezone(tz) if tz else value.replace(tzinfo=None)
            @classmethod
            def utcnow(cls): return owner.clock.value.astimezone(timezone.utc).replace(tzinfo=None)
        class FixedTime:
            def time(self): return owner.clock.epoch()
            def __getattr__(self, key): return getattr(time, key)
        for name in ('app.store', 'app.main', 'app.llm', 'app.assistant', 'app.widget_bridge',
                     'app.life', 'app.timers', 'app.widget_protocol.core', 'app.widget_protocol.adapters'):
            module = importlib.import_module(name)
            if hasattr(module, 'utcnow'):
                self.stack.enter_context(patch.object(module, 'utcnow', self.clock.iso))
            if getattr(module, 'datetime', None) is datetime:
                self.stack.enter_context(patch.object(module, 'datetime', FixedDatetime))
            if getattr(module, 'time', None) is time:
                self.stack.enter_context(patch.object(module, 'time', FixedTime()))
        self.stack.enter_context(patch('app.widget_protocol.core.now', self.clock.iso))

    def _fixtures(self, fixture):
        """Storage-format adapter, not an intent parser. Preserve source IDs verbatim.

        Room Hub stores calendar entries and todos in ONE task table. Do not create
        a benchmark-only domain filter or synthesize unsupported duration semantics.
        """
        from app.life import AlarmInput, next_fire
        at = self.clock.iso()
        from app.models import Layout
        layout = deepcopy(fixture.get('layout') or self.store.get('layout'))
        if not fixture.get('layout'):
            layout['widgets'].append({'id': 'fixture-notes', 'type': 'note', 'title': '메모',
                'x': 0, 'y': 6, 'w': 4, 'h': 2, 'config': {'text': ''}})
            layout['rows'] = 8
        # Explicit data-only layout: no invented model widget_id, no shared-current UI.
        layout = Layout.model_validate(layout).model_dump()
        self.store.set('layout', layout)
        with self.store.connect() as db:
            for row in fixture.get('todos', []):
                values = (row['id'], row.get('title', row.get('text', '')), row.get('date', row.get('due_date')),
                          row.get('time', row.get('due_time')), row.get('category', 'general'),
                          row.get('priority', 'normal'), row.get('notes', ''),
                          int(row.get('completed', row.get('status') == 'completed')), None,
                          row.get('version', 1), at, at)
                db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', values)
            for row in fixture.get('calendar', []):
                start = row['start']; day = start[:10]
                clock = None if row.get('all_day') else start[11:16]
                db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (row['id'], row['title'], day, clock, 'general', 'normal', '', 0, None, 1, at, at))
            for row in fixture.get('memos', []):
                pinned = row.get('pinned', row['id'] == fixture.get('session', {}).get('active_widget'))
                db.execute('INSERT INTO hub_notes VALUES(?,?,?,?,?,?,?,?)',
                    (row['id'], row.get('title', ''), row.get('body', row.get('content', '')),
                     int(pinned), int(row.get('shared', True)), row.get('version', 1),
                     row.get('created_at', at), row.get('updated_at', at)))
            for row in fixture.get('alarms', []):
                repeat = row.get('repeat', 'once')
                weekdays = list(range(7)) if repeat == 'daily' else list(range(5)) if repeat == 'weekdays' else row.get('weekdays', [])
                spec = AlarmInput(label=row.get('label', '알람'), time=row['time'],
                    timezone=row.get('timezone', self.store.get('settings')['timezone']),
                    repeat='weekly' if repeat in {'daily', 'weekdays', 'weekly'} else 'once',
                    date=None if repeat in {'daily', 'weekdays', 'weekly'} else row.get('date', self.clock.value.date().isoformat()),
                    weekdays=weekdays, enabled=row.get('enabled', True)).model_dump()
                due = next_fire(spec, self.clock.epoch()) if spec['enabled'] else None
                db.execute('INSERT INTO hub_alarms VALUES(?,?,?,?,?,?)',
                           (row['id'], json.dumps(spec), due, row.get('version', 1), at, at))
            from app.timers import TimerInput
            for row in fixture.get('timers', []):
                def epoch(value):
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        import math
                        if not math.isfinite(value): raise ValueError('Invalid fixture timestamp')
                        return value
                    return FrozenClock(value).epoch()
                spec = TimerInput(duration_seconds=row['duration_seconds'], label=row.get('label', '타이머'))
                start = epoch(row.get('started_at', at))
                deadline = epoch(row.get('deadline_at', start + spec.duration_seconds))
                state = row.get('state', 'running')
                if state not in {'running', 'stopped', 'expired'} or deadline < start:
                    raise ValueError('Invalid fixture timer state/deadline')
                ended = epoch(row['ended_at']) if row.get('ended_at') is not None else None
                if not isinstance(row.get('id'), str) or not row['id']:
                    raise ValueError('Fixture timer ID required')
                db.execute('INSERT INTO hub_timers(id,label,duration_seconds,state,started_at,deadline_at,ended_at,version,widget_id) VALUES(?,?,?,?,?,?,?,?,?)',
                    (row['id'], spec.label, spec.duration_seconds, state, start, deadline, ended,
                     row.get('version', 1), row.get('widget_id')))
        # Same reconciliation as production after a layout save: deadlines/receipts survive.
        self.app.state.timers.reconcile_widgets()
        self.fixture_notes = [
            'Todo and calendar fixtures share the production task table; calendar end/duration is not represented.',
            'active_widget seeds the default pinned shared memo card, not an implemented browser-selection resolver.',
            'Other fixture session fields are retained by the harness but are not understood by the current single-turn core.',
            'No alarm/timer/speech/weather background service is started.',
            'Timer widgets come only from explicit fixture.layout; omitted timer slots retain production no-layout behavior.',
            'UI widget ownership is not an LLM input slot. Protocol current remains latest-running workspace timer.']

    def _adapters(self):
        from app.widget_protocol.core import WidgetRegistry
        from app.widget_protocol.adapters import MemoAdapter, TodoAdapter, CalendarAdapter, AlarmAdapter
        from app.widget_protocol.timers import TimerAdapter
        owner = self
        class SandboxAdapter:
            async def execute(self, request, authority):
                spec = self.operations[request.action]
                store = getattr(self, 'store', None) or self.timers.store
                if not Path(store.path).resolve().is_relative_to(owner.root):
                    raise RuntimeError('Refusing non-sandbox adapter storage')
                # Registry already checked this. Recheck before a mode boundary as
                # direct calls must not turn invalid proposals into EXECUTE decisions.
                self.validate_request(request)
                self.authorize(request, authority, spec)
                if owner.mode == 'decision' and owner.phase == 'action':
                    owner.capture(request=request, boundary='decision', policy='EXECUTE')
                    raise BoundaryStop()
                event = {'adapter': type(self).__name__, 'production_adapter': type(self).__mro__[2].__name__,
                         'request': request.model_dump(mode='json'), 'read_only': spec.read_only,
                         'phase': owner.phase, 'entered': True, 'changed': None, 'result': None}
                owner.trace['adapter_calls'].append(event)
                start = time.perf_counter()
                try:
                    result = await super().execute(request, authority)
                    event['changed'] = result.changed
                    event['result'] = deepcopy(result.data)
                    return result
                finally:
                    event['latency_ms'] = (time.perf_counter() - start) * 1000
        # The service implementations and Operation schemas are inherited, never
        # copied. Their dependencies all point at the temporary application above.
        class FakeMemoAdapter(SandboxAdapter, MemoAdapter): pass
        class FakeTodoAdapter(SandboxAdapter, TodoAdapter): pass
        class FakeCalendarAdapter(SandboxAdapter, CalendarAdapter): pass
        class FakeAlarmAdapter(SandboxAdapter, AlarmAdapter): pass
        class FakeTimerAdapter(SandboxAdapter, TimerAdapter): pass
        fake_types = {MemoAdapter: FakeMemoAdapter, TodoAdapter: FakeTodoAdapter,
                      CalendarAdapter: FakeCalendarAdapter, AlarmAdapter: FakeAlarmAdapter,
                      TimerAdapter: FakeTimerAdapter}
        original = self.app.state.widget_protocol
        registry = WidgetRegistry()
        for name in original.list_widgets():
            real = original.get(name)
            # Unknown future adapters fail closed until their storage isolation is reviewed.
            if type(real) not in fake_types:
                raise ValueError(f'No reviewed sandbox binding for {type(real).__name__}')
            fake = fake_types[type(real)].__new__(fake_types[type(real)])
            fake.__dict__.update(real.__dict__)
            registry.register(fake)
        self.app.state.widget_protocol = registry
        self.hub.set_widget_registry(registry)
        self.registry = registry
        self.catalog = original.manifest()

    def capture(self, *, request=None, proposal=None, record=None, boundary=None, policy=None):
        if request is not None:
            self.trace['request'] = request.model_dump(mode='json') if hasattr(request, 'model_dump') else deepcopy(request)
        if proposal is not None:
            self.trace['proposal'] = deepcopy(proposal)
        if record is not None:
            self.trace['record'] = record  # final defensive copy only at caller boundary
        if boundary:
            self.trace['boundary'] = boundary
        if policy:
            self.trace['boundary_policy'] = policy

    def _observe(self):
        bridge = self.hub.widget_bridge
        prepare = bridge.prepare
        def prepare_observed(record):
            start = time.perf_counter()
            result = prepare(record)
            self.capture(record=result)
            self.trace['latency']['bridge_prepare_ms'] = (time.perf_counter() - start) * 1000
            return result
        bridge.prepare = prepare_observed
        validate = bridge.validate_projection
        def validate_observed(record, result):
            start = time.perf_counter()
            try:
                request, spec = validate(record, result)
                self.capture(request=request, record=record)
                if self.mode == 'nlu':
                    self.capture(boundary='nlu'); raise BoundaryStop()
                return request, spec
            finally:
                self.trace['latency']['validator_ms'] += (time.perf_counter() - start) * 1000
        bridge.validate_projection = validate_observed
        query = bridge.query
        async def query_observed(request, record):
            if self.phase == 'action':
                self.capture(request=request, record=record)
                if self.mode == 'nlu':
                    self.capture(boundary='nlu'); raise BoundaryStop()
            return await query(request, record)
        bridge.query = query_observed
        ground = bridge.ground
        async def ground_observed(record, request, spec):
            old = self.phase; self.phase = 'grounding'; start = time.perf_counter()
            try:
                value = await ground(record, request, spec)
                self.capture(request=value, record=record)
                return value
            finally:
                self.phase = old
                self.trace['latency']['grounding_ms'] += (time.perf_counter() - start) * 1000
        bridge.ground = ground_observed
        stage = self.hub.assistant.stage
        def stage_observed(rid, record, proposal):
            self.capture(proposal=proposal, record=record)
            if self.mode == 'nlu':
                self.capture(boundary='nlu'); raise BoundaryStop()
            return stage(rid, record, proposal)
        self.hub.assistant.stage = stage_observed
        finish = bridge.finish
        def finish_observed(rid, record, request, response, **kwargs):
            start = time.perf_counter()
            self.capture(request=request, record=record)
            value = finish(rid, record, request, response, **kwargs)
            self.trace['latency']['response_ms'] += (time.perf_counter() - start) * 1000
            return value
        bridge.finish = finish_observed

    def business_state(self):
        """Exclude journal/audit writes. These are not assistant business actions."""
        tables = ('tasks', 'series', 'hub_notes', 'hub_alarms', 'hub_timers')
        with self.store.connect() as db:
            return {name: [dict(r) for r in db.execute(f'SELECT * FROM {name} ORDER BY id')]
                    for name in tables}

    async def run(self, request: TextInput, mode='full') -> dict:
        if type(request) is not TextInput:
            raise TypeError('Runtime accepts only TextInput, never a dataset case')
        if mode not in {'nlu', 'decision', 'full'}:
            raise ValueError('Invalid mode')
        self.mode = mode; self.phase = 'action'; self.clock.set(request.reference_datetime)
        self.trace = {'request': None, 'proposal': None, 'record': {}, 'boundary': None,
                      'boundary_policy': None, 'adapter_calls': [],
                      'llm': {'attempts': [], 'mode': self.config.get('llm', 'disabled')},
                      'latency': {'normalizer_ms': None, 'router_ms': None, 'parser_ms': None,
                                  'context_ms': None, 'validator_ms': 0., 'grounding_ms': 0.,
                                  'response_ms': 0., 'policy_ms': None, 'repair_ms': None},
                      'layers': {'semantic_parser': {'enabled': False, 'version': None},
                                 'proposal_repair': {'enabled': False, 'version': None},
                                 'missing_value_normalization': {'enabled': True},
                                 'context': {'enabled': True, 'scope': 'production request clock/memo selectors only',
                                             'general_session_resolver_enabled': False,
                                             'provided_session_fields': sorted(request.session)}}}
        before = self.business_state()
        start = time.perf_counter(); identity = uuid.uuid4().hex; detail = None; harness_error = None
        try:
            # _insert is the common production text entry called by submit() after
            # transcript integrity checks. No voice, HTTP auth or ASR is benchmarked.
            detail = await self.hub._insert('bench:' + identity, identity, None, identity,
                request.text, {'created_at': self.clock.iso(), 'source': 'synthetic-text',
                               'kind': 'text', 'locale': 'ko-KR', 'status': 'received'}, mode='auto')
            if detail['status'] == 'queued':
                await self.hub._execute(detail['id'])
                detail = self.hub.get(detail['id'])
        except BoundaryStop:
            pass
        except Exception as exc:
            harness_error = f'{type(exc).__name__}: {exc}'
        elapsed = (time.perf_counter() - start) * 1000
        # All trace references are detached before any answer is accessible.
        if detail and not self.trace["boundary"]:
            record = detail.get('assistant') or self.trace['record']
            self.trace['record'] = record
        after = self.business_state()
        self.trace['business_state_changed'] = before != after
        self.trace['state_diff'] = {k: {'before': before[k], 'after': after[k]} for k in before if before[k] != after[k]}
        self.trace['latency']['total_ms'] = elapsed
        calls = self.trace['llm']['attempts']
        llm_ms = sum(c['latency_ms'] or 0 for c in calls)
        self.trace['latency']['llm_ms'] = llm_ms
        self.trace['latency']['non_llm_ms'] = max(0., elapsed - llm_ms)
        self.trace['latency']['adapter_ms'] = sum(c['latency_ms'] for c in self.trace['adapter_calls'])
        record = self.trace['record']
        routing = record.get('routing', {})
        semantic = record.get('semantic_parser')
        if semantic:
            self.trace['layers']['semantic_parser'] = {k: semantic.get(k) for k in ('enabled', 'version', 'scope')}
            self.trace['latency']['parser_ms'] = semantic.get('parser_ms')
        self.trace['stages_interaction_model'] = deepcopy(record.get('interaction_model'))
        from app.dialog_state import descriptor
        self.trace['stages_dialog'] = descriptor(record.get('dialog_state'))
        self.trace['stages_semantic_frame'] = deepcopy(record.get('semantic_frame'))
        self.trace['stages_semantic_attempt'] = deepcopy(record.get('semantic_parser_attempt'))
        self.trace['stages_execution_eligibility'] = deepcopy((record.get('widget_trace') or {}).get('execution_eligibility'))
        self.trace['stages_field_provenance'] = deepcopy((record.get('widget_trace') or {}).get('field_provenance'))
        self.trace['stages_entity_resolution'] = deepcopy(record.get('entity_resolution') or (record.get('widget_trace') or {}).get('entity_resolution'))
        self.trace['layers']['entity_resolver'] = {
            'enabled': bool(self.trace['stages_entity_resolution']),
            'scope': 'single_turn_server_rows; exact_then_unique_literal_substring',
            'general_session_resolver_enabled': False}
        router_seconds = routing.get('router_seconds')
        self.trace['latency']['router_ms'] = router_seconds * 1000 if router_seconds is not None else None
        self.trace['stages'] = {
            'normalizer': {'normalized_text': record.get('normalized')},
            'router': deepcopy(routing),
            'context': deepcopy(record.get('resolved_date') or routing.get('resolved_context')),
            'arbitration': {'confidence': routing.get('confidence'), 'reason': routing.get('route_reason')},
            'validation': deepcopy(record.get('validation')),
            'policy': {'state': record.get('state'), 'boundary_policy': self.trace['boundary_policy']},
            'widget_response': deepcopy((record.get('widget_trace') or {}).get('widget_response'))}
        self.trace.update(mode=mode, final_status=detail.get('status') if detail else None,
                          error_code=detail.get('error_code') if detail else None,
                          harness_error=harness_error, fixture_notes=self.fixture_notes,
                          clock={'reference_datetime': request.reference_datetime, 'source': 'frozen_test_clock'},
                          final_response=(detail.get('response_json') or {}).get('output') if detail else None,
                          runtime_catalog=self.catalog)
        return deepcopy(self.trace)

    async def close(self):
        if self._closed: return
        self._closed = True
        try:
            # A tampered dependency must not create/open an external DB even during
            # cancellation/cleanup. This caller never starts background workers.
            if Path(self.store.path).resolve() == self.owned_store_path:
                await self.hub.close()
        finally:
            self.stack.close()
            self.tmp.cleanup()
