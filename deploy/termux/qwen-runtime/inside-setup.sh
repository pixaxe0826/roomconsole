#!/bin/bash
set -Eeuo pipefail
umask 077
[ -f /etc/debian_version ] || { echo 'STOP: this script requires the existing Debian PRoot.' >&2; exit 1; }
[ "$(uname -m)" = aarch64 ] || { echo 'STOP: this setup is for the V35 ARM64 environment.' >&2; exit 1; }
ROOT=/opt/room-hub
BASE="$ROOT/runtime/qwen"
TOOLS="$ROOT/deploy/termux/qwen-runtime"
mkdir -p "$BASE" "$ROOT/data/qwen-runtime"
# Hold the entire setup lock, including apt/build; don't overlap two installers.
exec 9>"$ROOT/data/qwen-runtime/installation.lock"
flock -n 9 || { echo 'STOP: another installation is running.' >&2; exit 1; }
python3 "$TOOLS/preflight.py" --root "$ROOT"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends build-essential cmake git ca-certificates python3
COMMIT=4762ad7316dcdec20016ab5985fb46a27902204d
TAG=b6000
SRC="$BASE/llama.cpp"
if [ ! -d "$SRC/.git" ]; then
    [ ! -e "$SRC" ] || { echo 'STOP: unexpected source folder. Inspect it; nothing removed.' >&2; exit 1; }
    git clone --depth 1 --branch "$TAG" https://github.com/ggml-org/llama.cpp.git "$SRC"
fi
[ "$(git -C "$SRC" rev-parse HEAD)" = "$COMMIT" ] || { echo 'STOP: source commit mismatch.' >&2; exit 1; }
[ -z "$(git -C "$SRC" status --porcelain --untracked-files=no)" ] || { echo 'STOP: source files were modified.' >&2; exit 1; }
# CPU-only, conservative ARMv8-A build; do not assume dotprod/i8mm support.
cmake -S "$SRC" -B "$BASE/build" \
 -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
 -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
 -DLLAMA_BUILD_SERVER=ON -DLLAMA_BUILD_TOOLS=ON \
 -DGGML_NATIVE=OFF -DGGML_CUDA=OFF -DGGML_VULKAN=OFF -DGGML_OPENCL=OFF \
 -DGGML_BLAS=OFF -DGGML_OPENMP=OFF -DGGML_LLAMAFILE=OFF \
 -DGGML_CPU_ARM_ARCH=armv8-a -DCMAKE_C_FLAGS=-march=armv8-a -DCMAKE_CXX_FLAGS=-march=armv8-a
cmake --build "$BASE/build" --target llama-server -j 2
"$BASE/build/bin/llama-server" --help > "$BASE/llama-help.txt" 2>&1
for flag in --jinja --chat-template-kwargs --reasoning-budget --no-context-shift --poll-batch --alias; do
    grep -q -- "$flag" "$BASE/llama-help.txt" || { echo "STOP: missing required engine option: $flag" >&2; exit 1; }
done
python3 - "$TOOLS" "$ROOT" <<'PY'
import pathlib,sys
sys.path.insert(0,sys.argv[1]);import runtime as r
root=pathlib.Path(sys.argv[2])
r.atomic_json(r.runtime_dir(root)/'build-receipt.json',{
 'commit':r.COMMIT,'tag':'b6000','binary_sha256':r.digest(r.binary_path(root)),
 'built_at':r.now(),'cpu_only':True,'arch':'armv8-a','build_jobs':2})
PY
python3 "$TOOLS/runtime.py" --root "$ROOT" download
python3 "$TOOLS/runtime.py" --root "$ROOT" smoke
printf '\nSetup complete. No Manager setting was enabled. Next: install-service.sh, then enable.\n'
