"""SSH foreground deployment supervisor. Stdlib only; no operational DB imports.

Run the SAME fetched Git commit's updater, not the old checked-out Python code.
No force updates, no secrets, no model changes. CI failure blocks before downtime.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request
import uuid

REPOSITORY = 'pixaxe0826/roomconsole'


class VerifyError(RuntimeError):
    pass


def output(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()


def validate_source(root: Path, target: str) -> None:
    if not re.fullmatch(r'[0-9a-f]{40}', target):
        raise VerifyError('Invalid target commit.')
    if output('git', '-C', str(root), 'branch', '--show-current') != 'main':
        raise VerifyError('Deployment branch must be main.')
    url = output('git', '-C', str(root), 'remote', 'get-url', 'origin')
    if url not in {f'https://github.com/{REPOSITORY}', f'https://github.com/{REPOSITORY}.git',
                   f'git@github.com:{REPOSITORY}.git'}:
        raise VerifyError('Unexpected origin. No update performed.')
    if output('git', '-C', str(root), 'status', '--porcelain', '--untracked-files=normal'):
        raise VerifyError('Working tree is dirty. Do not force/reset local changes.')
    if output('git', '-C', str(root), 'rev-parse', 'origin/main') != target:
        raise VerifyError('Fetched origin/main changed. Run again.')
    subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', 'HEAD', target], check=True)


def json_get(url: str) -> dict:
    # No inherited authentication, no proxy, no token in the public CI check.
    request = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json',
                                                 'User-Agent': 'Room-Hub-V35-Updater'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=15) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise VerifyError('Oversized status response.')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise VerifyError('Invalid JSON status.')
    return value


def ci_state(payload: dict, target: str) -> str:
    runs = [r for r in payload.get('workflow_runs', [])
            if r.get('head_sha') == target and r.get('head_branch') == 'main'
            and r.get('event') == 'push']
    if not runs:
        return 'pending'
    latest = max(runs, key=lambda r: r['id'])
    if latest.get('status') != 'completed':
        return 'pending'
    return 'success' if latest.get('conclusion') == 'success' else 'failed'


def wait_ci(target: str, seconds: float = 600) -> None:
    url = (f'https://api.github.com/repos/{REPOSITORY}/actions/workflows/ci.yml/runs'
           f'?branch=main&event=push&head_sha={target}&per_page=10')
    deadline = time.monotonic() + seconds
    while True:
        state = ci_state(json_get(url), target)
        if state == 'success':
            print('[OK] Merged main CI SUCCESS for ' + target, flush=True)
            return
        if state == 'failed':
            raise VerifyError('Merged main CI failed/cancelled. No update performed.')
        if time.monotonic() >= deadline:
            raise VerifyError('Merged main CI is not successful yet. No update performed; retry later.')
        print('[WAIT] CI pending; service is still running. Checking in 30 seconds...', flush=True)
        time.sleep(min(30, max(0, deadline - time.monotonic())))


def verify_running(root: Path, prefix: Path, target: str, *, samples: int = 3, delay: float = 2) -> dict:
    if samples < 1 or delay < 0:
        raise VerifyError('Invalid verification sampling policy.')
    head = output('git', '-C', str(root), 'rev-parse', 'HEAD')
    remote = output('git', '-C', str(root), 'rev-parse', 'origin/main')
    tree = output('git', '-C', str(root), 'rev-parse', 'HEAD^{tree}')
    remote_tree = output('git', '-C', str(root), 'rev-parse', 'origin/main^{tree}')
    if head != target or remote != target or tree != remote_tree:
        raise VerifyError('Post-update HEAD/tree is not the verified target.')
    if output('git', '-C', str(root), 'status', '--porcelain', '--untracked-files=normal'):
        raise VerifyError('Post-update working tree is dirty.')
    version = (root / 'VERSION').read_text(encoding='utf-8').strip()
    service = str(prefix / 'var/service/room-hub')
    pid = None
    for sample in range(samples):
        status = output('sv', 'status', service)
        match = re.match(r'^run: ' + re.escape(service) + r': \(pid (\d+)\)', status)
        if not match:
            raise VerifyError('Room Hub service is not run: ' + status)
        if pid is not None and match[1] != pid:
            raise VerifyError('Room Hub restarted during verification; inspect service logs.')
        pid = match[1]
        health = json_get('http://127.0.0.1:8088/healthz')
        if health.get('status') != 'ok' or health.get('version') != version:
            raise VerifyError('Health status/version does not match the deployed source.')
        print(f'[OK] Verification {sample + 1}/{samples}: run PID={pid}; health={version}', flush=True)
        if sample + 1 < samples:
            time.sleep(delay)
    return {'head': head, 'tree': tree, 'version': version, 'pid': pid,
            'health': health, 'source_synced': True, 'samples': samples,
            'verified_at': datetime.now(timezone.utc).isoformat()}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--target', required=True)
    args = parser.parse_args(argv)
    try:
        import fcntl  # Outer Termux/Linux only. OS releases the lock after disconnect/crash.
        root = args.root.resolve()
        prefix_value = os.environ.get('PREFIX')
        if not prefix_value:
            raise VerifyError('Run in outer Termux; PREFIX is missing.')
        prefix = Path(prefix_value)
        with (Path.home() / '.room-hub-update.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise VerifyError('Another SSH updater is active. Do not start a second update.') from None
            validate_source(root, args.target)
            print('[1/3] Checking merged main CI before any downtime...', flush=True)
            wait_ci(args.target)
            print('[2/3] Running the Git updater with backup and rollback...', flush=True)
            env = os.environ.copy()
            env.update(ROOM_HUB_ROOT=str(root), ROOM_HUB_EXPECTED_MAIN=args.target,
                       ROOM_HUB_GIT_REMOTE='origin', ROOM_HUB_GIT_BRANCH='main', PYTHONUNBUFFERED='1')
            updater = Path(__file__).with_name('update_from_git.py')
            subprocess.run([str(prefix / 'bin/python'), str(updater)], check=True, env=env, stdin=subprocess.DEVNULL)
            print('[3/3] Checking exact Git state, health and stable runit PID...', flush=True)
            report = verify_running(root, prefix, args.target)
            directory = Path.home() / 'room-hub-git-backups'
            directory.mkdir(mode=0o700, exist_ok=True)
            report_path = directory / ('verified-' + uuid.uuid4().hex + '.json')
            report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
            report_path.chmod(0o600)
            print('VERIFIED UPDATE COMPLETE: ' + report['head'], flush=True)
            print('Report: ' + str(report_path), flush=True)
        return 0
    except Exception as exc:
        print('UPDATE/VERIFICATION FAILED: ' + str(exc), file=sys.stderr, flush=True)
        print('Do not force reset or blindly repeat a disconnected update. Check health, Git and service logs first.',
              file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
