"""M2 derived reports; inputs stay immutable and no LLM summarizes private text."""
from __future__ import annotations

import csv

from .dataset import stable
from .reports import html_report, percent, safe_md, score_label

LABELS = {
    'UNSUPPORTED_CAPABILITY': '현재 Assistant 계약에 미노출/미등록/사용 불가',
    'ROUTING_FAILURE': '라우팅 또는 선행 후보·규칙 경계',
    'LLM_PROPOSAL_FAILURE': '관측된 모델 제안 형식/기능 선택',
    'CAPABILITY_FAILURE': '기능 선택 불일치(발생 단계 미확정)',
    'CONTEXT_FAILURE': '문맥·참조 필드 불일치',
    'TEMPORAL_FAILURE': '날짜·시간·기간 필드/제약 불일치',
    'ENTITY_GROUNDING_FAILURE': '실제 대상/결과 ID 불일치',
    'SLOT_EXTRACTION_FAILURE': '기타 필수 값 불일치',
    'POLICY_FAILURE': '실행·확인·명확화 정책 불일치',
    'ADAPTER_FAILURE': '진입한 Adapter의 결과/경계 불일치',
    'RESPONSE_ASSERTION_FAILURE': '명시된 응답 사실 불일치',
    'UNDETERMINED': '근거 부족/표현 투영 경계 점검 필요',
}


def _csv_cell(value):
    text = str(value) if value is not None else ''
    if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')):
        return "'" + text
    return text


