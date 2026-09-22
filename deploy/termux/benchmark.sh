#!/data/data/com.termux/files/usr/bin/bash
# Caller-read-only external data. PRoot bind is NOT a read-only security mount.
set -Eeuo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
: "${PREFIX:?Run this in the outer Termux shell.}"
DATA_ROOT="${ROOM_HUB_BENCHMARK_DATA:-$HOME/room-hub-benchmark-data}"
global=()
while (($#)); do
  case "$1" in
    --data-root) (($# >= 2)) || { echo 'Missing --data-root value' >&2; exit 2; }; DATA_ROOT="$2"; shift 2 ;;
    --suite-root) (($# >= 2)) && [[ "$2" == /opt/benchmark-data ]] || { echo 'Use --data-root for an outer-Termux path; guest root is /opt/benchmark-data' >&2; exit 2; }; shift 2 ;;
    --results-root) (($# >= 2)) || exit 2; global+=("$1" "$2"); shift 2 ;;
    *) break ;;
  esac
done
[[ -f "$ROOT/.venv-v35/pyvenv.cfg" ]] || { echo 'Existing V35 Python environment is required.' >&2; exit 1; }
[[ -x "$PREFIX/bin/proot-distro" ]] || { echo 'Existing proot-distro is required.' >&2; exit 1; }
binds=(--bind "$ROOT:/opt/room-hub")
# Offline diagnostics, comparison and help need result files only, not datasets.
if [[ "${1:-}" != compare && "${1:-}" != analyze && "${1:-}" != verify-analysis && "${1:-}" != compare-analysis && "${1:-}" != --help && "${1:-}" != -h ]]; then
  [[ -d "$DATA_ROOT" && ! -L "$DATA_ROOT" && "$DATA_ROOT" != *:* && "$DATA_ROOT" != *$'\n'* ]] || { echo 'Missing or unsafe external --data-root' >&2; exit 2; }
  [[ "$DATA_ROOT" == /* ]] || DATA_ROOT="$PWD/$DATA_ROOT"
  inspect="$DATA_ROOT"
  while [[ "$inspect" != / && "$inspect" != . ]]; do
    [[ ! -L "$inspect" ]] || { echo 'Symlink dataset path refused.' >&2; exit 2; }
    inspect="$(dirname -- "$inspect")"
  done
  DATA_ROOT="$(cd -- "$DATA_ROOT" && pwd -P)"
  case "$DATA_ROOT/" in "$ROOT/"*) echo 'Dataset cannot be inside the repository.' >&2; exit 2 ;; esac
  [[ ! -e "$DATA_ROOT/.import-lock" ]] || { echo 'Dataset import in progress; retry after it finishes.' >&2; exit 2; }
  binds+=(--bind "$DATA_ROOT:/opt/benchmark-data")
  global=(--suite-root /opt/benchmark-data "${global[@]}")
fi
exec "$PREFIX/bin/proot-distro" login "${binds[@]}" roomhub -- \
 /bin/bash -c 'cd /opt/room-hub && export PYTHONDONTWRITEBYTECODE=1; exec /opt/room-hub/.venv-v35/bin/python -m benchmarks "$@"' benchmark "${global[@]}" "$@"
