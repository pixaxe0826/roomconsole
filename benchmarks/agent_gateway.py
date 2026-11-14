"""Benchmark-only run-wide spend guard around the unchanged production gateway.

One RunBudget lives for the WHOLE worker, not one fixture, database or billing
month. Its checkpoint callback must return only after the parent durably records
the snapshot. No key, prompt, fixture or expected answer belongs in this ledger.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import time

from app.openai_gateway import OpenAIGateway, OpenAIGatewayError


def cost_limit_microdollars(value) -> int:
    if isinstance(value, bool):
        raise ValueError('max-cost-usd must be a finite positive decimal, at most 1000')
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('Invalid max-cost-usd') from None
    if not amount.is_finite() or not Decimal('0.000001') <= amount <= 1000:
        raise ValueError('max-cost-usd must be 0.000001..1000')
    micros = amount * 1_000_000
    if micros != micros.to_integral_value():
        raise ValueError('max-cost-usd accepts at most six decimal places')
    return int(micros)


class RunBudget:
    def __init__(self, max_cost_usd, max_calls: int, on_change=None):
        self.limit = cost_limit_microdollars(max_cost_usd)
        if type(max_calls) is not int or not 1 <= max_calls <= 10000:
            raise ValueError('max-calls must be 1..10000')
        self.max_calls = max_calls
        self.on_change = on_change
        self.calls: dict[str, dict] = {}
        self.stop_reason: str | None = None

    def snapshot(self) -> dict:
        calls = list(self.calls.values())
        spent = sum(c['actual_microdollars'] or 0 for c in calls if c['state'] == 'settled')
        reserved = sum(c['reserved_microdollars'] for c in calls if c['state'] == 'active')
        unknown = sum(c['reserved_microdollars'] for c in calls if c['state'] == 'unknown')
        return {
            'limit_microdollars': self.limit, 'max_calls': self.max_calls,
            'authorized_calls': len(calls),
            'dispatched_calls': sum(c['dispatched'] for c in calls),
            'dispatch_count_scope': 'authorized transport attempts; not proof of provider receipt',
            'spent_microdollars': spent, 'reserved_microdollars': reserved,
            'unknown_microdollars': unknown,
            'remaining_microdollars': max(0, self.limit - spent - reserved - unknown),
            'cost_complete': not (reserved or unknown), 'stop_reason': self.stop_reason,
            'cost_basis': 'production_price_catalog_and_provider_usage; not an invoice',
            'calls': deepcopy(calls),
        }

    def _checkpoint(self):
        if self.on_change:
            self.on_change(self.snapshot())

    def stop(self, reason: str):
        if self.stop_reason is None:
            self.stop_reason = reason
        self._checkpoint()

    def reserve(self, request_id: str, estimated: int, model: str):
        if self.stop_reason:
            raise OpenAIGatewayError('Benchmark has stopped; no automatic retry.', 'benchmark_stopped')
        if request_id in self.calls:
            self.stop('duplicate_request')
            raise OpenAIGatewayError('Benchmark request was already authorized.', 'benchmark_duplicate')
        if type(estimated) is not int or estimated <= 0:
            self.stop('invalid_reservation')
            raise OpenAIGatewayError('Invalid cost reservation.', 'benchmark_budget_invalid')
        state = self.snapshot()
        reason = ('max_calls_reached' if len(self.calls) >= self.max_calls else
                  'max_cost_reached' if estimated > state['remaining_microdollars'] else None)
        if reason:
            self.stop(reason)
            raise OpenAIGatewayError('Benchmark run limit reached before dispatch.', 'benchmark_limit_reached')
        self.calls[request_id] = {
            'request_id': request_id, 'model': model, 'reserved_microdollars': estimated,
            'actual_microdollars': None, 'state': 'active', 'dispatched': False,
        }
        self._checkpoint()

    def dispatched(self, request_id: str):
        call = self.calls[request_id]
        if call['state'] != 'active' or call['dispatched']:
            raise OpenAIGatewayError('Duplicate or unreserved transport.', 'benchmark_duplicate')
        call['dispatched'] = True
        self._checkpoint()

    def settle(self, request_id: str, reservation: dict | None):
        call = self.calls.get(request_id)
        if call is None:
            return  # A production or run guard rejected this call before authorization.
        status = reservation.get('status') if reservation else None
        actual = reservation.get('actual_microdollars') if reservation else None
        if status == 'settled' and type(actual) is int and actual >= 0:
            call.update(state='settled', actual_microdollars=actual)
            if actual > call['reserved_microdollars']:
                self.stop_reason = self.stop_reason or 'actual_cost_exceeded_reservation'
        elif status == 'released':
            call.update(state='released', actual_microdollars=0)
        else:
            call.update(state='unknown', actual_microdollars=None)
            self.stop_reason = self.stop_reason or 'unknown_cost'
        self._checkpoint()


class BenchmarkGateway(OpenAIGateway):
    """Real production request/schema/usage logic; only adds run limits and tracing.

