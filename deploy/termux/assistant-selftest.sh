#!/data/data/com.termux/files/usr/bin/bash
# Optional on-device synthetic Qwen tests. No DB/setting/task writes or installs.
set -Eeuo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
: "${PREFIX:?Run in the outer Termux shell}"
exec "$PREFIX/bin/proot-distro" login --bind "$ROOT:/opt/room-hub" roomhub -- \
 /opt/room-hub/.venv-v35/bin/python /opt/room-hub/scripts/assistant_model_probe.py
