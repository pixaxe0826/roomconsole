#!/data/data/com.termux/files/usr/bin/bash
# Foreground command. Use install-service.sh to detach from SSH/terminal sessions.
set -Eeuo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
: "${PREFIX:?Run this in the outer Termux shell.}"
[ -f "$ROOT/.venv-v35/pyvenv.cfg" ] || { echo 'Run bash deploy/termux/setup.sh first.' >&2; exit 1; }
exec "$PREFIX/bin/proot-distro" login --bind "$ROOT:/opt/room-hub" roomhub -- \
  /opt/room-hub/.venv-v35/bin/python /opt/room-hub/deploy/termux/server.py
