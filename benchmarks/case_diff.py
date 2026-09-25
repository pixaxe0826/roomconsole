"""Paired, offline transitions of SAVED scores. Never rescore or infer causes.

The supported/evaluated intersection is a performance cohort, not a safety filter.
All selected rows are also inspected so support or eligibility changes cannot hide
lost successes, missing evidence, or observed false executions.
"""
from __future__ import annotations

from collections import Counter
import csv

from .dataset import stable
from .reports import html_report, safe_md, write_json
from .scoring import aggregate

ACCURACY = ('mode_success', 'task_success', 'capability_ok', 'policy_ok',
            'status_ok', 'execution_ok', 'clarification_ok')


def _indexed(rows, ids, label):
    result = {}
    for row in rows:
        identity = row.get('case_id')
        if identity in result or identity not in ids:
            raise ValueError(f'{label}: duplicate or unexpected case ID')
        result[identity] = row
    if set(result) != set(ids):
        raise ValueError(f'{label}: missing case rows')
    return result


def _outcome(row):
    if not row['score']['eligible']:
        return 'NOT_EVALUATED'
    value = row['score']['mode_success']
    if type(value) is not bool:
        raise ValueError('Evaluated case requires a saved bool success')
    return 'PASS' if value else 'FAIL'


def _value(mapping, key):
    # Missing and explicitly null are different observations, not interchangeable.
    return {'present': key in mapping, 'value': mapping.get(key)}