`transport` is injectable by synthetic tests only. The CLI has no mock provider,
endpoint override or client-supplied credentials argument.
    """
    def __init__(self, governor, *, budget: RunBudget, live: bool, timeout: float,
                 profile, transport=None):
        if type(live) is not bool or not 10 <= timeout <= 600:
            raise ValueError('Invalid live/timeout benchmark configuration')
        super().__init__(governor, profile=profile, transport=transport,
                         api_key='benchmark-synthetic-transport' if transport is not None else None)
        self.budget = budget
        self.live = live
        self.timeout = timeout
        self.attempts: list[dict] = []
        self._active_attempt: dict | None = None

    def _reserve_call(self, *args, **kwargs):
        decision = super()._reserve_call(*args, **kwargs)
        request_id = args[0]
        try:
            self.budget.reserve(request_id, decision.estimated_call_microdollars, decision.selected_model)
        except BaseException:
            self.governor.release_reservation(request_id, 'benchmark_run_guard_before_dispatch')
            raise
        self._active_attempt['billing_month'] = decision.billing_month
        self._active_attempt['reservation_microdollars'] = decision.estimated_call_microdollars
        return decision

    async def _post_json(self, path, payload, timeout=60.0):
        attempt = self._active_attempt
        if attempt is None:
            raise OpenAIGatewayError('No benchmark invocation owns this transport.', 'benchmark_unowned_transport')
        if not self.live and self.transport is None:
            raise OpenAIGatewayError('Live OpenAI transport was not explicitly enabled.', 'benchmark_llm_unavailable')
        self.budget.dispatched(attempt['request_id'])
        attempt['transport_attempted'] = True
        attempt['request_payload'] = deepcopy(payload)
        started = time.perf_counter()
        try:
            body, raw = await super()._post_json(path, payload, timeout=timeout)
            attempt['response_raw'] = raw
            attempt['response_model'] = body.get('model')
            return body, raw
        except OpenAIGatewayError as exc:
            # Persist safe provider code only, never headers or exception body.
            attempt['http_status'] = exc.http_status
            raise
        finally:
            attempt['transport_ms'] = (time.perf_counter() - started) * 1000

    async def generate_structured(self, **kwargs):
        if self._active_attempt is not None:
            self.budget.stop('concurrent_gateway_call')
            raise OpenAIGatewayError('Benchmark gateway is serial.', 'benchmark_concurrent_call')
        request_id = kwargs['request_id']
        attempt = {'request_id': request_id, 'transport_attempted': False,
                   'request_payload': None, 'response_raw': None, 'error': None,
                   'native_error_code': None, 'latency_ms': None, 'cost_microdollars': None}
        self.attempts.append(attempt)
        self._active_attempt = attempt
        started = time.perf_counter()
        try:
            if not self.live and self.transport is None:
                raise OpenAIGatewayError('Benchmark live calls are disabled.', 'benchmark_llm_unavailable')
            result = await super().generate_structured(**(kwargs | {'timeout': self.timeout}))
            attempt['cost_microdollars'] = result['cost_microdollars']
            attempt['usage'] = deepcopy(result['usage'])
            if not result.get('cost_known'):
                attempt['error'] = 'unknown_cost'
                attempt['native_error_code'] = 'unknown_cost'
            return result
        except OpenAIGatewayError as exc:
            attempt['native_error_code'] = exc.code
            measured_output_errors = {'invalid_json', 'model_refusal', 'empty_output', 'incomplete_output'}
            attempt['error'] = exc.code if exc.code in measured_output_errors | {
                'timeout', 'http_error', 'connection_failed'
            } else 'benchmark_llm_unavailable'
            self.budget.stop('provider_' + exc.code)
            raise
        except BaseException:
            attempt['error'] = 'connection_failed'
            attempt['native_error_code'] = 'interrupted'
            self.budget.stop('gateway_interrupted')
            raise
        finally:
            attempt['latency_ms'] = (time.perf_counter() - started) * 1000
            try:
                month = attempt.get('billing_month')
                reservation = next((r for r in self.governor.list_reservations(month)
                                    if r['request_id'] == request_id), None) if month else None
                self.budget.settle(request_id, reservation)
                if reservation and reservation.get('status') == 'settled':
                    attempt['cost_microdollars'] = reservation['actual_microdollars']
            finally:
                attempt['gateway_total_ms'] = (time.perf_counter() - started) * 1000
                self._active_attempt = None

    async def transcribe_audio(self, **kwargs):
        raise OpenAIGatewayError('Text benchmark does not allow audio calls.', 'benchmark_audio_disabled')
