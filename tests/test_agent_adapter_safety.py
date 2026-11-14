"""Synthetic adapter regressions; real asyncio and SQLite, no live inference."""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

from pydantic import ValidationError

from app.life import AlarmCreate, AlarmUpdate, LifeService, NoteCreate, NoteUpdate
from app.models import HubSettings
from app.personal_agent import PersonalAgentService
from app.settings_service import apply_settings
from app.store import Store, uid, utcnow
from app.timers import TimerService
from app.widget_protocol.adapters import TaskServices
from app.widget_protocol.core import ExecutionContext, WidgetRegistry, request_digest
from app.widget_protocol.models import WidgetRequest
from app.widget_protocol.system_adapters import (
    FullAlarmAdapter,
    FullMemoAdapter,
    LLMDisplayAdapter,
    SettingsAdapter,
)


START = datetime.fromisoformat('2026-11-14T09:00:00+09:00').timestamp()


class TestAgentAdapterSafety(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agent-adapter-')
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'synthetic.sqlite3')
        self.events = []
        self.clock = [START]

        async def changed(event, detail=''):
            self.events.append((event, detail))

        self.changed = changed
        self.life = LifeService(self.store, changed, clock=lambda: self.clock[0])
        self.registry = self.new_registry()

    def new_registry(self):
        registry = WidgetRegistry()
        registry.register(FullAlarmAdapter(self.store, self.life, self.changed))
        registry.register(SettingsAdapter(self.store, self.changed))
        registry.register(LLMDisplayAdapter(self.store))
        registry.register(FullMemoAdapter(self.store, self.life, self.changed))
        return registry

    async def execute(self, widget, action, *, target=None, args=None, key=None, confirmed=True, registry=None):
        request = WidgetRequest(
            request_id=key or uid(), widget=widget, action=action,
            target={'type': 'item_id', 'value': target} if target else None,
            args=args or {},
        )
        authority = ExecutionContext(
            principal='synthetic-admin', role='admin',
            permissions=frozenset({'read', 'write', 'control'}),
            confirmed_digest=request_digest(request) if confirmed else None,
        )
        return await (registry or self.registry).execute(request, authority)

    def alarm(self, **updates):
        fields = {
            'request_id': uid(), 'label': 'Synthetic morning', 'time': '09:01',
            'timezone': 'Asia/Seoul', 'repeat': 'once',
            'date': '2026-11-14', 'weekdays': [], 'enabled': True,
        }
        fields.update(updates)
        return self.life.create_alarm(AlarmCreate(**fields))['id']

    def due_event(self, **updates):
        alarm_id = self.alarm(**updates)
        self.clock[0] += 60
        self.life.tick()
        return next(event for event in self.life.events() if event['alarm_id'] == alarm_id)

    def place_llm_and_note(self):
        layout = self.store.get('layout')
        layout['widgets'][0]['type'] = 'llm-response'
        layout['widgets'][1]['type'] = 'note'
        self.store.set('layout', layout)

    def agent_read_record(self, note):
        async def unused(*_args, **_kwargs):
            raise AssertionError('No business operation expected')

        service = PersonalAgentService(
            self.store, self.life, None, self.registry,
            TaskServices(unused, unused, unused, unused),
        )
        run_id = 'synthetic-' + uid()
        preview = {'mutations': [], 'reads_executed': [
            {'domain': 'memo', 'action': 'read', 'data': {
                **note, 'source': 'stored_note', 'note_id': note['id'],
            }},
        ]}
        with service._db() as db:
            db.execute(
                '''INSERT INTO hub_agent_runs
                (id, request_key, mode, source_text, tier_used, model_used,
                 status, summary_text, plan_json, preview_json, created_at, updated_at)
                VALUES (?, ?, 'local_only', ?, 'tier0', '', 'succeeded', ?, '{}', ?, ?, ?)''',
                (run_id, run_id, 'Read ' + note['title'], note['body'], json.dumps(preview), utcnow(), utcnow()),
            )
        return run_id

    def external_control_harness(self):
        calls = []

        async def unused(*_args, **_kwargs):
            raise AssertionError('No business operation expected')

        async def refresh():
            calls.append('weather.fetch')
            self.store.set('weather', {'temperature': 99})

        async def broadcast(*_args, **_kwargs):
            calls.append('display.broadcast')

        timers = TimerService(self.store, self.changed, clock=lambda: self.clock[0])
        service = PersonalAgentService(
            self.store, self.life, timers, None,
            TaskServices(unused, unused, unused, unused),
            changed=self.changed, refresh_weather=refresh, broadcast_fn=broadcast,
        )
        return service, calls

    async def test_agent_manifest_advertises_only_timer_immediate_controls(self):
        service, calls = self.external_control_harness()
        self.assertEqual(set(service.registry.immediate_actions()), {'timer.start', 'timer.stop'})
        for domain, action in (('weather', 'refresh'), ('layout', 'focus'), ('layout', 'select_date')):
            capability = next(cap for cap in service.registry.get_capabilities(domain) if cap.action == action)
            self.assertEqual(capability.availability, 'unavailable')
            self.assertTrue(capability.requires_confirmation)
        self.assertEqual(calls, [])

    async def test_external_controls_are_unavailable_even_with_confirmation_or_uow(self):
        service, calls = self.external_control_harness()
        weather = {'temperature': 8, 'location': 'Synthetic'}
        self.store.set('weather', weather)
        for confirmed in (False, True):
            for domain, action in (('weather', 'refresh'), ('layout', 'focus'), ('layout', 'select_date')):
                with self.subTest(domain=domain, action=action, confirmed=confirmed):
                    result = await self.execute(domain, action, confirmed=confirmed, registry=service.registry)
                    self.assertEqual(result.status, 'unavailable')
                    self.assertEqual(result.error.code, 'CAPABILITY_UNAVAILABLE')
                    with self.store.unit_of_work():
                        result = await self.execute(domain, action, confirmed=confirmed, registry=service.registry)
                        self.assertEqual(result.status, 'unavailable')
        self.assertEqual(calls, [])
        self.assertEqual(self.store.get('weather'), weather)
        self.assertEqual(self.events, [])
        # Unavailable external control must not remove safe snapshot reads.
        read = await self.execute('weather', 'read', registry=service.registry)
        self.assertEqual(read.status, 'success')
        self.assertEqual(read.data['temperature'], 8)

    async def test_weather_refresh_plan_is_refused_without_partial_external_effects(self):
        service, calls = self.external_control_harness()
        weather = {'temperature': 8, 'location': 'Synthetic'}
        self.store.set('weather', weather)
        snapshot = service.build_workspace_snapshot()
        steps = [
            {'domain': 'weather', 'action': 'refresh'},
            {'domain': 'not_supported', 'action': 'bad'},
        ]
        with self.assertRaisesRegex(ValueError, '외부 날씨 갱신은 지원하지 않습니다'):
            await service._ground_and_stage_steps(
                uid(), steps, snapshot, source_text='날씨를 갱신하고 미지원 동작을 실행해줘',
            )
        self.assertEqual(calls, [])
        self.assertEqual(self.store.get('weather'), weather)
        self.assertEqual(self.events, [])

    async def test_future_schedule_is_not_materialized_for_snooze_or_ack(self):
        alarm_id = self.alarm(date='2026-11-15', time='07:00')
        original = self.life.alarms()
        for action in ('snooze', 'ack'):
            result = await self.execute('alarm', action, target=alarm_id, args={'version': 1})
            self.assertEqual(result.status, 'not_found')
            self.assertEqual(self.life.events(), [])
            self.assertEqual(self.life.alarms(), original)

    async def test_schedule_id_never_aliases_active_occurrence(self):
        event = self.due_event()
        result = await self.execute('alarm', 'snooze', target=event['alarm_id'], args={'version': event['version']})
        self.assertEqual(result.status, 'not_found')
        self.assertEqual(self.life.events()[0]['state'], 'ringing')

    async def test_event_version_and_confirmation_are_required(self):
        event = self.due_event()
        no_version = await self.execute('alarm', 'snooze', target=event['id'])
        self.assertEqual(no_version.status, 'needs_clarification')
        no_confirmation = await self.execute('alarm', 'snooze', target=event['id'], args={'version': 1}, confirmed=False)
        self.assertEqual(no_confirmation.status, 'needs_confirmation')
        self.assertEqual(self.life.events()[0]['state'], 'ringing')

    async def test_existing_event_snooze_is_version_bound_and_restart_safe(self):
        event = self.due_event()
        key = uid()
        result = await self.execute('alarm', 'snooze', target=event['id'], args={'version': 1}, key=key)
        self.assertEqual(result.status, 'success')
        snoozed = self.life.events()[0]
        self.assertEqual(snoozed['state'], 'snoozed')
        self.assertEqual(snoozed['snoozes'], 1)
        replay = await self.execute(
            'alarm', 'snooze', target=event['id'], args={'version': 1}, key=key,
            registry=self.new_registry(),
        )
        self.assertEqual(replay.status, 'success')
        self.assertTrue(replay.data['duplicate'])
        self.assertEqual(self.life.events()[0], snoozed)
        stale = await self.execute('alarm', 'ack', target=event['id'], args={'version': 1})
        self.assertEqual(stale.status, 'conflict')

    async def test_expired_snooze_cannot_mutate_a_new_occurrence(self):
        first = self.due_event()
        self.clock[0] += 121
        self.life.tick()
        self.assertEqual(self.life.events()[0]['state'], 'missed')
        second_id = self.alarm(time='09:05')
        self.clock[0] = START + 300
        self.life.tick()
        second = next(event for event in self.life.events() if event['alarm_id'] == second_id)
        result = await self.execute('alarm', 'snooze', target=first['id'], args={'version': 1})
        self.assertEqual(result.status, 'conflict')
        self.assertEqual(next(event for event in self.life.events() if event['id'] == second['id']), second)

    async def test_schedule_edit_invalidates_occurrence_instead_of_retargeting(self):
        event = self.due_event()
        schedule = self.life.alarms()[0]
        self.life.update_alarm(schedule['id'], AlarmUpdate(
            version=schedule['version'], label=schedule['label'], time='10:00',
            timezone='Asia/Seoul', repeat='once', date='2026-11-14', weekdays=[], enabled=True,
        ))
        result = await self.execute('alarm', 'ack', target=event['id'], args={'version': event['version']})
        self.assertEqual(result.status, 'conflict')
        self.assertEqual(self.life.events()[0]['state'], 'cancelled')

    async def test_concurrent_snooze_acts_once(self):
        event = self.due_event()
        key = uid()
        results = await asyncio.gather(*[
            self.execute('alarm', 'snooze', target=event['id'], args={'version': 1}, key=key)
            for _ in range(2)
        ])
        self.assertTrue(all(result.status == 'success' for result in results))
        self.assertEqual(self.life.events()[0]['snoozes'], 1)

    async def test_settings_use_canonical_bounds_without_partial_writes(self):
        original = self.store.get('settings')
        for values in (
            {'title': 'x' * 51}, {'location_name': 'x' * 61},
            {'weather_interval_minutes': 121}, {'weather_interval_minutes': 180},
            {'latitude': 36.3}, {'timezone': '../not-a-zone'},
            {'title': None}, {'title': '   '},
        ):
            with self.subTest(values=values):
                result = await self.execute('settings', 'update', args=values)
                self.assertEqual(result.status, 'invalid_request')
                self.assertEqual(self.store.get('settings'), original)

    async def test_settings_invalidate_weather_atomically_for_location_or_timezone(self):
        for values in ({'location_name': 'B'}, {'timezone': 'UTC'}, {'latitude': 37.0, 'longitude': 128.0}):
            self.store.set('weather', {'temperature': 8, 'location': 'A'})
            result = await self.execute('settings', 'update', args=values)
            self.assertEqual(result.status, 'success')
            self.assertIsNone(self.store.get('weather'))
            self.assertEqual(HubSettings.model_validate(self.store.get('settings')).model_dump(), self.store.get('settings'))

    async def test_non_location_patch_preserves_weather_and_explicit_null_clears_coordinates(self):
        apply_settings(self.store, {'latitude': 36.0, 'longitude': 127.0}, patch=True)
        weather = {'temperature': 8, 'location': 'A'}
        self.store.set('weather', weather)
        result = await self.execute('settings', 'update', args={'theme': 'dark', 'weather_interval_minutes': 120})
        self.assertEqual(result.status, 'success')
        self.assertEqual(self.store.get('weather'), weather)
        cleared = await self.execute('settings', 'update', args={'latitude': None, 'longitude': None})
        self.assertEqual(cleared.status, 'success')
        self.assertIsNone(self.store.get('settings')['latitude'])
        self.assertIsNone(self.store.get('weather'))

    async def test_outer_rollback_restores_settings_and_weather_without_notification(self):
        original = self.store.get('settings')
        weather = {'temperature': 8, 'location': 'A'}
        self.store.set('weather', weather)
        with self.assertRaises(RuntimeError):
            with self.store.unit_of_work():
                result = await self.execute('settings', 'update', args={'location_name': 'B'})
                self.assertEqual(result.status, 'success')
                self.assertIsNone(self.store.get('weather'))
                raise RuntimeError('Synthetic later-step failure')
        self.assertEqual(self.store.get('settings'), original)
        self.assertEqual(self.store.get('weather'), weather)
        self.assertEqual(self.events, [])

    async def test_shared_helper_rejects_invalid_full_settings(self):
        before = self.store.get('settings')
        with self.assertRaises(ValidationError):
            apply_settings(self.store, {**before, 'weather_interval_minutes': 180})
        self.assertEqual(self.store.get('settings'), before)

    async def test_concurrent_settings_patches_do_not_lose_independent_fields(self):
        await asyncio.gather(
            asyncio.to_thread(apply_settings, self.store, {'theme': 'dark'}, patch=True),
            asyncio.to_thread(apply_settings, self.store, {'title': 'Synthetic title'}, patch=True),
        )
        self.assertEqual(self.store.get('settings')['theme'], 'dark')
        self.assertEqual(self.store.get('settings')['title'], 'Synthetic title')

    async def test_pinning_a_private_note_does_not_make_it_public(self):
        self.life.create_note(NoteCreate(request_id=uid(), title='Private pinned', body='Secret', pinned=True, shared=False))
        self.life.create_note(NoteCreate(request_id=uid(), title='Shared', body='Public', shared=True))
        result = await self.execute('memo', 'list', args={'include_private': False})
        self.assertEqual(result.status, 'success')
        self.assertEqual([note['title'] for note in result.data['items']], ['Shared'])

    async def test_llm_adapter_never_bypasses_display_opt_in_or_redaction(self):
        note_id = self.life.create_note(NoteCreate(
            request_id=uid(), title='Private title', body='SYNTHETIC_SECRET', shared=False,
        ))['id']
        note = next(note for note in self.life.notes() if note['id'] == note_id)
        run_id = self.agent_read_record(note)
        unplaced = await self.execute('llm-response', 'list')
        self.assertEqual(unplaced.status, 'success')
        self.assertEqual(unplaced.data['recent_agent_runs'], [])
        denied = await self.execute('llm-response', 'read', target=run_id)
        self.assertEqual(denied.status, 'permission_denied')
        self.place_llm_and_note()
        for action in ('list', 'read', 'latest', 'get'):
            result = await self.execute('llm-response', action, target=run_id if action in {'read', 'get'} else None)
            self.assertEqual(result.status, 'success')
            self.assertNotIn('SYNTHETIC_SECRET', json.dumps(result.data))
            self.assertNotIn('Private title', json.dumps(result.data))
            projected = result.data['recent_agent_runs'][0] if action == 'list' else result.data
            self.assertEqual(projected['id'], run_id)
            self.assertTrue(projected['redacted'])

    async def test_llm_adapter_rechecks_a_note_that_was_unshared(self):
        note_id = self.life.create_note(NoteCreate(
            request_id=uid(), title='Was shared', body='FORMER_SHARED_BODY', shared=True,
        ))['id']
        note = next(note for note in self.life.notes() if note['id'] == note_id)
        run_id = self.agent_read_record(note)
        self.place_llm_and_note()
        visible = await self.execute('llm-response', 'get', target=run_id)
        self.assertEqual(visible.status, 'success')
        self.assertIn('FORMER_SHARED_BODY', visible.data['summary_text'])
        self.life.update_note(note_id, NoteUpdate(
            version=note['version'], title=note['title'], body=note['body'], pinned=False, shared=False,
        ))
        result = await self.execute('llm-response', 'get', target=run_id)
        self.assertEqual(result.status, 'success')
        self.assertTrue(result.data['redacted'])
        self.assertNotIn('FORMER_SHARED_BODY', json.dumps(result.data))

    async def test_latest_response_skips_its_own_running_placeholder(self):
        note_id = self.life.create_note(NoteCreate(
            request_id=uid(), title='Shared result', body='PREVIOUS_ANSWER', shared=True,
        ))['id']
        note = next(note for note in self.life.notes() if note['id'] == note_id)
        prior_id = self.agent_read_record(note)
        running_id = self.agent_read_record(note)
        with self.store.connect() as db:
            db.execute("UPDATE hub_agent_runs SET status='running',summary_text='PENDING' WHERE id=?", (running_id,))
        self.place_llm_and_note()
        result = await self.execute('llm-response', 'latest')
        self.assertEqual(result.status, 'success')
        self.assertEqual(result.data['id'], prior_id)
        self.assertEqual(result.data['summary_text'], 'PREVIOUS_ANSWER')
        listed = await self.execute('llm-response', 'list')
        self.assertEqual([item['id'] for item in listed.data['recent_agent_runs']], [prior_id])


if __name__ == '__main__':
    unittest.main()
