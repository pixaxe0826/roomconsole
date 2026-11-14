"""One gold-free serial worker, one run budget, fresh fixture DBs.

The worker has no dataset/scoring imports. Reservation checkpoints are ACKed by
the parent BEFORE transport. Never restart/resume this process automatically.
"""
from __future__ import annotations

import asyncio
import json
import sys

from .agent_config import FORMAT_VERSION, validate_config
from .agent_gateway import RunBudget
from .personal_agent_runtime import PersonalAgentRuntime
from .runtime import TextInput


def validate_payload(payload):
    if not isinstance(payload, dict) or set(payload) != {'version', 'config', 'groups'}:
        raise ValueError('Invalid worker envelope')
    if payload['version'] != FORMAT_VERSION:
        raise ValueError('Unknown worker format')
    validate_config(payload['config'])
    if not isinstance(payload['groups'], list) or not payload['groups']:
        raise ValueError('Empty worker groups')
    for group in payload['groups']:
        if not isinstance(group, dict) or set(group) != {
            'fixture', 'reference_datetime', 'timezone', 'isolate_inputs', 'inputs'
        } or type(group['isolate_inputs']) is not bool:
            raise ValueError('Invalid gold-free group')
        if not isinstance(group['inputs'], list) or not group['inputs']:
            raise ValueError('Empty input group')
        for item in group['inputs']:
            if not isinstance(item, dict) or set(item) != {'text', 'reference_datetime', 'session'}:
                raise ValueError('Unexpected input field; only runtime text/clock/session allowed')
            if not isinstance(item['text'], str) or not isinstance(item['session'], dict):
                raise ValueError('Invalid runtime text/session')


async def run(payload, emit, checkpoint):
    validate_payload(payload)
    config = payload['config']
    budget = RunBudget(config['max_cost_usd'], config['max_calls'], on_change=checkpoint)
    count = 0
    for group in payload['groups']:
        runtime = None
        try:
            for item in group['inputs']:
                if budget.stop_reason:
                    emit({'kind': 'done', 'processed': count, 'budget': budget.snapshot()})
                    return
                if runtime is None:
                    runtime = PersonalAgentRuntime(group['fixture'], item['reference_datetime'],
                                                   group['timezone'], config, budget=budget)
                observed = await runtime.run(TextInput(**item))
                emit({'kind': 'observation', 'index': count, 'observation': observed})
                count += 1
                if observed.get('harness_error'):
                    budget.stop('runtime_error')
                if group['isolate_inputs']:
                    await runtime.close()
                    runtime = None
        finally:
            if runtime is not None:
                await runtime.close()
    emit({'kind': 'done', 'processed': count, 'budget': budget.snapshot()})


def main():
    def emit(value):
        print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)

    sequence = 0
    def checkpoint(snapshot):
        nonlocal sequence
        sequence += 1
        emit({'kind': 'budget', 'sequence': sequence, 'budget': snapshot})
        answer = sys.stdin.readline()
        if not answer or json.loads(answer) != {'ack': sequence}:
            raise RuntimeError('Budget checkpoint was not acknowledged; no further dispatch')

    try:
        payload = json.loads(sys.stdin.readline())
        asyncio.run(run(payload, emit, checkpoint))
        return 0
    except BaseException as exc:
        # No arbitrary exception text: SDK/transport errors can contain secrets.
        print('Personal Agent benchmark worker stopped: ' + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
