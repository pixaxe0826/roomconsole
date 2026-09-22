"""Dataset orchestration and durable per-case output; runtime never receives gold."""
from __future__ import annotations
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import threading
import tempfile
import subprocess
import sys
import time

from . import __version__
from .paths import result_directory
from .dataset import NAME, category, fingerprint, stable
from .reports import write_json, write_reports
from .scoring import project, score, summarize

ROOT = Path(__file__).resolve().parents[1]


def source_state():
    def git(*args):
        p = subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True, check=False)
        return p.stdout.strip() if p.returncode == 0 else None
    def files_hash(folder):
        h = hashlib.sha256()
        for p in sorted(folder.rglob('*.py')):
            if '__pycache__' not in p.parts:
                h.update(p.relative_to(ROOT).as_posix().encode()); h.update(p.read_bytes())
        return h.hexdigest()
    try:
        commit, tree, dirty = git('rev-parse', 'HEAD'), git('rev-parse', 'HEAD^{tree}'), bool(git('status', '--porcelain'))
    except FileNotFoundError:
        commit = tree = None; dirty = None
    layers = {
        'normalizer_version': ['app/assistant.py'],
        'router_version': ['app/assistant.py', 'app/fast_reads.py', 'app/command_routing.py', 'app/timer_commands.py', 'app/widget_bridge.py'],
        'context_version': ['app/clock_service.py', 'app/widget_bridge.py'],
        'policy_version': ['app/capabilities.py', 'app/widget_protocol/core.py', 'app/widget_bridge.py'],
        'formatter_version': ['app/widget_bridge.py', 'app/assistant.py', 'app/timer_commands.py']}
    layer_versions = {key: fingerprint({name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
                                      for name in names if (ROOT/name).is_file()})
                      for key, names in layers.items()}
    return {'layer_versions': layer_versions, 'parser_enabled': False, 'parser_version': None,
            'proposal_repair_enabled': False, 'layer_metadata_scope': 'Current baseline component hashes; observed production traces remain authoritative.',
            'git_commit': commit, 'production_git_sha': commit, 'benchmark_git_sha': commit, 'git_tree': tree, 'working_tree_dirty': dirty,
            'production_source_hash': files_hash(ROOT / 'app'), 'harness_source_hash': files_hash(ROOT / 'benchmarks')}


def runtime_envelope(suite, cases, config, mode):
    """Explicit input projection. Never forward a dataset record or expected.*."""
    initial = cases[0]
    fixture = suite.fixtures
    if initial.get('fixture_id'):
        fixture = suite.fixtures['snapshots'][initial['fixture_id']]
    for case in cases:
        if case.get('fixture_id') != initial.get('fixture_id'):
            raise ValueError('A session cannot switch fixture snapshots silently')
    return {'fixture': deepcopy(fixture), 'reference_datetime': initial.get('reference_datetime', suite.manifest['reference_datetime']),
            'timezone': suite.manifest.get('timezone', 'UTC'), 'config': deepcopy(config), 'mode': mode,
            'isolate_inputs': not bool(initial.get('session_id')),
            'inputs': [{'text': case['input_text'],
                        'reference_datetime': case.get('reference_datetime', suite.manifest['reference_datetime']),
                        'session': deepcopy(fixture.get('session', {}))} for case in cases]}


