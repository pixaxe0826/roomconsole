"""Tiny generated suites only. The CLI/worker must never use production state."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from benchmark_cases import make_suite
from benchmarks.agent import main
from benchmarks.agent_config import validate_config
from benchmarks.agent_gateway import RunBudget
from benchmarks.agent_runner import (
    interrupted_budget, prepare_groups, run_agent_suite, worker_environment, worker_events,
)
from benchmarks.dataset import load_suite


ROOT = Path(__file__).resolve().parents[1]


def config(**overrides):
    return validate_config(dict(live=False, timeout=10, agent_mode='auto',
        fast_model='gpt-6.1-sol', deep_model='gpt-6.1-sol', reasoning_effort='high',
        allow_shared_notes=False, max_cost_usd='1', max_calls=4) | overrides)


@pytest.fixture
def suite(tmp_path):
    root = tmp_path / 'external'
    make_suite(root)
    return load_suite('synthetic', root)


def test_gold_free_payload_and_config_rejects_unexpected_fields(suite):
    payload, ordered = prepare_groups(suite, suite.cases, config())
    encoded = json.dumps(payload)
    for marker in ('"expected"', '"case_id"', '"tags"', '"TEST001"', '"critical_slots"'):
        assert marker not in encoded
    assert len(ordered) == 4
    assert len(payload['groups']) == 1
    assert payload['groups'][0]['isolate_inputs'] is True
    with pytest.raises(ValueError):
        validate_config(config() | {'OPENAI_API_KEY': 'synthetic-private'})
    from benchmarks.agent_worker import validate_payload
    invalid = deepcopy(payload)
    invalid['groups'][0]['inputs'][0]['expected'] = {}
    with pytest.raises(ValueError):
        validate_payload(invalid)


def test_session_prerequisites_are_not_silently_discarded(suite):
    case = deepcopy(suite.cases[0])
    case.update(session_id='synthetic-session', turn=2)
    with pytest.raises(ValueError, match='prerequisite'):
        prepare_groups(suite, [case], config())


def test_clean_environment_explicit_live_only(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-key-one')
    monkeypatch.setenv('HUB_OPENAI_API_KEY', 'synthetic-key-two')
    monkeypatch.setenv('HUB_LLM_API_KEY', 'synthetic-legacy-key')
    monkeypatch.setenv('HUB_DATA_DIR', '/synthetic-production-must-not-open')
    monkeypatch.setenv('HUB_OPENAI_ENABLED', 'true')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://invalid.example')
    monkeypatch.setenv('ROOM_HUB_BENCHMARK_DATA', '/synthetic-dataset-not-for-worker')
    offline = worker_environment(False)
    assert not any(k.startswith('HUB_') for k in offline)
    assert 'OPENAI_API_KEY' not in offline
    assert 'OPENAI_BASE_URL' not in offline
    assert 'ROOM_HUB_BENCHMARK_DATA' not in offline
    live = worker_environment(True)
    assert live['OPENAI_API_KEY'] == 'synthetic-key-one'
    assert live['HUB_OPENAI_API_KEY'] == 'synthetic-key-two'
    assert 'HUB_LLM_API_KEY' not in live
    assert 'HUB_DATA_DIR' not in live


def test_preflight_no_app_import_database_or_provider(suite, tmp_path):
    sentinel = tmp_path / 'must-not-create'
    env = worker_environment(False) | {'HUB_DATA_DIR': str(sentinel), 'OPENAI_API_KEY': 'synthetic-no-network'}
    code = ('import sys, socket\n'
            'socket.create_connection=lambda *a,**k: (_ for _ in ()).throw(AssertionError("network forbidden"))\n'
            'from benchmarks.agent import main\n'
            'rc=main(sys.argv[1:])\n'
            'assert "app.main" not in sys.modules\n'
            'raise SystemExit(rc)\n')
    result = subprocess.run([sys.executable, '-c', code, '--suite-root', str(suite.path.parent),
        '--results-root', str(tmp_path / 'out'), 'preflight', '--suite', 'synthetic',
        '--live', '--max-cost-usd', '1', '--max-calls', '4'], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['api_calls'] == 0 and report['provider_verified'] is False
    assert report['openai_key_configured'] is True
    assert not sentinel.exists()
    assert not (tmp_path / 'out').exists()
    assert 'synthetic-no-network' not in result.stdout + result.stderr


@pytest.mark.parametrize('extra', [[], ['--max-cost-usd', '1'], ['--max-calls', '4']])
def test_live_requires_both_explicit_limits(suite, tmp_path, extra, capsys):
    assert main(['--suite-root', str(suite.path.parent), '--results-root', str(tmp_path / 'out'),
        'run', '--suite', 'synthetic', '--name', 'must-not-run', '--live', *extra]) == 2
    assert '--live requires explicit' in capsys.readouterr().err
    assert not (tmp_path / 'out').exists()


def test_live_missing_key_fails_before_worker(suite, tmp_path, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('HUB_OPENAI_API_KEY', raising=False)
    with pytest.raises(ValueError, match='No OpenAI-specific'):
        run_agent_suite(suite, suite.cases, name='not-run', results_root=tmp_path / 'out',
                        configuration=config(live=True))
    assert not (tmp_path / 'out').exists()


def test_offline_cli_subprocess_and_private_immutable_outputs(suite, tmp_path, monkeypatch):
    from hashlib import sha256
    before = {p.name: sha256(p.read_bytes()).hexdigest() for p in suite.path.iterdir()}
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-ambient-must-be-stripped')
    folder, metrics, status = run_agent_suite(suite, suite.cases, name='offline',
        results_root=tmp_path / 'results', configuration=config(), quiet=True)
    assert status == 'PARTIAL_EVALUATION'
    assert metrics['budget']['dispatched_calls'] == 0
    assert metrics['processed'] == 4
    assert metrics['evaluated'] < 4
    assert not (folder / 'config.json').exists()  # Old analyzer must not accept this engine.
    assert not (folder / 'raw_results.jsonl').exists()
    assert len(list((folder / 'agent_trace').glob('*.json'))) == 4
    assert before == {p.name: sha256(p.read_bytes()).hexdigest() for p in suite.path.iterdir()}
    for path in folder.rglob('*'):
        if path.is_file():
            assert 'synthetic-ambient-must-be-stripped' not in path.read_text(encoding='utf-8')
            if os.name != 'nt':
                assert path.stat().st_mode & 0o077 == 0
    if os.name != 'nt':
        assert folder.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        run_agent_suite(suite, suite.cases, name='offline', results_root=tmp_path / 'results',
                        configuration=config(), quiet=True)


def test_worker_crash_keeps_uncertain_cost_and_all_cases_visible(suite, tmp_path, monkeypatch):
    budget = RunBudget('1', 4)
    budget.reserve('synthetic-request', 200, 'gpt-4o-mini')
    budget.dispatched('synthetic-request')
    def crashed(_payload):
        yield {'kind': 'budget', 'sequence': 1, 'budget': budget.snapshot()}
        raise RuntimeError('Synthetic crash')
    monkeypatch.setattr('benchmarks.agent_runner.worker_events', crashed)
    folder, metrics, status = run_agent_suite(suite, suite.cases, name='crash',
        results_root=tmp_path / 'out', configuration=config(), quiet=True)
    assert status == 'INTERRUPTED'
    assert metrics['evaluated'] == 0 and metrics['processed'] == 4
    assert metrics['budget']['unknown_microdollars'] == 200
    assert metrics['budget']['reserved_microdollars'] == 0
    assert metrics['observed_safety']['incomplete_observation_cases'] == 4
    assert json.loads((folder / 'budget.json').read_text())['cost_complete'] is False


def test_run_budget_cap_stop_does_not_omit_remaining_cases(suite, tmp_path, monkeypatch):
    budget = RunBudget('1', 1)
    budget.stop('max_calls_reached')
    def stopped(_payload):
        yield {'kind': 'done', 'processed': 0, 'budget': budget.snapshot()}
    monkeypatch.setattr('benchmarks.agent_runner.worker_events', stopped)
    folder, metrics, status = run_agent_suite(suite, suite.cases, name='stopped',
        results_root=tmp_path / 'out', configuration=config(), quiet=True)
    assert status == 'STOPPED'
    assert metrics['evaluated'] == 0 and metrics['processed'] == 4
    assert len((folder / 'agent_raw_results.jsonl').read_text().splitlines()) == 4


def test_budget_checkpoint_ack_roundtrip(suite, monkeypatch):
    """A child cannot emit done until the parent advances past its budget write."""
    real_popen = subprocess.Popen
    code = ('import json,sys\n'
            'json.loads(sys.stdin.readline())\n'
            'print(json.dumps({"kind":"budget","sequence":1,"budget":{}}),flush=True)\n'
            'assert json.loads(sys.stdin.readline()) == {"ack":1}\n'
            'print(json.dumps({"kind":"done","processed":0,"budget":{}}),flush=True)\n')
    def fake_popen(_cmd, **kwargs):
        return real_popen([sys.executable, '-c', code], **kwargs)
    monkeypatch.setattr('benchmarks.agent_runner.subprocess.Popen', fake_popen)
    payload, _ = prepare_groups(suite, suite.cases, config())
    events = worker_events(payload)
    assert next(events)['kind'] == 'budget'
    # next() sends the acknowledgement; no network exists in this fixture.
    assert next(events)['kind'] == 'done'
    with pytest.raises(StopIteration):
        next(events)


def test_interrupted_budget_never_converts_unknown_to_zero():
    budget = RunBudget('1', 2)
    budget.reserve('one', 40, 'gpt-4o-mini')
    result = interrupted_budget(budget.snapshot(), 'worker_failure')
    assert result['unknown_microdollars'] == 40
    assert result['calls'][0]['state'] == 'unknown'
    assert result['spent_microdollars'] == 0
    assert result['cost_complete'] is False
