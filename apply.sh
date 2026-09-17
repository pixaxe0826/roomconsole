#!/usr/bin/env bash
# Run in the OUTER Termux shell: bash apply.sh "$HOME/room-hub"
# Only updates web/manager.js and web/manager.html. No restart or package install.
set -Eeuo pipefail
PATCH_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
TARGET="${1:-$HOME/room-hub}"
command -v python >/dev/null || { echo 'Python is required; run this in the outer Termux shell.' >&2; exit 1; }
python - "$PATCH_DIR" "$TARGET" <<'PY'
import hashlib,json,os,shutil,sys,tempfile
from datetime import datetime, timezone
from pathlib import Path
patch,root=map(lambda p:Path(p).expanduser().resolve(),sys.argv[1:])
manifest=json.loads((patch/'PATCH_MANIFEST.json').read_text('utf-8'))
files=['web/manager.js','web/manager.html']
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
try:
    changes=[]
    for relative in files:
        source,target=patch/relative,root/relative
        item=manifest['files'][relative]
        if not source.is_file() or source.is_symlink() or digest(source)!=item['sha256']:
            raise ValueError(f'Patch file failed integrity check: {relative}')
        if not target.is_file() or target.is_symlink():
            raise ValueError(f'Expected regular file is missing (or is a symlink): {target}')
        old=digest(target)
        if old==item['sha256']:continue
        if old not in item['compatible_before_sha256']:
            raise ValueError(f'Unknown or edited source file: {target}. No files changed. Compare your custom code before applying.')
        changes.append(relative)
    if not changes:
        print('Already applied: 0.1.1-pairfix1. No files changed.')
        raise SystemExit(0)
    backups=root.parent/'room-hub-patch-backups'
    backups.mkdir(exist_ok=True,mode=0o700)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup=Path(tempfile.mkdtemp(prefix=f'pairfix1-{stamp}-',dir=backups))
    for relative in files:
        dest=backup/relative;dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(root/relative,dest)
    staged=[];replaced=[]
    try:
        for relative in changes:
            target=root/relative
            fd,temp=tempfile.mkstemp(prefix='.pairfix-',dir=target.parent)
            with os.fdopen(fd,'wb') as f:
                f.write((patch/relative).read_bytes());f.flush();os.fsync(f.fileno())
            os.chmod(temp,target.stat().st_mode & 0o777)
            staged.append((relative,Path(temp)))
        # JS first; HTML with the fresh cache key last.
        for relative,temp in staged:
            os.replace(temp,root/relative);replaced.append(relative)
        for relative in files:
            if digest(root/relative)!=manifest['files'][relative]['sha256']:
                raise ValueError(f'Post-install check failed: {relative}')
    except Exception:
        for relative in reversed(replaced):shutil.copy2(backup/relative,root/relative)
        raise
    finally:
        for _,temp in staged:temp.unlink(missing_ok=True)
    print('Applied: Room Hub 0.1.1-pairfix1')
    for relative in changes:print('Updated:',root/relative)
    print('Backup:',backup)
    print('Data, keys, client, widgets, services, nginx and cloudflared were not modified.')
    print('Keep the running server. Hard-reload the Windows manager (Ctrl+F5).')
except Exception as exc:
    print('STOP:',exc,file=sys.stderr)
    raise SystemExit(1)
PY
