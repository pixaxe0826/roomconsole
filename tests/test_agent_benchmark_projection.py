"""Synthetic observations only; no external 250-case suite or paid model calls."""
from copy import deepcopy

import pytest

from benchmarks.agent_projection import AGENT_PROJECTION_VERSION, project_agent
from benchmarks.scoring import score


def observation(widget='todo', action='list', *, args=None, target=None,
                run_status='succeeded', response_status='success', error=None,
                entered=True, read_only=True):
    request = {'protocol_version': '1.0', 'request_id': 'synthetic:step:1',
               'widget': widget, 'action': action, 'args': args or {}}
    if target is not None:
        request['target'] = target
    response = {'status': response_status, 'data': {'count': 0, 'items': []}, 'error': error}
    call = {'request': request, 'response': response}
    event = {'request': deepcopy(request), 'read_only': read_only, 'phase': 'action',
             'entered': True, 'changed': not read_only, 'result': deepcopy(response['data'])}
    return {
        'mode': 'full',
        'agent_run': {'status': run_status, 'tier_used': 'tier0_local_rule',
                      'summary_text': 'Synthetic result',
                      'plan': {'intent_type': 'action_plan', 'steps': [{'domain': widget, 'action': action}]}},
        'registry_calls': [call], 'adapter_calls': [event] if entered else [],
        'business_state_changed': entered and not read_only,
        'harness_error': None, 'llm': {'attempts': []}, 'latency': {'total_ms': 1.0},
    }


def gold(capability='todo.list', *, slots=None, policy='EXECUTE', status='success', execute=True):
    return {'expected': {'capability': capability, 'domain': capability.split('.')[0],
                         'slots': slots or {}, 'policy': policy, 'status': status,
                         'adapter_should_execute': execute}}


def test_single_read_projects_exact_canonical_slots_without_mutating_observation():
    source = observation(args={'start': '2040-04-06', 'end': '2040-04-06', 'status': 'pending'})
    before = deepcopy(source)
    actual = project_agent(source, {})
    assert source == before
    assert actual['projection_version'] == AGENT_PROJECTION_VERSION
    assert actual['projection_status'] == 'projected'
    assert actual['capability'] == 'todo.list'
    assert actual['slots'] == {'start': '2040-04-06', 'end': '2040-04-06', 'status': 'pending',
                               'start_date': '2040-04-06', 'end_date': '2040-04-06', 'date': '2040-04-06'}
    assert actual['route'] == 'tier0_local_rule'
    assert actual['route_confidence'] is None
    assert actual['adapter_executed'] and not actual['state_changed']
    assert score(gold(slots={'date': '2040-04-06'}), actual, {})['task_success'] is True
    actual['native_plan']['steps'][0]['action'] = 'do-not-mutate-source'
    assert source == before


def test_saved_external_representation_aliases_are_applied_unchanged():
    source = observation('memo', 'read', target={'type': 'item_id', 'value': 'synthetic-note'})
    config = {'capability_aliases': {'memo.read': 'memo.get'}, 'target_slots': {'memo': 'memo_id'}}
    actual = project_agent(source, config)
    assert actual['native_capability'] == 'memo.read'
    assert actual['capability'] == 'memo.get'
    assert actual['slots']['memo_id'] == 'synthetic-note'


def test_confirmation_preview_does_not_become_executed_action():
    source = observation('todo', 'add', args={'title': 'Generated test item', 'date': '2040-04-06'},
                         run_status='awaiting_confirmation', response_status='needs_confirmation',
                         error={'code': 'CONFIRMATION_REQUIRED'}, entered=False, read_only=False)
    actual = project_agent(source, {})
    assert actual['projection_status'] == 'projected'
    assert actual['capability'] == 'todo.add'
    assert actual['policy'] == 'CONFIRM'
    assert actual['status'] == 'needs_confirmation'
    assert not actual['adapter_executed'] and not actual['state_changed']
    assert actual['actions'] == []
    verdict = score(gold('todo.add', slots={'date': '2040-04-06'}, policy='CONFIRM',
                         status='needs_confirmation', execute=False), actual, {})
    assert verdict['task_success'] is True
    assert verdict['false_execution'] is False


@pytest.mark.parametrize(('response_status', 'code'), [
    ('invalid_request', 'INVALID_ARGUMENT'),
    ('invalid_request', 'UNSUPPORTED_ACTION'),
    ('permission_denied', 'PERMISSION_DENIED'),
    ('needs_clarification', 'MISSING_REQUIRED_ARGUMENT'),
    ('needs_confirmation', 'SOME_OTHER_ERROR'),
])
def test_rejected_request_is_not_promoted_to_validated_intent(response_status, code):
    source = observation('todo', 'add', run_status='needs_clarification',
                         response_status=response_status, error={'code': code}, entered=False)
    source['agent_run']['plan']['steps'][0]['title'] = 'Unvalidated model text'
    actual = project_agent(source, {})
    assert actual['projection_status'] == 'unvalidated_request'
    assert actual['capability'] is None and actual['slots'] == {}
    assert actual['validation_error'] == code
    assert actual['native_plan']['steps'][0]['title'] == 'Unvalidated model text'
    verdict = score(gold('todo.add'), actual, {})
    assert verdict['eligible'] is True
    assert verdict['mode_success'] is False


