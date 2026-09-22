"""Compare M2 analyses on a fixed common supported cohort, without new inference."""
from __future__ import annotations

from .analysis import _json, _write_bundle, load_analysis, inventory
from .paths import NAME, result_directory
from .reports import write_json, html_report, score_label
from .scoring import aggregate


def compare_analyses(results_root, before, after, name):
    if not all(isinstance(v, str) and NAME.fullmatch(v) for v in (before, after, name)):
        raise ValueError('Comparison names must be simple identifiers')
    root = result_directory(results_root) / 'analysis'
    aroot, broot = root / before, root / after
    acfg, am, ahash = load_analysis(aroot)
    bcfg, bm, bhash = load_analysis(broot)
    same = ('dataset_hash', 'mode', 'selected_ids', 'support_definition', 'taxonomy_version', 'taxonomy_rules_hash')
    differences = [key for key in same if acfg.get(key) != bcfg.get(key)]
    if differences:
        raise ValueError('Incompatible analyses: ' + ', '.join(differences))
    def records(path):
        return [_json(line) for line in path.read_text(encoding='utf-8').splitlines()]
    ar, br = records(aroot / 'scored_results.jsonl'), records(broot / 'scored_results.jsonl')
    ad, bd = records(aroot / 'taxonomy.jsonl'), records(broot / 'taxonomy.jsonl')
    aset = {d['case_id'] for d in ad if d['support']['status'] == 'SUPPORTED'}
    bset = {d['case_id'] for d in bd if d['support']['status'] == 'SUPPORTED'}
    ae = {r['case_id'] for r in ar if r['score']['eligible']}
    be = {r['case_id'] for r in br if r['score']['eligible']}
    common = aset & bset & ae & be
    # Do not change the denominator independently on each side as features grow.
    left = aggregate([r for r in ar if r['case_id'] in common])
    right = aggregate([r for r in br if r['case_id'] in common])
    result = {'comparison_schema_version': '1.0', 'before': before, 'after': after,
              'scope': 'same_evaluated_supported_case_intersection', 'common_case_ids': sorted(common),
              'common_case_count': len(common), 'supported_added_ids': sorted(bset-aset),
              'supported_removed_ids': sorted(aset-bset),
              'unevaluated_excluded_ids': sorted((aset & bset) - common),
              'before_metrics': left, 'after_metrics': right, 'changes': {},
              'alias_provenance_equal': acfg.get('alias_provenance') == bcfg.get('alias_provenance'),
              'performance_conditions_equal': acfg.get('model_configuration') == bcfg.get('model_configuration'),
              'note': 'No subset improvement proves all-suite improvement; hardware/thermal conditions require manual review.'}
    for key in ('mode_success', 'task_success', 'capability_ok', 'slot_accuracy', 'policy_ok', 'llm_call_rate', 'false_execution'):
        x, y = left[key]['rate'], right[key]['rate']
        result['changes'][key] = None if x is None or y is None else (y-x)*100
    def build(stage):
        write_json(stage / 'comparison.json', result)
        text = '\n'.join(['# M2 지원 범위 고정 비교', '', f'{before} → {after}',
            f'양쪽에서 지원하고 실제 채점한 동일 {len(common)}개 문항만 비교합니다.',
            '지원 범위 확장/축소와 이해력 개선을 별도로 해석하세요.', '',
            '| 지표 | 이전 | 이후 | 변화 pp |', '|---|---|---|---|',
            *[f'| {key} | {score_label(left[key])} | {score_label(right[key])} | {delta} |' for key, delta in result['changes'].items()],
            '', '지원으로 분류가 바뀐 항목(별칭/계약 변화 확인): ' + ', '.join(result['supported_added_ids']),
            '미지원으로 분류가 바뀐 항목: ' + ', '.join(result['supported_removed_ids']),
            '미평가로 제외: ' + ', '.join(result['unevaluated_excluded_ids']),
            '원본 전체 성능은 기존 compare 명령으로 따로 비교합니다. 위 표의 분모 변경으로 전체 개선을 주장하지 않습니다.',
            '모델 설정 일치: ' + str(result['performance_conditions_equal']),
            '별칭 근거 일치: ' + str(result['alias_provenance_equal']),
            '하드웨어/발열/서비스 부하는 자동으로 같다고 확정하지 않습니다.']) + '\n'
        (stage / 'comparison.md').write_text(text, encoding='utf-8')
        (stage / 'comparison.html').write_text(html_report(text, 'M2 지원 범위 고정 비교'), encoding='utf-8')
        if inventory(aroot) != ahash or inventory(broot) != bhash:
            raise ValueError('Analysis input changed during comparison')
    folder = _write_bundle(root, name, build)
    return folder, result
