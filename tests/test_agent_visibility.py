"""Synthetic, offline regressions for live memo visibility and derived replies."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import llm_display
from app.life import NoteCreate, NoteUpdate
from app.llm import LLMHub
from app.personal_agent import AgentCommandInput
from test_personal_agent import build_test_harness


class AgentVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            'OPENAI_API_KEY': '', 'HUB_OPENAI_API_KEY': '',
            'HUB_RUN_LIVE_OPENAI_TESTS': '0',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store, self.life, _, _, self.agent, _ = build_test_harness(
            Path(tmp.name), enable_cloud=False, enable_audio_cloud=False,
        )
        self.store.set('layout', {'columns': 8, 'rows': 6, 'version': 1, 'widgets': [
            {'id': 'memo-card', 'type': 'note', 'x': 0, 'y': 0, 'w': 4, 'h': 2, 'config': {}},
            {'id': 'reply-card', 'type': 'llm-response', 'x': 4, 'y': 0, 'w': 4, 'h': 2, 'config': {}},
        ]})
        self.serial = 0

    def note(self, shared=True):
        self.serial += 1
        result = self.life.create_note(NoteCreate(
            request_id=f'synthetic-note-{self.serial}', title=f'합성비밀{self.serial}',
            body=f'SYNTHETIC_PRIVATE_BODY_{self.serial}', shared=shared,
        ))
        return next(note for note in self.life.notes() if note['id'] == result['id'])

    def read_note(self, note):
        self.serial += 1
        run = asyncio.run(self.agent.submit_command(AgentCommandInput(
            request_id=f'synthetic-read-{self.serial}', text=f"{note['title']} 메모 읽어줘",
            mode='local_only', local_only=True,
        )))
        self.assertEqual(run['status'], 'succeeded', run['summary_text'])
        self.assertIn(note['body'], run['summary_text'])
        return run

    def derived(self, source, *, listed=False):
        self.serial += 1
        run = asyncio.run(self.agent.submit_command(AgentCommandInput(
            request_id=f'synthetic-derived-{self.serial}', text='오늘 할 일 확인',
            mode='local_only', local_only=True,
        )))
        # Model-free fixture of already persisted output from the old raw-SQL
        # adapter. We deliberately retain a stale redacted=False flag.
        data = {'id': source['id'], 'status': 'succeeded',
                'summary_text': source['summary_text'], 'redacted': False}
        if listed:
            data = {'recent_agent_runs': [data], 'agent_run_count': 1}
        preview = {'mutations': [], 'reads_executed': [{
            'domain': 'llm-response', 'action': 'list' if listed else 'read', 'data': data,
        }]}
        with self.store.connect() as db:
            db.execute('UPDATE hub_agent_runs SET source_text=?,summary_text=?,preview_json=? WHERE id=?',
                       (source['source_text'], source['summary_text'], json.dumps(preview), run['id']))
        return self.agent.get_run(run['id'])

    def assert_masked(self, run, note):
        projected = llm_display.entry(self.store, run['id'])
        self.assertTrue(projected['redacted'])
        text = json.dumps(projected, ensure_ascii=False)
        self.assertNotIn(note['body'], text)
        self.assertNotIn(note['title'], text)
        # Read projection must not erase the private manager evidence.
        with self.store.connect() as db:
            saved = db.execute('SELECT summary_text FROM hub_agent_runs WHERE id=?', (run['id'],)).fetchone()
        self.assertIn(note['body'], saved['summary_text'])

    def test_private_read_is_masked(self):
        note = self.note(shared=False)
        self.assert_masked(self.read_note(note), note)

    def test_shared_read_rechecks_share_version_delete_and_widget(self):
        for mutation in ('unshare', 'body', 'delete', 'remove_widget'):
            with self.subTest(mutation=mutation):
                note = self.note()
                run = self.read_note(note)
                self.assertFalse(llm_display.entry(self.store, run['id'])['redacted'])
                if mutation == 'delete':
                    self.life.delete_note(note['id'], note['version'])
                elif mutation == 'remove_widget':
                    layout = self.store.get('layout')
                    layout['widgets'] = [w for w in layout['widgets'] if w['type'] != 'note']
                    self.store.set('layout', layout)
                else:
                    self.life.update_note(note['id'], NoteUpdate(
                        title=note['title'], body=note['body'] + (' changed' if mutation == 'body' else ''),
                        shared=mutation != 'unshare', version=note['version'],
                    ))
                self.assert_masked(run, note)

    def test_derived_read_and_list_cannot_launder_private_result(self):
        note = self.note(shared=False)
        original = self.read_note(note)
        for listed in (False, True):
            self.assert_masked(self.derived(original, listed=listed), note)

    def test_transitive_derived_reply_rechecks_after_unsharing(self):
        note = self.note()
        first = self.read_note(note)
        second = self.derived(first)
        third = self.derived(second)
        self.assertFalse(llm_display.entry(self.store, third['id'])['redacted'])
        self.life.update_note(note['id'], NoteUpdate(
            title=note['title'], body=note['body'], shared=False, version=note['version'],
        ))
        for run in (first, second, third):
            self.assert_masked(run, note)

    def test_missing_or_cyclic_ancestor_fails_closed(self):
        note = self.note()
        original = self.read_note(note)
        derived = self.derived(original)
        with self.store.connect() as db:
            db.execute('DELETE FROM hub_agent_runs WHERE id=?', (original['id'],))
        self.assert_masked(derived, note)
        preview = {'mutations': [], 'reads_executed': [{
            'domain': 'llm-response', 'action': 'read', 'data': {'id': derived['id']},
        }]}
        with self.store.connect() as db:
            db.execute('UPDATE hub_agent_runs SET preview_json=? WHERE id=?',
                       (json.dumps(preview), derived['id']))
        self.assert_masked(derived, note)

    def test_null_and_malformed_proof_never_share_raw_source(self):
        note = self.note(shared=False)
        run = self.read_note(note)
        for raw in (None, '{broken', '{}', '[]', '{"reads_executed":null,"mutations":[]}'):
            with self.subTest(raw=raw):
                with self.store.connect() as db:
                    db.execute('UPDATE hub_agent_runs SET preview_json=? WHERE id=?', (raw, run['id']))
                self.assert_masked(run, note)

    def test_decoded_owner_projection_matches_widget(self):
        note = self.note()
        run = self.read_note(note)
        self.life.update_note(note['id'], NoteUpdate(
            title=note['title'], body=note['body'], shared=False, version=note['version'],
        ))
        decoded = llm_display.project_agent_run(self.store, self.agent.get_run(run['id']))
        self.assertEqual(decoded, llm_display.entry(self.store, run['id']))
        # No widget is needed for an already authenticated owner's safe status.
        self.store.set('layout', {'widgets': []})
        self.assertTrue(llm_display.project_agent_run(self.store, run)['redacted'])

    def test_legacy_fixed_note_rechecks_body_and_widget(self):
        self.store.set('widget_data:note', {'text': 'SYNTHETIC_LEGACY_PRIVATE_BODY'})
        run = asyncio.run(self.agent.submit_command(AgentCommandInput(
            request_id='synthetic-fixed-read', text='현재 메모 읽어줘', mode='local_only', local_only=True,
        )))
        self.assertEqual(run['status'], 'succeeded')
        self.assertFalse(llm_display.entry(self.store, run['id'])['redacted'])
        self.store.set('widget_data:note', {'text': 'REPLACED_SYNTHETIC_BODY'})
        self.assertTrue(llm_display.entry(self.store, run['id'])['redacted'])

    def test_unknown_cost_is_not_projected_as_zero(self):
        note = self.note()
        run = self.read_note(note)
        run['cost_microdollars'] = None
        projected = llm_display.project_agent_run(self.store, run)
        self.assertIsNone(projected['cost_usd'])
        self.assertIsNone(projected['cost_microdollars'])

    def test_real_adapter_returns_only_safe_projection(self):
        note = self.note(shared=False)
        run = self.read_note(note)
        read = asyncio.run(self.agent.execute_adapter({
            'request_id': 'synthetic-safe-adapter-read', 'widget': 'llm-response', 'action': 'read',
            'target': {'type': 'item_id', 'value': run['id']},
        }))
        self.assertEqual(read['status'], 'success')
        self.assertTrue(read['data']['redacted'])
        listed = asyncio.run(self.agent.execute_adapter({
            'request_id': 'synthetic-safe-adapter-list', 'widget': 'llm-response', 'action': 'list',
        }))
        self.assertEqual(listed['status'], 'success')
        for dto in (read, listed):
            self.assertNotIn(note['body'], json.dumps(dto, ensure_ascii=False))
            self.assertNotIn(note['title'], json.dumps(dto, ensure_ascii=False))

    def test_owner_endpoint_rechecks_and_redacts_historical_owned_run(self):
        note = self.note()
        run = self.read_note(note)
        with self.store.connect() as db:
            db.execute("INSERT INTO devices VALUES('synthetic-owner','Synthetic iPad',?,0,?,'1024x768')",
                       (run['created_at'], run['created_at']))
            db.execute("UPDATE hub_agent_runs SET device_id='synthetic-owner',principal='display:synthetic-owner' WHERE id=?",
                       (run['id'],))
        owner = {'role': 'display', 'device_id': 'synthetic-owner'}
        self.assertFalse(self.agent.get_run_for_viewer(run['id'], owner)['redacted'])
        self.life.update_note(note['id'], NoteUpdate(
            title=note['title'], body=note['body'], shared=False, version=note['version'],
        ))
        dto = self.agent.get_run_for_viewer(run['id'], owner)
        self.assertTrue(dto['redacted'])
        self.assertNotIn(note['body'], json.dumps(dto, ensure_ascii=False))
        self.assertNotIn(note['title'], json.dumps(dto, ensure_ascii=False))

    def test_system_diagnostics_do_not_share_private_note_counts(self):
        note = self.note(shared=False)
        run = self.read_note(note)
        preview = {'mutations': [], 'reads_executed': [{
            'domain': 'system', 'action': 'status', 'data': {'note_count': 1},
        }]}
        with self.store.connect() as db:
            db.execute('UPDATE hub_agent_runs SET source_text=?,summary_text=?,preview_json=? WHERE id=?',
                       ('시스템 상태 확인', '메모 1개', json.dumps(preview), run['id']))
        dto = llm_display.entry(self.store, run['id'])
        self.assertTrue(dto['redacted'])
        self.assertNotIn('메모 1개', dto['output'])

    def test_derived_legacy_reply_rechecks_original_memo_visibility(self):
        note = self.note()

        async def noop(*args):
            pass

        legacy = LLMHub(self.store, noop)
        saved = asyncio.run(legacy._insert(
            'synthetic-legacy-key', 'synthetic-signature', None, 'synthetic-source',
            '합성 일반 입력', {}, mode='legacy',
        ))
        proof = {'memo_read': True, 'tool_result': {
            'shared': True, 'count': 1, 'note_id': note['id'], 'version': note['version'],
            'source': 'room_hub_sqlite.hub_notes',
        }}
        with self.store.connect() as db:
            db.execute('UPDATE llm_requests SET dispatch_attempted=1,started_at=?,status=?,response_json=? WHERE id=?',
                       (saved['created_at'], 'succeeded', json.dumps({'output': note['body']}), saved['id']))
            legacy.assistant.put(saved['id'], proof, db)
        original = {'id': saved['id'], 'source_text': note['title'], 'summary_text': note['body']}
        derived = self.derived(original)
        self.assertFalse(llm_display.entry(self.store, derived['id'])['redacted'])
        self.life.update_note(note['id'], NoteUpdate(
            title=note['title'], body=note['body'], shared=False, version=note['version'],
        ))
        self.assert_masked(derived, note)
        self.assertTrue(llm_display.entry(self.store, saved['id'])['redacted'])


if __name__ == '__main__':
    unittest.main()
