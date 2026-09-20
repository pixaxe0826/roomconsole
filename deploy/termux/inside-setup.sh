#!/bin/bash
# Invoked inside the dedicated Debian environment only.
set -Eeuo pipefail
umask 077
cd /opt/room-hub
[ -f /etc/debian_version ] || { echo 'Expected a Debian environment.' >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends python3 python3-venv python3-pip ca-certificates
python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ is required"'
if [ ! -d .venv-v35 ]; then
  python3 -m venv .venv-v35
fi
# Binary-only prevents unexpectedly compiling Rust/C dependencies on the phone.
# If no compatible wheel is available, stop and inspect; do not ignore the error.
.venv-v35/bin/python -m pip install --only-binary=:all: -r deploy/termux/requirements-v35.txt
.venv-v35/bin/python -m pip check
.venv-v35/bin/python - <<'PY'
import sys
import fastapi, uvicorn, pydantic, pydantic_core, httpx, websockets
import qrcode.image.svg, python_multipart
from zoneinfo import ZoneInfo
from app.models import TaskCompletion
assert TaskCompletion(version=1, completed=True).completed
ZoneInfo('Asia/Seoul')
print('Dependencies OK. Python:', sys.version.split()[0])
print('Pydantic:', pydantic.__version__, 'Uvicorn:', uvicorn.__version__)
print('Setup did not import app.main and did not initialize a database.')
PY
printf '\nInstallation complete. Return to Termux and run:\n  bash deploy/termux/start.sh\n'
