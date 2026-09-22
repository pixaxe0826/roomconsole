"""Deterministic Korean reports. All embedded user/model text is HTML-escaped."""
from __future__ import annotations
import csv
import html
import json
from pathlib import Path
from .dataset import stable


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def percent(value):
    return 'N/A' if value is None else f'{value * 100:.1f}%'


def score_label(value):
    return f"{value['correct']}/{value['total']} ({percent(value['rate'])})" if value['total'] else 'N/A'


def safe_md(value):
    return str(value).replace('|', '\\|').replace('\n', ' ').replace('\r', '')


def html_report(text, title):
    return ('<!doctype html><html lang="ko"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>' + html.escape(title) + '</title><style>'
            'body{max-width:1080px;margin:3em auto;padding:0 1.5em;font:16px/1.7 system-ui,sans-serif;}'
            'pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;}h1{font-size:1.6em;}'
            '</style><body><h1>' + html.escape(title) + '</h1><pre>' + html.escape(text) + '</pre></body></html>')


def failure_report(rows):
    order = ['false_execution', 'hallucination_structural', 'wrong_policy', 'wrong_capability',
             'critical_slot_error', 'wrong_final_status', 'wrong_execution_boundary', 'response_fact_error']
    def priority(row):
        fs = row['score']['failures']
        return min([order.index(f) for f in fs if f in order] or [len(order)]), row['case_id']
    failures = sorted((r for r in rows if r['score']['mode_success'] is not True), key=priority)
    lines = ['# 실패 및 미평가 사례', '', '미평가(LLM/환경 부재)는 측정된 의미 이해 실패와 구분합니다.', '']
    for row in failures:
        e, a = row['expected'], row['actual']
        lines += [f"## {row['case_id']} — {row['category']}", '', f"입력: {row['input_text']}", '',
                  f"기대: {e['capability']} / {e['policy']} / {e['status']}",
                  '기대 슬롯: ' + stable(e['slots']),
                  f"실제: {a['capability']} / {a['policy']} / {a['status']}",
                  '실제 슬롯: ' + stable(a['slots']),
                  f"경로: {a['route']} / 실제 LLM 전송: {a['llm_calls']}회 / 지연: {a['latency'].get('total_ms')} ms",
                  '원인: ' + ', '.join(row['score']['failures']), '',
                  '실제 응답: ' + (a.get('final_response') or '(없음)'), '']
    return '\n'.join(lines), failures


