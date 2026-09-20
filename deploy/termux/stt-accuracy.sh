#!/data/data/com.termux/files/usr/bin/bash
# Run in OUTER Termux. No apt install, model download or service reconfiguration.
set -Eeuo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
: "${PREFIX:?Run in outer Termux, not inside Debian.}"
[ -f "$ROOT/.venv-v35/pyvenv.cfg" ] || { echo 'STOP: existing V35 venv missing.' >&2; exit 1; }
[ -x "$PREFIX/bin/proot-distro" ] || { echo 'STOP: proot-distro missing.' >&2; exit 1; }
"$PREFIX/bin/termux-wake-lock" >/dev/null 2>&1 || true
exec "$PREFIX/bin/proot-distro" login --bind "$ROOT:/opt/room-hub" roomhub -- \
  /opt/room-hub/.venv-v35/bin/python /opt/room-hub/deploy/termux/stt_accuracy_cli.py "$@"
