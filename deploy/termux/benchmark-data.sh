#!/data/data/com.termux/files/usr/bin/bash
# Explicit dataset transfer endpoint. Never install packages or restart services.
set -Eeuo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
: "${PREFIX:?Run this in the outer Termux shell.}"
DATA_ROOT="$HOME/room-hub-benchmark-data"
[[ -f "$ROOT/.venv-v35/pyvenv.cfg" && -x "$PREFIX/bin/proot-distro" ]] || { echo 'Update code first; existing V35 environment required.' >&2; exit 1; }
[[ ! -L "$DATA_ROOT" ]] || { echo 'Symlink dataset root refused.' >&2; exit 2; }
case "${1:-}" in
  prepare)
    [[ $# == 2 && "$2" =~ ^[0-9a-f]{32}$ ]] || exit 2
    mkdir -p -- "$DATA_ROOT"
    arguments=(receive-init --root /opt/benchmark-data --token "$2") ;;
  install)
    [[ $# == 3 || $# == 4 ]] || exit 2
    [[ "$2" =~ ^[0-9a-f]{32}$ && "$3" =~ ^[0-9a-f]{64}$ ]] || exit 2
    arguments=(install --root /opt/benchmark-data --token "$2" --sha256 "$3")
    if [[ $# == 4 ]]; then [[ "$4" == --force ]] || exit 2; arguments+=(--force); fi ;;
  verify)
    [[ $# == 2 && "$2" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$ ]] || exit 2
    arguments=(verify --root /opt/benchmark-data --suite "$2") ;;
  *) echo 'Usage: benchmark-data.sh prepare TOKEN | install TOKEN SHA256 [--force] | verify SUITE' >&2; exit 2 ;;
esac
[[ -d "$DATA_ROOT" ]] || { echo 'Missing external dataset root.' >&2; exit 2; }
[[ "$(cd -- "$DATA_ROOT" && pwd -P)" == "$DATA_ROOT" ]] || { echo 'Noncanonical dataset root refused.' >&2; exit 2; }
exec "$PREFIX/bin/proot-distro" login --bind "$ROOT:/opt/room-hub" --bind "$DATA_ROOT:/opt/benchmark-data" roomhub -- \
 /bin/bash -c 'cd /opt/room-hub && export PYTHONDONTWRITEBYTECODE=1; exec /opt/room-hub/.venv-v35/bin/python -m benchmarks.transfer "$@"' transfer "${arguments[@]}"
