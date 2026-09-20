#!/data/data/com.termux/files/usr/bin/bash
# Adds only the room-hub service and a new, dedicated boot entry.
# Does not rewrite nginx, cloudflared, sshd or existing boot scripts.
set -Eeuo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
: "${PREFIX:?Run this in Termux.}"
export SVDIR="$PREFIX/var/service" LOGDIR="$PREFIX/var/log"
for bin in sv sv-enable svlogd; do command -v "$bin" >/dev/null || { echo 'Install termux-services first.' >&2; exit 1; }; done
[ -f "$ROOT/.venv-v35/pyvenv.cfg" ] || { echo 'Run setup.sh first.' >&2; exit 1; }
DEST="$SVDIR/room-hub"
BOOT="$HOME/.termux/boot/60-room-hub-services"
if [ -e "$DEST" ] || [ -e "$BOOT" ]; then
  echo "Refusing to overwrite an existing service/boot entry. Inspect: $DEST and $BOOT" >&2
  echo 'For an already installed service, use sv restart room-hub instead.' >&2
  exit 1
fi
# Fail early if the foreground test server is still running (or another server uses 8088).
if "$PREFIX/bin/python" - <<'PY'
import socket
with socket.socket() as s:
    s.settimeout(1)
    raise SystemExit(0 if s.connect_ex(('127.0.0.1', 8088)) == 0 else 1)
PY
then
  echo 'Port 8088 is in use. Stop the foreground Room Hub with Ctrl+C before installing the service.' >&2
  exit 1
fi
mkdir -p "$SVDIR" "$LOGDIR/sv/room-hub" "$HOME/.termux/boot"
chmod 700 "$LOGDIR/sv/room-hub"
printf 's1048576\nn10\n' > "$LOGDIR/sv/room-hub/config"
# Stage outside the watched service directory to avoid partially created services starting.
STAGE="$(mktemp -d "$PREFIX/tmp/room-hub-service.XXXXXX")"
trap 'rm -rf -- "$STAGE"' EXIT
mkdir -p "$STAGE/log"
touch "$STAGE/down"
{
  printf '#!%s/bin/bash\nset -eu\numask 077\nexec 2>&1\n' "$PREFIX"
  printf 'export HOME=%q\nexport PREFIX=%q\n' "$HOME" "$PREFIX"
  printf 'export PATH=%q\n' "$PREFIX/bin:$PREFIX/bin/applets:/system/bin"
  printf 'cd %q\nexec %q %q\n' "$ROOT" "$PREFIX/bin/bash" "$ROOT/deploy/termux/start.sh"
} > "$STAGE/run"
{
  printf '#!%s/bin/bash\nset -eu\numask 077\n' "$PREFIX"
  printf 'exec %q -tt %q\n' "$PREFIX/bin/svlogd" "$LOGDIR/sv/room-hub"
} > "$STAGE/log/run"
chmod 700 "$STAGE/run" "$STAGE/log/run"
mv "$STAGE" "$DEST"
{
  printf '#!%s/bin/bash\numask 077\n' "$PREFIX"
  printf 'export HOME=%q\nexport PREFIX=%q\n' "$HOME" "$PREFIX"
  printf 'export PATH=%q\n' "$PREFIX/bin:$PREFIX/bin/applets:/system/bin"
  printf '"$PREFIX/bin/termux-wake-lock"\n'
  printf '. "$PREFIX/etc/profile.d/start-services.sh"\n'
} > "$BOOT"
chmod 700 "$BOOT"
termux-wake-lock || true
. "$PREFIX/etc/profile.d/start-services.sh"
# runsvdir scans for newly created services periodically.
for _ in {1..15}; do [ -p "$DEST/supervise/ok" ] && break; sleep 1; done
sv-enable room-hub
for _ in {1..30}; do
  if curl -fsS --max-time 2 http://127.0.0.1:8088/healthz >/dev/null; then
    printf '\n'; sv status room-hub
    curl -fsS --max-time 3 http://127.0.0.1:8088/healthz; printf '\n'
    printf 'Service started. For reboot persistence, use Tasker or another Android boot automation to start termux-services; see docs/V35_TERMUX.md.\n'
    exit 0
  fi
  sleep 1
done
printf 'Service registered, but HTTP health check failed. Inspect:\n  tail -n 80 %s\n' "$LOGDIR/sv/room-hub/current" >&2
exit 1
