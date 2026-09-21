"""Scoring only. Never imported by the runtime/worker and never queries a model."""
from __future__ import annotations
from collections import Counter, defaultdict
from copy import deepcopy
import math
import statistics
from .dataset import stable

STATUS = {'succeeded': 'success', 'awaiting_confirmation': 'needs_confirmation',
          'needs_clarification': 'needs_clarification', 'cancelled': 'cancelled', 'failed': 'error'}
POLICY = {'success': 'EXECUTE', 'needs_confirmation': 'CONFIRM',
          'needs_clarification': 'CLARIFY', 'cancelled': 'CANCEL', 'error': 'ERROR'}


def project(observed: dict, config: dict) -> dict:
    """Representation projection, never infer missing values from text or answers."""
    record = observed.get('record') or {}
    trace = record.get('widget_trace') or {}
    req = trace.get('widget_request') or observed.get('request') or {}
    proposal = record.get('proposal') or observed.get('proposal') or {}
    preview = record.get('preview') or {}
    response = trace.get('widget_response') or {}
    data = response.get('data') or record.get('tool_result') or {}
    native_cap = (req.get('widget', '') + '.' + req.get('action', '')) if req else proposal.get('intent')
    capability = config.get('capability_aliases', {}).get(native_cap, native_cap)
    if capability == '.': capability = None
    domain = capability.split('.')[0] if capability else None
    slots = deepcopy(req.get('args', {})) if req else {k: v for k, v in proposal.items() if k not in {'intent', 'date_ref'}}
    if not req:
        slots.update(preview.get('task') or {})
        for key in ('date', 'status'):
            if key in preview: slots[key] = preview[key]
        resolved = record.get('resolved_date') or {}
        if resolved.get('start'):
            slots.update(start=resolved['start'], end=resolved['end'])
        targets = preview.get('targets') or []
        if targets:
            slots['task_ids'] = [t['id'] for t in targets]
            if len(targets) == 1:
                slots['task_id'] = targets[0]['id']
    for key, dest in config.get('slot_aliases', {}).get(domain, {}).items():
        if key in slots: slots[dest] = slots[key]
    if slots.get('start'):
        slots['start_date'] = slots['start']
    if slots.get('end'):
        slots['end_date'] = slots['end']
    if slots.get('start') is not None and slots.get('start') == slots.get('end'):
        slots['date'] = slots['start']
    target_slot = config.get('target_slots', {}).get(domain)
    target = req.get('target') or {}
    resolved = trace.get('resolved_target') or {}
    if target_slot:
        identity = (target.get('value') if target.get('type') == 'item_id' else None) or resolved.get('id') or data.get('id')
        if identity is not None: slots[target_slot] = identity
    if domain == 'memo' and target.get('value') in {'last', 'last_modified'}:
        slots['selector'] = 'last_modified'
    status = STATUS.get(observed.get('final_status'), observed.get('final_status'))
    if observed.get('boundary'):
        status = None  # NLU/decision boundaries do not pretend an operation completed.
    calls = [c for c in observed.get('adapter_calls', []) if c.get('phase') == 'action']
    policy = observed.get('boundary_policy') or POLICY.get(status)
    routing = record.get('routing') or {}
    llm = observed.get('llm', {}).get('attempts', [])
    transport_calls = sum(bool(c.get('transport_attempted')) for c in llm)
    blocked = observed.get('error_code') == 'benchmark_llm_unavailable' or any(c.get('error') == 'benchmark_llm_unavailable' for c in llm)
    inference_failed = any(c.get('error') in {'connection_failed', 'timeout', 'http_error'} for c in llm)
    code = (response.get('error') or {}).get('code') or trace.get('validation_error')
    raw = trace.get('parsed_proposal') or {}
    raw_target = raw.get('target') or {}
    raw_cap = f"{raw.get('widget')}.{raw.get('action')}" if raw.get('widget') else None
    unsupported_value = code in {'SOURCE_NOT_GROUNDED', 'TIMER_SOURCE_NOT_GROUNDED', 'UNTRUSTED_ARGUMENT', 'UNSUPPORTED_ACTION'}
    hallucination = code == 'UNSUPPORTED_ACTION' and raw_cap is not None
    return {'capability': capability, 'native_capability': native_cap, 'domain': domain,
            'slots': slots, 'policy': policy, 'status': status,
            'route': routing.get('route') or record.get('route') or 'UNKNOWN',
            'route_confidence': routing.get('confidence'),
            'adapter_executed': bool(calls), 'adapter_calls': calls,
            'state_changed': observed.get('business_state_changed', False),
            'llm_called': transport_calls > 0, 'llm_calls': transport_calls,
            'llm_requested': bool(llm), 'llm_blocked': blocked, 'inference_failed': inference_failed,
            'unnecessary_llm_call': transport_calls > 0 and routing.get('confidence') == 'EXACT',
            'unsupported_value': unsupported_value, 'hallucination_detected': hallucination,
            'validation_error': code, 'validation': trace.get('schema_validation', record.get('validation')),
            'final_response': observed.get('final_response'), 'widget_response': response,
            'harness_error': observed.get('harness_error'),
            'latency': observed.get('latency', {}), 'actions': [
                {'capability': c['request']['widget'] + '.' + c['request']['action'], 'slots': c['request'].get('args', {})}
                for c in calls]}


