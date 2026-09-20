#!/data/data/com.termux/files/usr/bin/bash
# One-time conversion of an existing non-Git V35 Room Hub install into a
# read-only deployment clone. Runtime/private state is preserved outside Git.
set -Eeuo pipefail
umask 077

: "${PREFIX:?Run this in the outer Termux shell.}"
ROOT="${ROOM_HUB_ROOT:-$HOME/room-hub}"
REPO_URL="${ROOM_HUB_REPO_URL:-https://github.com/pixaxe0826/roomconsole.git}"
BRANCH="${ROOM_HUB_BRANCH:-main}"
SERVICE="$PREFIX/var/service/room-hub"
STAMP="$(date +%Y%m%d-%H%M%S)"
STAGE="$HOME/.room-hub-git-stage-$STAMP"
BACKUP="$HOME/room-hub-pre-git-$STAMP"
FAILED="$HOME/room-hub-failed-git-$STAMP"
WIDGET_DIFF="$HOME/room-hub-widget-diff-$STAMP.txt"
PRESERVE=(data runtime .venv-v35 backups secrets)

stop() { printf 'STOP: %s\n' "$*" >&2; exit 1; }

command -v git >/dev/null || stop 'git is missing. Run: pkg install git'
command -v python >/dev/null || stop 'Termux Python is missing.'
command -v curl >/dev/null || stop 'curl is missing.'
command -v sv >/dev/null || stop 'termux-services is missing.'
[ -d "$ROOT" ] || stop "Room Hub directory not found: $ROOT"
[ ! -e "$ROOT/.git" ] || stop 'This installation is already Git-managed. Use update-from-git.sh.'
[ -f "$ROOT/VERSION" ] || stop 'VERSION is missing from the existing installation.'
[ -d "$SERVICE" ] || stop "room-hub service not found: $SERVICE"
case "$ROOT/" in "$HOME/"*) ;; *) stop 'Keep Room Hub below the Termux HOME filesystem.';; esac
[ ! -e "$STAGE" ] && [ ! -e "$BACKUP" ] && [ ! -e "$FAILED" ] || stop 'Staging/backup path already exists.'

printf 'Existing installation: %s (version %s)\n' "$ROOT" "$(cat "$ROOT/VERSION")"
printf 'Cloning %s branch %s into a staging directory...\n' "$REPO_URL" "$BRANCH"
git clone --branch "$BRANCH" --single-branch "$REPO_URL" "$STAGE"
python "$STAGE/scripts/check_repo.py"

# Built-in widget source should be identical before adoption. An unexpected
# difference can mean a local/custom widget edit that a clone swap would hide.
if [ -d "$ROOT/widgets" ]; then
  if ! python - "$ROOT/widgets" "$STAGE/widgets" "$WIDGET_DIFF" <<'PY'
from pathlib import Path
import hashlib, sys
old,new,out = map(Path, sys.argv[1:])
def snap(root):
    result={}
    for p in sorted(root.rglob('*')):
        if p.is_file() and not p.is_symlink():
            result[p.relative_to(root).as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
    return result
a,b=snap(old),snap(new)
lines=[]
for name in sorted(set(a)|set(b)):
    if a.get(name)!=b.get(name):
        lines.append(f"{name}: {'present' if name in a else 'missing'} local / {'present' if name in b else 'missing'} git")
out.write_text('\n'.join(lines)+('\n' if lines else ''), encoding='utf-8')
raise SystemExit(0 if not lines else 2)
PY
  then
    rm -rf -- "$STAGE"
    printf 'STOP: local widget source differs from GitHub. Review before adoption:\n  %s\n' "$WIDGET_DIFF" >&2
    exit 1
  fi
fi
rm -f -- "$WIDGET_DIFF"

rollback_swap() {
  trap - ERR
  set +e
  printf '\nAdoption failed after the directory swap; restoring the previous installation...\n' >&2
  sv down "$SERVICE" >/dev/null 2>&1 || true
  cd "$HOME" || true
  if [ -d "$ROOT" ] && [ -d "$BACKUP" ]; then
    for name in "${PRESERVE[@]}"; do
      if [ -e "$ROOT/$name" ] && [ ! -e "$BACKUP/$name" ]; then
        mv -- "$ROOT/$name" "$BACKUP/$name" || true
      fi
    done
    shopt -s nullglob
    for file in "$ROOT"/.env "$ROOT"/.env.*; do
      [ "$(basename "$file")" = '.env.example' ] && continue
      [ -e "$BACKUP/$(basename "$file")" ] || mv -- "$file" "$BACKUP/" || true
    done
    shopt -u nullglob
    mv -- "$ROOT" "$FAILED" || true
    mv -- "$BACKUP" "$ROOT" || true
  fi
  sv up "$SERVICE" >/dev/null 2>&1 || true
  printf 'Previous installation restored. Failed clone kept at: %s\n' "$FAILED" >&2
}
trap rollback_swap ERR

printf 'Stopping Room Hub before the atomic directory swap...\n'
python "$ROOT/deploy/termux/stop-room-hub.py" --root "$ROOT"
cd "$HOME"
mv -- "$ROOT" "$BACKUP"
mv -- "$STAGE" "$ROOT"

# Move only runtime/private paths that Git deliberately ignores.
for name in "${PRESERVE[@]}"; do
  if [ -e "$BACKUP/$name" ]; then
    [ ! -e "$ROOT/$name" ] || stop "Clone unexpectedly contains runtime path: $name"
    mv -- "$BACKUP/$name" "$ROOT/$name"
  fi
done
shopt -s nullglob
for file in "$BACKUP"/.env "$BACKUP"/.env.*; do
  [ "$(basename "$file")" = '.env.example' ] && continue
  [ ! -e "$ROOT/$(basename "$file")" ] || stop "Clone unexpectedly contains private env file: $(basename "$file")"
  mv -- "$file" "$ROOT/"
done
shopt -u nullglob

python "$ROOT/scripts/check_repo.py"
DIRTY="$(git -C "$ROOT" status --porcelain --untracked-files=normal)"
[ -z "$DIRTY" ] || { printf '%s\n' "$DIRTY" >&2; stop 'Unexpected non-ignored files exist after adoption.'; }

sv up "$SERVICE"
for _ in $(seq 1 30); do
  if curl -fsS --max-time 2 http://127.0.0.1:8088/healthz >/dev/null; then
    trap - ERR
    printf '\nGit adoption complete.\n'
    printf '  local HEAD : %s\n' "$(git -C "$ROOT" rev-parse HEAD)"
    printf '  local tree : %s\n' "$(git -C "$ROOT" rev-parse HEAD^{tree})"
    printf '  branch     : %s\n' "$(git -C "$ROOT" branch --show-current)"
    printf '  remote     : %s\n' "$(git -C "$ROOT" remote get-url origin)"
    printf '  old source : %s\n' "$BACKUP"
    printf '\nKeep the old source directory until you have tested the server, then use:\n'
    printf '  bash %q --check\n' "$ROOT/deploy/termux/update-from-git.sh"
    exit 0
  fi
  sleep 1
done
printf 'Health check failed after Git adoption.\n' >&2
false
