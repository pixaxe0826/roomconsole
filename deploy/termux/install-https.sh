#!/data/data/com.termux/files/usr/bin/bash
# Same runit supervisor as Room Hub. Does NOT require Termux:Boot; existing Tasker
# startup of termux-services will also start this enabled service.
set -Eeuo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
: "${PREFIX:?Run in the outer Termux shell.}"
IP="${1:-192.168.0.14}"
for bin in nginx openssl python sv svlogd; do command -v "$bin" >/dev/null || { echo "Missing $bin. Install Termux nginx, openssl-tool, python, termux-services." >&2; exit 1; }; done
DEST="$PREFIX/var/service/room-hub-https"
if [ -e "$DEST" ] && ! grep -q 'ROOM_HUB_HTTPS_SERVICE_V1' "$DEST/run"; then echo 'Existing service belongs to another configuration. No overwrite.' >&2; exit 1; fi
python "$ROOT/deploy/termux/setup_https.py" --root "$ROOT" --ip "$IP" "${@:2}"
nginx -t -p "$ROOT/data/https/" -c "$ROOT/data/https/nginx.conf"
if [ ! -e "$DEST" ]; then
 python - <<'PY'
import socket
s=socket.socket();s.settimeout(1)
if s.connect_ex(('127.0.0.1',8443))==0:raise SystemExit('STOP: 8443 already in use. No process was stopped.')
PY
 STAGE="$(mktemp -d "$PREFIX/tmp/roomhub-https.XXXXXX")"
 trap 'rm -rf -- "$STAGE"' EXIT
 mkdir -p "$STAGE/log" "$PREFIX/var/log/sv/room-hub-https"
 printf 's1048576\nn5\n' > "$PREFIX/var/log/sv/room-hub-https/config"
 touch "$STAGE/down"
 {
  printf '#!%s/bin/bash\n# ROOM_HUB_HTTPS_SERVICE_V1\nset -eu\nexec 2>&1\n' "$PREFIX"
  printf 'exec %q -p %q -c %q -g %q\n' "$PREFIX/bin/nginx" "$ROOT/data/https/" "$ROOT/data/https/nginx.conf" 'daemon off;'
 } > "$STAGE/run"
 {
  printf '#!%s/bin/bash\nexec %q -tt %q\n' "$PREFIX" "$PREFIX/bin/svlogd" "$PREFIX/var/log/sv/room-hub-https"
 } > "$STAGE/log/run"
 chmod 700 "$STAGE/run" "$STAGE/log/run"
 mv "$STAGE" "$DEST"
fi
termux-wake-lock || true
. "$PREFIX/etc/profile.d/start-services.sh"
for _ in {1..15}; do [ -p "$DEST/supervise/ok" ] && break; sleep 1; done
sv-enable room-hub-https
sv -w 15 restart "$DEST"
sleep 2
curl -fsS --max-time 10 --cacert "$ROOT/data/https/private/ca.crt" "https://127.0.0.1:8443/healthz"
printf '\nHTTPS service enabled. Existing nginx:8080, SSH and Cloudflare were not changed.\n'
