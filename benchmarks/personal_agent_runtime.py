"""Gold-free caller of the production Personal Agent, in disposable storage.

Only the fixture loader and frozen-clock helpers are reused from the legacy
benchmark. This caller never invokes LLMHub, starts application lifespan tasks,
or approves a proposed mutation. It records native Agent/Protocol evidence;
the separate parent-side projection decides whether a scalar legacy score is
representable. No expected answer or dataset record enters this module.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import importlib
import os
from pathlib import Path
import time
from unittest.mock import patch
import uuid

from .runtime import FrozenClock, Runtime, TextInput
from .storage import owned_sqlite


class PersonalAgentRuntime(Runtime):
    """A fresh production app per input, with a worker-wide paid-call meter."""

    def __init__(self, fixtures, reference_datetime, timezone_name, config, *,
                 budget, transport=None):
        self.run_budget = budget
        self._transport = transport
        super().__init__(fixtures, reference_datetime, timezone_name, config)

    def _initialize(self, fixtures, reference_datetime, timezone_name, config):
        self.clock = FrozenClock(reference_datetime)
        self.config = deepcopy(config)
        self.fixture_session = deepcopy(fixtures.get('session', {}))
        self.trace = {}
        self._closed = False
        self.mode = 'full'
        self.phase = 'action'
        self.agent_mode = config.get('agent_mode', 'auto')
        if self.agent_mode not in {'auto', 'fast', 'deep', 'local_only'}:
            raise ValueError('Personal Agent benchmark mode must be auto/fast/deep/local_only')
        if type(config.get('live', False)) is not bool:
            raise ValueError('Personal Agent benchmark live opt-in must be boolean')

        # Import-time app creation must also stay inside this temporary tree.
        # No production data/config/tokens, profile, backend URLs or implicit
        # cloud consent are inherited. Non-live tests receive no ambient key.
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith(('HUB_', 'OPENAI_'))}
        if config.get('live') and self._transport is None:
            for key in ('OPENAI_API_KEY', 'HUB_OPENAI_API_KEY'):
                if os.environ.get(key):
                    clean[key] = os.environ[key]
        clean.update(HUB_DATA_DIR=str(self.root / 'import-only'),
                     HUB_AI_PROFILE='odyssey_openai',
                     HUB_PERSONAL_AGENT_ENABLED='false', HUB_OPENAI_ENABLED='false',
                     HUB_OPENAI_AUDIO_ENABLED='false')
        self.stack.enter_context(patch.dict(os.environ, clean, clear=True))
        self.stack.enter_context(owned_sqlite(self.root))
        from app.main import create_app
        from app.openai_budget import BudgetPolicy
        from .agent_gateway import BenchmarkGateway

        self.app = create_app(self.root / 'case', weather_enabled=False)
        self.store = self.app.state.store
        self.owned_store_path = Path(self.store.path).resolve()
        connect = self.store.connect

        def sandbox_connect():
            if Path(self.store.path).resolve() != self.owned_store_path:
                raise RuntimeError('Refusing non-sandbox Personal Agent storage')
            return connect()

        self.store.connect = sandbox_connect
        self.agent = self.app.state.personal_agent
        self.owned_food_service = self.app.state.food
        # Kept for the inherited fixture helper only; never used for inference.
        self.hub = self.app.state.llm
        self._clock_patches()
        self.app.state.life.clock = self.clock.epoch
        self.app.state.timers.clock = self.clock.epoch
        self.app.state.food.clock = self.clock.epoch
        self.agent.temperature_store.clock = self.clock.epoch
        settings = self.store.get('settings')
        settings['timezone'] = timezone_name
        self.store.set('settings', settings)
        self._fixtures(deepcopy(fixtures))

        policy = BudgetPolicy(
            agent_enabled=True,
            cloud_enabled=bool(config.get('live') or self._transport is not None),
            audio_cloud_enabled=False,
            allow_shared_notes_in_cloud=config.get('allow_shared_notes', False),
            monthly_cap_usd=100.0,
            fast_model=config.get('fast_model', 'gpt-6.1-sol'),
            deep_model=config.get('deep_model', 'gpt-6.1-sol'),
            reasoning_effort=config.get('reasoning_effort', 'high'),
            auto_downgrade=False,
            fallback_to_local=False,
        )
        self.agent.governor.set_policy(policy)
        self.gateway = BenchmarkGateway(
            self.agent.governor, budget=self.run_budget,
            live=config.get('live', False), timeout=config.get('timeout', 90),
            profile=self.agent.profile, transport=self._transport,
        )
        self.agent.gateway = self.gateway
        self.fixture_notes.extend([
            'PersonalAgentService.submit_command is called directly; no HTTP/session, speech, or device latency is measured.',
            'Non-timer mutation previews are never automatically confirmed.',
            'The isolated monthly policy is $100; a separate worker-wide cost/call meter bounds the entire run.',
            'No general dialogue/session resolver is added to the production Personal Agent.',
        ])
        self._adapters()
        self._observe()

    def _clock_patches(self):
        super()._clock_patches()
        owner = self

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = owner.clock.value
                return value.astimezone(tz) if tz else value.replace(tzinfo=None)

            @classmethod
            def utcnow(cls):
                return owner.clock.value.astimezone(timezone.utc).replace(tzinfo=None)

        class FixedTime:
            def time(self):
                return owner.clock.epoch()

            def __getattr__(self, key):
                return getattr(time, key)

        for name in ('app.personal_agent', 'app.widget_protocol.system_adapters',
                     'app.openai_budget'):
            module = importlib.import_module(name)
            if hasattr(module, 'utcnow'):
                self.stack.enter_context(patch.object(module, 'utcnow', self.clock.iso))
            if getattr(module, 'datetime', None) is datetime:
                self.stack.enter_context(patch.object(module, 'datetime', FixedDatetime))
            if getattr(module, 'time', None) is time:
                self.stack.enter_context(patch.object(module, 'time', FixedTime()))

    def _adapters(self):
        from app.widget_protocol.system_adapters import (
            FullMemoAdapter, FullTodoAdapter, FullCalendarAdapter, FullAlarmAdapter,
            ClockAdapter, WeatherAdapter, IndoorSensorAdapter, LLMDisplayAdapter,
            LayoutAdapter, SettingsAdapter, SystemAdapter,
        )
        from app.widget_protocol.timers import TimerAdapter
        from app.widget_protocol.food import FoodAdapter
        reviewed = {FullMemoAdapter, FullTodoAdapter, FullCalendarAdapter,
                    FullAlarmAdapter, TimerAdapter, ClockAdapter, WeatherAdapter,
                    IndoorSensorAdapter, LLMDisplayAdapter, LayoutAdapter,
                    SettingsAdapter, SystemAdapter, FoodAdapter}

        async def no_external_effect(*_args, **_kwargs):
            raise RuntimeError('External weather/display actions are excluded from the benchmark')

        self.agent.refresh_weather = no_external_effect
        self.agent.broadcast_fn = no_external_effect
        self.registry = self.agent.registry
        self.catalog = self.registry.manifest()
        for name in self.registry.list_widgets():
            adapter = self.registry.get(name)
            if type(adapter) not in reviewed:
                raise ValueError('No reviewed Personal Agent sandbox binding for ' + type(adapter).__name__)
            store = getattr(adapter, 'store', None) or adapter.timers.store
            if Path(store.path).resolve() != self.owned_store_path:
                raise RuntimeError('Refusing non-sandbox adapter storage')
            self._guard_food_adapter(adapter)
            if hasattr(adapter, 'temperature_store'):
                if not Path(adapter.temperature_store.path).resolve().is_relative_to(self.root):
                    raise RuntimeError('Refusing non-sandbox sensor storage')
            self._observe_adapter(adapter, store)

        execute = self.registry.execute

        async def registry_observed(raw, authority):
            request = raw.model_dump(mode='json') if hasattr(raw, 'model_dump') else deepcopy(raw)
            event = {'request': request, 'response': None}
            self.trace['registry_calls'].append(event)
            started = time.perf_counter()
            try:
                response = await execute(raw, authority)
                event['response'] = response.model_dump(mode='json')
                return response
            finally:
                event['latency_ms'] = (time.perf_counter() - started) * 1000

        self.registry.execute = registry_observed

    def _guard_food_adapter(self, adapter):
        from app.widget_protocol.food import FoodAdapter
        if isinstance(adapter, FoodAdapter) and (
            adapter.store is not self.store
            or adapter.food is not self.owned_food_service
            or self.agent.food is not self.owned_food_service
            or self.app.state.food is not self.owned_food_service
            or adapter.food.store is not self.store
            or Path(adapter.food.store.path).resolve() != self.owned_store_path
        ):
            raise RuntimeError('Refusing non-sandbox food storage')

    def _observe_adapter(self, adapter, store):
        execute = adapter.execute

        async def adapter_observed(request, authority):
            if Path(store.path).resolve() != self.owned_store_path:
                raise RuntimeError('Refusing non-sandbox adapter storage')
            self._guard_food_adapter(adapter)
            if (request.widget, request.action) in {
                    ('weather', 'refresh'), ('layout', 'focus'), ('layout', 'select_date')}:
                raise RuntimeError('External actions are excluded from the benchmark')
            # Normal registry validation and authorization already ran. Only
            # entry into the real adapter counts; denied previews never do.
            spec = adapter.operations[request.action]
            event = {
                'adapter': type(adapter).__name__, 'production_adapter': type(adapter).__name__,
                'request': request.model_dump(mode='json'), 'read_only': spec.read_only,
                'phase': 'action', 'entered': True, 'changed': None, 'result': None,
            }
            self.trace['adapter_calls'].append(event)
            started = time.perf_counter()
            try:
                result = await execute(request, authority)
                event['changed'] = result.changed
                event['result'] = deepcopy(result.data)
                return result
            finally:
                event['latency_ms'] = (time.perf_counter() - started) * 1000

        adapter.execute = adapter_observed

    def _observe(self):
        cloud_gate = self.agent.should_use_cloud

        def cloud_gate_observed(mode, *, local_only=False, category='agent_fast'):
            allowed, reason = cloud_gate(mode, local_only=local_only, category=category)
            self.trace['cloud_gate'].append({
                'mode': mode, 'local_only': local_only, 'category': category,
                'allowed': allowed, 'reason': reason,
            })
            if not allowed and mode != 'local_only' and not local_only:
                # A missing cloud path in auto/fast/deep mode is incomplete
                # evaluation, not a semantically successful clarification.
                self.trace['error_code'] = 'benchmark_llm_unavailable'
            return allowed, reason

        self.agent.should_use_cloud = cloud_gate_observed
        ground = self.agent._ground_and_stage_steps

        async def ground_observed(*args, **kwargs):
            started = time.perf_counter()
            try:
                return await ground(*args, **kwargs)
            finally:
                self.trace['latency']['grounding_ms'] += (time.perf_counter() - started) * 1000

        self.agent._ground_and_stage_steps = ground_observed

    def business_state(self):
        state = super().business_state()
        # New Agent adapters can mutate layout/settings. Journal, policy,
        # revision and generated authentication material are not business data.
        with self.store.connect() as db:
            state['workspace_kv'] = [dict(r) for r in db.execute(
                "SELECT key,value FROM kv WHERE key IN ('layout','settings','weather') "
                "OR key LIKE 'widget_data:%' ORDER BY key")]
        return state

    async def run(self, request: TextInput, mode='full') -> dict:
        if type(request) is not TextInput:
            raise TypeError('Personal Agent runtime accepts only TextInput, never a dataset case')
        if mode != 'full':
            raise ValueError('Personal Agent benchmark supports full submission only')
        self.clock.set(request.reference_datetime)
        self.trace = {
            'agent_run': None, 'registry_calls': [], 'adapter_calls': [], 'cloud_gate': [],
            'latency': {'grounding_ms': 0.0},
            'latency_scope': {
                'total_ms': 'Production text submission including benchmark gateway/checkpoint overhead; excludes fixture setup.',
                'llm_ms': 'Production gateway including benchmark cost checkpoint/ACK overhead, not pure HTTP or model inference.',
                'gateway_ms': 'Same measured gateway interval used for llm_ms; includes final cost checkpoint when available.',
                'transport_ms': 'Gateway HTTP transport interval only, not isolated provider model inference; null if unavailable.',
            },
            'fixture_notes': self.fixture_notes,
            'clock': {'reference_datetime': request.reference_datetime, 'source': 'frozen_test_clock'},
            'runtime_catalog': self.catalog,
            'provided_session_fields': sorted(request.session),
            'harness_error': None, 'error_code': None,
            'mode': 'full', 'runtime': 'personal_agent', 'agent_mode': self.agent_mode,
        }
        before = self.business_state()
        attempts_start = len(self.gateway.attempts)
        started = time.perf_counter()
        detail = None
        try:
            from app.personal_agent import AgentCommandInput
            detail = await self.agent.submit_command(
                AgentCommandInput(request_id='bench:' + uuid.uuid4().hex,
                                  text=request.text, mode=self.agent_mode,
                                  local_only=self.agent_mode == 'local_only'),
                session={'role': 'admin', 'hash': 'isolated-benchmark-manager'},
            )
        except Exception as exc:
            # Arbitrary exception messages may contain provider/user material.
            self.trace['harness_error'] = type(exc).__name__ + ': Personal Agent runtime failed'
        elapsed = (time.perf_counter() - started) * 1000
        after = self.business_state()
        calls = deepcopy(self.gateway.attempts[attempts_start:])
        llm_ms = sum((c['gateway_total_ms'] if c.get('gateway_total_ms') is not None
                      else c.get('latency_ms')) or 0.0 for c in calls)
        dispatched = [c for c in calls if c.get('transport_attempted')]
        transport_ms = (sum(c['transport_ms'] for c in dispatched)
                        if all(c.get('transport_ms') is not None for c in dispatched) else None)
        self.trace['llm'] = {'attempts': calls, 'mode': 'openai' if self.config.get('live') else 'offline'}
        self.trace['business_state_changed'] = before != after
        self.trace['state_diff'] = {
            key: {'before': before[key], 'after': after[key]}
            for key in before if before[key] != after[key]
        }
        self.trace['latency'].update(
            total_ms=elapsed, llm_ms=llm_ms, non_llm_ms=max(0.0, elapsed - llm_ms),
            gateway_ms=llm_ms, transport_ms=transport_ms,
            adapter_ms=sum(e['latency_ms'] for e in self.trace['adapter_calls']),
        )
        self.trace.update(agent_run=detail, final_status=detail.get('status') if detail else None,
                          final_response=detail.get('summary_text') if detail else None)
        return deepcopy(self.trace)

    async def close(self):
        if self._closed:
            return
        self._closed = True
        # No lifespan, worker, scheduler or asynchronous background task was
        # started, so cleanup never opens a service/production database.
        self.stack.close()
        self.tmp.cleanup()
