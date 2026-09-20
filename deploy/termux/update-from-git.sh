#!/data/data/com.termux/files/usr/bin/bash
# Pull only reviewed/merged origin/main into a Git-managed V35 deployment.
# Runtime/private state stays ignored. On update failure, tracked code and the
# SQLite DB are restored to their pre-update state.
set -Eeuo pipefail
umask 077

: "${PREFIX:?Run this in the outer Termux shell.}"
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
SERVICE="$PREFIX/var/service/room-hub"
DATA_DIR="${ROOM_HUB_DATA_DIR:-$ROOT/data}"
MODE="${1:-}"
REMOTE_NAME="${ROOM_HUB_GIT_REMOTE:-origin}"
REMOTE_BRANCH="${ROOM_HUB_GIT_BRANCH:-main}"

stop() { printf 'STOP: %s\n' "$*" >&2; exit 1; }
command -v git >/dev/null || stop 'git is missing. Run: pkg install git'
command -v python >/dev/null || stop 'Termux Python is missing.'
command -v curl >/dev/null || stop 'curl is missing.'
[ -d "$ROOT/.git" ] || stop 'This Room Hub install is not Git-managed. Run adopt-git.sh once first.'
[ -d "$SERVICE" ] || stop "room-hub service not found: $SERVICE"
[ "$MODE" = "" ] || [ "$MODE" = "--check" ] || stop 'Usage: update-from-git.sh [--check]'

BRANCH="$(git -C "$ROOT" branch --show-current)"
[ "$BRANCH" = "main" ] || stop "V35 deployments must stay on local main; found: $BRANCH"
git -C "$ROOT" remote get-url "$REMOTE_NAME" >/dev/null || stop "Git remote is missing: $REMOTE_NAME"

printf 'Fetching %s/%s...\n' "$REMOTE_NAME" "$REMOTE_BRANCH"
git -C "$ROOT" fetch --prune "$REMOTE_NAME" "$REMOTE_BRANCH"
LOCAL="$(git -C "$ROOT" rev-parse HEAD)"
REMOTE="$(git -C "$ROOT" rev-parse "$REMOTE_NAME/$REMOTE_BRANCH")"
LOCAL_TREE="$(git -C "$ROOT" rev-parse HEAD^{tree})"
REMOTE_TREE="$(git -C "$ROOT" rev-parse "$REMOTE_NAME/$REMOTE_BRANCH^{tree})"
COUNTS="$(git -C "$ROOT" rev-list --left-right --count HEAD..."$REMOTE_NAME/$REMOTE_BRANCH")"
DIRTY="$(git -C "$ROOT" status --porcelain --untracked-files=normal)"

printf '\n=== Room Hub Git status ===\n'
printf 'branch       : %s\n' "$BRANCH"
printf 'local HEAD   : %s\n' "$LOCAL"
printf 'remote HEAD  : %s\n' "$REMOTE"
printf 'local tree   : %s\n' "$LOCAL_TREE"
printf 'remote tree  : %s\n' "$REMOTE_TREE"
printf 'ahead/behind : %s\n' "$COUNTS"
printf 'version      : %s\n' "$(cat "$ROOT/VERSION")"
if [ -n "$DIRTY" ]; then
  printf 'working tree : DIRTY\n%s\n' "$DIRTY"
else
  printf 'working tree : clean (ignored runtime/private files excluded)\n'
fi

if [ "$MODE" = "--check" ]; then
  [ -z "$DIRTY" ] || exit 2
  [ "$LOCAL" = "$REMOTE" ] || exit 3
  printf 'SYNCED: local tracked source is exactly origin/main.\n'
  exit 0
fi

[ -z "$DIRTY" ] || stop 'Tracked/unignored local changes exist. Do not overwrite them automatically.'
if [ "$LOCAL" = "$REMOTE" ]; then
  printf 'ALREADY CURRENT: no Git update is needed.\n'
  curl -fsS --max-time 3 http://127.0.0.1:8088/healthz; printf '\n'
  exit 0
fi
git -C "$ROOT" merge-base --is-ancestor "$LOCAL" "$REMOTE" ||
  stop 'Local history diverged from origin/main. Refusing a reset/force update.'

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$HOME/room-hub-git-backups/$STAMP-$(printf '%.12s' "$LOCAL")"
mkdir -p "$BACKUP"
chmod 700 "$HOME/room-hub-git-backups" "$BACKUP"
printf '%s\n' "$LOCAL" > "$BACKUP/previous-head.txt"
printf '%s\n' "$REMOTE" > "$BACKUP/target-head.txt"
git -C "$ROOT" show "$LOCAL:deploy/termux/requirements-v35.txt" > "$BACKUP/requirements-v35.before.txt"

