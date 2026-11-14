"""Synthetic, offline tests for durable audio ingress and await-time authority."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import json
import threading

import pytest
from fastapi import HTTPException

from app import llm_display
from app.agent_audio import AudioIngress
from app.agent_timer_control import source_timer_controls, verify_timer_requests
from app.personal_agent import AgentCommandInput
from app.store import Store, utcnow
from app.timers import TimerStart
from app.widget_protocol.models import WidgetRequest
from test_personal_agent import build_test_harness, make_step


@pytest.fixture(autouse=True)
def no_ambient_cloud(monkeypatch):
    for key in ('OPENAI_API_KEY', 'HUB_OPENAI_API_KEY', 'HUB_LLM_API_KEY'):
        monkeypatch.delenv(key, raising=False)
    for key in ('HUB_PERSONAL_AGENT_ENABLED', 'HUB_OPENAI_ENABLED',
                'HUB_OPENAI_AUDIO_ENABLED', 'HUB_RUN_LIVE_OPENAI_TESTS'):
        monkeypatch.setenv(key, 'false')


@pytest.fixture
def ingress(tmp_path):
    store = Store(tmp_path / 'synthetic-ingress.sqlite3')
    return store, AudioIngress(store)


def run(scenario):
    # Real scheduling is essential: fake coroutine runners hide duplicate races.
    return asyncio.run(asyncio.wait_for(scenario, timeout=5))


def rows(store):
    with store.connect() as db:
        return [dict(row) for row in db.execute('SELECT * FROM hub_agent_audio ORDER BY principal,request_id')]


async def call(ingress, callback, *, authorize=lambda: None, principal='display:synthetic-one',
               request_id='synthetic-audio-request', audio=b'synthetic-audio-bytes', options=None):
    return await ingress.transcribe_once(
        principal=principal, request_id=request_id, audio_bytes=audio,
        options=options if options is not None else {'mime': 'audio/wav', 'language': 'ko', 'mode': 'auto'},
        transcribe=callback, authorize=authorize,
    )


def test_concurrent_same_request_calls_provider_once_and_all_waiters_replay(ingress):
    store, audio_ingress = ingress

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def provider(stt_id):
            calls.append(stt_id)
            entered.set()
            await release.wait()
            return {'text': '합성 전사문', 'stt_request_id': stt_id, 'cost_microdollars': None}

        first = asyncio.create_task(call(audio_ingress, provider))
        await entered.wait()
        duplicates = [asyncio.create_task(call(audio_ingress, provider)) for _ in range(8)]
        await asyncio.sleep(0)
        assert len(calls) == 1 and not any(task.done() for task in duplicates)
        assert len(rows(store)) == 1 and rows(store)[0]['status'] == 'running'
        release.set()
        results = await asyncio.gather(first, *duplicates)
        assert results[0][1] is False
        assert all(result[1] is True for result in results[1:])
        assert all(result[0] == results[0][0] for result in results)
        assert calls[0].startswith('stt:') and len(calls) == 1
        assert rows(store)[0]['status'] == 'succeeded'
        assert audio_ingress.inflight == {}

    run(scenario())


@pytest.mark.parametrize('changed', [
    {'audio': b'different-synthetic-audio'},
    {'options': {'mime': 'audio/ogg', 'language': 'ko', 'mode': 'auto'}},
    {'options': {'mime': 'audio/wav', 'language': 'en', 'mode': 'auto'}},
    {'options': {'mime': 'audio/wav', 'language': 'ko', 'mode': 'local_only'}},
])
def test_same_id_changed_audio_or_options_conflicts_before_provider(ingress, changed):
    store, audio_ingress = ingress

    async def scenario():
        calls = []

        async def provider(stt_id):
            calls.append(stt_id)
            return {'text': '합성 전사문'}

        await call(audio_ingress, provider)
        for service in (audio_ingress, AudioIngress(store)):
            with pytest.raises(HTTPException) as error:
                await call(service, provider, **changed)
            assert error.value.status_code == 409
        assert len(calls) == 1
        assert rows(store)[0]['status'] == 'succeeded'

    run(scenario())


def test_changed_payload_conflicts_while_original_request_is_running(ingress):
    store, audio_ingress = ingress

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def provider(stt_id):
            calls.append(stt_id)
            entered.set()
            await release.wait()
            return {'text': '합성 전사문'}

        first = asyncio.create_task(call(audio_ingress, provider))
        await entered.wait()
        try:
            with pytest.raises(HTTPException) as error:
                await call(audio_ingress, provider, audio=b'conflicting-while-running')
            assert error.value.status_code == 409
            assert len(calls) == 1 and rows(store)[0]['status'] == 'running'
        finally:
            release.set()
        assert (await first)[1] is False

    run(scenario())


def test_principals_are_scoped_and_success_replays_after_service_restart(ingress):
    store, audio_ingress = ingress

    async def scenario():
        calls = []

        async def provider(stt_id):
            calls.append(stt_id)
            return {'text': '합성 전사문', 'stt_request_id': stt_id, 'cost_known': False,
                    'cost_microdollars': None}

        first = await call(audio_ingress, provider)
        second = await call(audio_ingress, provider, principal='display:synthetic-two')
        assert first[1] is False and second[1] is False
        assert first[0]['stt_request_id'] != second[0]['stt_request_id']
        restarted = AudioIngress(store)
        replay = await call(restarted, provider, options={'mode': 'auto', 'language': 'ko', 'mime': 'audio/wav'})
        assert replay == (first[0], True)  # Dict insertion order is not request identity.
        assert len(calls) == 2 and len(rows(store)) == 2
        assert all(row['status'] == 'succeeded' for row in rows(store))

    run(scenario())


@pytest.mark.parametrize('failure', [TimeoutError, ConnectionError, RuntimeError])
def test_uncertain_provider_failure_is_durable_and_never_retried(ingress, failure):
    store, audio_ingress = ingress

    async def scenario():
        calls = []

        async def provider(stt_id):
            calls.append(stt_id)
            await asyncio.sleep(0)
            raise failure('synthetic unknown outcome after dispatch')

        with pytest.raises(failure):
            await call(audio_ingress, provider)
        assert rows(store)[0]['status'] == 'interrupted'
        assert rows(store)[0]['result_json'] is None
        for service in (audio_ingress, AudioIngress(store)):
            with pytest.raises(HTTPException) as error:
                await call(service, provider)
            assert error.value.status_code == 409
        assert len(calls) == 1 and audio_ingress.inflight == {}

    run(scenario())


def test_producer_cancellation_unblocks_duplicate_and_forbids_retry(ingress):
    store, audio_ingress = ingress

    async def scenario():
        entered = asyncio.Event()
        calls = []

        async def provider(stt_id):
            calls.append(stt_id)
            entered.set()
            await asyncio.Event().wait()

        first = asyncio.create_task(call(audio_ingress, provider))
        await entered.wait()
        duplicate = asyncio.create_task(call(audio_ingress, provider))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        with pytest.raises(HTTPException) as error:
            await duplicate
        assert error.value.status_code == 409
        with pytest.raises(HTTPException) as error:
            await call(AudioIngress(store), provider)
        assert error.value.status_code == 409
        assert rows(store)[0]['status'] == 'interrupted'
        assert len(calls) == 1 and audio_ingress.inflight == {}

    run(scenario())


def test_cancelling_duplicate_does_not_cancel_authoritative_provider_request(ingress):
    store, audio_ingress = ingress

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def provider(stt_id):
            calls.append(stt_id)
            entered.set()
            await release.wait()
            return {'text': '합성 전사문'}

        first = asyncio.create_task(call(audio_ingress, provider))
        await entered.wait()
        duplicate = asyncio.create_task(call(audio_ingress, provider))
        await asyncio.sleep(0)
        duplicate.cancel()
        with pytest.raises(asyncio.CancelledError):
            await duplicate
        assert not first.done() and rows(store)[0]['status'] == 'running'
        release.set()
        result = await first
        assert await call(audio_ingress, provider) == (result[0], True)
        assert len(calls) == 1 and rows(store)[0]['status'] == 'succeeded'

    run(scenario())


def test_restart_marks_orphaned_running_record_interrupted_without_dispatch(ingress):
    store, audio_ingress = ingress

    async def scenario():
        calls = []

        async def provider(stt_id):
            calls.append(stt_id)
            return {'text': '합성 전사문'}

        await call(audio_ingress, provider)
        # Synthetic crash checkpoint: dispatch happened but no result was durable.
        with store.connect() as db:
            db.execute("UPDATE hub_agent_audio SET status='running',result_json=NULL")
        with pytest.raises(HTTPException) as error:
            await call(AudioIngress(store), provider)
        assert error.value.status_code == 409
        assert rows(store)[0]['status'] == 'interrupted'
        assert len(calls) == 1

    run(scenario())


def test_revoked_authority_is_checked_before_reservation_or_cached_reply(ingress):
    store, audio_ingress = ingress

    async def scenario():
        calls, revoked = [], [True]

        def authorize():
            if revoked[0]:
                raise HTTPException(401, 'synthetic revoked device')

        async def provider(stt_id):
            calls.append(stt_id)
            return {'text': 'private synthetic transcription'}

        with pytest.raises(HTTPException) as error:
            await call(audio_ingress, provider, authorize=authorize)
        assert error.value.status_code == 401
        assert audio_ingress.ready is False and calls == []
        revoked[0] = False
        await call(audio_ingress, provider, authorize=authorize)
        revoked[0] = True
        for service in (audio_ingress, AudioIngress(store)):
            with pytest.raises(HTTPException) as error:
                await call(service, provider, authorize=authorize)
            assert error.value.status_code == 401
        assert len(calls) == 1 and rows(store)[0]['status'] == 'succeeded'

    run(scenario())


def test_revocation_during_provider_await_denies_both_original_and_duplicate(ingress):
    store, audio_ingress = ingress

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        revoked, calls = [False], []

        def authorize():
            if revoked[0]:
                raise HTTPException(401, 'synthetic revoked device')

        async def provider(stt_id):
            calls.append(stt_id)
            entered.set()
            await release.wait()
            return {'text': 'private synthetic transcription'}

        first = asyncio.create_task(call(audio_ingress, provider, authorize=authorize))
        await entered.wait()
        duplicate = asyncio.create_task(call(audio_ingress, provider, authorize=authorize))
        await asyncio.sleep(0)
        revoked[0] = True
        release.set()
        results = await asyncio.gather(first, duplicate, return_exceptions=True)
        assert all(isinstance(error, HTTPException) and error.status_code == 401 for error in results)
        assert rows(store)[0]['status'] == 'interrupted'
        assert rows(store)[0]['result_json'] is None
        assert len(calls) == 1 and audio_ingress.inflight == {}
        revoked[0] = False
        with pytest.raises(HTTPException) as error:
            await call(AudioIngress(store), provider, authorize=authorize)
        assert error.value.status_code == 409
        assert len(calls) == 1

    run(scenario())


def test_revocation_while_planner_waits_blocks_timer_and_duplicate_result(tmp_path):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def planner(method, url, headers, payload):
            assert url.endswith('/chat/completions')
            calls.append(url)
            entered.set()
            await release.wait()
            plan = {'intent_type': 'action_plan', 'assistant_message': '합성 타이머 실행 계획',
                    'follow_up_question': None,
                    'steps': [make_step(domain='timer', action='start', title='합성 타이머', duration_seconds=120)]}
            return {'model': 'gpt-6.1-sol',
                    'choices': [{'message': {'content': json.dumps(plan, ensure_ascii=False)}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 120, 'completion_tokens': 30, 'total_tokens': 150}}

        store, _, _, _, agent, _ = build_test_harness(
            tmp_path, transport=planner, api_key='sk-synthetic-no-network', enable_audio_cloud=False)
        now = utcnow()
        with store.connect() as db:
            db.execute("INSERT INTO devices VALUES('synthetic-display','합성 표시 기기',?,0,?,'1024x768')", (now, now))
        store.set('layout', {'columns': 8, 'rows': 6, 'version': 1, 'widgets': [
            {'id': 'synthetic-timer-card', 'type': 'timers', 'x': 0, 'y': 0, 'w': 2, 'h': 2, 'config': {}},
        ]})
        session = {'role': 'display', 'device_id': 'synthetic-display'}
        body = AgentCommandInput(request_id='revoke-during-planner', text='2분 타이머 시작해줘', mode='fast')
        first = asyncio.create_task(agent.submit_command(body, session=session))
        await entered.wait()
        duplicate = asyncio.create_task(agent.submit_command(body, session=session))
        await asyncio.sleep(0)
        with store.connect() as db:
            db.execute("UPDATE devices SET revoked=1 WHERE id='synthetic-display'")
        release.set()
        results = await asyncio.gather(first, duplicate, return_exceptions=True)
        assert all(isinstance(error, HTTPException) and error.status_code == 401 for error in results), results
        with store.connect() as db:
            assert db.execute('SELECT COUNT(*) FROM hub_timers').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM hub_timer_requests').fetchone()[0] == 0
            states = db.execute('SELECT status FROM hub_agent_runs').fetchall()
        assert [row['status'] for row in states] == ['interrupted']
        assert len(calls) == 1

    run(scenario())


def test_revocation_during_registry_lock_wait_prevents_immediate_timer(tmp_path):
    async def scenario():
        waiting = asyncio.Event()

        class ObservedLock(asyncio.Lock):
            async def acquire(self):
                if self.locked():
                    waiting.set()
                return await super().acquire()

        store, _, _, _, agent, _ = build_test_harness(
            tmp_path, enable_cloud=False, enable_audio_cloud=False)
        now = utcnow()
        with store.connect() as db:
            db.execute("INSERT INTO devices VALUES('lock-wait-display','합성 표시 기기',?,0,?,'1024x768')", (now, now))
        store.set('layout', {'columns': 8, 'rows': 6, 'version': 1, 'widgets': [
            {'id': 'lock-wait-timer-card', 'type': 'timers', 'x': 0, 'y': 0, 'w': 2, 'h': 2, 'config': {}},
        ]})
        # Hold the real protocol mutation boundary, not a mock timer adapter.
        registry = agent.registry
        registry._lock = lock = ObservedLock()
        await lock.acquire()
        session = {'role': 'display', 'device_id': 'lock-wait-display'}
        command = asyncio.create_task(agent.submit_command(
            AgentCommandInput(request_id='revoke-during-lock', text='2분 타이머 시작해줘', mode='local_only'),
            session=session,
        ))
        try:
            await waiting.wait()
            with store.connect() as db:
                db.execute("UPDATE devices SET revoked=1 WHERE id='lock-wait-display'")
        finally:
            lock.release()
        with pytest.raises(HTTPException) as error:
            await command
        assert error.value.status_code == 401
        with store.connect() as db:
            assert db.execute('SELECT COUNT(*) FROM hub_timers').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM hub_timer_requests').fetchone()[0] == 0
            stored = dict(db.execute('SELECT * FROM hub_agent_runs').fetchone())
        assert stored['status'] in {'interrupted', 'needs_clarification'}
        projected = llm_display.project_agent_run(store, stored)
        assert projected['redacted'] is True
        assert '2분 타이머 시작해줘' not in projected['input']
        with pytest.raises(HTTPException) as error:
            agent.get_run_for_viewer(stored['id'], session)
        assert error.value.status_code == 401

    run(scenario())


def timer_plan_harness(tmp_path, steps):
    calls = []

    async def provider(method, url, headers, payload):
        assert url.endswith('/chat/completions')
        calls.append(url)
        plan = {'intent_type': 'action_plan', 'assistant_message': '합성 모델의 검증 대상 계획',
                'follow_up_question': None, 'steps': steps}
        return {'model': 'gpt-6.1-sol',
                'choices': [{'message': {'content': json.dumps(plan, ensure_ascii=False)}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 120, 'completion_tokens': 30, 'total_tokens': 150}}

    store, _, timers, _, agent, _ = build_test_harness(
        tmp_path, transport=provider, api_key='sk-synthetic-no-network', enable_audio_cloud=False)
    store.set('layout', {'columns': 8, 'rows': 6, 'version': 1, 'widgets': [
        {'id': 'source-timer-one', 'type': 'timers', 'x': 0, 'y': 0, 'w': 2, 'h': 2, 'config': {}},
        {'id': 'source-timer-two', 'type': 'timers', 'x': 2, 'y': 0, 'w': 2, 'h': 2, 'config': {}},
    ]})
    return store, timers, agent, calls


@pytest.mark.parametrize('source,seconds', [
    ('오늘 할 일 알려줘', 120),
    ('2분 타이머 시작', 240),
    ('내일 2분 타이머 시작', 120),
    ('2분 타이머 시작하지 마', 120),
    ('혹시 2분 타이머 시작할까?', 120),
    ('2분 타이머 시작하고 싶으면 오늘 일정 알려줘', 120),
    ('메모에 "2분 타이머 시작"이라고 적어줘', 120),
    ('2분 타이머 시작하고 10분 후에 타이머 종료', 120),
    ('2분 타이머 시작하고 내일 3분 타이머 시작', 120),
    ('타이머를 가동해 주세요, 4분 정도', 240),
    ('0초 타이머 시작', 1),
    ('601초 타이머 시작', 600),
    ('-4분 타이머 시작', 240),
    ('1.5초 타이머 시작', 1),
    ('1분 90초 타이머 시작', 150),
])
def test_model_cannot_invent_repair_or_schedule_immediate_timer(tmp_path, source, seconds):
    async def scenario():
        store, timers, agent, calls = timer_plan_harness(tmp_path, [
            make_step(domain='timer', action='start', duration_seconds=seconds),
        ])
        result = await agent.submit_command(AgentCommandInput(
            request_id='unsafe-model-timer', text=source, mode='fast'))
        assert result['status'] == 'needs_clarification', result
        assert timers.snapshot()['active_count'] == 0
        with store.connect() as db:
            assert db.execute('SELECT COUNT(*) FROM hub_timer_requests').fetchone()[0] == 0
        assert len(calls) == 1

    run(scenario())


@pytest.mark.parametrize('source,steps', [
    ('2분 타이머 시작', [120, 120]),
    ('타이머 2분 켜고 타이머 3분 켜', [120]),
    ('타이머 2분 켜고 타이머 3분 켜', [180, 120]),
])
def test_timer_source_clauses_are_consumed_once_completely_and_in_order(tmp_path, source, steps):
    async def scenario():
        store, timers, agent, _ = timer_plan_harness(tmp_path, [
            make_step(domain='timer', action='start', duration_seconds=seconds) for seconds in steps
        ])
        result = await agent.submit_command(AgentCommandInput(
            request_id='timer-source-consumption', text=source, mode='fast'))
        assert result['status'] == 'needs_clarification', result
        assert timers.snapshot()['items'] == []
        with store.connect() as db:
            assert db.execute('SELECT COUNT(*) FROM hub_timer_requests').fetchone()[0] == 0

    run(scenario())


def test_two_explicit_timer_clauses_create_exactly_two_countdowns(tmp_path):
    async def scenario():
        _, timers, agent, calls = timer_plan_harness(tmp_path, [
            make_step(domain='timer', action='start', duration_seconds=120),
            make_step(domain='timer', action='start', duration_seconds=180),
        ])
        result = await agent.submit_command(AgentCommandInput(
            request_id='two-explicit-source-timers',
            text='타이머 2분 켜고 타이머 3분 켜', mode='fast'))
        assert result['status'] == 'succeeded', result
        assert sorted(item['duration_seconds'] for item in timers.snapshot()['items']) == [120, 180]
        assert len(result['preview']['immediate_controls_executed']) == 2
        assert len(calls) == 1

    run(scenario())


@pytest.mark.parametrize('source_kind,target_kind', [
    ('id', 'other-id'), ('id', 'current'), ('current', 'current-id'),
    ('unrelated', 'current'), ('scheduled', 'current'),
])
def test_model_timer_stop_cannot_replace_source_target(tmp_path, source_kind, target_kind):
    async def scenario():
        steps = []
        store, timers, agent, calls = timer_plan_harness(tmp_path, steps)
        first = timers.start(TimerStart(request_id='seed-first-countdown', duration_seconds=240), 'synthetic-seed')
        second = timers.start(TimerStart(request_id='seed-second-countdown', duration_seconds=300), 'synthetic-seed')
        source = {
            'id': f"타이머 ID {first['id']} 종료",
            'current': '현재 타이머 종료',
            'unrelated': '오늘 할 일 알려줘',
            'scheduled': '10분 후 현재 타이머 종료',
        }[source_kind]
        target = {'target_id': second['id']} if target_kind in {'other-id', 'current-id'} else {'target_title': 'current'}
        steps.append(make_step(domain='timer', action='stop', **target))
        result = await agent.submit_command(AgentCommandInput(
            request_id='wrong-stop-source-target', text=source, mode='fast'))
        assert result['status'] == 'needs_clarification', result
        assert timers.get(first['id'])['state'] == timers.get(second['id'])['state'] == 'running'
        with store.connect() as db:
            assert db.execute('SELECT COUNT(*) FROM hub_timer_requests').fetchone()[0] == 2
        assert len(calls) == 1

    run(scenario())


@pytest.mark.parametrize('source_kind', ['id', 'current'])
def test_matching_timer_stop_preserves_existing_target_semantics(tmp_path, source_kind):
    async def scenario():
        steps = []
        _, timers, agent, _ = timer_plan_harness(tmp_path, steps)
        first = timers.start(TimerStart(request_id='seed-stop-first', duration_seconds=240), 'synthetic-seed')
        second = timers.start(TimerStart(request_id='seed-stop-second', duration_seconds=300), 'synthetic-seed')
        if source_kind == 'id':
            source = f"타이머 ID {first['id']} 종료"
            steps.append(make_step(domain='timer', action='stop', target_id=first['id']))
            expected, retained = first, second
        else:
            source = '현재 타이머 종료'
            steps.append(make_step(domain='timer', action='stop', target_title='current'))
            expected, retained = second, first
        result = await agent.submit_command(AgentCommandInput(
            request_id='source-proven-stop', text=source, mode='fast'))
        assert result['status'] == 'succeeded', result
        assert timers.get(expected['id'])['state'] == 'stopped'
        assert timers.get(retained['id'])['state'] == 'running'
        assert result['preview']['immediate_controls_executed'][0]['data']['id'] == expected['id']

    run(scenario())


def test_source_grounded_compound_keeps_timer_immediate_and_calendar_confirmation(tmp_path):
    async def scenario():
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        store, timers, agent, calls = timer_plan_harness(tmp_path, [
            make_step(domain='timer', action='start', duration_seconds=120),
            make_step(domain='calendar', action='add', title='합성회의', date=tomorrow, time='10:00'),
        ])
        result = await agent.submit_command(AgentCommandInput(
            request_id='source-proven-compound',
            text='타이머 2분 켜고 내일 10시 합성회의 일정 추가해줘', mode='fast'))
        assert result['status'] == 'awaiting_confirmation', result
        assert [item['duration_seconds'] for item in timers.snapshot()['items']] == [120]
        assert store.tasks() == []
        assert len(result['preview']['mutations']) == 1
        assert result['preview']['mutations'][0]['domain'] == 'calendar'
        assert len(result['preview']['immediate_controls_executed']) == 1
        assert len(calls) == 1

    run(scenario())


def test_execution_boundary_rechecks_exact_source_proof_after_request_tampering():
    source = '4분 타이머 시작'
    proof = source_timer_controls(source)
    assert proof[0]['args'] == {'duration_seconds': 240}
    request = WidgetRequest(request_id='source-boundary-check', widget='timer', action='start',
                            args={'duration_seconds': 240, 'label': '타이머'})
    verify_timer_requests(source, [request])
    request.args['duration_seconds'] = 540
    with pytest.raises(ValueError):
        verify_timer_requests(source, [request])


@pytest.mark.parametrize('action', ['start', 'stop'])
def test_revocation_while_timer_waits_in_thread_executor_blocks_mutation(tmp_path, monkeypatch, action):
    async def scenario():
        store, _, timers, _, agent, _ = build_test_harness(
            tmp_path, enable_cloud=False, enable_audio_cloud=False)
        now = utcnow()
        with store.connect() as db:
            db.execute("INSERT INTO devices VALUES('executor-wait-display','합성 표시 기기',?,0,?,'1024x768')", (now, now))
        store.set('layout', {'columns': 8, 'rows': 6, 'version': 1, 'widgets': [
            {'id': 'executor-wait-timer', 'type': 'timers', 'x': 0, 'y': 0, 'w': 2, 'h': 2, 'config': {}},
        ]})
        before = timers.start(TimerStart(request_id='seed-executor-stop', duration_seconds=240), 'synthetic-seed') if action == 'stop' else None
        expected_receipts = 1 if before else 0
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1, thread_name_prefix='synthetic-agent-guard'))
        entered, release = threading.Event(), threading.Event()
        queued = asyncio.Event()

        def occupy_executor():
            entered.set()
            if not release.wait(3):
                raise AssertionError('Synthetic executor hold was not released.')

        occupant = loop.run_in_executor(None, occupy_executor)
        while not entered.is_set():
            await asyncio.sleep(0.001)
        original_to_thread = asyncio.to_thread

        async def observe_to_thread(callback, *args, **kwargs):
            queued.set()
            return await original_to_thread(callback, *args, **kwargs)

        monkeypatch.setattr('app.widget_protocol.timers.asyncio.to_thread', observe_to_thread)
        session = {'role': 'display', 'device_id': 'executor-wait-display'}
        text = '2분 타이머 시작' if action == 'start' else '현재 타이머 종료'
        command = asyncio.create_task(agent.submit_command(AgentCommandInput(
            request_id='revoke-in-executor', text=text, mode='local_only'), session=session))
        try:
            await queued.wait()
            with store.connect() as db:
                db.execute("UPDATE devices SET revoked=1 WHERE id='executor-wait-display'")
        finally:
            release.set()
        await occupant
        with pytest.raises(HTTPException) as error:
            await command
        assert error.value.status_code == 401
        if before:
            assert timers.get(before['id'])['state'] == 'running'
        else:
            assert timers.snapshot()['items'] == []
        with store.connect() as db:
            assert db.execute('SELECT COUNT(*) FROM hub_timer_requests').fetchone()[0] == expected_receipts

    run(scenario())
