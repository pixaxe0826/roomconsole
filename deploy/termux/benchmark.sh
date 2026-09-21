#!/data/data/com.termux/files/usr/bin/bash
# Opt-in benchmark only. No install, service restart or production DB access.
set -Eeuo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
: "${PREFIX:?Run this in the outer Termux shell.}"
[ -f "$ROOT/.venv-v35/pyvenv.cfg" ] || { echo 'Existing V35 Python environment is required.' >&2; exit 1; }
exec "$PREFIX/bin/proot-distro" login --bind "$ROOT:/opt/room-hub" roomhub -- \
 /bin/bash -c 'cd /opt/room-hub && exec /opt/room-hub/.venv-v35/bin/python -m benchmarks "$@"' benchmark "$@"
