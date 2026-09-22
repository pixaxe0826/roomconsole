"""Evidence-labelled diagnostics over frozen observations; never re-score or infer text.

A primary label is the first *observed* blocker, not proof of a unique root cause.
All original score flags survive, including safety flags on unsupported requests.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import json
import hashlib
from pathlib import Path

from .scoring import aggregate, latency, rate
from .dataset import fingerprint, stable, reject_duplicate_keys

TAXONOMY_VERSION = '1.0.0'
TEMPORAL = frozenset({'date', 'time', 'start', 'end', 'start_date', 'end_date', 'new_date',
                     'new_time', 'new_start', 'new_end', 'period', 'relative_minutes',
                     'offset_minutes', 'requested_date', 'requested_time'})
ENTITIES = frozenset({'task_id', 'task_ids', 'event_id', 'memo_id', 'memo_ids', 'alarm_id',
                     'timer_id', 'widget_id', 'matches'})
CONTEXT = frozenset({'reference', 'pending_action', 'resolves_pending', 'cancels_pending',
                    'cancel_scope', 'position'})
PROPOSAL_ERRORS = frozenset({'INCOMPLETE_PROPOSAL', 'INVALID_PROPOSAL', 'INTENT_UNRESOLVED',
                            'UNSUPPORTED_ACTION', 'UNTRUSTED_ARGUMENT'})
PRIMARY_ORDER = (
    'UNSUPPORTED_CAPABILITY', 'ROUTING_FAILURE', 'LLM_PROPOSAL_FAILURE', 'CAPABILITY_FAILURE',
    'CONTEXT_FAILURE', 'TEMPORAL_FAILURE', 'ENTITY_GROUNDING_FAILURE', 'SLOT_EXTRACTION_FAILURE',
    'POLICY_FAILURE', 'ADAPTER_FAILURE', 'RESPONSE_ASSERTION_FAILURE', 'UNDETERMINED',
)


def _model_proposal(observation):
    widget = (observation.get('record') or {}).get('widget_trace') or {}
    value = widget.get('parsed_proposal')
    if isinstance(value, dict):
        return value, 'record.widget_trace.parsed_proposal'
    # Decode only the already saved provider envelope. No repair, regular-expression
    # extraction, code execution, new parser call or model invocation is allowed.
    calls = (observation.get('llm') or {}).get('attempts') or []
    for index in range(len(calls) - 1, -1, -1):
        call = calls[index]
        if not call.get('transport_attempted'):
            continue
        try:
            raw = call.get('response_raw')
            def decode(text):
                return json.loads(text, object_pairs_hook=reject_duplicate_keys,
                                  parse_constant=lambda v: (_ for _ in ()).throw(ValueError('Nonfinite proposal')))
            envelope = decode(raw) if isinstance(raw, str) else raw
            value = decode(envelope['choices'][0]['message']['content'])
            if isinstance(value, dict):
                return value, f'llm.attempts[{index}].response_raw'
        except (ValueError, TypeError, KeyError, IndexError, RecursionError):
            pass
    return None, None


def _name(proposal):
    if not isinstance(proposal, dict):
        return None
    if isinstance(proposal.get('widget'), str) and isinstance(proposal.get('action'), str):
        return proposal['widget'] + '.' + proposal['action']
    return proposal.get('intent') if isinstance(proposal.get('intent'), str) else None


def _excludes(schema, value):
    """Prove only simple literal exclusions from the recorded pre-model schema."""
    if not isinstance(schema, dict):
        return False
    if schema.get('type') == 'null':
        return value is not None
    if 'const' in schema:
        return stable(schema['const']) != stable(value)
    if 'enum' in schema:
        return stable(value) not in {stable(v) for v in schema['enum']}
    branches = schema.get('anyOf') or schema.get('oneOf')
    return bool(branches) and all(_excludes(branch, value) for branch in branches)


def classify(row, observation, support):
    actual, scores, expected = row['actual'], row['score'], row['expected']
    contract = support.lookup(expected['capability'])
    mismatches = sorted(k for k, matched in scores.get('slot_matches', {}).items() if matched is False)
    critical_keys = (row.get('scoring_context') or {}).get('critical_slots')
    if critical_keys is not None:
        mismatches = [k for k in mismatches if k in critical_keys]
    if 'critical_slot_error' not in scores['failures']:
        mismatches = []  # Advisory slot differences are not a critical-slot blocker.
    diagnosis = {
        'case_id': row['case_id'], 'parent_id': row.get('parent_id'),
        'domain': row['domain'], 'source_type': row['source_type'], 'category': row['category'],
        'support': contract, 'primary_cause': None, 'secondary_flags': deepcopy(scores['failures']),
        'diagnostic_flags': [], 'slot_mismatches': mismatches, 'evidence': [],
        'evidence_level': 'result', 'trace_available': observation is not None,
        'original_score': deepcopy(scores),
    }
    if not scores['eligible']:
        diagnosis['status'] = 'NOT_EVALUATED'
        diagnosis['not_evaluated_reason'] = ('HARNESS_ERROR' if actual.get('harness_error') else
            'LLM_UNAVAILABLE' if actual.get('llm_blocked') else 'INFERENCE_FAILED')
        return diagnosis
    if scores['mode_success']:
        diagnosis['status'] = 'SUCCESS'
        if contract['status'] != 'SUPPORTED':
            diagnosis['diagnostic_flags'].append('legacy_success_outside_declared_contract')
        return diagnosis
    diagnosis['status'] = 'FAILURE'

    def choose(primary, path, reason, level='result'):
        diagnosis.update(primary_cause=primary, evidence_level=level)
        diagnosis['evidence'].append({'path': path, 'reason': reason})
        return diagnosis

    if contract['status'] != 'SUPPORTED':
        return choose('UNSUPPORTED_CAPABILITY', 'support_snapshot.operations', contract['reason'], 'contract')

    obs = observation or {}
    record = obs.get('record') or {}
    trace = record.get('widget_trace') or {}
    route = record.get('routing') or {}
    proposal, proposal_path = _model_proposal(obs)
    transported = any(c.get('transport_attempted') for c in (obs.get('llm') or {}).get('attempts', []))
    code = actual.get('validation_error') or trace.get('validation_error') or obs.get('error_code')
    response = trace.get('widget_response') or {}
    error = response.get('error') or {}
    code = code or error.get('code')
    choices = trace.get('available_capabilities')
    wanted = expected['capability']

    # Not receiving an expected candidate is a pre-model route/contract boundary,
    # not evidence that a model ignored an action it had actually been offered.
    if (scores['capability_ok'] is False and isinstance(choices, list) and choices and
            not any(support.equivalent(v, wanted) for v in choices)):
        return choose('ROUTING_FAILURE', 'record.widget_trace.available_capabilities',
                      'Expected operation absent from recorded candidate set; no claim this guard is unsafe.', 'trace')
    if scores['capability_ok'] is False and not actual['llm_called'] and record:
        if route.get('route') in {'FAST_PATH', 'EXISTING_RULE'} or route.get('route_reason'):
            return choose('ROUTING_FAILURE', 'record.routing',
                          'Non-model route/guard produced a different or unresolved operation.', 'trace')

    # A forced null/enum in a pre-model grammar may explain a temporal mismatch.
    # Do not call this model failure. The original mismatch is still a FAIL.
    schema = trace.get('proposal_schema') or {}
    for branch_index, branch in enumerate(schema.get('anyOf') or []):
        props = branch.get('properties') or {}
        native = '.'.join(str(props.get(k, {}).get('const', '')) for k in ('widget', 'action'))
        if not support.equivalent(native, wanted):
            continue
        arg_props = (props.get('args') or {}).get('properties') or {}
        for key in mismatches:
            native_key = {'start_date': 'start', 'end_date': 'end'}.get(key, key)
            if key in TEMPORAL and _excludes(arg_props.get(native_key), expected['slots'][key]):
                diagnosis['diagnostic_flags'].append('pre_model_temporal_constraint')
                return choose('TEMPORAL_FAILURE',
                              f'record.widget_trace.proposal_schema.anyOf[{branch_index}].properties.args.properties.{native_key}',
                              'Recorded grammar excludes the expected temporal literal; cause precedes model output.', 'trace')

    if transported and code in PROPOSAL_ERRORS:
        return choose('LLM_PROPOSAL_FAILURE', 'record.widget_trace.validation_error',
                      'Transported proposal rejected at the recorded structure/allowed-action boundary: ' + code, 'trace')
    if scores['capability_ok'] is False:
        if transported and _name(proposal) and not support.equivalent(_name(proposal), wanted):
            return choose('LLM_PROPOSAL_FAILURE', proposal_path,
                          'Saved model proposal names a different operation.', 'trace')
        return choose('CAPABILITY_FAILURE', 'actual.capability',
                      'Operation mismatch is observed; origin cannot be proven from available trace.')

    # A correct raw/model field lost between stages is not proof of bad extraction.
    # Diagnose its observation gap without silently fixing the frozen score.
    raw_slots = proposal.get('args', proposal) if isinstance(proposal, dict) else {}
    if isinstance(raw_slots, dict):
        for key in mismatches:
            if key in raw_slots and stable(raw_slots[key]) == stable(expected['slots'][key]):
                diagnosis['diagnostic_flags'].append('correct_model_value_not_in_final_slots:' + key)
    for key in mismatches:
        if key == 'text' and 'text' not in actual['slots'] and actual['slots'].get('title') == expected['slots'].get(key):
            diagnosis['diagnostic_flags'].append('possible_title_text_representation_gap')
    resolved = trace.get('resolved_target') or {}
    if (any(key in ENTITIES for key in mismatches) and resolved.get('id') is not None and
            any(resolved['id'] == expected['slots'][key] for key in mismatches if key in ENTITIES)):
        diagnosis['diagnostic_flags'].append('resolved_id_not_in_scored_slots')
    if diagnosis['diagnostic_flags'] and mismatches:
        return choose('UNDETERMINED', proposal_path or 'actual.slots / record.widget_trace.resolved_target',
                      'Representation/stage evidence is incomplete; inspect projection before blaming NLU.', 'insufficient')

    if mismatches:
        if any(k in CONTEXT for k in mismatches):
            return choose('CONTEXT_FAILURE', 'score.slot_matches',
                          'Context/reference fields mismatch (symptom, not proof of a session implementation bug).')
        if any(k in TEMPORAL for k in mismatches):
            return choose('TEMPORAL_FAILURE', 'score.slot_matches',
                          'Temporal field mismatch; input parsing versus grounding needs trace inspection.')
        if any(k in ENTITIES for k in mismatches):
            return choose('ENTITY_GROUNDING_FAILURE', 'score.slot_matches',
                          'Server-resolved identity/result fields mismatch; not necessarily model slots.')
        return choose('SLOT_EXTRACTION_FAILURE', 'score.slot_matches',
                      'Other required value mismatches; original critical-slot rules are unchanged.')
    if scores.get('policy_ok') is False:
        return choose('POLICY_FAILURE', 'actual.policy',
                      'Operation and scored slots match but the expected execution/confirmation decision differs.')
    action_calls = [c for c in obs.get('adapter_calls', []) if c.get('phase') == 'action']
    if action_calls and any(f in scores['failures'] for f in
                            ('wrong_final_status', 'wrong_execution_boundary', 'wrong_actions')):
        return choose('ADAPTER_FAILURE', 'adapter_calls / record.widget_trace.widget_response',
                      'Action boundary was entered and the recorded outcome differs.', 'trace')
    if 'response_fact_error' in scores['failures']:
        return choose('RESPONSE_ASSERTION_FAILURE', 'score.response_assertions',
                      'An explicit response assertion failed; no judgement of all response prose.')
    return choose('UNDETERMINED', 'score.failures',
                  'No supported stage-level cause can be established from preserved evidence.', 'insufficient')


def diagnostics_metrics(rows, diagnoses):
    """All numerators use saved scores; never call score() or project() here."""
    accepted = {d['case_id'] for d in diagnoses if d['support']['status'] == 'SUPPORTED'}
    supported = [r for r in rows if r['case_id'] in accepted]
    failures = [d for d in diagnoses if d['status'] == 'FAILURE']
    overall = aggregate(rows)
    subset = aggregate(supported)
    primary = dict(sorted(Counter(d['primary_cause'] for d in failures).items()))
    if sum(primary.values()) != len(failures):
        raise ValueError('Primary taxonomy must partition evaluated failures')
    if subset['mode_success']['correct'] > overall['mode_success']['correct']:
        raise ValueError('Supported success cannot exceed overall success')
    by_domain = {}
    by_route = {}
    for name in sorted({r['domain'] for r in supported}):
        by_domain[name] = aggregate([r for r in supported if r['domain'] == name])
    for name in sorted({r['actual']['route'] for r in supported}):
        by_route[name] = aggregate([r for r in supported if r['actual']['route'] == name])
    groups = defaultdict(list)
    for row in rows:
        groups[row.get('parent_id') or row['case_id']].append(row)
    group_success, consistency, excluded = [], [], 0
    for members in groups.values():
        if len(members) < 2:
            continue
        if not all(r['case_id'] in accepted and r['score']['eligible'] for r in members):
            excluded += 1
            continue
        group_success.append(all(r['score']['mode_success'] for r in members))
        consistency.append(len({stable({k: r['actual'][k] for k in ('capability', 'slots', 'policy', 'status')})
                                for r in members}) == 1)
    eligible = [r for r in supported if r['score']['eligible']]
    clarified = [r for r in eligible if r['actual']['policy'] == 'CLARIFY']
    unneeded = [r for r in eligible if r['expected']['policy'] != 'CLARIFY']
    def group_slot(keys):
        return rate([matched for r in eligible for key, matched in r['score']['slot_matches'].items() if key in keys])
    return {
        'overall_preserved': overall,
        'support': {'processed': len(rows), 'supported': len(supported), 'unsupported': len(rows)-len(supported),
                    'coverage': len(supported)/len(rows) if rows else None,
                    'supported_evaluated': subset['evaluated'],
                    'unsupported_breakdown': dict(sorted(Counter(
                        d['support']['expected_capability'] for d in diagnoses if d['support']['status'] != 'SUPPORTED').items()))},
        'supported': subset,
        'supported_by_source_type': {s: aggregate([r for r in supported if r['source_type'] == s])
                                     for s in ('unique', 'paraphrase')},
        'supported_by_domain': by_domain, 'supported_by_route': by_route,
        'primary_failure_counts': primary,
        'outcomes': dict(sorted(Counter(d['status'] for d in diagnoses).items())),
        'secondary_flag_counts': dict(sorted(Counter(f for d in diagnoses for f in d['secondary_flags']).items())),
        'trace_coverage': rate([d['trace_available'] for d in diagnoses]),
        'supported_group_success': rate(group_success), 'supported_group_consistency': rate(consistency),
        'excluded_groups_with_unsupported_or_unevaluated_members': excluded,
        'supported_clarification_precision': rate([r['expected']['policy'] == 'CLARIFY' for r in clarified]),
        'supported_unnecessary_clarification_rate': rate([r['actual']['policy'] == 'CLARIFY' for r in unneeded]),
        'supported_slots_given_correct_capability': rate([v for r in eligible if r['score']['capability_ok']
                                                         for v in r['score']['slot_matches'].values()]),
        'supported_temporal_slots': group_slot(TEMPORAL), 'supported_entity_slots': group_slot(ENTITIES),
        'llm_call_case_latency_ms': latency([r['actual']['latency'].get('llm_ms') for r in rows if r['actual']['llm_called']]),
        'no_llm_case_total_latency_ms': latency([r['actual']['latency'].get('total_ms') for r in rows if not r['actual']['llm_called']]),
        'interpretation': [
            'Supported means exposed operation metadata, not every argument, synonym, fixture or UI capability.',
            'Primary cause is one evidence-labelled diagnostic per evaluated failure; symptoms can overlap.',
            'Saved scores including null/missing-slot semantics are not corrected by diagnostics.',
            'Supported clarification precision needs both false and true clarification outcomes; recall alone is insufficient.',
            'Group metrics keep entire selected groups; unsupported/unevaluated members are not silently dropped.',
            'Safety denominators and latency scopes stay those of the original run; zero observed writes is not a guarantee.',
        ],
    }


def rules_hash():
    return fingerprint({'version': TAXONOMY_VERSION, 'sources': {
        name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
        for name in ('taxonomy.py', 'support.py')}})
