"""Subprocess boundary: inputs + fixture state only; fresh apps for single turns."""
import asyncio
import json
import sys
from .runtime import Runtime, TextInput


async def execute(payload):
    allowed = {'fixture', 'reference_datetime', 'timezone', 'config', 'mode', 'inputs', 'isolate_inputs'}
    if set(payload) != allowed or type(payload['isolate_inputs']) is not bool:
        raise ValueError('Invalid worker envelope; evaluation metadata is not accepted')
    runtime = None
    try:
        for raw in payload['inputs']:
            if set(raw) != {'text', 'reference_datetime', 'session'}:
                raise ValueError('Only text and fixture/session input may enter the runtime')
            if runtime is None:
                runtime = Runtime(payload['fixture'], raw['reference_datetime'], payload['timezone'], payload['config'])
            result = await runtime.run(TextInput(**raw), payload['mode'])
            if payload['isolate_inputs']:
                await runtime.close()
                runtime = None
            print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
    finally:
        if runtime is not None:
            await runtime.close()


def main():
    try:
        payload = json.load(sys.stdin)
        asyncio.run(execute(payload))
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
