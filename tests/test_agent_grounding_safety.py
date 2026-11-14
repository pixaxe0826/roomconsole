"""Source-first task/note targeting regressions; synthetic SQLite only."""
from __future__ import annotations

import asyncio
from datetime import date
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.life import AlarmCreate, NoteCreate
from app.models import TaskCreate
from app.personal_agent import PREVIEW_CTX
from app.widget_protocol import ExecutionContext
from test_personal_agent import build_test_harness, make_step


AT = '2026-11-14T10:00:00+09:00'


class AgentGroundingSafetyTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store, self.life, _, self.registry, self.agent, _ = build_test_harness(
            Path(tmp.name), enable_cloud=False, enable_audio_cloud=False)
        self.store.set('layout', {'columns': 8, 'rows': 6, 'version': 1, 'widgets': [
            {'id': 'memo-card', 'type': 'note', 'x': 0, 'y': 0, 'w': 4, 'h': 2, 'config': {}},
        ]})
        self.serial = 0

    def task(self, title, day='2026-11-14', at=None):
        result = asyncio.run(self.agent.task_services.add(TaskCreate(title=title, date=date.fromisoformat(day), time=at)))
        return result['ids'][0]

    def note(self, title, *, shared=True, pinned=False):
        self.serial += 1
        with patch.object(self.life, 'clock', return_value=1_800_000_000 + self.serial):
            return self.life.create_note(NoteCreate(request_id=f'grounding-note-{self.serial}', title=title,
                                                   body=f'SYNTHETIC_BODY_{self.serial}', shared=shared,
                                                   pinned=pinned))['id']

    def resolve_task(self, source, **overrides):
        return self.agent._resolve_task_target(make_step(action='complete', **overrides), 'Asia/Seoul', AT,
                                                source_text=source,
                                                allowed_ids={t['id'] for t in self.store.tasks()})

    def resolve_note(self, source, **overrides):
        return self.agent._resolve_memo_target(make_step(domain='memo', action='delete', **overrides), AT,
                                                source_text=source)

    def test_task_wrong_id_cannot_borrow_other_title_evidence(self):
        intended = self.task('장보기')
        wrong = self.task('논문 제출')
        with self.assertRaises(ValueError):
            self.resolve_task('장보기 완료해', target_id=wrong, target_title='장보기')
        self.assertEqual(self.resolve_task('장보기 완료해', target_id=intended, target_title='장보기')['id'], intended)

    def test_task_date_time_or_common_word_is_not_identity_evidence(self):
        wrong = self.task('논문 제출', at='09:00')
        for source in ('2026-11-14 작업 하나 완료해', '09:00 작업 완료해', '제출 상태를 확인해'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.resolve_task(source, target_id=wrong)
        with self.assertRaises(ValueError):
            self.resolve_task('제출 상태를 확인해', target_id=wrong, target_title='논문 제출')

    def test_task_absent_source_never_authorizes_id_or_title(self):
        tid = self.task('장보기')
        for source in ('', '   '):
            with self.assertRaises(ValueError):
                self.resolve_task(source, target_id=tid, target_title='장보기')

    def test_duplicate_title_cannot_be_selected_by_model_date_or_time(self):
        tid = self.task('팀 주간 보고', day='2026-11-14', at='09:00')
        self.task('팀 주간 보고', day='2026-11-15', at='17:00')
        for extra in ({'date': '2026-11-14'}, {'time': '09:00'}, {'date': '2026-11-14', 'time': '09:00'}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.resolve_task('팀 주간 보고 완료', target_id=tid, target_title='팀 주간 보고', **extra)

    def test_source_date_and_time_disambiguate_not_model_selectors(self):
        morning = self.task('팀 주간 보고', day='2026-11-15', at='09:00')
        evening = self.task('팀 주간 보고', day='2026-11-15', at='17:00')
        self.task('팀 주간 보고', day='2026-11-14', at='09:00')
        result = self.resolve_task('내일 오전 9시 팀 주간 보고 완료', target_title='팀 주간 보고')
        self.assertEqual(result['id'], morning)
        with self.assertRaises(ValueError):
            self.resolve_task('내일 오전 9시 팀 주간 보고 완료', target_id=evening, target_title='팀 주간 보고', time='17:00')

    def test_unique_title_and_unique_source_substring_still_work(self):
        tid = self.task('프로젝트 최종 보고서')
        self.assertEqual(self.resolve_task('프로젝트 최종 보고서를 완료해', target_title='프로젝트 최종 보고서')['id'], tid)
        self.assertEqual(self.resolve_task('최종 보고서 완료', target_title='최종 보고서')['id'], tid)
        self.assertEqual(self.resolve_task('프로젝트 최종 보고서 완료', target_id=tid)['id'], tid)

    def test_update_new_date_does_not_select_another_same_name(self):
        tid = self.task('장보기')
        step = make_step(action='update', target_id=tid, target_title='장보기', date='2026-11-15')
        self.assertEqual(self.agent._resolve_task_target(step, 'Asia/Seoul', AT,
                                                         source_text='장보기를 내일로 옮겨 줘')['id'], tid)
        self.task('장보기', day='2026-11-15')
        with self.assertRaises(ValueError):
            self.agent._resolve_task_target(step, 'Asia/Seoul', AT, source_text='장보기를 내일로 옮겨 줘')

    def test_update_new_title_is_not_promoted_to_existing_target(self):
        self.task('장보기')
        wrong = self.task('논문 제출')
        step = make_step(action='update', target_id=wrong, target_title='논문 제출', title='논문 제출')
        with self.assertRaises(ValueError):
            self.agent._resolve_task_target(step, 'Asia/Seoul', AT,
                                             source_text='장보기 제목을 "논문 제출" 로 바꿔줘')

    def test_truncated_task_catalog_cannot_claim_uniqueness(self):
        tid = self.task('장보기')
        snapshot = self.store.query_tasks('0001-01-01', '9999-12-31', 'all', limit=10000) | {'truncated': True}
        with patch.object(self.store, 'query_tasks', return_value=snapshot), self.assertRaises(ValueError):
            self.agent._resolve_task_target(make_step(action='delete', target_id=tid), 'Asia/Seoul', AT,
                                             source_text='장보기 삭제해')

    def test_memo_wrong_id_cannot_borrow_other_title_evidence(self):
        intended = self.note('회의록')
        wrong = self.note('개인 일기', shared=False)
        with self.assertRaises(ValueError):
            self.resolve_note('회의록 삭제해', target_id=wrong, target_title='회의록')
        self.assertEqual(self.resolve_note('회의록 메모 삭제해', target_id=intended, target_title='회의록')['id'], intended)

    def test_memo_absent_source_and_invented_selectors_are_rejected(self):
        nid = self.note('회의록')
        for source, ref in (('', '회의록'), ('', 'current'), ('아무거나 읽어줘', 'last'),
                            ('메모 읽어줘', 'current'), ('오늘 할 일 확인해', 'current')):
            with self.subTest(source=source, ref=ref), self.assertRaises(ValueError):
                self.resolve_note(source, target_id=nid, target_title=ref)

    def test_explicit_current_and_last_select_canonical_note_and_check_id(self):
        current = self.note('공유 기본 카드', pinned=True)
        latest = self.note('최근 개인 메모', shared=False)
        self.assertEqual(self.resolve_note('현재 메모 삭제해', target_title='current')['id'], current)
        self.assertEqual(self.resolve_note('마지막 메모 삭제해', target_title='last')['id'], latest)
        self.assertEqual(self.resolve_note('방금 수정한 메모 삭제해', target_title='last_modified')['id'], latest)
        with self.assertRaises(ValueError):
            self.resolve_note('현재 메모 삭제해', target_id=latest, target_title='current')
        with self.assertRaises(ValueError):
            self.resolve_note('마지막 메모 삭제해', target_id=current, target_title='last')
        with self.assertRaises(ValueError):
            self.resolve_note('현재 메모 삭제해', target_title='last')

    def test_duplicate_memo_names_do_not_become_unique_from_id(self):
        first = self.note('회의록')
        self.note('회의록')
        with self.assertRaises(ValueError):
            self.resolve_note('회의록 메모 삭제해', target_id=first, target_title='회의록')

    def test_memo_read_branch_cannot_bypass_source_selector_check(self):
        self.note('회의록')
        snap = self.agent.build_workspace_snapshot(AT)
        with patch.object(self.registry, 'execute', wraps=self.registry.execute) as execute:
            with self.assertRaises(ValueError):
                asyncio.run(self.agent._ground_and_stage_steps('unrelated-read', [
                    make_step(domain='memo', action='read', target_title='current')], snap,
                    source_text='오늘 할 일 확인해', caller_auth=PREVIEW_CTX))
            execute.assert_not_called()

    def test_explicit_current_legacy_read_preserved_but_not_mutation(self):
        self.store.set('widget_data:note', {'text': 'SYNTHETIC_LEGACY_BODY'})
        snap = self.agent.build_workspace_snapshot(AT)
        reads, mutations, lines, _ = asyncio.run(self.agent._ground_and_stage_steps('legacy-read', [
            make_step(domain='memo', action='read', target_title='current')], snap,
            source_text='현재 메모 읽어줘', caller_auth=PREVIEW_CTX))
        self.assertEqual(len(reads), 1)
        self.assertFalse(mutations)
        self.assertIn('SYNTHETIC_LEGACY_BODY', ' '.join(lines))
        with self.assertRaises(ValueError):
            self.resolve_note('현재 메모 삭제해', target_title='current')

    def test_latest_private_memo_still_forbidden_to_display(self):
        self.note('개인 일기', shared=False)
        snap = self.agent.build_workspace_snapshot(AT)
        display = ExecutionContext(principal='display:synthetic', role='display', permissions=frozenset({'read', 'control'}))
        with patch.object(self.registry, 'execute', wraps=self.registry.execute) as execute:
            with self.assertRaises(ValueError):
                asyncio.run(self.agent._ground_and_stage_steps('private-read', [
                    make_step(domain='memo', action='read', target_title='last')], snap,
                    source_text='마지막 메모 읽어줘', caller_auth=display))
            execute.assert_not_called()

    def test_both_current_and_latest_are_ambiguous(self):
        self.note('공유 메모')
        with self.assertRaises(ValueError):
            self.resolve_note('현재 메모와 마지막 메모 삭제해', target_title='current')

    def test_alarm_literal_fragment_wrong_id_and_generic_time_ambiguity(self):
        first = self.life.create_alarm(AlarmCreate(request_id='alarm-grounding-1', label='회의 준비 기상',
                                                 time='08:30', date='2026-11-15', enabled=False))['id']
        second = self.life.create_alarm(AlarmCreate(request_id='alarm-grounding-2', label='외출 준비',
                                                  time='08:30', date='2026-11-15', enabled=False))['id']
        result = self.agent._resolve_alarm_target(make_step(target_title='기상'), source_text='기상 알람 삭제해')
        self.assertEqual(result['id'], first)
        with self.assertRaises(ValueError):
            self.agent._resolve_alarm_target(make_step(target_id=second, target_title='기상'),
                                              source_text='기상 알람 삭제해', allowed_ids={first, second})
        for source in ('알람 삭제해', '08:30 알람 삭제해'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.agent._resolve_alarm_target(make_step(target_id=first), source_text=source)