def write_analysis_reports(folder, state, metrics, rows, diagnoses):
    base, supported = metrics['overall_preserved'], metrics['supported']
    coverage = metrics['support']
    lines = [f"# M2 진단 — {safe_md(state['name'])}", '',
             '**기존 실행의 저장된 점수를 분석한 결과입니다. Qwen 재호출·재채점·운영 변경이 아닙니다.**', '',
             f"원본: {safe_md(state['source_run'])} / mode: {state['mode']}",
             f"처리 {base['processed']}건 / 채점 {base['evaluated']}건 / trace 확보 {score_label(metrics['trace_coverage'])}",
             '원본 파일과 집계의 일치 검사가 성공한 경우에만 이 분석 폴더를 게시합니다.', '',
             '## 1. 원본 지표와 Supported-only 지표', '',
             '| 지표 | 원본 전체 (불변) | 지원 operation만 |', '|---|---|---|']
    for key, label in [('task_success', 'Full Task Success'), ('mode_success', '현재 모드 성공'),
                       ('capability_ok', 'Capability'), ('slot_accuracy', 'Slot'), ('policy_ok', 'Policy'),
                       ('status_ok', '최종 상태'), ('execution_ok', '실행 경계')]:
        lines.append(f'| {label} | {score_label(base[key])} | {score_label(supported[key])} |')
    lines += ['', f"지원 {coverage['supported']}건 / 미지원 계약 {coverage['unsupported']}건 / 지원 범위 {percent(coverage['coverage'])}",
              f"지원 항목 중 실제 채점 {coverage['supported_evaluated']}건. 미평가는 정확도 분모에서 제외합니다.",
              'Supported는 Assistant가 노출한 operation입니다. 같은 기능명이라도 특정 필터/문맥/인자가 모두 구현됐다는 뜻은 아닙니다.',
              '별칭이 기록되지 않은 정답 이름은 미등록으로 남을 수 있습니다. UI에 존재하거나 비슷한 API가 있다는 이유로 자동으로 같은 기능으로 간주하지 않습니다.',
              '지원 subset의 성공 건수는 원본 전체 성공 건수를 초과할 수 없습니다.', '',
              '## 2. Primary Failure Taxonomy', '', '| 분류 | 건수 | 해석 |', '|---|---:|---|']
    for cause, count in sorted(metrics['primary_failure_counts'].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f'| {cause} | {count} | {LABELS[cause]} |')
    lines += ['', '실패 1건당 primary는 정확히 하나입니다. 원본 secondary flags는 모두 보존합니다.',
              'trace=단계 근거, result=저장된 불일치 증상, insufficient=원인 미확정입니다. 우선순위 분류가 유일한 인과관계의 증명은 아닙니다.',
              '라우팅/정책 실패 표시는 안전장치를 해제하라는 지시가 아닙니다.', '',
              '## 3. 지원 기능의 표현 내구성', '', '| 표현 | 모드 성공 | 필수 값 일치 |', '|---|---|---|']
    for kind, item in metrics['supported_by_source_type'].items():
        lines.append(f"| {kind} | {score_label(item['mode_success'])} | {score_label(item['slot_accuracy'])} |")
    lines += ['', '전체 구성원을 보존한 지원 그룹 성공: ' + score_label(metrics['supported_group_success']),
              '지원 그룹 출력 일관성: ' + score_label(metrics['supported_group_consistency']),
              f"미지원/미평가 구성원이 있어 제외한 그룹: {metrics['excluded_groups_with_unsupported_or_unevaluated_members']}",
              '그룹 범위는 원본 실행에 선택된 문항입니다. 없는 표현을 성공 처리하지 않고, 같은 오답의 일관성을 성공으로 취급하지 않습니다.', '',
              '## 4. Supported Domain / Route', '', '| Domain | 처리/채점 | 모드 성공 |', '|---|---|---|']
    for name, item in metrics['supported_by_domain'].items():
        lines.append(f"| {safe_md(name)} | {item['processed']}/{item['evaluated']} | {score_label(item['mode_success'])} |")
    lines += ['', '| Route | 처리/채점 | 모드 성공 | p95 total ms |', '|---|---|---|---|']
    for name, item in metrics['supported_by_route'].items():
        p95 = item['latency_ms']['total_ms']['p95']
        lines.append(f"| {safe_md(name)} | {item['processed']}/{item['evaluated']} | {score_label(item['mode_success'])} | {'N/A' if p95 is None else f'{p95:.3f}'} |")
    lines += ['', '## 5. 단계별 해석을 위한 보조 지표', '',
              '기능을 맞춘 지원 문항에서의 slot 일치: ' + score_label(metrics['supported_slots_given_correct_capability']),
              '지원 문항의 temporal slot: ' + score_label(metrics['supported_temporal_slots']),
              '지원 문항의 entity/result ID slot: ' + score_label(metrics['supported_entity_slots']),
              '지원 문항에서 되물은 요청 중 정말 명확화가 필요했던 비율: ' + score_label(metrics['supported_clarification_precision']),
              '지원 문항 중 명확화가 불필요한 요청을 되물은 비율: ' + score_label(metrics['supported_unnecessary_clarification_rate']),
              '기존 null/미관측 slot 채점 정의도 그대로 남아 있습니다. ID/결과 slot을 순수 모델 추출 정확도로 읽지 마세요.', '',
              '## 6. Safety / Latency — 원본 범위 유지', '',
              '원본의 잘못된 쓰기/제어 실행: ' + score_label(base['false_execution']),
              '미지원/미평가로 분류해도 기존 안전성 flag를 숨기지 않습니다. 0건은 실기기/다음 요청의 안전 보장이 아닙니다.',
              f"원본 실제 모델 전송: {base['llm_calls']}회; 분석 중 모델 전송: 0회", '',
              '| 구간 | n | mean ms | p50 ms | p95 ms |', '|---|---:|---:|---:|---:|']
    for label, values in [('원본 total', base['latency_ms']['total_ms']),
                          ('실제 LLM 호출 문항의 llm_ms', metrics['llm_call_case_latency_ms']),
                          ('LLM 미호출 문항의 total_ms', metrics['no_llm_case_total_latency_ms'])]:
        times = ['N/A' if values[k] is None else f'{values[k]:.3f}' for k in ('mean', 'p50', 'p95')]
        lines.append(f"| {label} | {values['n']} | " + ' | '.join(times) + ' |')
    lines += ['', '중첩 시간은 더하지 않습니다. 분석 실행시간을 V35 추론시간으로 대체하지 않습니다.', '',
              '## 7. 재현 정보와 한계', '', f"Dataset SHA256: {state['dataset_hash']}",
              f"원본 Production Git SHA: {state['production_git_sha']}",
              f"원본 Benchmark Git SHA: {state['original_benchmark_git_sha']}",
              f"분석 코드 Git SHA: {state['analysis_git_sha']}",
              f"Production source hash: {state['production_source_hash']}",
              f"Support snapshot hash: {state['support_snapshot_hash']}",
              f"Taxonomy version: {state['taxonomy_version']}",
              f"Taxonomy rules hash: {state['taxonomy_rules_hash']}",
              f"입력 무결성 manifest hash: {state['input_manifest_hash']}", '', *state['warnings'], '',
              '세부 파일: SUPPORTED_FAILURES.md, UNSUPPORTED_CAPABILITIES.md, UNDETERMINED.md, taxonomy.jsonl, cases.csv.',
              'verify-analysis로 원본/파생 결과 해시를 다시 확인할 수 있습니다. 로컬 해시는 전자서명이나 최초 실행 진실성의 증명이 아닙니다.']
    text = '\n'.join(lines) + '\n'
    (folder / 'summary.md').write_text(text, encoding='utf-8')
    (folder / 'summary.html').write_text(html_report(text, 'M2 오프라인 분석'), encoding='utf-8')
    lookup = {r['case_id']: r for r in rows}
    for filename, title, selected in [
        ('SUPPORTED_FAILURES.md', '지원 operation의 실패', [d for d in diagnoses if d['status'] == 'FAILURE' and d['support']['status'] == 'SUPPORTED']),
        ('UNSUPPORTED_CAPABILITIES.md', '현재 계약에 없는 요청', [d for d in diagnoses if d['support']['status'] != 'SUPPORTED']),
        ('UNDETERMINED.md', '근거 부족과 재검토 대상', [d for d in diagnoses if d['primary_cause'] == 'UNDETERMINED' or not d['trace_available']]),
    ]:
        details = ['# ' + title, '', '정답이나 원본 점수를 수정하지 않은 진단입니다.', '']
        for d in selected:
            row = lookup[d['case_id']]
            details += [f"## {safe_md(d['case_id'])} — {d['primary_cause'] or d['status']}", '',
                        '입력: ' + safe_md(row['input_text']),
                        f"기대 기능: {safe_md(row['expected']['capability'])} / 실제: {safe_md(row['actual']['capability'])}",
                        '기대 slots: ' + safe_md(stable(row['expected']['slots'])),
                        '실제 slots: ' + safe_md(stable(row['actual']['slots'])),
                        '계약 근거: ' + safe_md(stable(d['support'])),
                        '분류 근거: ' + safe_md(stable(d['evidence'])),
                        '근거 수준: ' + d['evidence_level'],
                        '보조 진단: ' + ', '.join(d['diagnostic_flags']),
                        '원본 flags: ' + ', '.join(d['secondary_flags']),
                        '원본 trace: ' + safe_md(row.get('trace_file') or '(없음)'), '']
        if not selected:
            details.append('해당 항목 없음')
        (folder / filename).write_text('\n'.join(details) + '\n', encoding='utf-8')
    with (folder / 'cases.csv').open('w', encoding='utf-8-sig', newline='') as out:
        writer = csv.writer(out)
        writer.writerow(['case_id', 'input_text', 'support', 'support_reason', 'status', 'primary_cause',
                         'evidence_level', 'trace_available', 'original_mode_success', 'secondary_flags', 'diagnostic_flags'])
        for d in diagnoses:
            values = [d['case_id'], lookup[d['case_id']]['input_text'], d['support']['status'], d['support']['reason'],
                      d['status'], d['primary_cause'], d['evidence_level'], d['trace_available'],
                      d['original_score']['mode_success'], ','.join(d['secondary_flags']), ','.join(d['diagnostic_flags'])]
            writer.writerow([_csv_cell(v) for v in values])
