"""Unit and integration tests for the OpenAI Personal Agent & $100/Month BudgetGovernor."""
from __future__ import annotations

from datetime import date, timedelta
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from typing import Any
import unittest


def run_async(coro):
    """Deterministic coroutine runner that does not require Windows loopback socketpair."""
    orig_to_thread = asyncio.to_thread
    orig_wait_for = asyncio.wait_for

    async def _sync_to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    async def _sync_wait_for(fut, timeout):
        return await fut

    asyncio.to_thread = _sync_to_thread  # type: ignore[assignment]
    asyncio.wait_for = _sync_wait_for  # type: ignore[assignment]
    try:
        while True:
            try:
                coro.send(None)
            except StopIteration as exc:
                return exc.value
    finally:
        asyncio.to_thread = orig_to_thread  # type: ignore[assignment]
        asyncio.wait_for = orig_wait_for  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Provide a lightweight FastAPI exception/dependency shim only when running in a bare
# Python environment without FastAPI installed (full CI environments use real FastAPI).
try:
    import fastapi  # noqa: F401
except ModuleNotFoundError:
    import types

    fastapi_stub = types.ModuleType('fastapi')
    fastapi_stub.__path__ = []  # type: ignore[attr-defined]

    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: Any = None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    def _identity(default: Any = None, **_kwargs: Any) -> Any:
        return default

    fastapi_stub.HTTPException = HTTPException  # type: ignore[attr-defined]
    fastapi_stub.Depends = _identity  # type: ignore[attr-defined]
    fastapi_stub.File = _identity  # type: ignore[attr-defined]
    fastapi_stub.Form = _identity  # type: ignore[attr-defined]
    fastapi_stub.UploadFile = object  # type: ignore[attr-defined]
    fastapi_stub.Request = object  # type: ignore[attr-defined]
    fastapi_stub.Response = object  # type: ignore[attr-defined]
    responses_stub = types.ModuleType('fastapi.responses')
    responses_stub.JSONResponse = object  # type: ignore[attr-defined]
    sys.modules['fastapi'] = fastapi_stub
    sys.modules['fastapi.responses'] = responses_stub

from fastapi import HTTPException

from app import widget_protocol
from app.life import LifeService, NoteCreate
from app.models import RepeatRule, TaskCompletion, TaskCreate, TaskPatch
from app.openai_budget import (
    BudgetGovernor,
    BudgetPolicy,
    calculate_cost_microdollars,
)
from app.openai_gateway import OpenAIGateway, resolve_openai_api_key, validate_openai_base_url
from app.personal_agent import (
    AgentCommandInput,
    PersonalAgentService,
)
from app.recurrence import occurrences
from app.store import Store, uid, utcnow
from app.timers import TimerService


