"""Offline M2 analysis. Frozen M1 files are inputs, never output destinations."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile

from . import __version__
from .dataset import fingerprint, reject_duplicate_keys, stable
from .paths import NAME, no_links, relative_file, result_directory
from .reports import write_json
from .scoring import summarize
from .support import (SupportIndex, capture_snapshot, production_hash, validate_snapshot,
                      SUPPORT_DEFINITION)
from .taxonomy import classify, diagnostics_metrics, rules_hash, TAXONOMY_VERSION

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_RUN_BYTES = 512 * 1024 * 1024
MAX_RUN_FILES = 20000
MAX_ROWS = 10000
REQUIRED = ('config.json', 'metrics.json', 'raw_results.jsonl', 'progress.json')


def _json(text):
    return json.loads(text, object_pairs_hook=reject_duplicate_keys,
                      parse_constant=lambda v: (_ for _ in ()).throw(ValueError('Nonfinite JSON value')))


def inventory(root: Path):
    """Hash regular result files only; reject links/devices and bounded-size abuse."""
    root = result_directory(root)
    if not root.is_dir():
        raise ValueError('Benchmark result directory does not exist: ' + str(root))
    files, total = {}, 0
    for parent, dirs, names in os.walk(root, followlinks=False):
        for name in dirs:
            path = Path(parent) / name
            no_links(path)
            if not path.is_dir():
                raise ValueError('Invalid directory in benchmark results')
        for name in names:
            path = Path(parent) / name
            no_links(path)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                raise ValueError('Benchmark inputs must be regular non-hardlinked files')
            total += info.st_size
            if info.st_size > MAX_FILE_BYTES or total > MAX_RUN_BYTES or len(files) >= MAX_RUN_FILES:
                raise ValueError('Benchmark result exceeds offline analysis safety limits')
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(block)
            files[path.relative_to(root).as_posix()] = {'sha256': digest.hexdigest(), 'bytes': info.st_size}
    return dict(sorted(files.items()))


def _read(root, name, hashes):
    relative_file(name)
    path = root / name
    no_links(path)
    if name not in hashes or not path.is_file():
        raise ValueError('Missing frozen result file: ' + name)
    data = path.read_bytes()
    if len(data) != hashes[name]['bytes'] or hashlib.sha256(data).hexdigest() != hashes[name]['sha256']:
        raise ValueError('Frozen input changed while reading: ' + name)
    return _json(data.decode('utf-8-sig'))


def _same(left, right):
    # Aggregation-only roundoff tolerances, never fuzzy answer/score matching.
    if isinstance(left, dict) and isinstance(right, dict):
        return set(left) == set(right) and all(_same(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    if type(left) is float or type(right) is float:
        return type(left) in (int, float) and type(right) in (int, float) and math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-7)
    return type(left) is type(right) and left == right


def load_run(folder: Path):
    folder = result_directory(folder)
    hashes = inventory(folder)
    if any(name not in hashes for name in REQUIRED):
        raise ValueError('Analyze requires config.json, metrics.json, raw_results.jsonl and progress.json; a pasted summary is not sufficient')
    config, metrics, progress = (_read(folder, name, hashes) for name in ('config.json', 'metrics.json', 'progress.json'))
    if not all(isinstance(x, dict) for x in (config, metrics, progress)):
        raise ValueError('Invalid frozen metadata')
    if config.get('run_status') not in {'COMPLETED', 'PARTIAL_EVALUATION'}:
        raise ValueError('Analysis requires a finished run; keep interrupted artifacts separately')
    if (not isinstance(config.get('dataset_hash'), str) or len(config['dataset_hash']) != 64 or
            any(c not in '0123456789abcdef' for c in config['dataset_hash'])):
        raise ValueError('Frozen dataset hash is missing or invalid')
    if config.get('mode') not in {'nlu', 'decision', 'full'}:
        raise ValueError('Unknown frozen evaluation mode')
    data = (folder / 'raw_results.jsonl').read_bytes()
    if hashlib.sha256(data).hexdigest() != hashes['raw_results.jsonl']['sha256']:
        raise ValueError('Frozen raw results changed while reading')
    lines = data.decode('utf-8-sig').splitlines()
    if not lines or len(lines) > MAX_ROWS:
        raise ValueError('Invalid result row count')
    rows = [_json(line) for line in lines]
    ids = []
    bool_fields = ('capability_ok', 'policy_ok', 'status_ok', 'execution_ok', 'mode_success', 'task_success',
                   'clarification_ok', 'false_execution', 'unsupported_value', 'hallucination_detected',
                   'unnecessary_llm_call')
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('case_id'), str):
            raise ValueError('Invalid result row')
        ids.append(row['case_id'])
        for key in ('expected', 'actual', 'score'):
            if not isinstance(row.get(key), dict):
                raise ValueError('Missing frozen result ' + key)
        s, a, e = row['score'], row['actual'], row['expected']
        if type(s.get('eligible')) is not bool:
            raise ValueError('Invalid eligible flag')
        if any(k not in s or s[k] is not None and type(s[k]) is not bool for k in bool_fields):
            raise ValueError('Frozen scores must be bool/null, not coerced strings')
        if s['eligible'] and type(s['mode_success']) is not bool:
            raise ValueError('Eligible rows require a saved mode success')
        if not s['eligible'] and (s['mode_success'] is not None or s['task_success'] is not None):
            raise ValueError('Unevaluated rows cannot be saved successes/failures')
        if (not isinstance(s.get('slot_matches'), dict) or
                any(type(v) is not bool for v in s['slot_matches'].values()) or
                not isinstance(s.get('failures'), list) or not all(isinstance(f, str) for f in s['failures'])):
            raise ValueError('Invalid frozen score evidence')
        if not isinstance(e.get('capability'), str) or not isinstance(e.get('slots'), dict):
            raise ValueError('Missing expected contract in frozen results')
        if not isinstance(a.get('slots'), dict) or not isinstance(a.get('latency'), dict):
            raise ValueError('Missing observed slots/latency')
        for key in ('llm_called', 'llm_requested', 'llm_blocked', 'inference_failed'):
            if type(a.get(key)) is not bool:
                raise ValueError('Invalid observed LLM flags')
        if type(a.get('llm_calls')) is not int or a['llm_calls'] < 0:
            raise ValueError('Invalid observed LLM call count')
        for value in a['latency'].values():
            if value is not None and (type(value) not in (int, float) or value < 0 or not math.isfinite(value)):
                raise ValueError('Invalid frozen latency')
        for key in ('input_text', 'source_type', 'domain', 'category', 'split'):
            if not isinstance(row.get(key), str):
                raise ValueError('Invalid frozen row ' + key)
        if not isinstance(row.get('tags'), list) or not all(isinstance(x, str) for x in row['tags']):
            raise ValueError('Invalid frozen tags')
        if row.get('parent_id') is not None and not isinstance(row['parent_id'], str):
            raise ValueError('Invalid parent ID')
    selected = config.get('selected_ids')
    if type(config.get('case_count')) is not int or config['case_count'] < len(ids):
        raise ValueError('Frozen suite case count is smaller than selected rows')
    if (len(ids) != len(set(ids)) or not isinstance(selected, list) or
            not all(isinstance(x, str) for x in selected) or len(selected) != len(set(selected)) or
            set(ids) != set(selected) or config.get('selected_count') != len(ids) or
            config.get('processed_count') != len(ids) or progress.get('completed') != len(ids) or
            progress.get('selected') != len(ids) or progress.get('run_status') != config['run_status']):
        raise ValueError('Frozen run IDs/counts/progress are inconsistent')
    if not _same(metrics, summarize(rows)):
        raise ValueError('Saved metrics differ from aggregation of saved scores; never repair M1 in place')
    traces = {}
    for row in rows:
        name = row.get('trace_file')
        if name is not None:
            relative_file(name)
            if not name.startswith('trace/') or not name.endswith('.json'):
                raise ValueError('Trace path must be a relative JSON file within trace/')
        value = _read(folder, name, hashes) if name in hashes else None
        if value is not None and not isinstance(value, dict):
            raise ValueError('Trace must be a JSON object')
        if value is not None:
            for key in ('record', 'llm'):
                if value.get(key) is not None and not isinstance(value[key], dict):
                    raise ValueError('Invalid trace ' + key)
            record = value.get('record') or {}
            for key in ('widget_trace', 'routing'):
                if record.get(key) is not None and not isinstance(record[key], dict):
                    raise ValueError('Invalid trace record.' + key)
            if value.get('adapter_calls') is not None and not isinstance(value['adapter_calls'], list):
                raise ValueError('Invalid trace adapter calls')
            # Cross-check projection-independent observations. Do not re-run the
            # M1 projection with a guessed or newly changed dataset config.
            calls = (value.get('llm') or {}).get('attempts') or []
            if not isinstance(calls, list) or not all(isinstance(c, dict) for c in calls):
                raise ValueError('Invalid LLM trace attempts')
            if sum(bool(c.get('transport_attempted')) for c in calls) != row['actual']['llm_calls']:
                raise ValueError('Raw result and trace LLM counts disagree')
        traces[row['case_id']] = value
    return config, metrics, rows, traces, hashes


def _write_bundle(parent, name, build):
    """Publish complete derived files atomically, with a single-writer name lock."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError('Analysis name must be a simple identifier')
    parent = result_directory(parent)
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / name
    no_links(destination)
    lock = parent / ('.' + name + '.lock')
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError('Analysis name is locked by another process; do not overwrite it') from None
    try:
        os.close(fd)
        if destination.exists():
            raise FileExistsError('Analysis output already exists; choose a new --name')
        with tempfile.TemporaryDirectory(prefix='.analysis-stage-', dir=parent) as temporary:
            stage = Path(temporary)
            build(stage)
            no_links(destination)
            if destination.exists():
                raise FileExistsError('Analysis output appeared during publication')
            stage.rename(destination)
    finally:
        lock.unlink()
    return destination


