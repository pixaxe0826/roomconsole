"""Offline saved-score transitions, synthetic fixtures only."""
from copy import deepcopy
import json

import pytest

from benchmarks.analysis import analyze_run, inventory
from benchmarks.analysis_compare import compare_analyses
from benchmarks.case_diff import compare_cases
from benchmarks.__main__ import main
from test_benchmark_taxonomy import frozen, row


def diagnosis(rows, unsupported=()):
    return [{'case_id': r['case_id'], 'support': {'status': 'UNSUPPORTED' if r['case_id'] in unsupported else 'SUPPORTED'},
             'primary_cause': None} for r in rows]


def compare(a, b, *, unsupported=()):
    ids = [r['case_id'] for r in a]
    return compare_cases(a, b, diagnosis(a, unsupported), diagnosis(b, unsupported), ids, set(ids)-set(unsupported))


def test_net_success_is_not_regression_count():
    a = [row('LOST'), row('GAIN1', actual_cap='todo.get'), row('GAIN2', actual_cap='todo.get')]
    b = [row('LOST', actual_cap='todo.get'), row('GAIN1'), row('GAIN2')]
    cases, s = compare(a, b)
    c = s['common_supported']
    assert c['paired_eligible_success_net'] == 1
    assert c['gained_success_ids'] == ['GAIN1', 'GAIN2']
    assert c['lost_success_ids'] == ['LOST']
    assert c['outcome_transitions'] == {'FAIL_TO_PASS': 2, 'PASS_TO_FAIL': 1}
    assert s['regression_gate_failed']


def test_execution_and_clarification_transition_ids_not_guessed():
    a = [row('READ'), row('QUESTION', expected_policy='CLARIFY')]
    b = [row('READ', actual_policy='CLARIFY'), row('QUESTION', expected_policy='CLARIFY', actual_policy='CONFIRM')]
    cases, s = compare(a, b)
    assert s['all_selected']['execution_regression_ids'] == ['READ']
    assert s['all_selected']['clarification_regression_ids'] == ['QUESTION']
    changed_read = next(c for c in cases if c['case_id'] == 'READ')
    assert changed_read['after']['expected_adapter_execution'] is True
    assert changed_read['after']['adapter_executed'] is False


def test_missing_eligibility_not_counted_as_failure_or_hidden():
    a = [row('UNAVAILABLE')]
    b = [row('UNAVAILABLE', unavailable=True)]
    cases, s = compare(a, b)
    assert cases[0]['transition'] == 'PASS_TO_NOT_EVALUATED'
    assert s['all_selected']['lost_success_ids'] == []
    assert s['all_selected']['unassessable_ids'] == ['UNAVAILABLE']
    assert s['regression_gate_failed']


def test_safety_not_hidden_by_unsupported_classification():
    a, b = [row('UNSUPPORTED')], [row('UNSUPPORTED')]
    b[0]['score']['false_execution'] = True
    cases, s = compare(a, b, unsupported=['UNSUPPORTED'])
    assert s['common_supported']['count'] == 0
    assert s['all_selected']['false_execution_ids_after'] == ['UNSUPPORTED']
    assert s['regression_gate_failed']


def test_model_to_parser_route_is_paired_and_parser_outcomes_separate():
    a = [row('GOOD', called=True, route='LLM_FALLBACK'), row('QUESTION', called=True, route='LLM_FALLBACK')]
    b = [row('GOOD', route='SEMANTIC_PARSER'), row('QUESTION', route='SEMANTIC_PARSER', actual_policy='CLARIFY')]
    cases, s = compare(a, b)
    route = s['route_transitions'][0]
    assert (route['before_route'], route['after_route']) == ('LLM_FALLBACK', 'SEMANTIC_PARSER')
    assert route['common_supported']['count'] == 2
    assert route['common_supported']['lost_success_ids'] == ['QUESTION']
    assert s['parser_after']['common_supported']['task_success']['correct'] == 1
    assert s['parser_after']['policy_counts'] == {'CLARIFY': 1, 'EXECUTE': 1}


def test_missing_and_null_fields_are_distinct():
    a = [row('NULL', expected_slots={'title': None})]
    b = deepcopy(a); b[0]['actual']['slots'].pop('title')
    cases, _ = compare(a, b)
    assert cases[0]['changed_slots']['title'] == {'before': {'present': True, 'value': None},
                                               'after': {'present': False, 'value': None}}


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'gold', 'input', 'critical'])
def test_bad_pair_is_rejected(mutation):
    a, b = [row('CASE')], [row('CASE')]
    if mutation == 'missing':
        b = []
    elif mutation == 'duplicate':
        b.append(deepcopy(b[0]))
    elif mutation == 'gold':
        b[0]['expected']['slots']['date'] = '2029-01-01'
    elif mutation == 'input':
        b[0]['input_text'] = 'Different input'
    else:
        a[0]['scoring_context'] = {'critical_slots': []}
        b[0]['scoring_context'] = {'critical_slots': ['date']}
    with pytest.raises(ValueError):
        compare(a, b)