def compare_cases(arows, brows, adiagnostics, bdiagnostics, ids, common):
    """Return per-case evidence and summaries; all numbers use frozen booleans."""
    aa, bb = _indexed(arows, ids, 'before'), _indexed(brows, ids, 'after')
    ad = _indexed(adiagnostics, ids, 'before diagnoses')
    bd = _indexed(bdiagnostics, ids, 'after diagnoses')
    result = []
    for identity in sorted(ids):
        a, b = aa[identity], bb[identity]
        # Equal dataset hashes alone are not permission to compare different gold.
        for key in ('input_text', 'expected', 'expected_response', 'parent_id',
                    'source_type', 'domain', 'category', 'tags', 'split'):
            if stable(a.get(key)) != stable(b.get(key)):
                raise ValueError(f'Paired case {identity}: frozen {key} differs')
        ca, cb = a.get('scoring_context'), b.get('scoring_context')
        if ca is not None and cb is not None and stable(ca) != stable(cb):
            raise ValueError(f'Paired case {identity}: critical-slot definition differs')
        regressions, improvements, unassessable = [], [], []
        for key in ACCURACY:
            x, y = a['score'].get(key), b['score'].get(key)
            if x is True and y is False:
                regressions.append(key)
            elif x is False and y is True:
                improvements.append(key)
            elif x is not None and y is None:
                unassessable.append(key)
        if b['score'].get('false_execution') is True and a['score'].get('false_execution') is not True:
            regressions.append('false_execution')
        if a['score'].get('false_execution') is True and b['score'].get('false_execution') is False:
            improvements.append('false_execution')
        sm_a, sm_b = a['score']['slot_matches'], b['score']['slot_matches']
        for key in sorted(set(sm_a) | set(sm_b)):
            if sm_a.get(key) is True and sm_b.get(key) is False:
                regressions.append('slot:' + key)
            elif sm_a.get(key) is False and sm_b.get(key) is True:
                improvements.append('slot:' + key)
            elif key in sm_a and key not in sm_b:
                unassessable.append('slot:' + key)
        actual_a, actual_b = a['actual'], b['actual']
        slots_a, slots_b = actual_a['slots'], actual_b['slots']
        fields = ('capability', 'native_capability', 'policy', 'status', 'adapter_executed', 'llm_calls')
        changed = {key: {'before': _value(actual_a, key), 'after': _value(actual_b, key)}
                   for key in fields if stable(_value(actual_a, key)) != stable(_value(actual_b, key))}
        slot_changes = {key: {'before': _value(slots_a, key), 'after': _value(slots_b, key)}
                        for key in sorted(set(slots_a) | set(slots_b))
                        if stable(_value(slots_a, key)) != stable(_value(slots_b, key))}
        def side(r, diag):
            act, sc = r['actual'], r['score']
            return {'outcome': _outcome(r), 'eligible': sc['eligible'],
                    'support': diag['support']['status'], 'route': act.get('route'),
                    'capability': act.get('capability'), 'native_capability': act.get('native_capability'),
                    'policy': act.get('policy'), 'status': act.get('status'),
                    'slots': act['slots'], 'llm_calls': act['llm_calls'],
                    'adapter_executed': act.get('adapter_executed'),
                    'expected_adapter_execution': r['expected'].get('adapter_should_execute'),
                    'latency_ms': act['latency'].get('total_ms'),
                    'saved_score': sc, 'primary_cause': diag.get('primary_cause'),
                    'trace_file': r.get('trace_file')}
        result.append({'case_id': identity, 'input_text': a['input_text'], 'expected': a['expected'],
                       'in_common_supported_cohort': identity in common,
                       'transition': _outcome(a) + '_TO_' + _outcome(b),
                       'regression_dimensions': regressions, 'improvement_dimensions': improvements,
                       'unassessable_dimensions': unassessable,
                       'scoring_context_complete': ca is not None and cb is not None,
                       'changed_fields': changed, 'changed_slots': slot_changes,
                       'before': side(a, ad[identity]), 'after': side(b, bd[identity])})
    def summary(rows):
        counts = Counter(r['transition'] for r in rows)
        eligible = [r for r in rows if r['before']['eligible'] and r['after']['eligible']]
        gained = [r['case_id'] for r in eligible if r['transition'] == 'FAIL_TO_PASS']
        lost = [r['case_id'] for r in eligible if r['transition'] == 'PASS_TO_FAIL']
        net = sum(r['after']['outcome'] == 'PASS' for r in eligible) - sum(r['before']['outcome'] == 'PASS' for r in eligible)
        assert net == len(gained) - len(lost)
        return {'count': len(rows), 'outcome_transitions': dict(sorted(counts.items())),
                'gained_success_ids': gained, 'lost_success_ids': lost, 'paired_eligible_success_net': net,
                'regression_ids': [r['case_id'] for r in rows if r['regression_dimensions']],
                'unassessable_ids': [r['case_id'] for r in rows if r['unassessable_dimensions']],
                'clarification_regression_ids': [r['case_id'] for r in rows if 'clarification_ok' in r['regression_dimensions']],
                'execution_regression_ids': [r['case_id'] for r in rows if 'execution_ok' in r['regression_dimensions']],
                'false_execution_ids_after': [r['case_id'] for r in rows if r['after']['saved_score'].get('false_execution') is True]}
    routes = []
    for x, y in sorted({(str(r['before']['route']), str(r['after']['route'])) for r in result}):
        group = [r for r in result if (str(r['before']['route']), str(r['after']['route'])) == (x, y)]
        routes.append({'before_route': x, 'after_route': y, 'all_selected': summary(group),
                       'common_supported': summary([r for r in group if r['in_common_supported_cohort']])})
    parser_ids = {r['case_id'] for r in result if r['after']['route'] == 'SEMANTIC_PARSER'}
    parser_rows = [bb[i] for i in sorted(parser_ids)]
    parser_common = [bb[i] for i in sorted(parser_ids & set(common))]
    full = summary(result)
    return result, {'case_diff_schema_version': '1.0', 'all_selected': full,
                    'common_supported': summary([r for r in result if r['in_common_supported_cohort']]),
                    'route_transitions': routes,
                    'parser_after': {'all_selected': aggregate(parser_rows),
                                     'common_supported': aggregate(parser_common),
                                     'policy_counts': dict(sorted(Counter(r['actual'].get('policy') for r in parser_rows).items()))},
                    'regression_gate_failed': bool(full['regression_ids'] or full['unassessable_ids'] or full['false_execution_ids_after']),
                    'notes': ['Transitions compare saved scores; they are not causal diagnoses or rescoring.',
                              'Safety flags and lost evidence use all selected IDs, including unsupported cases.',
                              'Faster clarification/refusal is not successful execution. Parser latency is grouped by outcome.',
                              'Missing critical-slot metadata is reported, not filled from gold.']}