def test_adapter_failure_retains_validated_request_without_success_claim():
    source = observation('todo', 'get', target={'type': 'item_id', 'value': 'synthetic-missing'},
                         run_status='needs_clarification', response_status='not_found',
                         error={'code': 'TASK_NOT_FOUND'})
    actual = project_agent(source, {'target_slots': {'todo': 'task_id'}})
    assert actual['projection_status'] == 'projected'
    assert actual['capability'] == 'todo.get'
    assert actual['slots']['task_id'] == 'synthetic-missing'
    assert actual['status'] == 'needs_clarification'
    assert actual['widget_response']['error']['code'] == 'TASK_NOT_FOUND'
    assert score(gold('todo.get'), actual, {})['task_success'] is False


def test_multi_step_plan_keeps_all_actions_but_never_selects_first_or_matching():
    source = observation()
    extra = observation('memo', 'read')
    extra['registry_calls'][0]['request']['request_id'] = 'synthetic:step:2'
    extra['adapter_calls'][0]['request']['request_id'] = 'synthetic:step:2'
    source['agent_run']['plan']['steps'].extend(extra['agent_run']['plan']['steps'])
    source['registry_calls'].extend(extra['registry_calls'])
    source['adapter_calls'].extend(extra['adapter_calls'])
    actual = project_agent(source, {})
    assert actual['projection_status'] == 'multiple_actions'
    assert actual['planned_action_count'] == 2
    assert actual['capability'] is None and actual['slots'] == {}
    assert [a['capability'] for a in actual['actions']] == ['todo.list', 'memo.read']
    assert len(actual['native_plan']['steps']) == 2
    for capability in ('todo.list', 'memo.read'):
        verdict = score(gold(capability), actual, {})
        assert verdict['eligible'] is True and verdict['task_success'] is False


def test_multi_step_partial_validation_is_not_reduced_to_only_observed_step():
    source = observation()
    source['agent_run']['plan']['steps'].append({'domain': 'memo', 'action': 'invented'})
    source['agent_run']['status'] = 'needs_clarification'
    actual = project_agent(source, {})
    assert actual['projection_status'] == 'multiple_actions'
    assert actual['capability'] is None
    assert actual['adapter_executed'] is True
    assert score(gold(), actual, {})['eligible'] is True


def test_empty_plan_clarification_does_not_infer_capability_from_text():
    source = observation()
    source['agent_run'].update(status='needs_clarification', summary_text='todo.list looks plausible but is not evidence')
    source['agent_run']['plan'] = {'intent_type': 'clarification', 'steps': []}
    source['registry_calls'] = []
    source['adapter_calls'] = []
    actual = project_agent(source, {})
    assert actual['projection_status'] == 'no_actions'
    assert actual['capability'] is None and actual['slots'] == {}
    assert actual['policy'] == 'CLARIFY'
    verdict = score(gold(policy='CLARIFY', status='needs_clarification', execute=False), actual, {})
    assert verdict['policy_ok'] is True
    assert verdict['eligible'] is True and verdict['task_success'] is False


def test_no_step_briefing_does_not_choose_one_of_its_native_reads():
    source = observation()
    source['agent_run']['plan'] = {'intent_type': 'briefing', 'steps': []}
    actual = project_agent(source, {})
    assert actual['projection_status'] == 'no_actions'
    assert actual['capability'] is None
    assert actual['adapter_executed'] is True


def test_extra_registry_request_is_not_hidden_by_single_plan_step():
    source = observation()
    extra = observation('memo', 'read')['registry_calls'][0]
    source['registry_calls'].append(extra)
    actual = project_agent(source, {})
    assert actual['projection_status'] == 'ambiguous_requests'
    assert actual['observed_request_count'] == 2
    assert actual['capability'] is None


def test_extra_adapter_without_registry_record_preserves_safety_evidence():
    source = observation()
    source['adapter_calls'].extend(observation('timer', 'stop', read_only=False)['adapter_calls'])
    source['business_state_changed'] = True
    actual = project_agent(source, {})
    assert actual['projection_status'] == 'ambiguous_requests'
    assert len(actual['actions']) == 2
    verdict = score(gold(), actual, {})
    assert verdict['eligible'] is True
    assert verdict['false_execution'] is True


def test_timer_record_is_counted_once_despite_duplicate_native_preview_lists():
    source = observation('timer', 'start', args={'duration_seconds': 72}, read_only=False)
    control = {'domain': 'timer', 'action': 'start', 'step': 1, 'data': {'duration_seconds': 72}}
    source['agent_run']['preview'] = {'reads_executed': [control], 'immediate_controls_executed': [control]}
    actual = project_agent(source, {})
    assert len(actual['actions']) == 1
    verdict = score(gold('timer.start', slots={'duration_seconds': 72}), actual, {})
    assert verdict['task_success'] is True and verdict['false_execution'] is False


