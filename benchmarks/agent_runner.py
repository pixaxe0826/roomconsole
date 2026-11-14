"""Private, durable orchestration for the Personal Agent text benchmark.

Never use the deployed HTTP server or its database. Only gold-free fixture/input
envelopes cross into one serial worker; grading stays here in the parent.
"""
from __future__ import annotations

from collections import Counter, OrderedDict
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time

from .agent_config import ENGINE, FORMAT_VERSION, validate_config
from .agent_gateway import RunBudget
from .agent_projection import AGENT_PROJECTION_VERSION, project_agent
from .dataset import NAME, category, stable
from .paths import no_links, result_directory
from .reports import failure_report, html_report, score_label
from .runner import ROOT, runtime_envelope, source_state
from .scoring import latency, score, summarize


@contextmanager
def private_outputs():
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def durable_json(path: Path, value):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def worker_environment(live: bool) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('HUB_') and k not in {
               'OPENAI_API_KEY', 'OPENAI_ORG_ID', 'OPENAI_PROJECT_ID', 'OPENAI_BASE_URL',
               'PYTHONPATH', 'PYTHONSTARTUP', 'ROOM_HUB_BENCHMARK_DATA'}}
    if live:
        # Credentials are inherited through environment only, never JSON/argv/files.
        for key in ('OPENAI_API_KEY', 'HUB_OPENAI_API_KEY'):
            if os.environ.get(key, '').strip():
                env[key] = os.environ[key]
    env.update(PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
    return env


def prepare_groups(suite, selected, config):
    groups = OrderedDict()
    for case in selected:
        key = ('session', case['session_id']) if case.get('session_id') else ('isolated', case.get('fixture_id'))
        groups.setdefault(key, []).append(case)
    payloads, ordered = [], []
    for (kind, identity), items in groups.items():
        if kind == 'session':
            items.sort(key=lambda c: c['turn'])
            if [c['turn'] for c in items] != list(range(1, len(items) + 1)):
                raise ValueError(f'Session {identity}: select all prerequisite turns starting at 1')
        envelope = runtime_envelope(suite, items, config, 'full')
        del envelope['config'], envelope['mode']
        payloads.append(envelope)
        ordered.extend(items)
    return {'version': FORMAT_VERSION, 'config': dict(config), 'groups': payloads}, ordered


def worker_events(payload):
    """Budget events yield before ACK: caller must fsync a checkpoint before next()."""
    timeout = payload['config']['timeout'] + 60
    with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as errors:
        process = subprocess.Popen([sys.executable, '-m', 'benchmarks.agent_worker'], cwd=ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
            text=True, encoding='utf-8', env=worker_environment(payload['config']['live']))
        lines = queue.Queue()
        def read_lines():
            try:
                for line in process.stdout:
                    lines.put(line)
            finally:
                lines.put(None)
        reader = threading.Thread(target=read_lines, daemon=True)
        reader.start()
        sequence = 0
        done = False
        try:
            process.stdin.write(stable(payload) + '\n')
            process.stdin.flush()
            while True:
                try:
                    line = lines.get(timeout=timeout)
                except queue.Empty:
                    raise RuntimeError('worker_timeout') from None
                if line is None:
                    if process.wait(timeout=5) != 0 or not done:
                        raise RuntimeError('worker_interrupted')
                    break
                if done or len(line) > 16 * 1024 * 1024:
                    raise RuntimeError('invalid_worker_output')
                event = json.loads(line)
                if not isinstance(event, dict) or event.get('kind') not in {'budget', 'observation', 'done'}:
                    raise RuntimeError('invalid_worker_event')
                if event['kind'] == 'budget':
                    sequence += 1
                    if event.get('sequence') != sequence or not isinstance(event.get('budget'), dict):
                        raise RuntimeError('invalid_budget_checkpoint')
                    yield event
                    # Reaching here proves caller's durable write returned successfully.
                    process.stdin.write(stable({'ack': sequence}) + '\n')
                    process.stdin.flush()
                else:
                    done = event['kind'] == 'done'
                    yield event
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            reader.join(timeout=5)
            process.stdout.close()
            process.stdin.close()


def interrupted_budget(snapshot, reason):
    """An acknowledged active reservation is uncertain after worker termination."""
    value = deepcopy(snapshot)
    for call in value['calls']:
        if call['state'] == 'active':
            call['state'] = 'unknown'
    value['unknown_microdollars'] += value['reserved_microdollars']
    value['reserved_microdollars'] = 0
    value['cost_complete'] = not value['unknown_microdollars']
    value['stop_reason'] = value.get('stop_reason') or reason
    return value


def not_evaluated(reason):
    return {'mode': 'full', 'agent_run': {}, 'registry_calls': [], 'adapter_calls': [],
            'llm': {'attempts': []}, 'latency': {}, 'business_state_changed': False,
            'harness_error': reason, 'observation_complete': False}


def write_summary(folder, config, rows, metrics, budget):
    stats = metrics['task_success']
    lines = [f"# Personal Agent text benchmark — {config['name']}", '',
        f"상태: {config['run_status']} · 선택 {config['selected_count']} · 채점 {metrics['evaluated']}",
        f"기존 V1 계약 기준 성공: {score_label(stats)}",
        f"선택 전체 대비 확인된 성공: {stats['correct']}/{config['selected_count']}", '',
        '실제 production PersonalAgentService / OpenAIGateway / 어댑터를 임시 DB에서 호출합니다.',
        '배포 서버 HTTP·인증·저장 정책·iPad·마이크·전사 품질은 이 실행의 검증 범위가 아닙니다.',
        '변경 미리보기는 승인하지 않습니다. 조회 및 기존의 즉시 타이머 제어만 실행합니다.',
        f"호출 모드: {config['configuration']['agent_mode']} · live={config['configuration']['live']}",
        f"OpenAI 전송 예약/시도: {budget['dispatched_calls']} · 로컬 규칙: {metrics['routes'].get('tier0_local_rule', 0)}",
        '전송 시도 기록은 공급자 수신/과금 성공 증거가 아닙니다.',
        '공급자/비용 오류: ' + stable(metrics['provider_errors']),
        f"로컬 fallback 결과: {metrics['routes'].get('tier0_fallback', 0)}건 (모델 응답 성공과 구분)", '',
        '## 비용과 중단', '',
        f"확인된 사용량 기준 비용: ${budget['spent_microdollars'] / 1_000_000:.6f}",
        f"미정 예약액: ${budget['unknown_microdollars'] / 1_000_000:.6f} · 진행 중 예약: ${budget['reserved_microdollars'] / 1_000_000:.6f}",
        f"실행별 한도: ${budget['limit_microdollars'] / 1_000_000:.6f} / {budget['max_calls']}회",
        f"중단 사유: {budget.get('stop_reason') or '(없음)'}",
        '비용은 저장된 production 가격표와 공급자 usage의 계산값이며 공급자 청구서가 아닙니다.',
        '새 run 이름으로 시작하면 별도 한도입니다. 중단 요청의 자동 재시도/재개는 없습니다.', '',
        '## 채점·안전 해석', '',
        '원본 dataset/gold/projection/score는 변경하지 않습니다. 다중 단계에서 첫 동작을 골라 점수를 만들지 않습니다.',
        '매핑되지 않는 계획은 기록된 실패/제약으로 남깁니다. 호출/실행 환경 문제에 의한 미평가는 성공이 아닙니다.',
        f"V1 보수적 오실행 플래그: {score_label(metrics['false_execution'])}",
        '다중 계획의 단일 capability 매핑 불가로 발생한 플래그는 원래 계획/동작을 검토해야 합니다.',
        f"채점 가능 여부와 독립적인 실제 어댑터 진입: {metrics['observed_safety']['adapter_entries']}회; "
        f"쓰기/제어 진입: {metrics['observed_safety']['write_entries']}회; "
        f"business state 변경: {metrics['observed_safety']['state_changed_cases']}건",
        f"실행 관측이 불완전/없음: {metrics['observed_safety']['incomplete_observation_cases']}건",
        '미평가/관측 누락이 있는 경우 쓰기 0회만으로 안전성을 증명하지 않습니다.',
        '이 엔진의 supported cohort는 계산하지 않습니다. 과거 legacy 144문항 분모를 가져오지 않습니다.',
        'Qwen 결과와는 다른 전체 플랫폼 경로이므로 모델만의 A/B 결과로 해석하지 마세요.',
        '기존 benchmarks analyze/compare-analysis 입력 형식과 의도적으로 분리했습니다.', '',
        '## 상세 결과', '',
        f"Capability: {score_label(metrics['capability_ok'])} · Policy: {score_label(metrics['policy_ok'])}",
        f"Slots: {score_label(metrics['slot_accuracy'])} · 실행 경계: {score_label(metrics['execution_ok'])}",
        '경로: ' + stable(metrics['routes']),
        'V1 투영 상태: ' + stable(metrics['projection_statuses']),
        '지연(ms): ' + stable(metrics['latency_ms']), '',
        'llm_ms/gateway_ms는 비용 원장·부모 체크포인트 ACK를 포함합니다. transport_ms는 그 ACK 이후의 provider transport 구간입니다.',
        'agent_raw_results.jsonl / agent_trace/ 에 원본 계획·요청·응답·채점 근거가 있습니다.',
        '이 결과에는 평가 문항과 private prompt/fixture가 포함될 수 있습니다. Git/공개 이슈에 올리지 마세요.', '',
        f"Dataset SHA256: {config['dataset_hash']}", f"Source commit: {config['source']['git_commit']}",
        f"Source hash: {config['source']['production_source_hash']}",
        f"Harness hash: {config['source']['harness_source_hash']}",
        '이 보고서는 모델 개선이나 운영 배포를 수행하지 않습니다.']
    text = '\n'.join(lines) + '\n'
    (folder / 'summary.md').write_text(text, encoding='utf-8')
    (folder / 'summary.html').write_text(html_report(text, 'Personal Agent benchmark'), encoding='utf-8')
    failures, _ = failure_report(rows)
    (folder / 'FAILURES.md').write_text(failures, encoding='utf-8')


def run_agent_suite(suite, selected, *, name, results_root, configuration, quiet=False):
    configuration = validate_config(configuration)
    if not NAME.fullmatch(name) or not selected:
        raise ValueError('A simple new run name and at least one selected case are required')
    if configuration['live'] and not any(os.environ.get(k, '').strip() for k in ('OPENAI_API_KEY', 'HUB_OPENAI_API_KEY')):
        raise ValueError('No OpenAI-specific API key is configured in this shell')
    payload, ordered = prepare_groups(suite, selected, configuration)
    folder = result_directory(results_root, suite.path) / name
    no_links(folder)
    with private_outputs():
        folder.mkdir(parents=True, exist_ok=False, mode=0o700)
        (folder / 'agent_trace').mkdir(mode=0o700)
        source = source_state()
        config = {
            'engine': ENGINE, 'format_version': FORMAT_VERSION, 'projection_version': AGENT_PROJECTION_VERSION,
            'name': name, 'suite': suite.name, 'suite_version': suite.manifest['version'],
            'dataset_hash': suite.digest, 'dataset_source': 'external',
            'dataset_write_protection': 'caller_read_only_not_a_kernel_mount_boundary',
            'selected_count': len(ordered), 'selected_ids': [c['id'] for c in ordered],
            'case_count': len(suite.cases), 'mode': 'full', 'configuration': configuration,
            'reference_datetime': suite.manifest['reference_datetime'], 'timezone': suite.manifest.get('timezone', 'UTC'),
            'profile': 'odyssey_openai', 'source': source,
            'policy_source': 'fresh benchmark fixture; never deployed policy',
            'isolated_monthly_cap_usd': 100.0, 'auto_downgrade': False, 'automatic_confirmation': False,
            'http_auth_ui_audio_tested': False, 'supported_cohort': 'not_calculated_for_personal_agent',
            'started_at': datetime.now(timezone.utc).isoformat(), 'run_status': 'RUNNING',
        }
        budget = RunBudget(configuration['max_cost_usd'], configuration['max_calls']).snapshot()
        durable_json(folder / 'agent_config.json', config)
        durable_json(folder / 'budget.json', budget)
        rows, safety = [], {'adapter_entries': 0, 'write_entries': 0, 'state_changed_cases': 0,
                            'incomplete_observation_cases': 0}
        provider_errors = Counter()
        iterator = worker_events(payload)
        interruption = None
        completed = False
        start = time.perf_counter()

        def save_observation(case, obs, raw):
            actual = project_agent(obs, suite.projection)
            scores = score(case, actual, suite.projection, 'full')
            trace_name = hashlib.sha256(case['id'].encode()).hexdigest()[:24] + '.json'
            row = {'case_id': case['id'], 'parent_id': case.get('parent_id'), 'input_text': case['input_text'],
                'source_type': case['source_type'], 'domain': case['expected']['domain'], 'category': category(case),
                'tags': case.get('tags', []), 'split': case.get('split', 'development'), 'expected': case['expected'],
                'expected_response': case.get('expected_response'),
                'scoring_context': {'critical_slots': sorted(case.get('critical_slots', case['expected']['slots']))},
                'actual': actual, 'score': scores, 'trace_file': 'agent_trace/' + trace_name}
            durable_json(folder / 'agent_trace' / trace_name, obs)
            raw.write(stable(row) + '\n'); raw.flush(); os.fsync(raw.fileno())
            rows.append(row)
            actions = [c for c in obs.get('adapter_calls', []) if c.get('phase') == 'action' and c.get('entered')]
            safety['adapter_entries'] += len(actions)
            safety['write_entries'] += sum(not c.get('read_only', True) for c in actions)
            safety['state_changed_cases'] += bool(obs.get('business_state_changed'))
            safety['incomplete_observation_cases'] += bool(obs.get('harness_error') or obs.get('observation_complete') is False)
            provider_errors.update(c['native_error_code'] for c in obs.get('llm', {}).get('attempts', [])
                                   if c.get('native_error_code'))
            durable_json(folder / 'progress.json', {'processed': len(rows), 'selected': len(ordered), 'run_status': 'RUNNING'})
            if not quiet:
                state = 'PASS' if scores['mode_success'] is True else 'FAIL' if scores['eligible'] else 'NOT_EVALUATED'
                print(f"[{len(rows)}/{len(ordered)}] {case['id']} {state} {actual['route']}", flush=True)

        try:
            with (folder / 'agent_raw_results.jsonl').open('x', encoding='utf-8') as raw, \
                 (folder / 'budget_journal.jsonl').open('x', encoding='utf-8') as journal:
                try:
                    for event in iterator:
                        if event['kind'] == 'budget':
                            journal.write(stable(event) + '\n'); journal.flush(); os.fsync(journal.fileno())
                            budget = event['budget']
                            durable_json(folder / 'budget.json', budget)
                        elif event['kind'] == 'observation':
                            if event.get('index') != len(rows) or len(rows) >= len(ordered):
                                raise RuntimeError('Worker result order mismatch')
                            save_observation(ordered[len(rows)], event['observation'], raw)
                        else:
                            if event.get('processed') != len(rows):
                                raise RuntimeError('Worker count mismatch')
                            budget = event['budget']
                            completed = True
                except (RuntimeError, OSError, ValueError, KeyError, TypeError) as exc:
                    interruption = 'worker_or_checkpoint_failure:' + type(exc).__name__
                except KeyboardInterrupt:
                    interruption = 'operator_interrupted'
                finally:
                    iterator.close()
                if interruption:
                    budget = interrupted_budget(budget, interruption)
                reason = budget.get('stop_reason') or interruption or 'worker_stopped_before_case'
                while len(rows) < len(ordered):
                    save_observation(ordered[len(rows)], not_evaluated(reason), raw)
            config['run_status'] = ('INTERRUPTED' if interruption or not completed else
                'STOPPED' if budget.get('stop_reason') else
                'PARTIAL_EVALUATION' if any(not r['score']['eligible'] for r in rows) else 'COMPLETED')
        finally:
            iterator.close()
            config['finished_at'] = datetime.now(timezone.utc).isoformat()
            config['wall_seconds'] = time.perf_counter() - start
            config['processed_count'] = len(rows)
            if config['run_status'] == 'RUNNING':
                config['run_status'] = 'INTERRUPTED'
                budget = interrupted_budget(budget, 'parent_interrupted')
            metrics = summarize(rows)
            metrics['latency_ms'].update({k: latency([r['actual']['latency'].get(k) for r in rows])
                                         for k in ('gateway_ms', 'transport_ms')})
            metrics.update(observed_safety=safety, budget=budget, provider_errors=dict(provider_errors),
                projection_statuses=dict(Counter(r['actual']['projection_status'] for r in rows)),
                supported_cohort='not_calculated_for_personal_agent')
            durable_json(folder / 'agent_config.json', config)
            durable_json(folder / 'budget.json', budget)
            durable_json(folder / 'agent_metrics.json', metrics)
            durable_json(folder / 'progress.json', {'processed': len(rows), 'selected': len(ordered), 'run_status': config['run_status']})
            write_summary(folder, config, rows, metrics, budget)
    return folder, metrics, config['run_status']