def build_test_harness(tmp_dir: Path, transport: Any = None, api_key: str | None = None):
    store = Store(tmp_dir / 'room-hub.sqlite3')
    events: list[tuple[str, str]] = []

    async def changed(event: str, detail: str = ''):
        store.bump(event, detail)
        events.append((event, detail))

    life = LifeService(store, changed)
    timers = TimerService(store, changed)

    async def add_task(body: TaskCreate):
        dates = occurrences(body)
        sid = uid() if body.repeat.frequency != 'none' else None
        now = utcnow()
        ids = []
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if sid:
                db.execute('INSERT INTO series VALUES(?,?,?)', (sid, body.model_dump_json(), now))
            for d in dates:
                tid = uid()
                ids.append(tid)
                db.execute(
                    'INSERT INTO tasks VALUES(?,?,?,?,?,?,?,0,?,1,?,?)',
                    (tid, body.title, d.isoformat(), body.time, body.category, body.priority, body.notes, sid, now, now),
                )
        await changed('task.created', f'{body.title} · {len(ids)}개')
        return {'ids': ids, 'count': len(ids), 'series_id': sid}

    async def edit_task(tid: str, body: TaskPatch):
        fields = body.model_dump(exclude_unset=True, mode='json')
        version = fields.pop('version')
        fields['updated_at'] = utcnow()
        with store.connect() as db:
            updated = db.execute(
                'UPDATE tasks SET ' + ','.join(k + '=?' for k in fields) + ',version=version+1 WHERE id=? AND version=?',
                (*fields.values(), tid, version),
            )
            if not updated.rowcount:
                exists = db.execute('SELECT id FROM tasks WHERE id=?', (tid,)).fetchone()
                raise HTTPException(409 if exists else 404, '작업이 변경되었거나 삭제되었습니다.')
            task = store.task_dict(db.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
        await changed('task.updated', tid)
        return task

    async def complete_task(tid: str, body: TaskCompletion, session: dict):
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
            if not row:
                raise HTTPException(404, '작업이 삭제되었거나 존재하지 않습니다.')
            if row['version'] != body.version:
                raise HTTPException(409, '다른 화면에서 작업이 변경되었습니다.')
            if bool(row['completed']) != body.completed:
                db.execute(
                    'UPDATE tasks SET completed=?,version=version+1,updated_at=? WHERE id=? AND version=?',
                    (int(body.completed), utcnow(), tid, body.version),
                )
            task = store.task_dict(db.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
        await changed('task.completion', tid)
        return task

    async def delete_task(tid: str, scope: str = 'one', version: int | None = None):
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = db.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
            if not task:
                raise HTTPException(404, '작업이 없습니다.')
            if version is not None and task['version'] != version:
                raise HTTPException(409, '작업이 변경되었습니다.')
            if scope == 'one' or not task['series_id']:
                cur = db.execute('DELETE FROM tasks WHERE id=?', (tid,))
            elif scope == 'future':
                cur = db.execute('DELETE FROM tasks WHERE series_id=? AND date>=?', (task['series_id'], task['date']))
            else:
                cur = db.execute('DELETE FROM tasks WHERE series_id=?', (task['series_id'],))
            count = cur.rowcount
        await changed('task.deleted', str(count))
        return {'deleted': count}

    task_services = widget_protocol.TaskServices(add_task, edit_task, complete_task, delete_task)
    protocol = widget_protocol.build_registry(store, life, changed, task_services, timers=timers)
    gov = BudgetGovernor(store)
    gw = OpenAIGateway(gov, api_key=api_key, transport=transport)
    agent = PersonalAgentService(
        store,
        life,
        timers,
        protocol,
        task_services,
        governor=gov,
        gateway=gw,
        changed=changed,
    )
    return store, life, timers, protocol, agent, events


def make_step(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
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


class TestBudgetGovernor(unittest.TestCase):
    def test_exact_microdollar_pricing_math(self):
        # gpt-4o-mini: $0.15 / 1M input (0.15 microdollars/token), $0.60 / 1M output (0.60 microdollars/token)
        # 10,000 prompt tokens = 1,500 microdollars; 2,000 completion tokens = 1,200 microdollars -> total 2,700
        cost = calculate_cost_microdollars('gpt-4o-mini', prompt_tokens=10_000, completion_tokens=2_000)
        self.assertEqual(cost, 2_700)

        # Cached input tokens get 50% discount: 10,000 cached gpt-4o tokens -> $1.25/1M -> 12,500 microdollars
        cost_cached = calculate_cost_microdollars(
            'gpt-4o',
            prompt_tokens=10_000,
            cached_tokens=10_000,
            completion_tokens=1_000,
        )
        # 10,000 cached @ 1.25 = 12,500 + 1,000 output @ 10.0 = 10,000 -> 22,500 microdollars
        self.assertEqual(cost_cached, 22_500)

        # Audio STT: gpt-4o-mini-transcribe is $0.003 / min = 50 microdollars / sec
        # 12 seconds -> 600 microdollars ($0.0006)
        stt_cost = calculate_cost_microdollars('gpt-4o-mini-transcribe', audio_seconds=12.0)
        self.assertEqual(stt_cost, 600)

    def test_four_tier_budget_states_across_100_dollar_cap(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = Store(Path(tmp) / 'budget.sqlite3')
            gov = BudgetGovernor(store)
            month = gov.current_billing_month()

            # 1. Initial state is NORMAL ($0.00 / $100.00)
            dec_normal = gov.gate('briefing', requested_model='gpt-4o')
            self.assertEqual(dec_normal.state, 'NORMAL')
            self.assertTrue(dec_normal.allowed_cloud)
            self.assertEqual(dec_normal.selected_model, 'gpt-4o')
            self.assertFalse(dec_normal.downgraded)

            # 2. Spend $72.00 (7,200,000 output tokens on gpt-4o @ $10/1M = $72.00) -> enters SOFT_CAP ($70-$90)
            gov.record_usage(
                request_id='seed-soft-cap',
                category='briefing',
                model='gpt-4o',
                completion_tokens=7_200_000,
            )
            dec_soft = gov.gate('briefing', requested_model='gpt-4o')
            self.assertEqual(dec_soft.state, 'SOFT_CAP')
            self.assertTrue(dec_soft.allowed_cloud)
            self.assertEqual(dec_soft.selected_model, 'gpt-4o-mini')
            self.assertTrue(dec_soft.downgraded)

            # STT also auto-downgrades from gpt-4o-transcribe to gpt-4o-mini-transcribe in SOFT_CAP
            dec_stt = gov.gate('stt', requested_model='gpt-4o-transcribe', audio_seconds=10.0)
            self.assertEqual(dec_stt.selected_model, 'gpt-4o-mini-transcribe')
            self.assertTrue(dec_stt.downgraded)

            # 3. Spend another $20.00 ($92.00 total) -> enters CONSERVATION ($90-$100)
            gov.record_usage(
                request_id='seed-conservation',
                category='agent_deep',
                model='gpt-4o',
                completion_tokens=2_000_000,
            )
            dec_cons = gov.gate('agent_deep', requested_model='gpt-4o')
            self.assertEqual(dec_cons.state, 'CONSERVATION')
            self.assertTrue(dec_cons.allowed_cloud)
            self.assertEqual(dec_cons.selected_model, 'gpt-4o-mini')
            self.assertLessEqual(dec_cons.max_output_tokens, 384)

            # 4. Spend another $8.50 ($100.50 total) -> enters HARD_CAP_REACHED (>= $100.00)
            gov.record_usage(
                request_id='seed-hard-cap',
                category='agent_fast',
                model='gpt-4o',
                completion_tokens=850_000,
            )
            dec_hard = gov.gate('agent_fast', requested_model='gpt-4o-mini')
            self.assertEqual(dec_hard.state, 'HARD_CAP_REACHED')
            self.assertFalse(dec_hard.allowed_cloud)
            self.assertEqual(dec_hard.selected_model, 'local')

            summary = gov.summary(month)
            self.assertEqual(summary['state'], 'HARD_CAP_REACHED')
            self.assertEqual(summary['monthly_cap_usd'], 100.0)
            self.assertAlmostEqual(summary['spent_usd'], 100.5, places=2)
            self.assertEqual(summary['remaining_usd'], 0.0)


class TestOpenAIGatewayValidation(unittest.TestCase):
    def test_base_url_security_policy(self):
        self.assertEqual(validate_openai_base_url('https://api.openai.com/v1/'), 'https://api.openai.com/v1')
        self.assertEqual(validate_openai_base_url('http://127.0.0.1:8090/v1'), 'http://127.0.0.1:8090/v1')
        with self.assertRaises(ValueError):
            validate_openai_base_url('http://api.openai.com/v1')  # non-HTTPS external
        with self.assertRaises(ValueError):
            validate_openai_base_url('https://evil.example.com/v1')  # untrusted host
        with self.assertRaises(ValueError):
            validate_openai_base_url('http://127.0.0.1:8088/v1')  # reserved Room Hub port


class TestPersonalAgentEndToEnd(unittest.TestCase):
    def test_tier0_zero_cost_reads_and_timer_execution(self):
        run_async(self._test_tier0_zero_cost_reads_and_timer_execution())

    async def _test_tier0_zero_cost_reads_and_timer_execution(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store, life, timers, protocol, agent, _ = build_test_harness(Path(tmp))
            # Create a named memo to test named memo catalog lookup
            life.create_note(
                NoteCreate(
                    request_id='seed-note-1',
                    title='장보기',
                    body='우유, 계란, 두부',
                    pinned=True,
                    shared=True,
                )
            )

            # 1. Exact timer start executes immediately at $0 cost
            timer_run = await agent.submit_command(
                AgentCommandInput(request_id='t0-timer-001', text='타이머 3분 시작')
            )
            self.assertEqual(timer_run['status'], 'succeeded')
            self.assertEqual(timer_run['tier_used'], 'tier0_local_rule')
            self.assertEqual(timer_run['cost_microdollars'], 0)
            self.assertEqual(len(timer_run['preview']['reads_executed']), 1)
            self.assertEqual(timer_run['preview']['reads_executed'][0]['domain'], 'timer')

            # 2. Named memo lookup resolves via EntityCatalog at $0 cost
            memo_run = await agent.submit_command(
                AgentCommandInput(request_id='t0-memo-read-001', text='장보기 메모 읽어줘')
            )
            self.assertEqual(memo_run['status'], 'succeeded')
            self.assertEqual(memo_run['tier_used'], 'tier0_local_rule')
            self.assertEqual(memo_run['cost_microdollars'], 0)
            self.assertIn('우유, 계란, 두부', memo_run['summary_text'])

    def test_tier2_openai_compound_plan_conflict_detection_and_atomic_confirm(self):
        run_async(self._test_tier2_openai_compound_plan_conflict_detection_and_atomic_confirm())

    async def _test_tier2_openai_compound_plan_conflict_detection_and_atomic_confirm(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tomorrow_iso = (date.today() + timedelta(days=1)).isoformat()

            async def fake_transport(method: str, url: str, headers: dict, body: Any):
                self.assertIn('Authorization', headers)
                self.assertTrue(url.endswith('/chat/completions'))
                plan_payload = {
                    'intent_type': 'action_plan',
                    'assistant_message': '타이머 2분을 바로 시작했고, 내일 일정·알람·메모 변경안 3건을 확인 대기열에 준비했습니다.',
                    'follow_up_question': None,
                    'steps': [
                        make_step(
                            domain='timer',
                            action='start',
                            title='집중 타이머',
                            duration_seconds=120,
                        ),
                        make_step(
                            domain='calendar',
                            action='add',
                            title='팀 프로젝트 회의',
                            body='분기 계획 점검',
                            date=tomorrow_iso,
                            time='10:00',
                            category='work',
                            priority='high',
                            repeat_frequency='none',
                        ),
                        make_step(
                            domain='alarm',
                            action='set',
                            title='회의 준비 기상',
                            date=tomorrow_iso,
                            time='08:30',
                            repeat_frequency='none',
                        ),
                        make_step(
                            domain='memo',
                            action='write',
                            title='회의 준비물',
                            body='발표 자료 PDF, 노트북 충전기',
                            pinned=True,
                            shared=True,
                        ),
                    ],
                }
                return {
                    'model': 'gpt-4o-mini-2024-07-18',
                    'choices': [
                        {
                            'message': {'content': json.dumps(plan_payload, ensure_ascii=False)},
                            'finish_reason': 'stop',
                        }
                    ],
                    'usage': {
                        'prompt_tokens': 1400,
                        'completion_tokens': 320,
                        'total_tokens': 1720,
                        'prompt_tokens_details': {'cached_tokens': 400},
                    },
                }

            store, life, timers, protocol, agent, _ = build_test_harness(
                Path(tmp),
                transport=fake_transport,
                api_key='sk-test-dummy-key-for-unit-tests-only',
            )

            # Seed an existing schedule item at tomorrow 10:00 so slot conflict detection triggers
            await agent.task_services.add(
                TaskCreate(
                    title='치과 정기검진',
                    date=date.fromisoformat(tomorrow_iso),
                    time='10:00',
                    category='general',
                    priority='normal',
                    notes='스케일링',
                    repeat=RepeatRule(frequency='none'),
                )
            )

            run = await agent.submit_command(
                AgentCommandInput(
                    request_id='compound-run-001',
                    text='타이머 2분 켜고 내일 10시 팀 프로젝트 회의 일정 추가하고 8시 반 알람이랑 회의 준비물 메모도 적어줘',
                    mode='auto',
                )
            )

            self.assertEqual(run['status'], 'awaiting_confirmation', msg=run['summary_text'])
            self.assertEqual(run['tier_used'], 'tier2_openai_fast')
            self.assertGreater(run['cost_microdollars'], 0)

            preview = run['preview']
            self.assertIsNotNone(preview)
            self.assertEqual(len(preview['reads_executed']), 1)
            self.assertEqual(preview['reads_executed'][0]['domain'], 'timer')
            self.assertEqual(len(preview['mutations']), 3)

            # Conflict detector should warn that tomorrow 10:00 conflicts with '치과 정기검진'
            calendar_mutation = preview['mutations'][0]
            self.assertEqual(calendar_mutation['domain'], 'calendar')
            self.assertIn('치과 정기검진', calendar_mutation['conflicts'])

            # Reject confirmation with wrong digest
            with self.assertRaises(HTTPException) as ctx:
                await agent.confirm_run(run['id'], '0' * 64)
            self.assertEqual(ctx.exception.status_code, 409)

            # Confirm with valid preview_sha256 -> executes all 3 staged mutations atomically
            confirmed = await agent.confirm_run(run['id'], run['preview_sha256'])
            self.assertEqual(confirmed['status'], 'succeeded')
            self.assertFalse(confirmed['execution']['duplicate'])
            self.assertEqual(confirmed['execution']['mutation_count'], 3)

            # Verify all 3 entities now exist in Store & LifeService
            tasks_on_day = store.query_tasks(tomorrow_iso, tomorrow_iso, 'all')['items']
            titles = {t['title'] for t in tasks_on_day}
            self.assertIn('치과 정기검진', titles)
            self.assertIn('팀 프로젝트 회의', titles)

            alarms = life.alarms()
            self.assertEqual(len(alarms), 1)
            self.assertEqual(alarms[0]['time'], '08:30')
            self.assertEqual(alarms[0]['label'], '회의 준비 기상')

            notes = life.notes()
            self.assertTrue(any(n['title'] == '회의 준비물' for n in notes))

            # Idempotent replay of confirm_run returns the same receipt without duplicating entities
            replay = await agent.confirm_run(run['id'], run['preview_sha256'])
            self.assertTrue(replay['execution']['duplicate'])
            self.assertEqual(len(life.alarms()), 1)

    def test_optimistic_version_lock_prevents_stale_mutation(self):
        run_async(self._test_optimistic_version_lock_prevents_stale_mutation())

    async def _test_optimistic_version_lock_prevents_stale_mutation(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store, life, timers, protocol, agent, _ = build_test_harness(Path(tmp))
            today_iso = agent.build_workspace_snapshot()['clock']['today']
            created = await agent.task_services.add(
                TaskCreate(
                    title='세탁소 맡기기',
                    date=date.fromisoformat(today_iso),
                    time='14:00',
                    category='general',
                    priority='normal',
                    notes='',
                    repeat=RepeatRule(frequency='none'),
                )
            )
            task_id = created['ids'][0]

            # Stage completion via deterministic semantic parser
            run = await agent.submit_command(
                AgentCommandInput(
                    request_id='ver-lock-001',
                    text='오늘 세탁소 맡기기 완료',
                    mode='local_only',
                )
            )
            self.assertEqual(run['status'], 'awaiting_confirmation', msg=run['summary_text'])
            digest = run['preview_sha256']

            # Simulate concurrent edit from another screen before confirmation
            await agent.task_services.update(
                task_id,
                TaskPatch(title='세탁소 코트 맡기기', version=1),
            )

            # Confirmation must now fail with HTTP 409 because version changed from 1 -> 2
            with self.assertRaises(HTTPException) as ctx:
                await agent.confirm_run(run['id'], digest)
            self.assertEqual(ctx.exception.status_code, 409)

    def test_daily_briefing_and_hard_cap_fallback(self):
        run_async(self._test_daily_briefing_and_hard_cap_fallback())

    async def _test_daily_briefing_and_hard_cap_fallback(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store, life, timers, protocol, agent, _ = build_test_harness(Path(tmp))
            today_iso = agent.build_workspace_snapshot()['clock']['today']
            await agent.task_services.add(
                TaskCreate(
                    title='오전 스탠드업 미팅',
                    date=date.fromisoformat(today_iso),
                    time='09:30',
                    category='work',
                    priority='high',
                    notes='',
                    repeat=RepeatRule(frequency='none'),
                )
            )
            await agent.task_services.add(
                TaskCreate(
                    title='프로젝트 기획서 제출',
                    date=date.fromisoformat(today_iso),
                    time='09:30',
                    category='work',
                    priority='high',
                    notes='',
                    repeat=RepeatRule(frequency='none'),
                )
            )

            # Force hard cap ($100.00 spent) and verify briefing still succeeds deterministically
            agent.governor.record_usage(
                request_id='exhaust-cap',
                category='briefing',
                model='gpt-4o',
                completion_tokens=10_000_000,
            )
            briefing_run = await agent.submit_command(
                AgentCommandInput(
                    request_id='briefing-local-001',
                    text='오늘 브리핑 해줘',
                    mode='briefing',
                )
            )
            self.assertEqual(briefing_run['status'], 'succeeded')
            self.assertEqual(briefing_run['tier_used'], 'tier0_deterministic_briefing')
            self.assertEqual(briefing_run['plan']['snapshot_summary']['conflict_count'], 1)
            self.assertIn('오전 스탠드업 미팅', briefing_run['summary_text'])

    def test_audio_stt_with_vocabulary_hints(self):
        run_async(self._test_audio_stt_with_vocabulary_hints())

    async def _test_audio_stt_with_vocabulary_hints(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            captured_form: dict[str, Any] = {}

            async def fake_stt_transport(method: str, url: str, headers: dict, body: Any):
                self.assertTrue(url.endswith('/audio/transcriptions'))
                captured_form['bytes_len'] = len(body)
                return {'text': '타이머 1분 시작'}

            store, life, timers, protocol, agent, _ = build_test_harness(
                Path(tmp),
                transport=fake_stt_transport,
                api_key='sk-test-dummy-key-for-unit-tests-only',
            )

            # Minimal valid WAV with 1 second of 16kHz PCM audio (32000 bytes payload)
            pcm_bytes = b'\x00\x00' * 16000
            wav_bytes = (
                b'RIFF'
                + (36 + len(pcm_bytes)).to_bytes(4, 'little')
                + b'WAVEfmt '
                + (16).to_bytes(4, 'little')
                + (1).to_bytes(2, 'little')
                + (1).to_bytes(2, 'little')
                + (16000).to_bytes(4, 'little')
                + (32000).to_bytes(4, 'little')
                + (2).to_bytes(2, 'little')
                + (16).to_bytes(2, 'little')
                + b'data'
                + len(pcm_bytes).to_bytes(4, 'little')
                + pcm_bytes
            )

            res = await agent.transcribe_and_submit(
                request_id='voice-cmd-001',
                audio_bytes=wav_bytes,
                mime_type='audio/wav',
                duration_seconds=6.0,
                language='ko',
            )
            self.assertEqual(res['transcription']['text'], '타이머 1분 시작')
            self.assertEqual(res['transcription']['cost_microdollars'], 300)  # 6s @ 50 microdollars/sec
            self.assertEqual(res['agent_run']['status'], 'succeeded')
            self.assertEqual(res['agent_run']['preview']['reads_executed'][0]['domain'], 'timer')


class TestAllElevenWidgetAdapters(unittest.TestCase):
    """Verify full WidgetProtocol 1.0 compliance across all 11 widgets and systems."""

    def test_all_11_adapters_registered_healthy_and_executable(self):
        run_async(self._test_all_11_adapters())

    async def _test_all_11_adapters(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store, life, timers, _base_protocol, agent, _ = build_test_harness(Path(tmp))
            expected_adapters = [
                'memo',
                'todo',
                'calendar',
                'alarm',
                'timer',
                'clock',
                'weather',
                'llm-response',
                'layout',
                'settings',
                'system',
            ]
            self.assertEqual(agent.registry.list_widgets(), expected_adapters)

            # 1. Health check across all 11 adapters
            health = await agent.adapters_health()
            self.assertEqual(health['adapter_count'], 11)
            for name in expected_adapters:
                self.assertTrue(health['adapters'][name]['available'], f'{name} adapter should be healthy')

            # 2. Clock adapter: read & resolve
            clock_read = await agent.execute_adapter({'request_id': 'w-clock-1', 'widget': 'clock', 'action': 'read'})
            self.assertEqual(clock_read['status'], 'success')
            clock_res = await agent.execute_adapter(
                {'request_id': 'w-clock-2', 'widget': 'clock', 'action': 'resolve', 'args': {'expression': '내일'}}
            )
            self.assertEqual(clock_res['status'], 'success')
            self.assertEqual(clock_res['data']['start'], clock_read['data']['tomorrow'])

            # 3. Weather adapter: read & immediate refresh
            wx_read = await agent.execute_adapter({'request_id': 'w-wx-1', 'widget': 'weather', 'action': 'read'})
            self.assertEqual(wx_read['status'], 'success')
            wx_ref = await agent.execute_adapter({'request_id': 'w-wx-2', 'widget': 'weather', 'action': 'refresh'})
            self.assertEqual(wx_ref['status'], 'success')

            # 4. Settings adapter: read, unconfirmed preview -> needs_confirmation, confirmed update -> success
            set_read = await agent.execute_adapter({'request_id': 'w-set-1', 'widget': 'settings', 'action': 'read'})
            self.assertEqual(set_read['status'], 'success')
            set_unconf = await agent.execute_adapter(
                {
                    'request_id': 'w-set-2',
                    'widget': 'settings',
                    'action': 'update',
                    'args': {'title': '스마트 룸 콘솔', 'theme': 'dark'},
                },
                confirmed=False,
            )
            self.assertEqual(set_unconf['status'], 'needs_confirmation')
            set_conf = await agent.execute_adapter(
                {
                    'request_id': 'w-set-3',
                    'widget': 'settings',
                    'action': 'update',
                    'args': {'title': '스마트 룸 콘솔', 'theme': 'dark'},
                },
                confirmed=True,
            )
            self.assertEqual(set_conf['status'], 'success')
            self.assertEqual(set_conf['data']['theme'], 'dark')
            self.assertEqual(set_conf['data']['title'], '스마트 룸 콘솔')

            # 5. Layout adapter: read, update_widget, add_widget, remove_widget, set_data, get_data
            lay_read = await agent.execute_adapter({'request_id': 'w-lay-1', 'widget': 'layout', 'action': 'read'})
            self.assertEqual(lay_read['status'], 'success')
            self.assertIn('all-todos', lay_read['data']['available_widget_types'])
            ver = lay_read['data']['version']

            lay_add = await agent.execute_adapter(
                {
                    'request_id': 'w-lay-2',
                    'widget': 'layout',
                    'action': 'add_widget',
                    'args': {'version': ver, 'id': 'all-todos-1', 'type': 'all-todos', 'title': '전체 할 일', 'x': 0, 'y': 0, 'w': 4, 'h': 2},
                },
                confirmed=True,
            )
            self.assertEqual(lay_add['status'], 'success')
            ver = lay_add['data']['version']

            lay_upd = await agent.execute_adapter(
                {
                    'request_id': 'w-lay-3',
                    'widget': 'layout',
                    'action': 'update_widget',
                    'target': {'type': 'widget_id', 'value': 'all-todos-1'},
                    'args': {'version': ver, 'title': '마스터 할 일 보드'},
                },
                confirmed=True,
            )
            self.assertEqual(lay_upd['status'], 'success')
            ver = lay_upd['data']['version']

            lay_rem = await agent.execute_adapter(
                {
                    'request_id': 'w-lay-4',
                    'widget': 'layout',
                    'action': 'remove_widget',
                    'target': {'type': 'widget_id', 'value': 'all-todos-1'},
                    'args': {'version': ver},
                },
                confirmed=True,
            )
            self.assertEqual(lay_rem['status'], 'success')

            lay_data_set = await agent.execute_adapter(
                {
                    'request_id': 'w-lay-5',
                    'widget': 'layout',
                    'action': 'set_data',
                    'target': {'type': 'widget_id', 'value': 'note'},
                    'args': {'data': {'Draft': '위젯 프로토콜 테스트'}},
                },
                confirmed=True,
            )
            self.assertEqual(lay_data_set['status'], 'success')
            lay_data_get = await agent.execute_adapter(
                {
                    'request_id': 'w-lay-6',
                    'widget': 'layout',
                    'action': 'get_data',
                    'target': {'type': 'widget_id', 'value': 'note'},
                }
            )
            self.assertEqual(lay_data_get['data']['data']['Draft'], '위젯 프로토콜 테스트')

            # 6. System adapter: status, devices, audit
            sys_st = await agent.execute_adapter({'request_id': 'w-sys-1', 'widget': 'system', 'action': 'status'})
            self.assertEqual(sys_st['status'], 'success')
            sys_dev = await agent.execute_adapter({'request_id': 'w-sys-2', 'widget': 'system', 'action': 'devices'})
            self.assertEqual(sys_dev['status'], 'success')
            sys_aud = await agent.execute_adapter(
                {'request_id': 'w-sys-3', 'widget': 'system', 'action': 'audit', 'args': {'limit': 5}}
            )
            self.assertEqual(sys_aud['status'], 'success')
            self.assertGreaterEqual(sys_aud['data']['count'], 1)

            # 7. LLM-Response adapter: list
            llm_list = await agent.execute_adapter({'request_id': 'w-llm-1', 'widget': 'llm-response', 'action': 'list'})
            self.assertEqual(llm_list['status'], 'success')


class TestLiveGPT61SolHigh(unittest.TestCase):
    """Live OpenAI API integration test using GPT-6.1 Sol High under a strict $5.00 budget cap."""

    def test_live_gpt_6_1_sol_high_multi_widget_plan_and_briefing_under_5_usd(self):
        if not resolve_openai_api_key():
            self.skipTest('OPENAI_API_KEY not present in environment')
        run_async(self._run_live_gpt_6_1_sol_high())

    async def _run_live_gpt_6_1_sol_high(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store, life, timers, _protocol, agent, _ = build_test_harness(Path(tmp))
            # Enforce the user's explicit $5.00 test budget cap and GPT-6.1 Sol High
            policy = agent.governor.set_policy(
                BudgetPolicy(
                    monthly_cap_usd=5.0,
                    fast_model='gpt-6.1-sol',
                    deep_model='gpt-6.1-sol',
                    reasoning_effort='high',
                )
            )
            self.assertEqual(policy.monthly_cap_usd, 5.0)
            self.assertEqual(policy.fast_model, 'gpt-6.1-sol')
            self.assertEqual(policy.deep_model, 'gpt-6.1-sol')
            self.assertEqual(policy.reasoning_effort, 'high')

            # Seed an existing note and task for realistic workspace context
            life.create_note(
                NoteCreate(
                    request_id='live-seed-note-1',
                    title='업무 체크리스트',
                    body='서버 배포 점검',
                    pinned=True,
                    shared=True,
                )
            )

            # 1. Live multi-widget command on GPT-6.1 Sol High
            run = await agent.submit_command(
                AgentCommandInput(
                    request_id='live-gpt61-sol-high-001',
                    text=(
                        '타이머 90초 시작하고, 내일 오전 10시 30분에 아키텍처 리뷰 회의 일정 추가하고, '
                        '내일 오전 8시 기상 알람 맞추고, 제목이 준비물인 새 메모에 발표 슬라이드 챙기기라고 적고, '
                        '현재 시스템 상태도 함께 조회해줘.'
                    ),
                    mode='fast',
                )
            )
            self.assertEqual(run['model_used'], 'gpt-6.1-sol')
            self.assertEqual(run['status'], 'awaiting_confirmation', msg=run['summary_text'])
            self.assertGreater(run['cost_microdollars'], 0)

            preview = run['preview']
            self.assertIsNotNone(preview)
            self.assertGreaterEqual(len(preview['mutations']), 3)
            # Verify every staged mutation carries a canonical WidgetProtocol 1.0 request & digest
            for m in preview['mutations']:
                self.assertIn('widget_request', m)
                self.assertIn('request_digest', m)
                self.assertEqual(m['widget_request']['protocol_version'], '1.0')

            # Confirm the staged mutations through WidgetRegistry
            confirmed = await agent.confirm_run(run['id'], run['preview_sha256'])
            self.assertEqual(confirmed['status'], 'succeeded')
            self.assertGreaterEqual(confirmed['execution']['mutation_count'], 3)

            # 2. Live daily briefing synthesis on GPT-6.1 Sol High
            briefing = await agent.submit_command(
                AgentCommandInput(
                    request_id='live-gpt61-sol-high-briefing-001',
                    text='오늘과 내일 전체 브리핑 및 우선순위를 정리해줘',
                    mode='briefing',
                )
            )
            self.assertEqual(briefing['status'], 'succeeded')
            self.assertEqual(briefing['model_used'], 'gpt-6.1-sol')
            self.assertIn('synthesis', briefing['plan'])

            # 3. Verify total test spend is strictly under the $5.00 test budget cap
            budget_summary = agent.governor.summary()
            self.assertEqual(budget_summary['monthly_cap_usd'], 5.0)
            self.assertGreater(budget_summary['spent_usd'], 0.0)
            self.assertLess(budget_summary['spent_usd'], 5.0)


if __name__ == '__main__':
    unittest.main()
