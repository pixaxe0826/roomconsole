#!/data/data/com.termux/files/usr/bin/bash
set -Eeuo pipefail
umask 077
: "${PREFIX:?Run in outer Termux}"
ROOT="$(cd "$(dirname "$0")/../../.." && pwd -P)"
TOOLS="$ROOT/deploy/termux/qwen-runtime"
SERVICE="$PREFIX/var/service/room-hub-llm"
LOG="$PREFIX/var/log/sv/room-hub-llm"
# Validate installed model and actual-device receipt before registering anything.
"$PREFIX/bin/python" "$TOOLS/runtime.py" status >/dev/null
command -v sv >/dev/null || { echo 'STOP: termux-services must already be installed.' >&2; exit 1; }
if [ -e "$SERVICE" ]; then
  [ ! -L "$SERVICE" ] && [ -f "$SERVICE/roomhub-owner" ] && \
    [ "$(cat "$SERVICE/roomhub-owner")" = "$ROOT" ] || { echo 'STOP: existing unowned service directory.' >&2; exit 1; }
  echo 'Service already installed; files preserved. Enabling the existing service.'
else
  "$PREFIX/bin/python" - <<'PY'
import socket
with socket.socket() as s:
 try:s.bind(('127.0.0.1',8090))
 except OSError:raise SystemExit('STOP: port 8090 occupied; no existing service stopped.')
PY
  mkdir -p "$LOG";chmod 700 "$LOG"
  STAGE="$(mktemp -d "$PREFIX/tmp/room-hub-llm.XXXXXX")"
  trap 'rm -rf -- "$STAGE"' EXIT
  mkdir "$STAGE/log"
  printf '#!%s/bin/sh\nexec 2>&1\numask 077\nexec "%s/bin/python" "%s/supervise.py" --root "%s"\n' \
    "$PREFIX" "$PREFIX" "$TOOLS" "$ROOT" > "$STAGE/run"
  printf '#!%s/bin/sh\nexec "%s/bin/svlogd" -tt "%s"\n' "$PREFIX" "$PREFIX" "$LOG" > "$STAGE/log/run"
  printf 's1048576\nn5\n' > "$LOG/config"
  printf '%s\n' "$ROOT" > "$STAGE/roomhub-owner"
  touch "$STAGE/down"
  chmod 700 "$STAGE/run" "$STAGE/log/run"
  mv "$STAGE" "$SERVICE"
  trap - EXIT
fi
# Do not touch Tasker or existing nginx/SSH/Cloudflare/Room Hub startup entries.
. "$PREFIX/etc/profile.d/start-services.sh"
for _ in $(seq 1 30); do [ -e "$SERVICE/supervise/ok" ] && break; sleep 1; done
sv-enable room-hub-llm
sv up "$SERVICE"
echo 'room-hub-llm requested. It may take time to load the model; Manager dispatch is still unchanged.'
for _ in $(seq 1 150); do
 if curl -fsS --max-time 2 http://127.0.0.1:8090/health >/dev/null 2>&1; then
   "$PREFIX/bin/python" "$TOOLS/runtime.py" probe
   echo 'LLM SERVICE READY: loopback :8090. Next: runtime.py enable';exit 0
 fi
 sleep 2
done
echo "STOP: model service not ready. See $LOG/current; no Manager settings changed." >&2
exit 1