def write_reports(folder, config, rows, metrics):
    folder = Path(folder)
    write_json(folder / 'metrics.json', metrics)
    failures_text, failures = failure_report(rows)
    (folder / 'FAILURES.md').write_text(failures_text, encoding='utf-8')
    (folder / 'failures.jsonl').write_text(''.join(stable(row) + '\n' for row in failures), encoding='utf-8')
    with (folder / 'cases.csv').open('w', encoding='utf-8-sig', newline='') as out:
        fields = ['case_id', 'source_type', 'domain', 'category', 'input_text', 'expected_capability',
                  'actual_capability', 'expected_policy', 'actual_policy', 'mode_success', 'task_success',
                  'route', 'llm_called', 'false_execution', 'latency_ms', 'failures']
        writer = csv.DictWriter(out, fieldnames=fields); writer.writeheader()
        for row in rows:
            a, e, s = row['actual'], row['expected'], row['score']
            item = {k: row[k] for k in ('case_id', 'source_type', 'domain', 'category', 'input_text')}
            item.update(expected_capability=e['capability'], actual_capability=a['capability'],
                        expected_policy=e['policy'], actual_policy=a['policy'], mode_success=s['mode_success'],
                        task_success=s['task_success'], route=a['route'], llm_called=a['llm_called'],
                        false_execution=s['false_execution'], latency_ms=a['latency'].get('total_ms'),
                        failures=','.join(s['failures']))
            # Spreadsheet formula injection is not a valid way to display model/user text.
            item = {k: ("'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t')) else v) for k, v in item.items()}
            writer.writerow(item)
    mode = config['mode']
    metric = metrics['task_success'] if mode == 'full' else metrics['mode_success']
    incomplete = len(rows) != config['selected_count'] or metrics['evaluated'] != len(rows)
    lines = [f"# Room Hub 벤치마크 — {config['name']}", '',
             '## 1. Executive Summary', '',
             f"상태: {'부분 측정 — 실제 LLM 포함 baseline이 아닙니다.' if incomplete else '선택된 항목의 실행이 완료되었습니다.'}",
             f"선택 {config['selected_count']}건 중 {len(rows)}건을 처리했고, {metrics['evaluated']}건을 채점했습니다.",
             f"{'전체 작업 성공' if mode == 'full' else mode + ' 경계까지 성공'}은 {score_label(metric)}입니다.",
             f"LLM 부재·연결 실패로 {metrics['llm_blocked']}건을 채점에서 제외했습니다. 제외된 항목은 성공도 실패도 아닙니다.",
             '전체 선택 항목 대비 확인된 성공의 하한: ' + percent(metric['correct'] / config['selected_count'] if config['selected_count'] else None) + '.', '',
             '## 2. Overall Score', '', '| 지표 | 결과 |', '|---|---|']
    for field, label in [('task_success', 'Full Task Success'), ('mode_success', '현재 모드 성공'),
                         ('capability_ok', 'Capability'), ('slot_accuracy', 'Slot'), ('policy_ok', 'Policy'),
                         ('status_ok', '최종 상태'), ('execution_ok', 'Adapter 실행 경계'), ('clarification_ok', '올바른 명확화')]:
        lines.append(f'| {label} | {score_label(metrics[field])} |')
    lines += ['', '## 3. Unique vs Paraphrase', '']
    for source, group in metrics['by_source_type'].items():
        lines.append(f"{source}: {score_label(group['mode_success'])}; 처리 {group['processed']} / 채점 {group['evaluated']}")
    lines += ['표현 그룹 출력 일관성: ' + score_label(metrics['paraphrase_consistency']),
              '그룹 전체 성공률: ' + score_label(metrics['paraphrase_group_success']),
              '모든 표현이 동일하게 틀려도 출력 일관성은 높을 수 있습니다. 그룹 성공률과 함께 해석합니다.', '']
    for key, heading in [('by_domain', '4. Domain별 성능'), ('by_category', '5. Category별 성능')]:
        lines += ['## ' + heading, '', '| 구분 | 현재 모드 성공 | 처리/채점 |', '|---|---|---|']
        for label, group in metrics[key].items():
            lines.append(f"| {safe_md(label)} | {score_label(group['mode_success'])} | {group['processed']}/{group['evaluated']} |")
        lines.append('')
    lines += ['## 6. Routing 비율', '', *[f'{route}: {n}건' for route, n in metrics['routes'].items()], '',
              '## 7. LLM 사용률', '', f"실제 전송: {metrics['llm_calls']}회 / 호출률 {score_label(metrics['llm_call_rate'])}",
              f"모델 필요 경로: {metrics['llm_requested']}건; 차단/실패: {metrics['llm_blocked']}건.",
              '관측 가능한 불필요 호출: ' + score_label(metrics['unnecessary_llm_call']),
              '판정 기준은 실제 EXACT 경로의 모델 전송뿐입니다. route_hint는 참고값이며 미래 Parser의 처리 가능성을 가정하지 않습니다.', '',
              '## 8. Safety', '', '잘못된 쓰기/제어 실행: ' + score_label(metrics['false_execution']),
              '근거/계약 위반 제안: ' + score_label(metrics['unsupported_value']),
              '구조적으로 탐지한 환각: ' + score_label(metrics['hallucination_detected']),
              '환각 지표는 지원하지 않는 모델 capability 제안을 탐지한 하한입니다. 응답 전체의 의미적 사실성을 평가한 수치가 아닙니다.',
              '0건이어도 미평가 요청·미실행 경로·실기기의 안전성을 보증하지 않습니다.', '',
              '## 9. Latency', '', '| 구간 | n | mean | p50 | p90 | p95 | p99 | max (ms) |', '|---|---|---|---|---|---|---|---|']
    for label, stats in metrics['latency_ms'].items():
        lines.append('| ' + label + ' | ' + str(stats['n']) + ' | ' + ' | '.join(
            'N/A' if stats[k] is None else f'{stats[k]:.3f}' for k in ('mean', 'p50', 'p90', 'p95', 'p99', 'max')) + ' |')
    lines += ['프로세스 시작·fixture 생성·보고서 생성은 위 지연에서 제외합니다. 중첩 단계 시간은 합산하면 안 됩니다.',
              'LLM 없는 부분 측정 지연을 V35/Qwen의 실제 응답 지연으로 해석하지 마세요.', '',
              '## 10. 실패·미평가 유형', '']
    lines += [f'{kind}: {count}건' for kind, count in sorted(metrics['failure_types'].items(), key=lambda x: -x[1])]
    lines += ['', '## 11. 대표 실패 사례', '', '| ID | 입력 | 주요 원인 |', '|---|---|---|']
    lines += [f"| {safe_md(r['case_id'])} | {safe_md(r['input_text'])} | {safe_md(', '.join(r['score']['failures']))} |" for r in failures[:20]]
    lines += ['', '모든 사례와 실제/기대 슬롯은 FAILURES.md 및 raw_results.jsonl에 있습니다.', '',
              '## 12. 이전 Benchmark와 비교', '', '이 보고서만으로는 비교하지 않았습니다. compare 명령의 comparison.md를 사용하세요.', '',
              '## 13. 개선된 부분', '', '비교 실행 전에는 개선을 주장하지 않습니다.', '',
              '## 14. 악화된 부분', '', '비교 실행 전에는 악화를 주장하지 않습니다.', '',
              '## 15. 다음 개발에서 우선할 영역', '']
    if metrics['llm_blocked']:
        lines.append('먼저 같은 소스로 실제 local LLM baseline을 실행해 미평가 항목을 채우세요. 부분 점수로 Parser/모델의 우열을 정하지 마세요.')
    measured_failures = {k: g['mode_success']['total'] - g['mode_success']['correct'] for k, g in metrics['by_domain'].items()}
    if measured_failures:
        worst = max(measured_failures, key=measured_failures.get)
        if measured_failures[worst]:
            lines.append(f'관측된 실패 수가 가장 많은 영역은 {worst}({measured_failures[worst]}건)입니다. 계약 차이와 해석 실패를 먼저 구분하세요.')
    lines += ['이 도구는 운영 규칙을 자동 수정하거나 학습하지 않습니다. 별도 Parser PR에서 같은 suite/hash로 재측정합니다.', '',
              '## 재현성과 제한', '', f"Suite: {config['suite']} / {config['suite_version']}",
              f"Dataset SHA256: {config['dataset_hash']}", f"Source commit: {config.get('git_commit')}",
              f"Production Git SHA: {config.get('production_git_sha')}",
              f"Benchmark Git SHA: {config.get('benchmark_git_sha')}",
              f"Case count: {config.get('case_count')} / Selected IDs: {stable(config.get('selected_ids', []))}",
              f"Model / endpoint: {config.get('model_configuration', {}).get('model')} / {config.get('model_configuration', {}).get('endpoint')}",
              f"Mode: {mode}", '모델 seed/context/quantization은 확인하지 못하면 null이며, 실제 전송 payload는 trace에 보존됩니다.',
              '현재 fixture의 Calendar/Todo는 운영 코드처럼 같은 테이블을 공유합니다. Calendar 종료시각과 범용 세션 해석은 미지원입니다.',
              'expected_response의 문장 일치는 참고 지표입니다. 필수 응답 사실은 response_assertions로 명시해야 채점합니다.',
              '자세한 지표 정의와 한계는 docs/ASSISTANT_BENCHMARK.md를 참고하세요.']
    text = '\n'.join(lines) + '\n'
    (folder / 'summary.md').write_text(text, encoding='utf-8')
    (folder / 'summary.html').write_text(html_report(text, config['name']), encoding='utf-8')
    return text


def compare_runs(before, after, output, allow_incompatible=False):
    before, after, output = Path(before), Path(after), Path(output)
    bcfg = json.loads((before / 'config.json').read_text())
    acfg = json.loads((after / 'config.json').read_text())
    mismatches = [k for k in ('dataset_hash', 'mode', 'selected_ids') if bcfg.get(k) != acfg.get(k)]
    brows = [json.loads(s) for s in (before / 'raw_results.jsonl').read_text().splitlines()]
    arows = [json.loads(s) for s in (after / 'raw_results.jsonl').read_text().splitlines()]
    bvalid = {r['case_id'] for r in brows if r['score']['eligible']}
    avalid = {r['case_id'] for r in arows if r['score']['eligible']}
    if bvalid != avalid: mismatches.append('evaluated_case_ids')
    if mismatches and not allow_incompatible:
        raise ValueError('Incompatible runs: ' + ', '.join(mismatches) + '. Use --allow-incompatible for a labelled intersection only.')
    common = bvalid & avalid
    from .scoring import aggregate
    bm = aggregate([r for r in brows if r['case_id'] in common])
    am = aggregate([r for r in arows if r['case_id'] in common])
    changes = {}
    for key in ('task_success', 'mode_success', 'capability_ok', 'slot_accuracy', 'policy_ok', 'llm_call_rate', 'false_execution'):
        b, a = bm[key]['rate'], am[key]['rate']
        changes[key] = {'before': b, 'after': a, 'delta_percentage_points': None if b is None or a is None else (a - b) * 100}
    for key in ('total_ms', 'llm_ms', 'non_llm_ms'):
        b, a = bm['latency_ms'][key]['p95'], am['latency_ms'][key]['p95']
        changes[key + '_p95'] = {'before': b, 'after': a, 'delta_ms': None if b is None or a is None else a - b}
    lookup = {r['case_id']: r for r in brows}
    improved, regressed = [], []
    for row in arows:
        if row['case_id'] not in common: continue
        prev = lookup[row['case_id']]['score']['mode_success']; curr = row['score']['mode_success']
        if not prev and curr: improved.append(row['case_id'])
        if prev and not curr: regressed.append(row['case_id'])
    result = {'comparable': not mismatches, 'mismatches': mismatches, 'scope': 'shared_evaluated_cases_only',
              'shared_case_count': len(common), 'before': bcfg['name'], 'after': acfg['name'],
              'changes': changes, 'llm_calls': {'before': bm['llm_calls'], 'after': am['llm_calls']},
              'improved': improved, 'regressed': regressed}
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'comparison.json', result)
    lines = ['# Benchmark 비교', '', f"{bcfg['name']} → {acfg['name']}",
             f'공통으로 채점된 {len(common)}건만 비교합니다.',
             '비호환 항목: ' + (', '.join(mismatches) or '없음'), '', '| 지표 | 이전 | 이후 | 변화 |', '|---|---|---|---|']
    for key, values in changes.items():
        if 'delta_percentage_points' in values:
            delta = values['delta_percentage_points']
            lines.append(f"| {key} | {percent(values['before'])} | {percent(values['after'])} | {'N/A' if delta is None else f'{delta:+.2f} pp'} |")
        else:
            lines.append(f"| {key} | {values['before']} | {values['after']} | {values['delta_ms']} ms |")
    lines += ['', f"LLM 전송 횟수: {bm['llm_calls']} → {am['llm_calls']}", '',
              '## 개선 사례', ', '.join(improved) or '없음', '', '## 악화 사례', ', '.join(regressed) or '없음', '',
              '데이터셋·모드·평가 범위가 다르면 전체 수치의 개선/악화를 단정하지 않습니다. CPU 지연은 같은 하드웨어/조건에서 비교하세요.']
    text = '\n'.join(lines) + '\n'
    (output / 'comparison.md').write_text(text, encoding='utf-8')
    (output / 'comparison.html').write_text(html_report(text, 'Benchmark 비교'), encoding='utf-8')
    return result