def _cell(value):
    value = str(value)
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')) else value


def write_case_reports(stage, rows, summary, before, after):
    write_json(stage / 'case_diff_summary.json', summary)
    (stage / 'case_transitions.jsonl').write_text(''.join(stable(r)+'\n' for r in rows), encoding='utf-8')
    def report(title, subset):
        lines = [f'# {title}', '', f'{safe_md(before)} → {safe_md(after)}',
                 '저장된 점수의 문항별 비교입니다. 모델 재호출·재채점·원본 변경 없음.',
                 'PASS/FAIL은 현재 모드 기준입니다. 지원 교집합 밖의 관측된 안전성 문제도 숨기지 않습니다.',
                 '실행 경계 불일치는 잘못된 쓰기 실행과 다릅니다. trace를 확인한 뒤 원인을 판정하세요.', '']
        for r in subset:
            a, b = r['before'], r['after']
            lines += [f"## {safe_md(r['case_id'])} — {r['transition']}", '',
                      '입력: ' + safe_md(r['input_text']),
                      f"지원 교집합: {r['in_common_supported_cohort']}; 경로: {safe_md(a['route'])} → {safe_md(b['route'])}",
                      '회귀: ' + ', '.join(r['regression_dimensions']),
                      '개선: ' + ', '.join(r['improvement_dimensions']),
                      '근거 누락/미평가: ' + ', '.join(r['unassessable_dimensions']),
                      '기대: ' + safe_md(stable(r['expected'])),
                      '변경 값: ' + safe_md(stable(r['changed_fields'])),
                      '변경 슬롯: ' + safe_md(stable(r['changed_slots'])),
                      f"실행 기대/이전/이후: {a['expected_adapter_execution']} / {a['adapter_executed']} / {b['adapter_executed']}",
                      f"원본 trace: {safe_md(a['trace_file'])} / {safe_md(b['trace_file'])}", '']
        if not subset:
            lines.append('해당 문항 없음.')
        return '\n'.join(lines) + '\n'
    all_text = report('문항별 전환', rows)
    for path, title, subset in [
        ('CASE_TRANSITIONS.md', '문항별 전환', rows),
        ('REGRESSIONS.md', '회귀 및 관측 누락', [r for r in rows if r['regression_dimensions'] or r['unassessable_dimensions']]),
        ('IMPROVEMENTS.md', '개선 문항', [r for r in rows if r['improvement_dimensions']]),
    ]:
        (stage / path).write_text(report(title, subset), encoding='utf-8')
    (stage / 'case_transitions.html').write_text(html_report(all_text, '문항별 전환'), encoding='utf-8')
    with (stage / 'ROUTE_TRANSITIONS.csv').open('w', encoding='utf-8-sig', newline='') as out:
        writer = csv.writer(out)
        writer.writerow(['case_id', 'common_supported', 'before_route', 'after_route', 'transition',
                         'regressions', 'improvements', 'unassessable', 'before_ms', 'after_ms'])
        for r in rows:
            writer.writerow([_cell(v) for v in [r['case_id'], r['in_common_supported_cohort'], r['before']['route'],
                r['after']['route'], r['transition'], ','.join(r['regression_dimensions']),
                ','.join(r['improvement_dimensions']), ','.join(r['unassessable_dimensions']),
                r['before']['latency_ms'], r['after']['latency_ms']]])
    parser = [r for r in rows if r['after']['route'] == 'SEMANTIC_PARSER']
    text = report('Semantic Parser 처리 문항', parser)
    text += '\n## 모드 성공/실패/미평가별 지연\n\n'
    from .scoring import latency
    for state in ('PASS', 'FAIL', 'NOT_EVALUATED'):
        for scope in ('all_selected', 'common_supported'):
            group = [r for r in parser if r['after']['outcome'] == state and
                     (scope == 'all_selected' or r['in_common_supported_cohort'])]
            text += f'{scope} / {state}: ' + stable(latency([r['after']['latency_ms'] for r in group])) + '\n'
    (stage / 'PARSER_OUTCOMES.md').write_text(text, encoding='utf-8')
