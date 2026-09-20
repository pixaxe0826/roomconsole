#!/data/data/com.termux/files/usr/bin/bash
# Invoke from OUTER Termux. Reuse the existing dedicated Debian environment.
# This tool does not install apt packages, rebuild Whisper, stop services or touch TLS.
set -Eeuo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
: "${PREFIX:?Run in outer Termux, not inside Debian.}"
[ -f "$ROOT/.venv-v35/pyvenv.cfg" ] || { echo 'STOP: existing V35 Python environment not found.' >&2; exit 1; }
[ -x "$PREFIX/bin/proot-distro" ] || { echo 'STOP: proot-distro is missing.' >&2; exit 1; }
# This is idempotent, not a toggle. Do not call termux-wake-unlock here.
"$PREFIX/bin/termux-wake-lock" >/dev/null 2>&1 || true
exec "$PREFIX/bin/proot-distro" login --bind "$ROOT:/opt/room-hub" roomhub -- \
  /opt/room-hub/.venv-v35/bin/python /opt/room-hub/deploy/termux/speech_model.py "$@"
