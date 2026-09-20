#!/data/data/com.termux/files/usr/bin/bash
# Thin Termux wrapper. The updater logic lives in Python to avoid shell-parser
# differences between GitHub Actions Linux and Android/Termux Bash.
set -Eeuo pipefail
umask 077

: "${PREFIX:?Run this in the outer Termux shell.}"
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
exec "$PREFIX/bin/python" "$ROOT/deploy/termux/update_from_git.py" "$@"
