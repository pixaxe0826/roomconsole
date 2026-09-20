#!/data/data/com.termux/files/usr/bin/bash
# Run in the outer Termux shell, not in Debian. Never removes/resets a distro.
set -Eeuo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
: "${PREFIX:?Run this in Termux. PREFIX is not set.}"
case "$ROOT/" in "$HOME/"*) ;; *) echo 'Extract Room Hub below Termux HOME, not /sdcard or Downloads.' >&2; exit 1;; esac
ARCH="$(dpkg --print-architecture)"
[ "$ARCH" = aarch64 ] || { echo "This guide targets aarch64 Termux; found $ARCH. Stop and inspect before changing the existing installation." >&2; exit 1; }
command -v proot-distro >/dev/null || { echo 'First: pkg install python proot-distro termux-services unzip curl' >&2; exit 1; }
[ -f "$ROOT/app/main.py" ] && [ -f "$ROOT/requirements.txt" ] || { echo 'Room Hub source is missing.' >&2; exit 1; }
# Latest CLI: Docker/OCI images. Older CLI: bundled distro definitions.
if proot-distro login roomhub -- /bin/true >/dev/null 2>&1; then
  echo 'Reusing existing roomhub environment (not resetting it).'
else
  HELP="$(proot-distro install --help 2>&1)"
  if printf '%s\n' "$HELP" | grep -Eq -- '--name([ =]|$)'; then
    proot-distro install --name roomhub debian:bookworm
  else
    proot-distro install --override-alias roomhub debian
  fi
fi
exec proot-distro login --bind "$ROOT:/opt/room-hub" roomhub -- /bin/bash /opt/room-hub/deploy/termux/inside-setup.sh
