#!/data/data/com.termux/files/usr/bin/bash
# Run ONLY in the outer Termux shell. Existing Room Hub/Whisper stay installed.
set -Eeuo pipefail
umask 077
ROOT="$(cd "$(dirname "$0")/../../.." && pwd -P)"
: "${PREFIX:?Run this in the outer Termux shell}"
[ "$(uname -m)" = aarch64 ] || { echo 'STOP: expected aarch64 Termux.' >&2; exit 1; }
[ -f "$ROOT/.venv-v35/pyvenv.cfg" ] || { echo 'STOP: existing V35 environment missing.' >&2; exit 1; }
"$PREFIX/bin/python" "$ROOT/deploy/termux/qwen-runtime/preflight.py" --root "$ROOT"
"$PREFIX/bin/termux-wake-lock"
exec "$PREFIX/bin/proot-distro" login --bind "$ROOT:/opt/room-hub" roomhub -- \
    /bin/bash /opt/room-hub/deploy/termux/qwen-runtime/inside-setup.sh