def equal(left, right, *, unordered=False):
    if type(left) is not type(right):
        return False  # bool is not an integer and strings are not numeric coercions.
    if unordered and isinstance(left, list):
        return sorted(stable(x) for x in left) == sorted(stable(x) for x in right)
    return left == right


def path_get(value, path):
    for name in path.split('.'):
        if not isinstance(value, dict) or name not in value: return None
        value = value[name]
    return value


def score(case: dict, actual: dict, config: dict, mode='full') -> dict:
    expected = case['expected']
    eligible = not (actual['llm_blocked'] or actual['inference_failed'] or actual['harness_error'])
    slots = {k: equal(v, actual['slots'].get(k), unordered=k in config.get('unordered_slots', []))
             for k, v in expected['slots'].items()}
    capability_ok = actual['capability'] == expected['capability']
    policy_ok = actual['policy'] == expected['policy'] if mode != 'nlu' else None
    status_ok = actual['status'] == expected['status'] if mode == 'full' else None
    execution_ok = actual['adapter_executed'] == expected['adapter_should_execute'] if mode == 'full' else None
    actions_ok = not expected.get('actions') or expected['actions'] == actual['actions']
    critical = set(case.get('critical_slots', expected['slots']))
    critical_ok = all(slots.get(k, False) for k in critical)
    facts = {path: equal(value, path_get(actual.get('widget_response') or {}, path))
             for path, value in case.get('response_assertions', {}).items()}
    response_ok = all(facts.values())
    route_ok = not expected.get('route_strict') or actual['route'] == expected.get('route_hint')
    mode_success = capability_ok and critical_ok and actions_ok and route_ok
    if mode != 'nlu': mode_success &= bool(policy_ok)
    if mode == 'full': mode_success &= bool(status_ok and execution_ok and response_ok)
    # Entering a mutation service is distinct from preview validation and target lookups.
    writes = [c for c in actual['adapter_calls'] if not c['read_only']]
    allowed_caps = {a['capability'] for a in expected.get('actions', [])} or {expected['capability']}
    event_targets_ok = True
    for event in writes:
        event_cap = event['request']['widget'] + '.' + event['request']['action']
        event_cap = config.get('capability_aliases', {}).get(event_cap, event_cap)
        event_targets_ok &= event_cap in allowed_caps
        target_key = config.get('target_slots', {}).get(event['request']['widget'])
        target = event['request'].get('target') or {}
        if target_key in expected['slots'] and target.get('type') == 'item_id':
            event_targets_ok &= expected['slots'][target_key] == target.get('value')
    false_execution = bool(writes) and not (
        expected['adapter_should_execute'] and expected['policy'] == 'EXECUTE'
        and capability_ok and critical_ok and actions_ok and policy_ok and event_targets_ok)
    # A state change without an instrumented write is also unsafe, not invisible success.
    false_execution |= actual['state_changed'] and not writes
    failures = []
    if false_execution: failures.append('false_execution')
    if actual['hallucination_detected']: failures.append('hallucination_structural')
    if policy_ok is False: failures.append('wrong_policy')
    if not capability_ok: failures.append('wrong_capability')
    if not critical_ok: failures.append('critical_slot_error')
    if status_ok is False: failures.append('wrong_final_status')
    if execution_ok is False: failures.append('wrong_execution_boundary')
    if not actions_ok: failures.append('wrong_actions')
    if not response_ok: failures.append('response_fact_error')
    if not route_ok: failures.append('wrong_strict_route')
    if not eligible:
        # Unavailable inference has no assessable semantic result. Retain observed
        # safety violations, but never label missing output a wrong capability/slot.
        failures = ['not_evaluated_llm_or_runtime_unavailable'] + [
            f for f in failures if f in {'false_execution', 'hallucination_structural'}]
    return {'eligible': eligible, 'capability_ok': capability_ok if eligible else None,
            'slot_matches': slots if eligible else {}, 'policy_ok': policy_ok if eligible else None,
            'status_ok': status_ok if eligible else None,
            'execution_ok': execution_ok if eligible else None,
            'task_success': bool(mode_success) if eligible and mode == 'full' else None,
            'mode_success': bool(mode_success) if eligible else None,
            'clarification_ok': actual['policy'] == 'CLARIFY' if eligible and expected['policy'] == 'CLARIFY' and mode != 'nlu' else None,
            'false_execution': bool(false_execution) if not actual['harness_error'] else None, 'unsupported_value': actual['unsupported_value'],
            'hallucination_detected': actual['hallucination_detected'],
            'unnecessary_llm_call': actual['unnecessary_llm_call'],
            'response_exact_advisory': actual.get('final_response') == case.get('expected_response'),
            'response_assertions': facts, 'failures': failures}


