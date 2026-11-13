"""Explicit, reversible migration from a local CLI/wrapper to remote HTTP.

Dry-run by default. Never restarts services, downloads a model, edits accuracy
policy, opens the production DB or reads/prints authentication tokens.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.speech import SpeechConfig


def configure(data_dir: Path, *, endpoint: str, model: str, device: str, apply=False):
    data_dir = data_dir.expanduser().absolute()
    path = data_dir / 'speech-config.json'
    if not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1 or path.stat().st_size > 16384:
        raise ValueError('Expected an existing regular, non-linked speech-config.json (max 16KiB)')
    original = path.read_bytes()
    cfg = SpeechConfig.read(path)
    new = replace(cfg, backend='remote_http', remote_url=endpoint,
                  remote_model=model, remote_device=device)
    new.validate_backend()
    plan = {'apply': bool(apply), 'backend_before': cfg.backend, 'backend_after': new.backend,
            'enabled_unchanged': new.enabled, 'configured_model': model,
            'configured_device': device, 'model_device_verified': False,
            'accuracy_policy': 'unchanged', 'service_restart': 'not_run'}
    if not apply:
        return plan
    if path.read_bytes() != original:
        raise ValueError('Configuration changed during migration; not overwritten')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = data_dir / ('speech-config.before-m340-' + stamp + '-' + uuid.uuid4().hex[:8] + '.json')
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(original); out.flush(); os.fsync(out.fileno())
    fd, temporary = tempfile.mkstemp(prefix='.speech-config-', dir=data_dir)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as out:
            json.dump(asdict(new), out, ensure_ascii=False, indent=2)
            out.write('\n'); out.flush(); os.fsync(out.fileno())
        os.chmod(temporary, 0o600)
        # Round-trip the new settings through the same production validator.
        SpeechConfig.read(Path(temporary))
        if path.is_symlink() or path.read_bytes() != original:
            raise ValueError('Configuration changed during migration; backup preserved')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {**plan, 'backup': str(backup), 'written': str(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--endpoint', default='http://127.0.0.1:8178/inference')
    parser.add_argument('--model', required=True, help='Operator-configured remote identity, not backend verification')
    parser.add_argument('--device', default='unknown', choices=['unknown','cpu','cuda','vulkan','metal','openvino'])
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(configure(args.data_dir, endpoint=args.endpoint, model=args.model,
                                   device=args.device, apply=args.apply), ensure_ascii=False, indent=2))
    except (OSError, ValueError, TypeError) as exc:
        print('Remote speech configuration refused: ' + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