def execute_group(payload):
    """Stream observations across a gold-free process boundary; checkpoint each case.

    The worker imports Python once per fixture/session, but single-turn applications,
    clocks, SQLite stores and model notebooks are recreated for EVERY input.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith('HUB_')}
    if payload['config'].get('llm') == 'local' and os.environ.get('HUB_LLM_API_KEY'):
        env['HUB_LLM_API_KEY'] = os.environ['HUB_LLM_API_KEY']
    env.update(PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
    started = time.perf_counter()
    timeout = payload['config'].get('timeout', 180) + 30
    with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as errors:
        process = subprocess.Popen([sys.executable, '-m', 'benchmarks.worker'], cwd=ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
            text=True, encoding='utf-8', env=env)
        lines = queue.Queue()
        def read_lines():
            try:
                for line in process.stdout: lines.put(line)
            finally: lines.put(None)
        reader = threading.Thread(target=read_lines, daemon=True)
        reader.start()
        try:
            process.stdin.write(stable(payload)); process.stdin.close()
            for index in range(len(payload['inputs'])):
                error = None
                try:
                    line = lines.get(timeout=timeout)
                    if line is None:
                        process.wait(timeout=5); errors.seek(0)
                        raise ValueError(errors.read(8000).strip() or 'Worker returned no observation')
                    observed = json.loads(line)
                    if not isinstance(observed, dict): raise ValueError('Invalid worker observation')
                except (queue.Empty, ValueError, subprocess.TimeoutExpired) as exc:
                    error = str(exc) or 'Worker timed out; unprocessed cases were not evaluated'
                if error is not None:
                    for _ in range(index, len(payload['inputs'])):
                        yield {'harness_error': error, 'record': {}, 'adapter_calls': [],
                               'llm': {'attempts': []}, 'business_state_changed': False,
                               'latency': {}}, (time.perf_counter()-started)*1000
                    break
                yield observed, (time.perf_counter()-started)*1000
            else:
                extra = lines.get(timeout=timeout)
                if extra is not None: raise ValueError('Worker returned unexpected extra output')
                if process.wait(timeout=5) != 0:
                    errors.seek(0); raise ValueError('Worker cleanup failed: ' + errors.read(8000))
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
            reader.join(timeout=5)
            process.stdout.close()
            if not process.stdin.closed: process.stdin.close()


def run_suite(suite, selected, *, name, results_root, mode='full', backend=None, quiet=False):
    if not NAME.fullmatch(name): raise ValueError('Invalid run name')
    if not selected: raise ValueError('No cases match the selection')
    groups = OrderedDict()
    for case in selected:
        key = ('session', case['session_id']) if case.get('session_id') else ('isolated', case.get('fixture_id'))
        groups.setdefault(key, []).append(case)
    for (kind, identity), items in groups.items():
        if kind == 'session':
            items.sort(key=lambda c: c['turn'])
            if [c['turn'] for c in items] != list(range(1, len(items) + 1)):
                raise ValueError(f'Session {identity}: select all prerequisite turns starting at 1')
    folder = result_directory(results_root, suite.path) / name
    folder.mkdir(parents=True, exist_ok=False)
    (folder / 'trace').mkdir()
    backend = backend or {'llm': 'disabled'}
    config = {'name': name, 'suite': suite.name, 'suite_version': suite.manifest['version'],
              'dataset_hash': suite.digest, 'case_count': len(suite.cases), 'dataset_source': 'external',
              'dataset_write_protection': 'caller_read_only_not_a_kernel_mount_boundary', 'mode': mode, 'selected_count': len(selected),
              'selected_ids': [c['id'] for c in selected], 'harness_version': __version__,
              'reference_datetime': suite.manifest['reference_datetime'],
              'timezone': suite.manifest.get('timezone', 'UTC'),
              'model_configuration': dict(backend, temperature=0, non_thinking=True, seed=None, context_size=None, quantization=None,
                  parameter_provenance='Actual payload per trace; unsupported backend properties remain null.'),
              'python': platform.python_version(), 'platform': platform.platform(),
              'started_at': datetime.now(timezone.utc).isoformat(), 'run_status': 'RUNNING',
              'warnings': suite.warnings, **source_state()}
    write_json(folder / 'config.json', config)
    rows = []
    try:
        with (folder / 'raw_results.jsonl').open('x', encoding='utf-8') as raw:
            for items in groups.values():
                observations = execute_group(runtime_envelope(suite, items, backend, mode))
                for index, (obs, wall_ms) in enumerate(observations):
                    case = items[index]
                    actual = project(obs, suite.projection)
                    scores = score(case, actual, suite.projection, mode)
                    trace_name = hashlib.sha256(case['id'].encode()).hexdigest()[:24] + '.json'
                    row = {'case_id': case['id'], 'parent_id': case.get('parent_id'), 'input_text': case['input_text'],
                           'source_type': case['source_type'], 'domain': case['expected']['domain'],
                           'category': category(case), 'tags': case.get('tags', []),
                           'split': case.get('split', 'development'), 'expected': case['expected'],
                           'expected_response': case.get('expected_response'),
                           'actual': actual, 'score': scores, 'trace_file': 'trace/' + trace_name,
                           'session_wall_ms': wall_ms}
                    write_json(folder / 'trace' / trace_name, obs)
                    raw.write(stable(row) + '\n'); raw.flush(); os.fsync(raw.fileno())
                    rows.append(row)
                    write_json(folder / 'progress.json', {'completed': len(rows), 'selected': len(selected),
                                                          'last_case': case['id'], 'run_status': 'RUNNING'})
                    if not quiet:
                        state = 'PASS' if scores['mode_success'] is True else 'FAIL' if scores['eligible'] else 'NOT_EVALUATED'
                        print(f'[{len(rows)}/{len(selected)}] {case["id"]} {state} {actual["route"]}', flush=True)
        config['run_status'] = 'COMPLETED' if all(r['score']['eligible'] for r in rows) else 'PARTIAL_EVALUATION'
    except BaseException:
        config['run_status'] = 'INTERRUPTED'
        raise
    finally:
        config['finished_at'] = datetime.now(timezone.utc).isoformat()
        config['processed_count'] = len(rows)
        write_json(folder / 'config.json', config)
        metrics = summarize(rows)
        write_reports(folder, config, rows, metrics)
        write_json(folder / 'progress.json', {'completed': len(rows), 'selected': len(selected), 'run_status': config['run_status']})
    return folder, metrics
