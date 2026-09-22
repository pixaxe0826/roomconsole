"""Harness tests use only synthetic state and fake provider responses, never Qwen."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys

import pytest

from benchmarks.dataset import load_suite, stable
from benchmark_cases import make_suite
from benchmarks.runtime import Runtime, TextInput
from benchmarks.runner import runtime_envelope, run_suite
from benchmarks.scoring import equal, latency, project, score, summarize
from benchmarks.reports import compare_runs, html_report


@pytest.fixture
def suite(tmp_path_factory):
    root = tmp_path_factory.mktemp('synthetic-benchmark')
    make_suite(root)
    return load_suite('synthetic', root)


@pytest.fixture
def copied_suite(tmp_path):
    return make_suite(tmp_path / 'suites', 'custom')


def edit_cases(path, fn):
    p = path / 'cases.jsonl'
    rows = [json.loads(line) for line in p.read_text().splitlines()]
    fn(rows)
    p.write_text(''.join(stable(r) + '\n' for r in rows), encoding='utf-8')


def run_text(suite, text, mode='full', config=None, inspect=None):
    async def run():
        runtime = Runtime(suite.fixtures, suite.manifest['reference_datetime'], suite.manifest['timezone'], config or {'llm': 'disabled'})
        try:
            result = await runtime.run(TextInput(text, suite.manifest['reference_datetime'], suite.fixtures.get('session', {})), mode)
            if inspect: inspect(runtime, result)
            return result
        finally:
            await runtime.close()
    return asyncio.run(run())


def test_generated_suite_counts_and_gold_structure(suite):
    assert len(suite.cases) == 4
    assert len(suite.select(source_type='unique')) == 3
    assert len(suite.select(source_type='paraphrase')) == 1
    assert len({c['expected']['capability'] for c in suite.cases}) == 3
    assert not suite.warnings


@pytest.mark.parametrize('mutation', [
    lambda r: r.append(deepcopy(r[0])),
    lambda r: r[1].update(input_text=r[0]['input_text']),
    lambda r: r[3].update(parent_id='not-present'),
    lambda r: r[0].pop('expected'),
    lambda r: r[0]['expected'].update(capability='WRONG SPACE'),
    lambda r: r[0]['expected'].update(policy='MAYBE'),
    lambda r: r[0]['expected'].update(status='invented_status'),
    lambda r: r[0]['expected']['slots'].update(date=17),
    lambda r: r[2]['expected']['slots'].update(memo_id='not-in-fixture'),
    lambda r: r[0].update(reference_datetime='2026-09-21T09:00:00'),
    lambda r: r[0].update(fixture_id='not-present'),
])
def test_dataset_validation_rejects_corruption(copied_suite, mutation):
    edit_cases(copied_suite, mutation)
    with pytest.raises((ValueError, KeyError)):
        load_suite('custom', copied_suite.parent)


def test_filters_and_future_split_without_code_change(copied_suite):
    edit_cases(copied_suite, lambda r: r[0].update(split='holdout', category='new_category', tags=['new_tag']))
    suite = load_suite('custom', copied_suite.parent)
    assert len(suite.select(split='holdout', categories=['new_category'], tags=['new_tag'])) == 1
    assert not suite.select(tags=['absent'])


def test_dataset_path_escape(copied_suite):
    p = copied_suite / 'manifest.json'; data = json.loads(p.read_text())
    data['fixtures_file'] = '../outside.json'; p.write_text(json.dumps(data))
    with pytest.raises(ValueError): load_suite('custom', copied_suite.parent)


def test_duplicate_json_keys(copied_suite):
    p = copied_suite / 'cases.jsonl'
    p.write_text(p.read_text().replace('"id":"TEST001"', '"id":"TEST001","id":"TEST001"', 1)) if '"id":"TEST001"' in p.read_text() else p.write_text(p.read_text().replace('"id": "TEST001"', '"id": "TEST001", "id": "TEST001"', 1))
    with pytest.raises(ValueError): load_suite('custom', copied_suite.parent)


def test_gold_never_enters_runtime_envelope(suite):
    case = deepcopy(suite.cases[0]); case['expected']['capability'] = 'GOLD_SENTINEL'
    case.update(parent_id='PARENT_SENTINEL', tags=['TAG_SENTINEL'], expected_response='ANSWER_SENTINEL')
    payload = runtime_envelope(suite, [case], {'llm': 'disabled'}, 'full')
    text = stable(payload)
    assert not any(s in text for s in ('GOLD_SENTINEL', 'PARENT_SENTINEL', 'TAG_SENTINEL', 'ANSWER_SENTINEL'))
    assert set(payload['inputs'][0]) == {'text', 'reference_datetime', 'session'}


def test_same_core_detect_is_called(suite, monkeypatch):
    import app.llm
    original = app.llm.detect; calls = []
    def spy(*args, **kwargs):
        calls.append(args[0]); return original(*args, **kwargs)
    monkeypatch.setattr(app.llm, 'detect', spy)
    result = run_text(suite, '현재 메모 읽어줘')
    assert calls == ['현재 메모 읽어줘']
    assert result['record']['routing']['route'] == 'FAST_PATH'
    assert result['adapter_calls'][0]['adapter'] == 'FakeMemoAdapter'
    assert not result['business_state_changed']


@pytest.mark.parametrize('mode', ['nlu', 'decision'])
def test_mode_boundaries_stop_before_adapter(suite, mode):
    result = run_text(suite, '4분 타이머 시작', mode)
    assert result['boundary'] == mode
    assert not result['adapter_calls']
    assert not result['business_state_changed']
    assert not result['llm']['attempts']
    actual = project(result, suite.projection)
    assert actual['capability'] == 'timer.start'
    assert actual['status'] is None
    assert actual['policy'] == ('EXECUTE' if mode == 'decision' else None)


def test_fixed_clock_relative_day_and_confirmation(suite):
    result = run_text(suite, '내일 할 일에 합성 항목 추가해')
    actual = project(result, suite.projection)
    assert actual['capability'] == 'todo.add'
    assert actual['slots']['date'] == '2026-09-22'
    assert actual['policy'] == 'CONFIRM'
    assert not actual['adapter_executed'] and not actual['state_changed']


def test_unavailable_llm_is_not_fake_inference(suite):
    result = run_text(suite, '메모 내용 좀 읽어 볼래')
    actual = project(result, suite.projection)
    assert actual['llm_blocked']
    assert not actual['llm_called'] and actual['llm_requested']
    result_score = score(suite.cases[2], actual, suite.projection)
    assert result_score['task_success'] is None
    assert result_score['failures'] == ['not_evaluated_llm_or_runtime_unavailable']
    assert result_score['capability_ok'] is None and not result_score['slot_matches']


def test_actual_backend_reused_with_synthetic_provider(suite, monkeypatch):
    import app.llm
    calls = []
    async def provider(self, endpoint, body, timeout):
        calls.append(json.loads(body))
        result = {'model': 'synthetic-test-provider', 'choices': [{'finish_reason': 'stop', 'message': {
            'content': stable({'widget': 'memo', 'action': 'read', 'target': None, 'args': {}})}}]}
        return result, stable(result)
    monkeypatch.setattr(app.llm.ChatBackend, 'generate', provider)
    result = run_text(suite, '메모 내용 좀 읽어 볼래', config={'llm': 'local'})
    actual = project(result, suite.projection)
    assert actual['llm_called'] and actual['status'] == 'success'
    assert calls[0]['temperature'] == 0
    assert 'expected' not in stable(calls)
    assert calls[0]['messages'][-1]['content'].find('메모') >= 0


def test_fixture_reset_and_explicit_session_state(suite):
    async def run():
        r = Runtime(suite.fixtures, suite.manifest['reference_datetime'], suite.manifest['timezone'], {'llm': 'disabled'})
        try:
            first = await r.run(TextInput('4분 타이머 시작', suite.manifest['reference_datetime'], {}))
            second = await r.run(TextInput('현재 타이머 종료', suite.manifest['reference_datetime'], {}))
            assert first['business_state_changed'] and second['business_state_changed']
            assert second['adapter_calls'][0]['changed']
        finally: await r.close()
    asyncio.run(run())
    reset = run_text(suite, '현재 타이머 종료')
    assert not reset['business_state_changed']


def test_sandbox_refuses_external_database(suite, tmp_path):
    from app.widget_protocol import WidgetRequest
    from app.widget_protocol.core import ExecutionContext
    async def run():
        r = Runtime(suite.fixtures, suite.manifest['reference_datetime'], suite.manifest['timezone'], {'llm': 'disabled'})
        try:
            r.store.path = tmp_path / 'forbidden.sqlite3'
            with pytest.raises(RuntimeError, match='non-sandbox'):
                await r.registry.get('memo').execute(WidgetRequest(request_id='isolation-test', widget='memo', action='read'),
                    ExecutionContext(principal='test', role='admin', permissions=frozenset({'read'})))
            assert not r.store.path.exists()
        finally: await r.close()
    asyncio.run(run())


def test_no_corpus_utterances_or_gold_imports_in_runtime():
    root = Path(__file__).parents[1] / 'benchmarks'
    text = (root / 'runtime.py').read_text()
    assert 'TEST001' not in text and 'all_250' not in text
    assert 'from .scoring' not in text and 'from .dataset' not in text


@pytest.mark.parametrize('left,right,result', [(1, True, False), ('1', 1, False), (None, None, True),
                                              ([1, 2], [2, 1], False), ('17:00', '17:00', True)])
def test_strict_slot_equality(left, right, result):
    assert equal(left, right) is result
    assert equal([1, 2], [2, 1], unordered=True)


def test_latency_aggregation_and_empty():
    result = latency([1, 2, 3, 4, 5])
    assert result['p50'] == 3 and result['p95'] == pytest.approx(4.8)
    assert result['p99'] == pytest.approx(4.96)
    assert latency([])['max'] is None


def test_full_task_scoring_and_false_execution(suite):
    observation = run_text(suite, '현재 메모 읽어줘')
    actual = project(observation, suite.projection)
    case = suite.cases[2]
    assert score(case, actual, suite.projection)['task_success'] is True
    actual['slots']['memo_id'] = 'wrong'
    assert score(case, actual, suite.projection)['task_success'] is False
    actual['adapter_calls'][0]['read_only'] = False
    actual['state_changed'] = True
    assert score(case, actual, suite.projection)['false_execution'] is True


def test_clarification_policy_and_null_slots(suite):
    actual = project({'record': {}, 'final_status': 'needs_clarification'}, {})
    case = deepcopy(suite.cases[1])
    result = score(case, actual, suite.projection)
    assert result['clarification_ok'] is True and result['task_success'] is False
    assert result['slot_matches']['date'] is True


def test_report_html_escaping():
    report = html_report('<script>alert(1)</script>', 'test')
    assert '<script>' not in report and '&lt;script&gt;' in report


def test_run_reports_comparison_and_no_overwrite(suite, tmp_path):
    selected = [suite.cases[2]]
    a, m = run_suite(suite, selected, name='first', results_root=tmp_path, quiet=True)
    b, _ = run_suite(suite, selected, name='second', results_root=tmp_path, quiet=True)
    assert m['task_success']['rate'] == 1
    for name in ['raw_results.jsonl', 'cases.csv', 'metrics.json', 'failures.jsonl', 'summary.md', 'summary.html', 'FAILURES.md', 'config.json']:
        assert (a / name).is_file()
    comparison = compare_runs(a, b, tmp_path / 'comparison')
    assert comparison['shared_case_count'] == 1
    assert comparison['changes']['task_success']['delta_percentage_points'] == 0
    cfg = json.loads((b / 'config.json').read_text()); cfg['dataset_hash'] = 'changed'
    (b / 'config.json').write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match='Incompatible'): compare_runs(a, b, tmp_path / 'bad')
    with pytest.raises(FileExistsError): run_suite(suite, selected, name='first', results_root=tmp_path, quiet=True)


@pytest.mark.parametrize('shared', [False, True])
def test_reused_worker_isolation_and_explicit_session(suite, shared):
    from benchmarks.runner import execute_group
    cases = [deepcopy(suite.cases[0]), deepcopy(suite.cases[1])]
    cases[0]['input_text'] = '4분 타이머 시작'
    cases[1]['input_text'] = '현재 타이머 종료'
    if shared:
        for i, case in enumerate(cases): case.update(session_id='synthetic-session', turn=i+1)
    payload = runtime_envelope(suite, cases, {'llm': 'disabled'}, 'full')
    observations = [o for o, wall in execute_group(payload)]
    assert len(observations) == 2 and not any(o['harness_error'] for o in observations)
    assert observations[0]['business_state_changed']
    assert observations[1]['business_state_changed'] is shared


def test_small_external_suite_add_replace_remove_without_code(copied_suite):
    original = [json.loads(x) for x in (copied_suite/'cases.jsonl').read_text().splitlines()]
    manifest_path = copied_suite/'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    for key in ('registry_file', 'case_count', 'unique_instruction_count', 'paraphrase_count'):
        manifest.pop(key, None)
    manifest.update(version='external-test-2', case_files=['different-cases.jsonl'])
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    path = copied_suite/'different-cases.jsonl'
    path.write_text(stable(original[0])+'\n', encoding='utf-8')
    assert len(load_suite('custom', copied_suite.parent).cases) == 1
    path.write_text(stable(original[1])+'\n'+stable(original[2])+'\n', encoding='utf-8')
    assert len(load_suite('custom', copied_suite.parent).cases) == 2
    from benchmarks.dataset import list_suites
    shutil.rmtree(copied_suite)
    assert list_suites(copied_suite.parent) == []


def test_fake_provider_unsupported_arguments_never_execute(suite, monkeypatch):
    import app.llm
    async def provider(self, endpoint, body, timeout):
        result = {'model': 'synthetic-test-provider', 'choices': [{'finish_reason': 'stop', 'message': {
            'content': stable({'widget':'memo', 'action':'read', 'target':None, 'args':{'user_confirmed':True}})}}]}
        return result, stable(result)
    monkeypatch.setattr(app.llm.ChatBackend, 'generate', provider)
    observation = run_text(suite, '메모 내용 좀 읽어 볼래', config={'llm':'local'})
    actual = project(observation, suite.projection)
    assert actual['unsupported_value'] and not actual['adapter_executed']
    assert not actual['state_changed']


def test_evaluation_suites_cannot_be_published():
    from scripts.repo_policy import source_path_allowed
    assert not source_path_allowed('benchmarks/suites/example/cases.jsonl')
    assert not source_path_allowed('benchmarks/suites/example/manifest.json')
    assert source_path_allowed('benchmarks/dataset.py')
    assert not source_path_allowed('benchmarks/results/real-history.jsonl')
    assert not source_path_allowed('app/log.jsonl')
    assert not source_path_allowed('benchmarks/suites/example/data/real.jsonl')


def test_group_consistency_is_separate_from_group_success(suite, tmp_path):
    selected = [suite.cases[2]]
    path, _ = run_suite(suite, selected, name='group-source', results_root=tmp_path, quiet=True)
    first = json.loads((path/'raw_results.jsonl').read_text().splitlines()[0])
    first['actual']['capability'] = 'unknown'
    first['score']['mode_success'] = False
    first['score']['task_success'] = False
    second = deepcopy(first); second.update(case_id='expression-variant',parent_id=first['case_id'],source_type='paraphrase')
    metrics = summarize([first,second])
    assert metrics['paraphrase_consistency']['rate'] == 1
    assert metrics['paraphrase_group_success']['rate'] == 0
    second['actual']['slots']['memo_id'] = 'different-target'
    assert summarize([first,second])['paraphrase_consistency']['rate'] == 0


@pytest.mark.parametrize('fail_transaction', [False, True])
def test_owned_sqlite_closes_retained_handles_and_preserves_transactions(tmp_path, fail_transaction):
    import sqlite3
    from benchmarks.storage import owned_sqlite
    original = sqlite3.connect
    path = tmp_path / 'owned.sqlite3'
    retained = []
    with owned_sqlite(tmp_path):
        with sqlite3.connect(path) as setup:
            setup.execute('CREATE TABLE evidence(value INTEGER)')
        try:
            with sqlite3.connect(path) as db:
                retained.append(db)  # Strong reference survives cleanup and GC.
                db.execute('INSERT INTO evidence VALUES(1)')
                if fail_transaction: raise ValueError('synthetic transaction failure')
        except ValueError:
            pass
        with pytest.raises(sqlite3.ProgrammingError):
            retained[0].execute('SELECT 1')
        with sqlite3.connect(path) as check:
            assert check.execute('SELECT count(*) FROM evidence').fetchone()[0] == int(not fail_transaction)
        outside = tmp_path.parent / 'not-owned.sqlite3'
        with pytest.raises(RuntimeError, match='outside'):
            sqlite3.connect(outside)
        assert not outside.exists()
    assert sqlite3.connect is original
    path.unlink()  # Also exercises Windows file-handle release.


def test_runtime_cleanup_closes_retained_store_context(suite):
    import sqlite3
    original = sqlite3.connect
    async def run():
        runtime = Runtime(suite.fixtures, suite.manifest['reference_datetime'], suite.manifest['timezone'], {'llm':'disabled'})
        root = runtime.root
        try:
            with runtime.store.connect() as retained:
                retained.execute('SELECT 1')
            await runtime.run(TextInput('메모 내용 좀 읽어 볼래', suite.manifest['reference_datetime'], {}))
        finally:
            await runtime.close()
        assert not root.exists()
        with pytest.raises(sqlite3.ProgrammingError): retained.execute('SELECT 1')
    asyncio.run(run())
    assert sqlite3.connect is original