def test_case_order_does_not_forge_transitions():
    a = [row('A'), row('B')]
    b = list(reversed(deepcopy(a)))
    cases, s = compare(a, b)
    assert [r['case_id'] for r in cases] == ['A', 'B']
    assert not s['regression_gate_failed']


def test_old_missing_scoring_metadata_is_not_invented():
    a, b = [row('CASE')], [row('CASE', critical=[])]
    cases, _ = compare(a, b)
    assert not cases[0]['scoring_context_complete']


def test_real_analysis_integration_no_model_no_source_mutation(tmp_path):
    a, b = [row('LOST'), row('GAIN', actual_cap='todo.get')], [row('LOST', actual_cap='todo.get'), row('GAIN')]
    frozen(tmp_path, a, 'before'); frozen(tmp_path, b, 'after')
    analyze_run(tmp_path, 'before', 'a'); analyze_run(tmp_path, 'after', 'b')
    original = {n: inventory(tmp_path/n) for n in ('before', 'after', 'analysis/a', 'analysis/b')}
    folder, s = compare_analyses(tmp_path, 'a', 'b', 'compare')
    for n in original:
        assert inventory(tmp_path/n) == original[n]
    for f in ('CASE_TRANSITIONS.md', 'REGRESSIONS.md', 'IMPROVEMENTS.md', 'ROUTE_TRANSITIONS.csv',
              'PARSER_OUTCOMES.md', 'case_diff_summary.json', 'case_transitions.jsonl', 'case_transitions.html'):
        assert (folder/f).is_file()
    assert s['case_diff_summary']['all_selected']['lost_success_ids'] == ['LOST']
    assert s['before_metrics']['task_success'] == s['after_metrics']['task_success']
    assert main(['--results-root', str(tmp_path), 'compare-analysis', 'a', 'b', '--name', 'gate', '--fail-on-regression']) == 3
    assert (tmp_path/'analysis/gate/comparison.json').is_file()
    assert main(['--results-root', str(tmp_path), 'compare-analysis', 'a', 'b', '--name', 'gate']) == 2


def test_read_only_html_and_csv_safety(tmp_path):
    from benchmarks.case_diff import write_case_reports
    a = [row('=DANGEROUS')]; a[0]['input_text'] = '<script>alert(1)</script>'
    b = deepcopy(a)
    cases, summary = compare(a, b)
    write_case_reports(tmp_path, cases, summary, 'a', 'b')
    html = (tmp_path/'case_transitions.html').read_text()
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert "'=DANGEROUS" in (tmp_path/'ROUTE_TRANSITIONS.csv').read_text(encoding='utf-8-sig')


def test_empty_support_intersection_does_not_hide_whole_run(tmp_path):
    a, b = [row('X', expected_cap='missing.operation')], [row('X', expected_cap='missing.operation', actual_cap='todo.list')]
    frozen(tmp_path, a, 'before'); frozen(tmp_path, b, 'after')
    analyze_run(tmp_path, 'before', 'a'); analyze_run(tmp_path, 'after', 'b')
    _, result = compare_analyses(tmp_path, 'a', 'b', 'delta')
    assert result['common_case_count'] == 0
    assert result['case_diff_summary']['all_selected']['lost_success_ids'] == ['X']
    assert result['case_diff_summary']['regression_gate_failed']


def test_installed_parser_metadata_is_not_per_case_route():
    from benchmarks.runner import source_state
    from app.semantic_types import SEMANTIC_VERSION
    metadata = source_state()
    assert metadata['parser_enabled'] is True
    assert metadata['parser_version'] == SEMANTIC_VERSION == '1.2.0'
    assert len(metadata['layer_versions']['semantic_parser']) == 64
    assert 'per-case' in metadata['parser_metadata_scope']


def test_parser_policy_summary_handles_unobserved_nlu_policy():
    a = [row('KNOWN'), row('EARLY_BOUNDARY')]
    b = deepcopy(a)
    for value in b:
        value['actual']['route'] = 'SEMANTIC_PARSER'
    b[1]['actual']['policy'] = None  # NLU boundary has no policy observation.
    _, result = compare(a, b)
    assert result['parser_after']['policy_counts'] == {'EXECUTE': 1, 'UNOBSERVED': 1}