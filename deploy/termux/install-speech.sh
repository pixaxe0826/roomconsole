#!/data/data/com.termux/files/usr/bin/bash
# Run in OUTER Termux, not inside Debian. Does not change nginx, tunnels or Tasker.
set -Eeuo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
: "${PREFIX:?Run in outer Termux.}"
MODEL="${1:-tiny}"
case "$MODEL" in tiny|base) ;; *) echo 'Usage: bash deploy/termux/install-speech.sh tiny|base' >&2; exit 1;; esac
[ -f "$ROOT/.venv-v35/pyvenv.cfg" ] || { echo 'Existing V35 Debian/Python setup is required.' >&2; exit 1; }
command -v proot-distro >/dev/null
termux-wake-lock || true
python - <<'PY_PORT'
import socket
with socket.socket() as s:
 s.settimeout(.5)
 if s.connect_ex(('127.0.0.1',8088))==0:raise SystemExit('STOP: stop only Room Hub before installing/testing speech. Existing config was not changed.')
PY_PORT
exec "$PREFIX/bin/proot-distro" login --bind "$ROOT:/opt/room-hub" roomhub -- \
 /bin/bash /opt/room-hub/deploy/termux/inside-speech-setup.sh "$MODEL"
