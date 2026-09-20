"""Run CI pytest in a bounded child and retain diagnostics even on a hard hang.

No production data: invoke only in the CI/synthetic-test checkout. Test selection
is supplied by the workflow; this wrapper never skips or retries a failed test.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    output = ROOT / 'artifacts/test-results'
    output.mkdir(parents=True, exist_ok=True)
    log = output / 'pytest-process.log'
    code = (
        'import faulthandler, sys; '
        'faulthandler.enable(); '
        'faulthandler.dump_traceback_later(20, repeat=True); '
        'import pytest; '
        'sys.exit(pytest.main(sys.argv[1:]))'
    )
    env = dict(os.environ, PYTHONUNBUFFERED='1', PYTHONUTF8='1')
    deadline = time.monotonic() + 240
    timed_out = False
    with log.open('wb') as stream:
        proc = subprocess.Popen(
            [sys.executable, '-u', '-c', code, *sys.argv[1:]],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
            stdout=stream, stderr=subprocess.STDOUT,
        )
        try:
            while proc.poll() is None:
                if time.monotonic() >= deadline:
                    timed_out = True
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       timeout=10, check=False)
                    break
                time.sleep(1)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
    print(log.read_text('utf-8', errors='replace')[-100000:], flush=True)
    if timed_out:
        print('ERROR: pytest child exceeded 240 seconds; see pytest-process.log.', flush=True)
        return 124
    return proc.returncode


if __name__ == '__main__':
    raise SystemExit(main())
