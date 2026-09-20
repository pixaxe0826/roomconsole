"""V35 launcher: one process, standard asyncio/h11, WebSockets, no token in logs.

Run with the Debian virtual environment through start.sh. Does not alter app code.
An advisory lock blocks a second launcher using the same data directory; it cannot
prevent someone from bypassing this launcher with run.py against that directory.
"""
from __future__ import annotations
import fcntl
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    os.umask(0o077)
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    data = Path(os.environ.setdefault('HUB_DATA_DIR', str(ROOT / 'data'))).resolve()
    os.environ.setdefault('HUB_WIDGET_DIR', str(ROOT / 'widgets'))
    os.environ.setdefault('HUB_SECURE_COOKIE', 'false')
    os.environ.setdefault('PYTHONUNBUFFERED', '1')
    port = int(os.environ.get('HUB_PORT', '8088'))
    if not 1024 <= port <= 65535:
        raise ValueError('Use an unprivileged port between 1024 and 65535.')
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (data / '.v35-server.lock').open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another Room Hub V35 launcher already owns this data directory.', file=sys.stderr)
            return 2
        from app.main import app
        import uvicorn
        print(f'Room Hub {app.version} | V35 service | port {port}', flush=True)
        print(f'Admin key is in {data / "admin-token.txt"} unless supplied via environment. Keep it private.', flush=True)
        uvicorn.run(app, host=os.environ.get('HUB_HOST', '0.0.0.0'), port=port,
                    workers=1, loop='asyncio', http='h11', ws='websockets',
                    ws_max_size=16384, access_log=False, timeout_graceful_shutdown=10)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