def test_exact_repeat_of_identical_request_does_not_create_competing_projection():
    source = observation()
    source['registry_calls'].append(deepcopy(source['registry_calls'][0]))
    actual = project_agent(source, {})
    assert actual['projection_status'] == 'projected'
    assert actual['observed_request_count'] == 1
    assert len(actual['actions']) == 1


def test_reference_target_is_not_changed_into_invented_entity_id():
    source = observation('timer', 'get', target={'type': 'reference', 'value': 'current'})
    actual = project_agent(source, {'target_slots': {'timer': 'timer_id'}})
    assert actual['projection_status'] == 'projected'
    assert 'timer_id' not in actual['slots']


def test_actual_response_entity_id_can_fill_existing_projection_target_slot():
    source = observation('timer', 'get', target={'type': 'reference', 'value': 'current'})
    source['registry_calls'][0]['response']['data'] = {'id': 'synthetic-running-timer'}
    actual = project_agent(source, {'target_slots': {'timer': 'timer_id'}})
    assert actual['slots']['timer_id'] == 'synthetic-running-timer'


@pytest.mark.parametrize('error', ['timeout', 'http_error', 'connection_failed', 'benchmark_llm_unavailable'])
def test_provider_failure_does_not_become_successful_model_inference(error):
    source = observation()
    source['llm']['attempts'] = [{'transport_attempted': error != 'benchmark_llm_unavailable', 'error': error}]
    source['agent_run']['tier_used'] = 'tier0_fallback'
    actual = project_agent(source, {})
    verdict = score(gold(), actual, {})
    assert actual['route'] == 'tier0_fallback'
    assert verdict['eligible'] is False and verdict['task_success'] is None


def test_harness_error_keeps_not_evaluated_without_manufacturing_native_plan():
    source = {'mode': 'full', 'agent_run': None, 'harness_error': 'Synthetic harness fault',
              'registry_calls': [], 'adapter_calls': [], 'llm': {'attempts': []}}
    actual = project_agent(source, {})
    assert actual['projection_status'] == 'missing_plan'
    assert actual['capability'] is None
    assert score(gold(), actual, {})['eligible'] is False


@pytest.mark.parametrize('error', ['invalid_json', 'model_refusal', 'empty_output', 'incomplete_output', 'unknown_cost'])
def test_native_response_or_usage_errors_do_not_disappear_from_denominator(error):
    source = observation()
    source['agent_run'].update(status='needs_clarification', tier_used='tier0_fallback')
    source['agent_run']['plan'] = {'intent_type': 'clarification', 'steps': []}
    source['registry_calls'] = []
    source['adapter_calls'] = []
    source['llm']['attempts'] = [{'transport_attempted': True, 'error': error, 'native_error_code': error}]
    verdict = score(gold(), project_agent(source, {}), {})
    assert verdict['eligible'] is True
    assert verdict['task_success'] is False


@pytest.mark.parametrize(('text', 'capability', 'slots', 'policy', 'status', 'execute'), [
    ('오늘 할 일 알려줘', 'todo.list', {'date': '2028-02-09'}, 'EXECUTE', 'success', True),
    ('내일 할 일에 합성 포장 검수 추가해', 'todo.add', {'date': '2028-02-10'}, 'CONFIRM', 'needs_confirmation', False),
    ('37초 타이머 시작', 'timer.start', {'duration_seconds': 37}, 'EXECUTE', 'success', True),
])
def test_projection_accepts_real_isolated_production_runtime_observation(text, capability, slots, policy, status, execute):
    from test_personal_agent_benchmark_runtime import run_text

    observed = run_text(text)
    assert observed['harness_error'] is None
    actual = project_agent(observed, {})
    verdict = score(gold(capability, slots=slots, policy=policy, status=status, execute=execute), actual, {})
    assert actual['projection_status'] == 'projected'
    assert verdict['eligible'] is True and verdict['task_success'] is True
    assert verdict['false_execution'] is False


def test_real_multi_step_production_runtime_cannot_pass_single_action_gold():
    from test_personal_agent_benchmark_runtime import DAY, fake_provider, plan, run_text

    native = plan({'domain': 'todo', 'action': 'list', 'date': DAY}, {'domain': 'clock', 'action': 'read'})
    observed = run_text('오늘 할 일과 현재 시각을 확인해', config={'agent_mode': 'fast'},
                        transport=fake_provider(native))
    assert observed['harness_error'] is None
    actual = project_agent(observed, {})
    assert actual['native_plan'] == native
    assert len(actual['actions']) == 2
    assert actual['projection_status'] == 'multiple_actions'
    verdict = score(gold(), actual, {})
    assert verdict['eligible'] is True and verdict['task_success'] is False


@pytest.mark.parametrize('mode', ['nlu', 'decision'])
def test_other_boundaries_cannot_be_mislabelled_as_full(mode):
    source = observation()
    source['mode'] = mode
    with pytest.raises(ValueError, match='full mode only'):
        project_agent(source, {})