def analyze_run(results_root, before, name):
    if not isinstance(before, str) or not NAME.fullmatch(before):
        raise ValueError('Input run name must be a simple identifier')
    root = result_directory(results_root)
    folder = root / before
    config, legacy, rows, traces, hashes = load_run(folder)
    expected_hash = config.get('production_source_hash')
    current = production_hash()
    if not isinstance(expected_hash, str) or len(expected_hash) != 64 or current != expected_hash:
        raise ValueError('Production source mismatch: use the app/ bytes that produced this run; M2-only changes are allowed. No override is provided.')
    snapshot = capture_snapshot()
    if config.get('support_snapshot_hash') and 'support_snapshot.json' not in hashes:
        raise ValueError('Run-declared support snapshot is missing')
    if 'support_snapshot.json' in hashes:
        captured = _read(folder, 'support_snapshot.json', hashes)
        validate_snapshot(captured, expected_hash)
        if captured['snapshot_hash'] != config.get('support_snapshot_hash'):
            raise ValueError('Run support snapshot receipt mismatch')
        if captured['snapshot_hash'] != snapshot['snapshot_hash']:
            raise ValueError('Current exposed contract differs from run support snapshot')
    for observation in traces.values():
        catalog = (observation or {}).get('runtime_catalog')
        if catalog is not None and fingerprint(catalog) != snapshot['widget_catalog_hash']:
            raise ValueError('Recorded runtime catalog differs from the current source-matched catalog')
    projection = config.get('support_projection')
    index = SupportIndex(snapshot, rows, projection)
    diagnoses = [classify(row, traces[row['case_id']], index) for row in rows]
    derived = diagnostics_metrics(rows, diagnoses)
    state = {
        'analysis_schema_version': '1.0', 'taxonomy_version': TAXONOMY_VERSION,
        'taxonomy_rules_hash': rules_hash(), 'harness_version': __version__, 'name': name,
        'source_run': before, 'mode': config['mode'], 'suite': config.get('suite'),
        'suite_version': config.get('suite_version'), 'dataset_hash': config.get('dataset_hash'),
        'selected_ids': config['selected_ids'], 'selected_count': config['selected_count'],
        'case_count': config.get('case_count'), 'source_run_status': config['run_status'],
        'production_source_hash': expected_hash, 'production_git_sha': config.get('production_git_sha'),
        'original_benchmark_git_sha': config.get('benchmark_git_sha'),
        'model_configuration': config.get('model_configuration'),
        'support_snapshot_hash': snapshot['snapshot_hash'], 'support_definition': SUPPORT_DEFINITION,
        'input_manifest_hash': fingerprint(hashes), 'score_source': 'frozen_saved_scores',
        'legacy_metrics_preserved': True, 'source_match': True, 'original_files_unchanged': True,
        'model_calls_during_analysis': 0, 'production_db_opened': False,
        'alias_provenance': index.alias_evidence,
        'analysis_started_at': datetime.now(timezone.utc).isoformat(),
        'warnings': [
            'Operation support does not certify all parameters, targets, language or UI behavior.',
            'An undeclared name may need an explicit representation mapping; it is not proof that no equivalent API could exist.',
            'Source matching covers production Python bytes. Model/dependency/hardware reproducibility is separate.',
            'Missing traces reduce evidence coverage; diagnostic labels are not hidden model-reasoning access.',
        ],
    }
    if projection is None:
        state['warnings'].append('M1 lacks projection metadata: only recorded actual/native aliases and production metadata are used; gold is never used to invent aliases.')
    from .runner import source_state
    analysis_source = source_state()
    state['analysis_git_sha'] = analysis_source['git_commit']
    state['analysis_working_tree_dirty'] = analysis_source['working_tree_dirty']
    state['analysis_source_hash'] = analysis_source['harness_source_hash']

    def build(stage):
        write_json(stage / 'support_snapshot.json', snapshot)
        write_json(stage / 'input_integrity.json', hashes)
        write_json(stage / 'original_metrics.json', legacy)
        write_json(stage / 'metrics.json', derived)
        (stage / 'taxonomy.jsonl').write_text(''.join(stable(d) + '\n' for d in diagnoses), encoding='utf-8')
        # Scored result copies are for local comparisons only; never commit them.
        (stage / 'scored_results.jsonl').write_text(''.join(stable(r) + '\n' for r in rows), encoding='utf-8')
        from .analysis_reports import write_analysis_reports
        write_analysis_reports(stage, state, derived, rows, diagnoses)
        if inventory(folder) != hashes:
            raise ValueError('M1 input changed during analysis; derived publication aborted')
        if production_hash() != current:
            raise ValueError('Production source changed during analysis')
        state['analysis_finished_at'] = datetime.now(timezone.utc).isoformat()
        state['run_status'] = 'COMPLETED'
        write_json(stage / 'analysis.json', state)
        # Self-checkable output manifest (hashes detect later changes, not authorship).
        write_json(stage / 'analysis_integrity.json', inventory(stage))
    destination = _write_bundle(root / 'analysis', name, build)
    return destination, state, derived


