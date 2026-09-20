#!/bin/bash
# Explicit optional download/build. NEVER triggered by an audio request.
set -Eeuo pipefail
umask 077
[ -f /etc/debian_version ] || { echo 'This script belongs inside the dedicated Debian environment.' >&2; exit 1; }
cd /opt/room-hub
MODEL="${1:-tiny}"
case "$MODEL" in tiny|base) ;; *) exit 2;; esac
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends build-essential cmake git curl ca-certificates ffmpeg
BASE=/opt/room-hub/runtime/stt
mkdir -p "$BASE/models"
VERSION=v1.8.3
if [ ! -d "$BASE/whisper.cpp/.git" ]; then
 [ ! -e "$BASE/whisper.cpp" ] || { echo 'Unexpected existing engine folder. Inspect it; nothing was deleted.' >&2; exit 1; }
 git clone --depth 1 --branch "$VERSION" https://github.com/ggml-org/whisper.cpp.git "$BASE/whisper.cpp"
fi
[ "$(git -C "$BASE/whisper.cpp" describe --tags --exact-match)" = "$VERSION" ] || { echo 'Engine checkout is not the pinned version.' >&2; exit 1; }
[ -z "$(git -C "$BASE/whisper.cpp" status --porcelain --untracked-files=no)" ] || { echo 'Engine tracked sources were modified; inspect manually.' >&2; exit 1; }
CMAKE_ARGS=(-DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DWHISPER_BUILD_TESTS=OFF -DGGML_NATIVE=OFF -DGGML_CUDA=OFF -DGGML_VULKAN=OFF)
# Do not compile instructions by probing the build machine; V35 is an ARMv8 CPU.
if [ "$(uname -m)" = aarch64 ]; then CMAKE_ARGS+=(-DGGML_CPU_ARM_ARCH=armv8-a -DCMAKE_C_FLAGS=-march=armv8-a -DCMAKE_CXX_FLAGS=-march=armv8-a); fi
cmake -S "$BASE/whisper.cpp" -B "$BASE/build" "${CMAKE_ARGS[@]}"
cmake --build "$BASE/build" --target whisper-cli -j 2
FILE="$BASE/models/ggml-$MODEL.bin"
valid_model() {
 python3 - "$1" "$MODEL" <<'PY'
import pathlib,struct,sys
p=pathlib.Path(sys.argv[1]);minimum=70_000_000 if sys.argv[2]=='tiny' else 140_000_000
if not p.is_file() or p.stat().st_size<minimum:raise SystemExit(1)
with p.open('rb') as f:
 if struct.unpack('<I',f.read(4))[0] != 0x67676d6c:raise SystemExit(1)
PY
}
if ! valid_model "$FILE"; then
 curl --fail --location --retry 3 --connect-timeout 20 --max-time 1800 \
  "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-$MODEL.bin" -o "$FILE.part"
 valid_model "$FILE.part" || { echo 'Invalid/incomplete model download. Configuration was not enabled.' >&2; exit 1; }
 mv "$FILE.part" "$FILE"
fi
# Test executable format and argument availability before enabling.
"$BASE/build/bin/whisper-cli" --help > "$BASE/cli-help.txt" 2>&1
grep -q -- '--output-txt' "$BASE/cli-help.txt"
python3 - "$BASE" "$MODEL" <<'PY'
import hashlib,json,os,pathlib,sys,time
base=pathlib.Path(sys.argv[1]);name=sys.argv[2];data=pathlib.Path('/opt/room-hub/data');data.mkdir(exist_ok=True)
p=data/'speech-config.json'
if p.exists():
 backup=data/('speech-config.before-'+str(time.time_ns())+'.json');backup.write_bytes(p.read_bytes());backup.chmod(0o600)
cfg={'enabled':True,'binary':str(base/'build/bin/whisper-cli'),'model':str(base/f'models/ggml-{name}.bin'),'model_name':f'{name} · multilingual','ffmpeg':'/usr/bin/ffmpeg','language':'ko','threads':2,'max_seconds':30,'timeout_seconds':180,'max_pending':4,'max_bytes':8388608,'max_stored_bytes':134217728}
tmp=data/'speech-config.json.tmp';tmp.write_text(json.dumps(cfg,indent=2,ensure_ascii=False)+'\n');tmp.chmod(0o600);tmp.replace(p)
h=hashlib.sha256()
with pathlib.Path(cfg['model']).open('rb') as f:
 for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
(base/'model-sha256.txt').write_text(h.hexdigest()+'  '+pathlib.Path(cfg['model']).name+'\n')
print('Engine installed. The SHA256 is a local integrity record, not an independent upstream signature.')
print('Now checking the actual installed engine with the upstream public English sample. This is not a Korean accuracy benchmark.')
PY

if ! /opt/room-hub/.venv-v35/bin/python /opt/room-hub/deploy/termux/speech_selftest.py; then
 python3 - <<'PY_DISABLE'
import json,pathlib
p=pathlib.Path('/opt/room-hub/data/speech-config.json');cfg=json.loads(p.read_text());cfg['enabled']=False;p.write_text(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n');p.chmod(0o600)
PY_DISABLE
 echo 'STOP: actual engine self-test failed. Speech was disabled; tasks and calendar still work. Inspect before retry.' >&2
 exit 1
fi
printf '\nSPEECH READY: actual engine smoke check passed. Restart Room Hub and test a short Korean recording.\n'