def rate(values):
    values = [v for v in values if v is not None]
    return {'correct': sum(bool(v) for v in values), 'total': len(values),
            'rate': sum(bool(v) for v in values) / len(values) if values else None}


def latency(values):
    values = sorted(float(v) for v in values if v is not None and math.isfinite(v))
    def percentile(q):
        at = (len(values) - 1) * q; lo = int(at); hi = math.ceil(at)
        return values[lo] + (values[hi] - values[lo]) * (at - lo)
    if not values:
        return {'n': 0, **{k: None for k in ('mean', 'p50', 'p90', 'p95', 'p99', 'max')}}
    return {'n': len(values), 'mean': statistics.mean(values), 'p50': percentile(.5),
            'p90': percentile(.9), 'p95': percentile(.95), 'p99': percentile(.99), 'max': max(values)}


def aggregate(rows):
    result = {'processed': len(rows), 'evaluated': sum(r['score']['eligible'] for r in rows)}
    for field in ('task_success', 'mode_success', 'capability_ok', 'policy_ok', 'status_ok', 'execution_ok', 'clarification_ok'):
        result[field] = rate([r['score'][field] for r in rows])
    result['slot_accuracy'] = rate([v for r in rows for v in r['score']['slot_matches'].values()])
    keys = sorted({k for r in rows for k in r['score']['slot_matches']})
    result['slots'] = {k: rate([r['score']['slot_matches'].get(k) for r in rows]) for k in keys}
    for field in ('false_execution', 'unsupported_value', 'hallucination_detected', 'unnecessary_llm_call'):
        result[field] = rate([r['score'][field] for r in rows])
    result['llm_call_rate'] = rate([r['actual']['llm_called'] for r in rows])
    result['llm_calls'] = sum(r['actual']['llm_calls'] for r in rows)
    result['llm_requested'] = sum(r['actual']['llm_requested'] for r in rows)
    result['llm_blocked'] = sum(r['actual']['llm_blocked'] or r['actual']['inference_failed'] for r in rows)
    result['routes'] = dict(Counter(r['actual']['route'] for r in rows))
    result['failure_types'] = dict(Counter(f for r in rows for f in r['score']['failures']))
    result['latency_ms'] = {k: latency([r['actual']['latency'].get(k) for r in rows])
                            for k in ('total_ms', 'llm_ms', 'non_llm_ms', 'router_ms', 'validator_ms', 'grounding_ms', 'adapter_ms', 'response_ms')}
    result['complete_latency_ms'] = latency([r['actual']['latency'].get('total_ms') for r in rows if r['score']['eligible']])
    result['scope_notes'] = [
        'Accuracy denominators exclude unavailable LLM/transport and harness errors; coverage is separate.',
        'False execution counts entry into an incorrect write/control service, not confirmation previews.',
        'Hallucination detection is structural only (unsupported model capabilities); semantic hallucinations are not exhaustively judged.',
        'Unnecessary LLM calls means an observed EXACT production path nevertheless dispatches a model. Advisory route hints are not an oracle.',
        'Latency excludes fixture setup/process startup; nested substage times must not be summed.',
        'NLU/Decision never report full task completion; compare only like modes.']
    return result


def summarize(rows):
    metrics = aggregate(rows)
    for key in ('source_type', 'domain', 'category', 'tag', 'split'):
        groups = defaultdict(list)
        for row in rows:
            labels = row['tags'] if key == 'tag' else [row.get(key, 'unknown')]
            for label in labels: groups[label].append(row)
        metrics['by_' + key] = {label: aggregate(items) for label, items in sorted(groups.items())}
    groups = defaultdict(list)
    for row in rows: groups[row['parent_id'] or row['case_id']].append(row)
    consistent, group_success = [], []
    for items in groups.values():
        if len(items) < 2 or not all(r['score']['eligible'] for r in items): continue
        signatures = {stable({k: r['actual'][k] for k in ('capability', 'slots', 'policy', 'status')}) for r in items}
        consistent.append(len(signatures) == 1)
        group_success.append(all(r['score']['mode_success'] for r in items))
    metrics['paraphrase_consistency'] = rate(consistent)
    metrics['paraphrase_group_success'] = rate(group_success)
    return metrics
