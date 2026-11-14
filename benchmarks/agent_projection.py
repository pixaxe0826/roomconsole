"""Evidence-only compatibility projection for the Personal Agent benchmark.

The production Agent can emit a multi-step plan. The frozen V1 benchmark has a
single capability/slot contract. Only one plan step with one validated canonical
WidgetRequest can populate that contract. No source-text interpretation, guessed
targets, expected answers, or first-step selection belongs here.
"""
from __future__ import annotations

from copy import deepcopy

from .dataset import stable
from .scoring import project


AGENT_PROJECTION_VERSION = 'personal-agent-single-request-v1'


def _request(call: dict) -> dict | None:
    request = call.get('request')
    if not isinstance(request, dict):
        return None
    if not all(isinstance(request.get(key), str) and request[key] for key in ('widget', 'action')):
        return None
    if not isinstance(request.get('args', {}), dict):
        return None
    return request


def _validated(call: dict, entered_requests: set[str]) -> bool:
    """Evidence from the real registry, never a second validator invocation."""
    request = _request(call)
    if request is None:
        return False
    response = call.get('response') or {}
    if not isinstance(response, dict):
        return False
    error = response.get('error') or {}
    if response.get('status') == 'success':
        return True
    if (response.get('status') == 'needs_confirmation' and isinstance(error, dict)
            and error.get('code') == 'CONFIRMATION_REQUIRED'):
        return True
    # A service can fail after schema/permission checks (e.g. missing target).
    # Entry into its actual adapter is evidence of those checks, not success.
    return stable(request) in entered_requests


def project_agent(observed: dict, projection: dict) -> dict:
    """Return the unchanged scorer's actual schema plus compatibility metadata.

    The caller must save the original observation, including every native plan,
    registry result and adapter event. Unprojectable model output is an ordinary
    evaluated mismatch, not a fabricated harness error that reduces the accuracy
    denominator. Actual transport/harness failures retain the existing scorer's
    NOT_EVALUATED behavior. This connector measures full, single-turn processing
    without automatically confirming pending writes.
    """
    if not isinstance(observed, dict) or not isinstance(projection, dict):
        raise TypeError('Agent projection requires observation and projection dictionaries')
    if observed.get('mode', 'full') != 'full':
        raise ValueError('Personal Agent compatibility projection supports full mode only')
    native = observed.get('agent_run') or {}
    if not isinstance(native, dict):
        raise TypeError('agent_run must be a dictionary')
    plan = native.get('plan') or {}
    if not isinstance(plan, dict):
        raise TypeError('Native Agent plan must be a dictionary')
    steps = plan.get('steps')
    calls = observed.get('registry_calls') or []
    adapters = observed.get('adapter_calls') or []
    if (not isinstance(calls, list) or not all(isinstance(call, dict) for call in calls)
            or not isinstance(adapters, list) or not all(isinstance(call, dict) for call in adapters)):
        raise TypeError('Registry and adapter observations must be lists of dictionaries')

    entered_requests = {
        stable(request) for call in adapters
        if call.get('entered') is True and call.get('phase') == 'action'
        and (request := _request(call)) is not None
    }
    requests = {
        stable(request) for call in calls
        if (request := _request(call)) is not None
    }
    # Extra actual adapter entries cannot be hidden merely because the registry
    # observation is incomplete. The original events remain safety evidence.
    all_requests = requests | entered_requests
    selected = None
    if not isinstance(steps, list):
        status, reason = 'missing_plan', 'No native plan step list was observed.'
    elif not steps:
        status, reason = 'no_actions', 'No canonical single action; do not infer one from the answer or input.'
    elif len(steps) != 1:
        status, reason = 'multiple_actions', 'The native multi-step plan cannot be reduced to the V1 single-action contract.'
    elif len(all_requests) != 1 or len(requests) != 1:
        status, reason = 'ambiguous_requests', 'Exactly one observed canonical request is required.'
    else:
        validated = [call for call in calls if _validated(call, entered_requests)]
        if not validated:
            status, reason = 'unvalidated_request', 'The observed request did not pass the production validation/authorization boundary.'
        else:
            # Only exact repeats of ONE request reach here, not competing steps.
            # Keep its last observed result; every event is preserved in the trace.
            selected = validated[-1]
            status, reason = 'projected', 'One native action and one production-validated canonical request.'

    widget_trace = {}
    if selected is not None:
        widget_trace = {
            'widget_request': deepcopy(_request(selected)),
            'widget_response': deepcopy(selected.get('response') or {}),
        }
    elif len(calls) == 1:
        # Preserve a rejected registry code without promoting its request or raw
        # model plan into a validated proposal/capability.
        response = calls[0].get('response') or {}
        if isinstance(response, dict):
            error = response.get('error') or {}
            if isinstance(error, dict) and isinstance(error.get('code'), str):
                widget_trace['validation_error'] = error['code']

    compatible = deepcopy(observed)
    compatible.update(
        record={
            'routing': {'route': native.get('tier_used') or 'UNKNOWN', 'confidence': None},
            'widget_trace': widget_trace,
        },
        request=None,
        proposal=None,
        boundary=None,
        boundary_policy=None,
        final_status=native.get('status', observed.get('final_status')),
        final_response=native.get('summary_text', observed.get('final_response')),
    )
    actual = project(compatible, projection)
    actual.update(
        projection_version=AGENT_PROJECTION_VERSION,
        projection_status=status,
        projection_reason=reason,
        projection_compatible=selected is not None,
        planned_action_count=len(steps) if isinstance(steps, list) else None,
        observed_request_count=len(all_requests),
        native_plan=deepcopy(plan),
        agent_status=native.get('status'),
        agent_tier=native.get('tier_used'),
    )
    return actual
