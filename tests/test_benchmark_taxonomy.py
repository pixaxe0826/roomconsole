"""Generated diagnostic evidence only; no user dataset, V35 or model access."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys

import pytest

from benchmarks.analysis import (analyze_run, inventory, load_run, verify_analysis, _same)
from benchmarks.analysis_compare import compare_analyses
from benchmarks.dataset import fingerprint, stable
from benchmarks.reports import write_json
from benchmarks.scoring import score, summarize
from benchmarks.support import (SupportIndex, capture_snapshot, production_hash, projection_receipt,
                                validate_snapshot)
from benchmarks.taxonomy import classify, diagnostics_metrics, PRIMARY_ORDER

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def snapshot():
    return capture_snapshot()


@pytest.fixture
def index(snapshot):
    return SupportIndex(snapshot)


def row(identity='CASE1', *, expected_cap='todo.list', actual_cap=None, expected_slots=None,
        actual_slots=None, expected_policy='EXECUTE', actual_policy=None, called=False,
        route='FAST_PATH', unavailable=False, mode='full', critical=None):
    expected_slots = {} if expected_slots is None else expected_slots
    expected_status = {'EXECUTE': 'success', 'CONFIRM': 'needs_confirmation', 'CLARIFY': 'needs_clarification'}[expected_policy]
    case = {'id': identity, 'input_text': 'Synthetic test input only', 'source_type': 'unique',
            'expected': {'domain': expected_cap.split('.')[0], 'capability': expected_cap,
                         'policy': expected_policy, 'status': expected_status, 'slots': expected_slots,
                         'adapter_should_execute': expected_policy == 'EXECUTE'}}
    if critical is not None:
        case['critical_slots'] = critical
    policy = actual_policy or expected_policy
    actual = {
        'capability': actual_cap or expected_cap, 'native_capability': actual_cap or expected_cap,
        'slots': deepcopy(expected_slots if actual_slots is None else actual_slots),
        'policy': policy, 'status': {'EXECUTE': 'success', 'CONFIRM': 'needs_confirmation', 'CLARIFY': 'needs_clarification'}[policy],
        'adapter_executed': policy == 'EXECUTE', 'adapter_calls': [], 'actions': [], 'state_changed': False,
        'route': route, 'llm_called': called, 'llm_calls': int(called), 'llm_requested': called or unavailable,
        'llm_blocked': unavailable, 'inference_failed': False, 'harness_error': None,
        'unsupported_value': False, 'hallucination_detected': False, 'unnecessary_llm_call': False,
        'validation_error': None, 'final_response': 'Synthetic response', 'widget_response': {},
        'latency': {'total_ms': 20.0, 'llm_ms': 10.0 if called else 0.0, 'non_llm_ms': 10.0 if called else 20.0},
    }
    scores = score(case, actual, {}, mode)
    result = {'case_id': identity, 'parent_id': None, 'input_text': case['input_text'],
              'source_type': 'unique', 'category': 'synthetic', 'domain': case['expected']['domain'],
              'tags': [], 'split': 'development', 'expected': case['expected'], 'expected_response': None,
              'actual': actual, 'score': scores,
              'trace_file': 'trace/' + hashlib.sha256(identity.encode()).hexdigest()[:24] + '.json'}
    if critical is not None:
        result['scoring_context'] = {'critical_slots': critical}
    return result


def observation(r, **record):
    return {'record': record, 'llm': {'attempts': [
        {'transport_attempted': True, 'response_raw': None}] if r['actual']['llm_called'] else []},
        'adapter_calls': [], 'mode': 'full'}


def frozen(root, rows=None, name='old-run', *, traces=None, mode='full', snapshot=None):
    rows = rows or [row('FIRST'), row('SECOND', expected_slots={'date': '2026-01-02'}, actual_slots={'date': None})]
    path = root / name; (path / 'trace').mkdir(parents=True)
    cfg = {'name': name, 'suite': 'generated-suite', 'suite_version': 'test-1', 'dataset_hash': 'a'*64,
           'mode': mode, 'selected_count': len(rows), 'processed_count': len(rows), 'case_count': len(rows),
           'selected_ids': [r['case_id'] for r in rows], 'harness_version': '1.1.0',
           'production_source_hash': production_hash(), 'git_commit': 'b'*40,
           'production_git_sha': 'b'*40, 'benchmark_git_sha': 'b'*40,
           'run_status': 'COMPLETED' if all(r['score']['eligible'] for r in rows) else 'PARTIAL_EVALUATION',
           'model_configuration': {'model': 'test-only', 'llm': 'disabled'}}
    if snapshot:
        cfg['support_snapshot_hash'] = snapshot['snapshot_hash']
        cfg['support_projection'] = projection_receipt({})
        write_json(path / 'support_snapshot.json', snapshot)
    write_json(path / 'config.json', cfg)
    write_json(path / 'progress.json', {'completed': len(rows), 'selected': len(rows), 'run_status': cfg['run_status']})
    write_json(path / 'metrics.json', summarize(rows))
    (path / 'raw_results.jsonl').write_text(''.join(stable(r)+'\n' for r in rows), encoding='utf-8')
    (path / 'summary.md').write_bytes(b'Immutable original summary\r\n')
    (path / 'summary.html').write_text('<html>Immutable original</html>', encoding='utf-8')
    for r in rows:
        obs = observation(r) if traces is None else traces.get(r['case_id'])
        if obs is not None:
            write_json(path / r['trace_file'], obs)
    return path


def test_snapshot_deterministic_no_database_or_app_main(snapshot):
    again = capture_snapshot()
    assert snapshot == again
    assert snapshot['io'] == {'network_calls': 0, 'sqlite_connections': 0, 'app_main_imported': False}
    from benchmarks.runner import source_state
    assert production_hash() == source_state()['production_source_hash']
    validate_snapshot(snapshot, production_hash())


def test_actual_widget_and_legacy_catalog_are_combined(index):
    for name in ('memo.append', 'memo.write', 'alarm.set', 'calendar.add', 'calendar.update',
                 'timer.start', 'timer.stop', 'todo.create', 'todo.reopen', 'time.query'):
        assert index.lookup(name)['status'] == 'SUPPORTED', name
    assert not index.equivalent('calendar.add', 'todo.create')
    assert index.equivalent('todo.create', 'todo.add')
    assert index.equivalent('calendar.query', 'calendar.list')
    assert index.lookup('made_up.operation')['status'] == 'UNSUPPORTED'


@pytest.mark.parametrize('field,value', [('exposed', False), ('availability', 'unavailable')])
def test_unexposed_and_unavailable_are_not_supported(snapshot, field, value):
    s = deepcopy(snapshot)
    s['operations'] = [dict(s['operations'][0], native='test.only', **{field: value})]
    support = SupportIndex(s)
    assert support.lookup('test.only')['status'] == 'UNSUPPORTED'
    assert support.lookup('test.only')['reason'] in {'declared_but_unavailable', 'not_exposed_to_assistant'}


def test_legacy_projection_alias_uses_actual_not_gold(snapshot):
    r = row(expected_cap='gold.not_an_alias')
    r['actual'].update(native_capability='todo.create', capability='test.canonical')
    idx = SupportIndex(snapshot, [r])
    assert idx.lookup('test.canonical')['status'] == 'SUPPORTED'
    assert idx.lookup('gold.not_an_alias')['status'] == 'UNSUPPORTED'
    other = deepcopy(r); other['actual']['capability'] = 'different.name'
    with pytest.raises(ValueError, match='Conflicting'):
        SupportIndex(snapshot, [r, other])


def test_recorded_projection_hash_and_snapshot_hash_fail_closed(snapshot):
    receipt = projection_receipt({'capability_aliases': {'todo.add': 'generated.add'}})
    assert SupportIndex(snapshot, projection=receipt).lookup('generated.add')['status'] == 'SUPPORTED'
    receipt['capability_aliases']['todo.add'] = 'other.add'
    with pytest.raises(ValueError, match='receipt'):
        SupportIndex(snapshot, projection=receipt)
    bad = deepcopy(snapshot); bad['operations'][0]['native'] = 'changed.name'
    with pytest.raises(ValueError, match='snapshot'):
        validate_snapshot(bad, production_hash())


def test_unsupported_primary_does_not_hide_safety(index):
    r = row(expected_cap='not_registered.write', actual_cap='timer.start', expected_policy='CONFIRM')
    r['score']['false_execution'] = True; r['score']['failures'].append('false_execution')
    d = classify(r, None, index)
    assert d['primary_cause'] == 'UNSUPPORTED_CAPABILITY'
    assert d['original_score']['false_execution'] is True and 'false_execution' in d['secondary_flags']
    assert diagnostics_metrics([r], [d])['overall_preserved']['false_execution']['correct'] == 1


def test_route_label_alone_never_proves_model_failure(index):
    r = row(actual_cap='alarm.list', route='LLM_FALLBACK')
    d = classify(r, None, index)
    assert d['primary_cause'] == 'CAPABILITY_FAILURE' and d['evidence_level'] == 'result'


def test_pre_model_candidate_exclusion_is_routing(index):
    r = row(actual_cap='alarm.list', called=True, route='LLM_FALLBACK')
    obs = observation(r, widget_trace={'available_capabilities': ['alarm.list'],
                                     'parsed_proposal': {'widget': 'alarm', 'action': 'list', 'args': {}}})
    assert classify(r, obs, index)['primary_cause'] == 'ROUTING_FAILURE'


def test_direct_route_with_wrong_capability(index):
    r = row(actual_cap='alarm.list')
    obs = observation(r, routing={'route': 'EXISTING_RULE', 'route_reason': 'domain_not_resolved'})
    assert classify(r, obs, index)['primary_cause'] == 'ROUTING_FAILURE'


@pytest.mark.parametrize('code', ['INCOMPLETE_PROPOSAL', 'INVALID_PROPOSAL', 'UNSUPPORTED_ACTION', 'INTENT_UNRESOLVED'])
def test_transported_proposal_errors(index, code):
    r = row(actual_cap='alarm.list', called=True)
    r['actual']['validation_error'] = code
    obs = observation(r)
    assert classify(r, obs, index)['primary_cause'] == 'LLM_PROPOSAL_FAILURE'
    assert classify(r, None, index)['primary_cause'] != 'LLM_PROPOSAL_FAILURE'


def test_saved_raw_proposal_is_decoded_without_repair(index):
    r = row(actual_cap='alarm.list', called=True)
    obs = observation(r)
    obs['llm']['attempts'][0]['response_raw'] = stable({'choices': [{'message': {'content': stable(
        {'widget': 'alarm', 'action': 'list', 'args': {}, 'target': None})}}]})
    assert classify(r, obs, index)['primary_cause'] == 'LLM_PROPOSAL_FAILURE'
    obs['llm']['attempts'][0]['response_raw'] = 'not json; never execute'
    assert classify(r, obs, index)['primary_cause'] == 'CAPABILITY_FAILURE'


def test_pre_model_null_constraint_is_not_qwen_blame(index):
    r = row(expected_slots={'date': '2026-01-02'}, actual_slots={'date': None}, called=True)
    obs = observation(r, widget_trace={'proposal_schema': {'anyOf': [{'properties': {
        'widget': {'const': 'todo'}, 'action': {'const': 'list'},
        'args': {'properties': {'date': {'type': 'null'}}}}}]}})
    d = classify(r, obs, index)
    assert d['primary_cause'] == 'TEMPORAL_FAILURE'
    assert 'pre_model_temporal_constraint' in d['diagnostic_flags'] and d['evidence_level'] == 'trace'


@pytest.mark.parametrize('key,cause', [('date', 'TEMPORAL_FAILURE'), ('task_id', 'ENTITY_GROUNDING_FAILURE'),
                                      ('reference', 'CONTEXT_FAILURE'), ('text', 'SLOT_EXTRACTION_FAILURE')])
def test_slot_families_are_symptom_labels(index, key, cause):
    r = row(expected_slots={key: 'test-value'}, actual_slots={key: None})
    d = classify(r, None, index)
    assert d['primary_cause'] == cause and d['evidence_level'] == 'result'


def test_correct_model_value_missing_downstream_is_unresolved(index):
    r = row(expected_slots={'date': '2026-01-02'}, actual_slots={'date': None}, called=True)
    obs = observation(r, widget_trace={'parsed_proposal': {'widget': 'todo', 'action': 'list',
                                                        'args': {'date': '2026-01-02'}}})
    d = classify(r, obs, index)
    assert d['primary_cause'] == 'UNDETERMINED'
    assert 'correct_model_value_not_in_final_slots:date' in d['diagnostic_flags']


def test_possible_title_text_gap_is_not_silently_repaired(index):
    r = row(expected_cap='todo.add', expected_slots={'text': 'synthetic title'}, actual_slots={'title': 'synthetic title'})
    before = deepcopy(r)
    d = classify(r, None, index)
    assert d['primary_cause'] == 'UNDETERMINED'
    assert 'possible_title_text_representation_gap' in d['diagnostic_flags'] and r == before


def test_resolved_target_not_projected_is_observation_gap(index):
    r = row(expected_slots={'task_id': 'unit-id'}, actual_slots={})
    obs = observation(r, widget_trace={'resolved_target': {'id': 'unit-id'}})
    assert classify(r, obs, index)['primary_cause'] == 'UNDETERMINED'


def test_policy_after_correct_slots_and_advisory_slot_mismatch(index):
    r = row(expected_slots={'date': '2026-01-02', 'advisory': 'wanted'},
            actual_slots={'date': '2026-01-02', 'advisory': 'other'},
            critical=['date'], actual_policy='CLARIFY')
    assert 'critical_slot_error' not in r['score']['failures']
    assert classify(r, None, index)['primary_cause'] == 'POLICY_FAILURE'


def test_adapter_boundary_requires_action_evidence(index):
    r = row(); r['score']['mode_success'] = r['score']['task_success'] = False
    r['score']['status_ok'] = False; r['score']['failures'] = ['wrong_final_status']
    assert classify(r, None, index)['primary_cause'] == 'UNDETERMINED'
    obs = observation(r); obs['adapter_calls'] = [{'phase': 'action', 'entered': True}]
    assert classify(r, obs, index)['primary_cause'] == 'ADAPTER_FAILURE'


def test_explicit_response_assertions(index):
    r = row(); r['score'].update(mode_success=False, task_success=False, response_assertions={'data.value': False},
                                 failures=['response_fact_error'])
    assert classify(r, None, index)['primary_cause'] == 'RESPONSE_ASSERTION_FAILURE'


def test_not_evaluated_not_counted_as_semantic_failure(index):
    rows = [row('A'), row('B', unavailable=True), row('C', expected_slots={'date': 'x'}, actual_slots={})]
    diagnoses = [classify(r, None, index) for r in rows]
    assert diagnoses[1]['status'] == 'NOT_EVALUATED' and diagnoses[1]['primary_cause'] is None
    m = diagnostics_metrics(rows, diagnoses)
    assert sum(m['primary_failure_counts'].values()) == 1
    assert m['supported']['mode_success']['total'] == 2 and m['support']['supported'] == 3


def test_supported_success_subset_and_original_flags(index):
    rows = [row('A'), row('B', expected_cap='not_declared.test', actual_policy='CLARIFY'),
            row('C', expected_slots={'date': 'x'}, actual_slots={})]
    before = deepcopy(rows)
    diagnoses = [classify(r, None, index) for r in rows]
    m = diagnostics_metrics(rows, diagnoses)
    assert m['overall_preserved']['mode_success'] == {'correct': 1, 'total': 3, 'rate': 1/3}
    assert m['supported']['mode_success'] == {'correct': 1, 'total': 2, 'rate': .5}
    assert sum(m['primary_failure_counts'].values()) == 2
    assert rows == before


def test_group_cannot_drop_an_unsupported_member(index):
    rows = [row('A'), row('B', expected_cap='not_declared.test', actual_policy='CLARIFY')]
    rows[1].update(parent_id='A', source_type='paraphrase')
    m = diagnostics_metrics(rows, [classify(r, None, index) for r in rows])
    assert m['supported_group_success']['total'] == 0
    assert m['excluded_groups_with_unsupported_or_unevaluated_members'] == 1


def test_clarification_precision_distinct_from_recall(index):
    rows = [row('A', expected_policy='CLARIFY'), row('B', actual_policy='CLARIFY'), row('C')]
    m = diagnostics_metrics(rows, [classify(r, None, index) for r in rows])
    assert m['supported']['clarification_ok']['rate'] == 1
    assert m['supported_clarification_precision']['rate'] == .5
    assert m['supported_unnecessary_clarification_rate']['rate'] == .5


def test_legacy_analysis_is_offline_immutable_and_verifiable(tmp_path, monkeypatch):
    old = frozen(tmp_path)
    before = inventory(old)
    def deny(*args, **kwargs): raise AssertionError('No DB/network during offline analysis')
    monkeypatch.setattr(sqlite3, 'connect', deny)
    monkeypatch.setattr(socket, 'create_connection', deny)
    monkeypatch.setenv('ROOM_HUB_BENCHMARK_DATA', str(tmp_path/'missing-dataset'))
    out, state, metrics = analyze_run(tmp_path, 'old-run', 'diagnostic')
    assert before == inventory(old)
    assert state['legacy_metrics_preserved'] and state['model_calls_during_analysis'] == 0
    assert metrics['overall_preserved']['mode_success']['correct'] == 1
    assert out == tmp_path/'analysis'/'diagnostic'
    assert verify_analysis(tmp_path, 'diagnostic')['valid']
    for name in ('summary.md', 'summary.html', 'metrics.json', 'analysis.json', 'taxonomy.jsonl',
                 'input_integrity.json', 'support_snapshot.json', 'SUPPORTED_FAILURES.md',
                 'UNSUPPORTED_CAPABILITIES.md', 'UNDETERMINED.md', 'analysis_integrity.json'):
        assert (out/name).is_file()


def test_new_run_snapshot_and_old_analyze_compatibility(tmp_path, snapshot):
    frozen(tmp_path, snapshot=snapshot)
    out, state, _ = analyze_run(tmp_path, 'old-run', 'analysis-new')
    assert state['support_snapshot_hash'] == snapshot['snapshot_hash']
    assert verify_analysis(tmp_path, 'analysis-new')['original_files_unchanged']


def test_missing_trace_is_visible_not_fake_inference(tmp_path):
    frozen(tmp_path, traces={})
    out, state, m = analyze_run(tmp_path, 'old-run', 'missing-trace')
    assert m['trace_coverage']['correct'] == 0
    assert 'trace' in (out/'summary.md').read_text(encoding='utf-8')
    assert verify_analysis(tmp_path, 'missing-trace')['valid']


@pytest.mark.parametrize('kind', ['metrics', 'progress', 'duplicate-id', 'bool-string', 'nonfinite', 'interrupted', 'source'])
def test_corrupt_input_fails_before_output(tmp_path, kind):
    old = frozen(tmp_path)
    if kind == 'metrics':
        p=old/'metrics.json'; m=json.loads(p.read_text());m['evaluated']=0;write_json(p,m)
    elif kind == 'progress':
        p=old/'progress.json'; m=json.loads(p.read_text());m['completed']=1;write_json(p,m)
    elif kind in {'duplicate-id', 'bool-string', 'nonfinite'}:
        p=old/'raw_results.jsonl'; rows=[json.loads(x) for x in p.read_text().splitlines()]
        if kind=='duplicate-id':rows[1]['case_id']=rows[0]['case_id']
        if kind=='bool-string':rows[0]['score']['eligible']='true'
        if kind=='nonfinite':rows[0]['actual']['latency']['total_ms']=float('nan')
        p.write_text(''.join(json.dumps(x)+'\n' for x in rows))
    else:
        p=old/'config.json';m=json.loads(p.read_text())
        m['run_status' if kind=='interrupted' else 'production_source_hash']='INTERRUPTED' if kind=='interrupted' else '0'*64
        write_json(p,m)
    with pytest.raises(ValueError):analyze_run(tmp_path,'old-run','rejected')
    assert not (tmp_path/'analysis'/'rejected').exists()


@pytest.mark.parametrize('name', ['../escape.json', '/tmp/file.json', 'trace/../../outside.json', 'config.json', 'trace\\file.json'])
def test_trace_escape_is_rejected(tmp_path,name):
    r=row();r['trace_file']=name
    old=frozen(tmp_path,[r],traces={})
    with pytest.raises(ValueError):load_run(old)


def test_symlink_and_hardlink_inputs_rejected(tmp_path):
    old=frozen(tmp_path);p=old/'trace/link.json';target=tmp_path/'outside.json';target.write_text('{}')
    try:p.symlink_to(target)
    except OSError:pytest.skip('Symlink privilege unavailable')
    with pytest.raises(ValueError):load_run(old)
    p.unlink();os.link(target,p)
    with pytest.raises(ValueError,match='hardlinked'):load_run(old)


def test_hardlink_without_symlink_privilege(tmp_path):
    old=frozen(tmp_path);target=tmp_path/'outside.json';target.write_text('{}')
    try:os.link(target,old/'trace/hard.json')
    except OSError:pytest.skip('Host does not support hardlinks')
    with pytest.raises(ValueError,match='hardlinked'):load_run(old)


def test_existing_analysis_and_lock_are_not_overwritten(tmp_path):
    old=frozen(tmp_path);out,_,_=analyze_run(tmp_path,'old-run','kept')
    before=inventory(out)
    with pytest.raises(FileExistsError):analyze_run(tmp_path,'old-run','kept')
    assert inventory(out)==before
    lock=out.parent/'.locked.lock';lock.write_text('another writer')
    with pytest.raises(ValueError,match='locked'):analyze_run(tmp_path,'old-run','locked')
    assert lock.read_text()=='another writer'


def test_input_change_during_analysis_is_not_published(tmp_path,monkeypatch):
    import benchmarks.analysis_reports as reports
    old=frozen(tmp_path);real=reports.write_analysis_reports
    def changed(*args):
        real(*args);(old/'summary.md').write_text('concurrent writer')
    monkeypatch.setattr(reports,'write_analysis_reports',changed)
    with pytest.raises(ValueError,match='changed during'):analyze_run(tmp_path,'old-run','not-published')
    assert not (tmp_path/'analysis/not-published').exists()
    assert not list((tmp_path/'analysis').glob('.analysis-stage-*'))


def test_source_change_during_analysis_rejected(tmp_path,monkeypatch):
    import benchmarks.analysis as analysis
    import benchmarks.analysis_reports as reports
    frozen(tmp_path);real=reports.write_analysis_reports
    def changed(*args):
        real(*args);monkeypatch.setattr(analysis,'production_hash',lambda:'0'*64)
    monkeypatch.setattr(reports,'write_analysis_reports',changed)
    with pytest.raises(ValueError,match='Production source changed'):analyze_run(tmp_path,'old-run','not-published')
    assert not (tmp_path/'analysis/not-published').exists()


def test_trace_catalog_and_llm_call_mismatch_refused(tmp_path):
    r=row(called=True)
    obs=observation(r);obs['runtime_catalog']={'widgets':[]}
    old=frozen(tmp_path,[r],traces={r['case_id']:obs})
    with pytest.raises(ValueError,match='catalog differs'):analyze_run(tmp_path,'old-run','bad')
    obs.pop('runtime_catalog');obs['llm']['attempts']=[];write_json(old/r['trace_file'],obs)
    with pytest.raises(ValueError,match='LLM counts'):load_run(old)


def test_verify_detects_modified_source_and_modified_analysis(tmp_path):
    old=frozen(tmp_path);out,_,_=analyze_run(tmp_path,'old-run','kept')
    original=(old/'summary.md').read_bytes();(old/'summary.md').write_text('changed')
    with pytest.raises(ValueError,match='Original run differs'):verify_analysis(tmp_path,'kept')
    (old/'summary.md').write_bytes(original)
    (out/'summary.html').write_text('tampered')
    with pytest.raises(ValueError,match='Derived analysis files changed'):verify_analysis(tmp_path,'kept')


def test_html_csv_escape_and_private_output_stays_local(tmp_path):
    r=row();r['input_text']='  =HYPERLINK("example")<script>alert(1)</script>'
    old=frozen(tmp_path,[r]);out,_,_=analyze_run(tmp_path,'old-run','escaped')
    assert '<script>' not in (out/'summary.html').read_text(encoding='utf-8')
    import csv
    cells=list(csv.DictReader((out/'cases.csv').open(encoding='utf-8-sig')))
    assert cells[0]['input_text'].startswith("'")
    assert r['input_text'] in (out/'scored_results.jsonl').read_text(encoding='utf-8').replace('\\"','"')


def test_nlu_and_decision_do_not_claim_full_task_success(tmp_path):
    for mode in ('nlu','decision'):
        frozen(tmp_path,[row(mode=mode)],name=mode,mode=mode)
        out,_,metrics=analyze_run(tmp_path,mode,mode+'-analysis')
        assert metrics['supported']['task_success']['total']==0
        assert metrics['supported']['mode_success']['total']==1


def test_compare_analysis_fixed_cohort_and_no_input_changes(tmp_path):
    frozen(tmp_path)
    a,_,_=analyze_run(tmp_path,'old-run','left')
    b,_,_=analyze_run(tmp_path,'old-run','right')
    ah,bh=inventory(a),inventory(b)
    out,result=compare_analyses(tmp_path,'left','right','same-result')
    assert result['common_case_count']==2 and result['changes']['mode_success']==0
    assert inventory(a)==ah and inventory(b)==bh
    assert (out/'comparison.html').is_file()


def test_compare_analysis_different_dataset_refused(tmp_path):
    frozen(tmp_path,name='first');second=frozen(tmp_path,name='second')
    p=second/'config.json';cfg=json.loads(p.read_text());cfg['dataset_hash']='c'*64;write_json(p,cfg)
    analyze_run(tmp_path,'first','left');analyze_run(tmp_path,'second','right')
    with pytest.raises(ValueError,match='dataset_hash'):compare_analyses(tmp_path,'left','right','bad')


def test_cli_analyze_and_verify_without_dataset_or_model(tmp_path):
    frozen(tmp_path)
    env={**os.environ,'ROOM_HUB_BENCHMARK_DATA':str(tmp_path/'absent'),'PYTHONDONTWRITEBYTECODE':'1'}
    base=[sys.executable,'-m','benchmarks','--results-root',str(tmp_path)]
    proc=subprocess.run(base+['analyze','old-run','--name','cli-analysis'],cwd=ROOT,env=env,capture_output=True,text=True)
    assert proc.returncode==0,proc.stderr
    assert json.loads(proc.stdout)['model_calls_during_analysis']==0
    proc=subprocess.run(base+['verify-analysis','cli-analysis'],cwd=ROOT,env=env,capture_output=True,text=True)
    assert proc.returncode==0 and json.loads(proc.stdout)['valid']
    proc=subprocess.run(base+['analyze','old-run','--name','cli-analysis'],cwd=ROOT,env=env,capture_output=True,text=True)
    assert proc.returncode==2 and 'already exists' in proc.stderr


def test_offline_commands_dont_import_runtime_or_model():
    proc=subprocess.run([sys.executable,'-c',
        "import sys; import benchmarks.analysis; assert 'benchmarks.runtime' not in sys.modules; assert 'app.main' not in sys.modules; assert 'app.llm' not in sys.modules"],
        cwd=ROOT,capture_output=True,text=True)
    assert proc.returncode==0,proc.stderr


@pytest.mark.skipif(os.name=='nt',reason='Termux wrapper is Bash; Windows CLI covered separately')
def test_outer_termux_offline_commands_skip_dataset_mount(tmp_path):
    repo=tmp_path/'source with spaces';d=repo/'deploy/termux';d.mkdir(parents=True)
    shutil.copyfile(ROOT/'deploy/termux/benchmark.sh',d/'benchmark.sh')
    (repo/'.venv-v35').mkdir();(repo/'.venv-v35/pyvenv.cfg').touch()
    prefix=tmp_path/'prefix';(prefix/'bin').mkdir(parents=True)
    mock=prefix/'bin/proot-distro';mock.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n');mock.chmod(0o700)
    env={**os.environ,'PREFIX':str(prefix),'HOME':str(tmp_path/'empty-home')}
    env.pop('ROOM_HUB_BENCHMARK_DATA',None)
    for command in ('analyze','verify-analysis','compare-analysis'):
        proc=subprocess.run(['bash',str(d/'benchmark.sh'),command,'old-run'],env=env,capture_output=True,text=True)
        assert proc.returncode==0,proc.stderr
        assert '/opt/benchmark-data' not in proc.stdout and command in proc.stdout


def test_existing_scoring_code_is_not_a_diagnostics_dependency():
    # Structural guard: diagnostics aggregate saved scores but never re-run project/score.
    import ast
    for name in ('analysis.py','taxonomy.py','analysis_compare.py'):
        tree=ast.parse((ROOT/'benchmarks'/name).read_text(encoding='utf-8'))
        calls=[n.func.id for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)]
        assert 'score' not in calls and 'project' not in calls and 'Runtime' not in calls


def test_declared_missing_snapshot_is_not_silently_reconstructed(tmp_path,snapshot):
    old=frozen(tmp_path,snapshot=snapshot);(old/'support_snapshot.json').unlink()
    with pytest.raises(ValueError,match='snapshot is missing'):analyze_run(tmp_path,'old-run','bad')


def test_invalid_trace_nested_types_fail_cleanly(tmp_path):
    r=row();obs=observation(r);obs['record']='not an object'
    old=frozen(tmp_path,[r],traces={r['case_id']:obs})
    with pytest.raises(ValueError,match='Invalid trace'):load_run(old)


def test_supported_filter_does_not_turn_unavailable_inference_into_zero_score(tmp_path):
    frozen(tmp_path,[row('A'),row('B',unavailable=True)])
    _,_,m=analyze_run(tmp_path,'old-run','partial-evaluation')
    assert m['supported']['evaluated']==1
    assert m['supported']['mode_success']['rate']==1.0
    assert m['outcomes']['NOT_EVALUATED']==1


def test_full_run_metadata_snapshot_and_offline_roundtrip(tmp_path):
    from benchmark_cases import make_suite
    from benchmarks.dataset import load_suite
    from benchmarks.runner import run_suite
    source=make_suite(tmp_path/'external')
    suite=load_suite('synthetic',source.parent)
    old,metrics=run_suite(suite,suite.cases,name='new-run',results_root=tmp_path/'results')
    old_hashes=inventory(old)
    config=json.loads((old/'config.json').read_text())
    assert config['support_snapshot_hash'] and config['support_projection']['receipt_hash']
    # Delete only this generated dataset: reanalysis does not need its path.
    shutil.rmtree(source.parent)
    _,_,derived=analyze_run(tmp_path/'results','new-run','after')
    assert derived['overall_preserved']['task_success']==metrics['task_success']
    assert inventory(old)==old_hashes


def test_comparison_freezes_supported_membership_instead_of_changing_denominator(tmp_path,snapshot):
    # Two *synthetic* analysis outputs use different recorded representation maps.
    r=row('A',expected_cap='generated.action');r['actual']['native_capability']='todo.list'
    second=row('B')
    frozen(tmp_path,[r,second],name='before')
    later=frozen(tmp_path,[r,second],name='after',snapshot=snapshot)
    # Explicit recorded projection in the newer run deliberately has no alias.
    # The earlier M1-style run recovers generated.action from actual/native only.
    analyze_run(tmp_path,'before','left');analyze_run(tmp_path,'after','right')
    _,result=compare_analyses(tmp_path,'left','right','fixed')
    assert result['common_case_ids']==['B']
    assert result['supported_removed_ids']==['A']
    assert result['changes']['mode_success']==0
    assert result['alias_provenance_equal'] is False


def test_output_security_limits_fail_closed(tmp_path,monkeypatch):
    import benchmarks.analysis as analysis
    old=frozen(tmp_path)
    monkeypatch.setattr(analysis,'MAX_FILE_BYTES',10)
    with pytest.raises(ValueError,match='safety limits'):load_run(old)


def test_all_primary_causes_in_declared_taxonomy(index):
    inputs=[row(),row('B',actual_cap='alarm.list'),row('C',expected_cap='not.registered',actual_policy='CLARIFY'),
            row('D',expected_slots={'date':'x'},actual_slots={}),row('E',unavailable=True)]
    for r in inputs:
        d=classify(r,None,index)
        assert d['primary_cause'] is None or d['primary_cause'] in PRIMARY_ORDER