REQ_CHANGED=0
git -C "$ROOT" diff --quiet "$LOCAL" "$REMOTE" -- deploy/termux/requirements-v35.txt || REQ_CHANGED=1
DB_BACKED_UP=0
DB="$DATA_DIR/room-hub.sqlite3"
DB_COPY="$BACKUP/room-hub.sqlite3"

sync_v35_deps() {
  [ -f "$ROOT/.venv-v35/pyvenv.cfg" ] || return 20
  "$PREFIX/bin/proot-distro" login --bind "$ROOT:/opt/room-hub" roomhub --     /opt/room-hub/.venv-v35/bin/python -m pip install --only-binary=:all:     -r /opt/room-hub/deploy/termux/requirements-v35.txt
  "$PREFIX/bin/proot-distro" login --bind "$ROOT:/opt/room-hub" roomhub --     /opt/room-hub/.venv-v35/bin/python -m pip check
}

restore_db() {
  [ "$DB_BACKED_UP" -eq 1 ] || return 0
  python - "$DB_COPY" "$DB" <<'PY'
from pathlib import Path
import shutil, sys
src,dst=map(Path,sys.argv[1:])
dst.parent.mkdir(parents=True,exist_ok=True)
for suffix in ('-wal','-shm'):
    Path(str(dst)+suffix).unlink(missing_ok=True)
shutil.copy2(src,dst)
PY
}

rollback() {
  rc=$?
  trap - ERR
  set +e
  printf '\nUPDATE FAILED (exit %s). Rolling back code and SQLite DB...\n' "$rc" >&2
  if [ -f "$ROOT/deploy/termux/stop-room-hub.py" ]; then
    python "$ROOT/deploy/termux/stop-room-hub.py" --root "$ROOT" >/dev/null 2>&1 || true
  else
    sv down "$SERVICE" >/dev/null 2>&1 || true
  fi
  git -C "$ROOT" reset --hard "$LOCAL" || true
  if [ "$REQ_CHANGED" -eq 1 ]; then
    sync_v35_deps || printf 'WARNING: old V35 dependencies could not be fully restored.\n' >&2
  fi
  restore_db || printf 'WARNING: SQLite backup could not be restored automatically: %s\n' "$DB_COPY" >&2
  sv up "$SERVICE" >/dev/null 2>&1 || true
  printf 'Rollback target HEAD: %s\nBackup directory: %s\n' "$LOCAL" "$BACKUP" >&2
  exit "$rc"
}

printf '\nStopping Room Hub and creating a rollback point...\n'
python "$ROOT/deploy/termux/stop-room-hub.py" --root "$ROOT"
if [ -f "$DB" ]; then
  python - "$DB" "$DB_COPY" <<'PY'
from pathlib import Path
import sqlite3, sys
src,dst=map(Path,sys.argv[1:])
dst.parent.mkdir(parents=True,exist_ok=True)
with sqlite3.connect(src) as source, sqlite3.connect(dst) as target:
    source.backup(target)
    if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise SystemExit('SQLite backup integrity check failed')
PY
  DB_BACKED_UP=1
fi

trap rollback ERR
printf 'Fast-forwarding local main to fetched origin/main...\n'
git -C "$ROOT" merge --ff-only "$REMOTE"
python "$ROOT/scripts/check_repo.py"

if [ "$REQ_CHANGED" -eq 1 ]; then
  printf 'V35 dependency pins changed; synchronizing only deploy/termux/requirements-v35.txt...\n'
  sync_v35_deps
else
  printf 'V35 dependency pins unchanged; keeping the existing validated V35 environment.\n'
fi

[ -z "$(git -C "$ROOT" status --porcelain --untracked-files=normal)" ] ||
  { printf 'Unexpected working tree changes after update.\n' >&2; false; }

sv up "$SERVICE"
EXPECTED="$(cat "$ROOT/VERSION")"
HEALTH=''
for _ in $(seq 1 30); do
  if HEALTH="$(curl -fsS --max-time 2 http://127.0.0.1:8088/healthz 2>/dev/null)"; then
    if python - "$EXPECTED" "$HEALTH" <<'PY'
import json,sys
expected=sys.argv[1]
value=json.loads(sys.argv[2])
raise SystemExit(0 if str(value.get('version'))==expected else 2)
PY
    then
      trap - ERR
      printf '\nUPDATED SUCCESSFULLY\n'
      printf '  HEAD    : %s\n' "$(git -C "$ROOT" rev-parse HEAD)"
      printf '  tree    : %s\n' "$(git -C "$ROOT" rev-parse HEAD^{tree})"
      printf '  version : %s\n' "$EXPECTED"
      printf '  backup  : %s\n' "$BACKUP"
      printf '  health  : %s\n' "$HEALTH"
      exit 0
    fi
  fi
  sleep 1
done
printf 'Health/version verification failed after update.\n' >&2
false