def load_analysis(folder):
    folder = result_directory(folder)
    hashes = inventory(folder)
    if 'analysis_integrity.json' not in hashes:
        raise ValueError('Analysis integrity manifest is required')
    manifest = _read(folder, 'analysis_integrity.json', hashes)
    if manifest != {k: v for k, v in hashes.items() if k != 'analysis_integrity.json'}:
        raise ValueError('Derived analysis files changed after publication')
    state = _read(folder, 'analysis.json', hashes)
    if state.get('run_status') != 'COMPLETED' or state.get('analysis_schema_version') != '1.0':
        raise ValueError('Analysis did not complete or uses an unsupported version')
    return state, _read(folder, 'metrics.json', hashes), hashes


def verify_analysis(results_root, name):
    if not NAME.fullmatch(name):
        raise ValueError('Invalid analysis name')
    root = result_directory(results_root)
    folder = root / 'analysis' / name
    state, metrics, hashes = load_analysis(folder)
    source = state.get('source_run')
    if not isinstance(source, str) or not NAME.fullmatch(source):
        raise ValueError('Invalid source run reference')
    original = _read(folder, 'input_integrity.json', hashes)
    if inventory(root / source) != original or fingerprint(original) != state['input_manifest_hash']:
        raise ValueError('Original run differs from the frozen analysis input manifest')
    return {'valid': True, 'analysis': name, 'source_run': source, 'original_files_unchanged': True,
            'derived_files_unchanged': True, 'original_metrics_preserved': state['legacy_metrics_preserved'],
            'overall_task_success': metrics['overall_preserved']['task_success'],
            'false_execution': metrics['overall_preserved']['false_execution'],
            'dataset_hash': state['dataset_hash'], 'production_git_sha': state['production_git_sha']}
