# M3.4-0 — Odyssey / PC 추론 분리의 정식 런타임

이 패치는 Room Hub 0.1.7의 운영 안정화입니다. Parser, LLM prompt/JSON schema,
operation 등록, 확인·버전·receipt 정책, 벤치마크 채점과 평가 데이터는 변경하지 않습니다.
M3.4-A/NLU v2는 다음 작업이며 이 패치에 포함하지 않습니다.

## 1. 고정할 운영 구조와 적용 범위

```text
표시 기기 → 기존 HTTPS → Odyssey Room Hub / SQLite / 위젯 / 타이머·알람
                            ├─ loopback :8090/v1 → 기존 SSH → PC llama-server
                            └─ loopback :8178/inference → 기존 SSH → PC whisper-server
```

운영자가 선택한 구성은 Qwen3-1.7B Q5_K_M, Whisper Medium / careful(beam 5)입니다.
이 PR은 모델·CUDA·SSH·예약 작업을 설치하거나 현재 설정을 자동으로 교체하지 않습니다.
다른 기존 사용자의 기본 모델과 local CLI 설정도 그대로 보존합니다.
Windows의 로그인 시 자동 실행/숨김 창/supervisor는 기존 검증된 설치를 계속 사용합니다.
PC 전원만 켜진 상태와 Windows 해당 계정에 로그인한 상태는 다릅니다.

PC가 없어도 Room Hub core는 시작됩니다. 타이머·알람의 서버 상태 처리, 일반 HTTP API,
DB, 장치 연결 관리에는 원격 모델 준비를 기다리는 의존성이 없습니다. AI가 필요한 새
요청은 연결 실패/시간 초과로 종료합니다. 연결이 복구되어도 실패·취소한 요청을 자동
재전송하지 않습니다. 실제 소리 재생/브라우저 백그라운드 보장은 기존과 동일하게 별개입니다.

## 2. SQLite 연결 수명과 벤치마크

`app/sqlite_connection.py`의 `ClosingConnection` 하나를 운영 Store와
`benchmarks/storage.py`가 함께 사용합니다. SQLite의 기존 context manager가 먼저
commit/rollback을 처리하고, `finally`에서 handle을 닫습니다. PRAGMA 초기화 실패도 닫습니다.
WAL, 15초 busy timeout, foreign keys, 트랜잭션 내용과 lock 정책은 변경하지 않습니다.

`with store.connect()`는 단회 소유권 범위입니다. 종료된 connection을 재사용하거나
호출자가 소유한 connection을 다른 함수에서 다시 context manager로 감싸면 안 됩니다.

벤치마크가 만드는 전역 SQLite patch는 여전히 worker의 ExitStack 안에서만 유효합니다.
임시 디렉터리 밖 DB 접근, 별도 factory, 같은 이름의 다른 클래스, 임의 subclass는 계속
거부합니다. factory 검사를 삭제하거나 `issubclass`로 넓혀 충돌을 우회하지 않습니다.
운영 app의 함수나 소스에 benchmark용 분기 역시 넣지 않습니다.

## 3. Speech 설정

기존 `speech-config.json`에서 아래 네 필드만 추가로 사용합니다. 미지정이면 기존
`local_cli`입니다. remote는 운영자가 명시적으로 선택해야 하며 모델 경로를 추측하지 않습니다.

| 필드 | 의미 |
|---|---|
| `backend` | `local_cli` 또는 `remote_http` |
| `remote_url` | 숫자 loopback HTTP의 정확한 `/inference` 경로 |
| `remote_model` | 운영자가 확인해서 입력한 모델 이름. PC 파일 경로가 아님 |
| `remote_device` | `unknown`, `cpu`, `cuda`, `vulkan`, `metal`, `openvino` 중 설정값 |

remote 예시는 다음과 같습니다. 기존 전체 파일을 이 네 필드만으로 덮어쓰지 마세요.

```json
{
  "backend": "remote_http",
  "remote_url": "http://127.0.0.1:8178/inference",
  "remote_model": "medium",
  "remote_device": "cuda"
}
```

`remote_http`에서는 Odyssey에 Whisper 실행 파일/모델이 없어도 됩니다. FFmpeg는 계속
Odyssey에 필요합니다. 기존 `binary`/`model` 값은 롤백용으로 보존할 수 있지만 원격 추론에는
사용하지 않습니다. `threads`도 기존 값을 보존하되 remote 서버의 thread 수로 주장하지 않습니다.
실제 모델·스레드·GPU는 PC 서버 실행 인자가 결정합니다. 전송하는 어휘 힌트에는 기존 정책에
따라 작업 제목이 포함될 수 있으므로 PC도 신뢰하는 개인 장비여야 합니다.

허용 URL은 `http://127.0.0.1:<port>/inference` 또는 `[::1]`입니다. DNS 이름, LAN IP,
인증정보 포함 URL, query/fragment, `/load`, 기존 core 포트는 거부합니다.
HTTP 프록시 환경변수를 상속하지 않고 redirect/retry를 하지 않습니다. SSH는 운영자가
관리하며 PC 서버는 loopback에 바인딩합니다. Room Hub가 `/load`를 호출하거나 모델을 받거나
다른 호스트로 자동 fallback하는 기능은 없습니다.

선택적 `HUB_SPEECH_API_KEY`는 이를 지원하는 별도 신뢰 프록시의 인증 용도입니다.
stock whisper-server가 이 키를 검사한다고 가정하지 마세요. 관리자/입력/LLM 키를 재사용하거나
파일·로그·브라우저에 노출하지 않습니다. 기존 loopback SSH 구성에는 새 인증키가 필요 없습니다.

## 4. 전사와 취소 계약

로컬/원격 모두 기존 FFmpeg 변환, 16kHz mono PCM16, 파일 크기·길이·무음 검사를 공유합니다.
queue 한도와 한 번에 한 worker, 소유자별 조회/취소 권한은 그대로입니다. 원문 음원은 변경하지 않습니다.

원격은 변환한 WAV만 `input.wav`라는 고정 업로드 이름으로 한 번 POST합니다. 한국어 설정,
beam 1/3/5와 best_of=1, 현재 동적 hint를 전달합니다. `stt-accuracy.json`은 변경하지 않습니다.
현재 배포 wrapper와 같이 `verbose_json`, `no_timestamps=false`,
`no_language_probabilities=true`를 사용합니다. local CLI 인자는 종전 그대로이므로
CLI 대 서버 차이까지 사라진 정확도 A/B라고 주장하지 않습니다.

응답은 JSON/512KiB 이하이어야 하고 HTTP 200의 `error` 객체도 실패입니다. 중복 JSON key,
NaN, 빈 전사, 잘못된 응답, 지나친 길이를 거부합니다. 업스트림 오류 본문/자격증명은 사용자에게
반사하지 않습니다. 결과 `text`의 양끝 공백만 제거하며 LLM 보정·두 번째 패스는 없습니다.

연결 제한은 3초, 요청 전체 deadline은 기존 `timeout_seconds`, 변환은 기존 20초입니다.
취소하면 HTTP 대기를 취소하고 로컬 임시 파일을 정리합니다. 이것은 원격 GPU 연산 중지가
확인됐다는 뜻은 아닙니다. 서버가 disconnect를 감지하는 동작은 서버 버전/환경에 따릅니다.
PC 재연결 시 새 요청부터 가능하며, 이전 실패나 완료된 작업을 자동 재실행하지 않습니다.

## 5. 상태·모델·지연의 정직한 표시

관리자 LLM 화면의 **AI 노드 진단** 링크 또는 기존 HTTPS의 `/manager/runtime`을 사용합니다.
로그인한 관리자만 접근할 수 있습니다. paired display/ingest 키에는 새 권한이 없습니다.

| 요청 | 동작 |
|---|---|
| `GET /api/ai/status` | 설정과 최근 캐시만 읽음. 외부 접속/추론 없음 |
| `POST /api/ai/probe` | STT `/health`, LLM `/v1/models`를 병렬로 확인. 음성 전송/생성 없음 |
| `GET /healthz` | 기존 core 상태. AI가 꺼져 있어도 정상일 수 있음 |

probe 결과는 30초 뒤 미확인으로 바뀝니다. 설정이 달라져도 이전 probe를 재사용하지 않습니다.
화면의 자동 갱신은 GET 캐시 조회만 하며, 연결 확인 버튼을 눌러야 실제 probe가 발생합니다.
모델 목록의 ID가 설정과 다르면 `model_mismatch`이지 연결 성공이 아닙니다.

stock whisper.cpp v1.8.3의 `/health`는 모델명/장치 증거를 주지 않습니다. 따라서 `medium`과
`cuda`는 **설정값**, `reported_model`/`reported_device`는 **미확인(null)** 으로 남깁니다.
LLM `/models`의 ID 일치 역시 가중치 SHA 또는 GPU 사용의 증명이 아닙니다. 예전 CLI wrapper의
`base`/`cpu_only=true`를 실제 원격 실행 정보로 재사용하지 않습니다.

원격 전사 진단은 `backend=remote_http`, `model_identity_source=operator_config_not_verified`,
`cpu_only=null`, `threads=null`을 기록합니다. `remote_request_seconds`는 HTTP 전송·서버 대기·추론·응답
전체이고 순수 GPU 시간은 아닙니다. `whisper_seconds_including_load`는 remote에서 null입니다.
`runner_total_seconds`는 변환까지 포함합니다. 내부 시간이 겹치므로 더하지 않습니다.
서버가 제공하지 않는 token confidence/model-load 시간은 미확인이고, 원격 segment는 제한된
별도 진단일 뿐 확률을 실제 정확도로 표시하지 않습니다. 과거 진단은 재작성하지 않습니다.

PC에서는 `deploy/windows/RoomHub_AI_Diagnostics.ps1`로 모델 실행 인자/포트/프로세스 시작 시각과
예약 작업 상태를 읽을 수 있습니다. Highest 작업이면 관리자 PowerShell이 필요할 수 있습니다.
읽기 전용이며 SSH command line/키 경로를 출력하거나 프로세스를 종료하지 않습니다.
이 결과도 모델 가중치 검증이나 CUDA 사용 증거를 대신하지 않습니다.

## 6. 기존 설치에 적용 — 병합/검토 후 운영자가 수행

1. PR의 CI와 변경 내용을 확인하고 원본 코드·설정·SQLite의 일관된 백업을 별도로 보존합니다.
   실행 중 WAL DB는 main 파일만 복사하지 말고 SQLite backup API/`.backup` 또는 정지 후 백업을 사용합니다.
2. `/opt/room-hub`의 `git diff -- app/store.py`를 별도로 보존합니다. 기존 로컬 FD hotfix와 새 공통
   factory가 같은 목적임을 검토합니다. `git reset --hard`로 다른 로컬 설정·변경을 버리지 마세요.
   운영 중인 소스의 로컬 수정 때문에 checkout이 막히면 새 검토된 소스를 별도 checkout에 준비하고
   짧은 정지 구간에 교체합니다. 데이터 디렉터리·systemd 실행 사용자·기존 venv/HTTPS 경로는 유지합니다.
3. 새 코드에서도 기존 설정은 자동 이관되지 않습니다. 먼저 기존 local wrapper로 동작하는 상태를 확인한
   다음 아래 dry-run/apply로 native remote backend를 선택합니다. 기존 wrapper와 모델 파일은 지우지 않습니다.

```bash
# 새 M3.4-0 코드가 /opt/room-hub에 배치된 뒤; 실제 변경 없는 dry-run
cd /opt/room-hub
.venv/bin/python scripts/configure_remote_speech.py \
  --data-dir /var/lib/room-hub --model medium --device cuda

# 계획 검토 후에만 적용: 기존 설정의 0600 백업 + 원자적 교체
.venv/bin/python scripts/configure_remote_speech.py \
  --data-dir /var/lib/room-hub --model medium --device cuda --apply

# 위 도구는 enabled/accuracy/모델/서비스를 자동 변경하지 않음
sudo systemctl restart room-hub
systemctl is-active room-hub
curl -fsS --max-time 5 http://127.0.0.1:8088/healthz
```

운영 LLM URL/모델 ID는 Manager에서 기존 `http://127.0.0.1:8090/v1`, 선택한 1.7B ID를 유지합니다.
이 PR은 default 모델을 바꿔 기존 설치 전체에 강제 적용하지 않습니다. 일반 대화/실제 전사 시험과
관리자 진단 버튼으로 연결을 확인하세요. UI의 `ready`는 로컬 전제조건+worker 준비이며 원격 가용성은
별도 probe/요청 결과로 확인합니다.

운영 검증은 아래 순서로 수행합니다. 이 PR의 모의 HTTP 테스트가 실기기 검증을 대신하지 않습니다.

- PC online: 실제 음성 한 건, LLM 한 건, 진단 화면 모델/상태/지연. parser가 만든 변경은 기존 확인 절차 유지.
- PC offline: 기존 supervisor를 정상 종료하거나 PC를 끄고 core, 장치 연결/해제, 위젯과 타이머/알람 상태 유지 확인.
- PC online 복귀: 신규 AI 요청 성공, 이전 실패/취소 요청의 자동 재실행 없음 확인.
- 관리자 화면을 반복 열고 닫은 후 DB FD가 계속 증가하지 않는지 관찰. 재부팅/로그인 자동 연결도 다시 확인.

설정 롤백은 도구가 출력한 정확한 `speech-config.before-m340-...json`을 원래 이름으로 복원하고
Room Hub만 재시작합니다. 그러면 기존 `binary` wrapper 또는 local CLI를 다시 사용합니다.
코드 롤백 때는 반드시 기존 FD hotfix도 보존·재적용해야 합니다. 수정 전 원본 Store로만 되돌리면
장시간 운영에서 FD 누수가 재발할 수 있습니다. PC 1.7B/Whisper 설치와 다른 SSH 세션은 건드리지 않습니다.

## 7. 벤치마크/검증 범위

새 Store와 benchmark가 공통 factory를 사용하므로 M3.4-0 소스에서는 factory 충돌 없이 실행되어야 합니다.
기존 clean M3.3-B.1 worktree와 과거 결과는 그대로 보존합니다. app 소스 hash가 달라졌기 때문에
새 코드로 과거 raw를 다시 analyze하지 않습니다. 기존 `verify-analysis`/보존된 분석 비교를 사용하고,
새 런타임을 평가할 때는 새 이름으로 run/analyze/verify합니다. 평가 dataset·gold·projection은 불변입니다.

재현 명령:

```bash
python -m pytest -q tests/test_sqlite_runtime_m340.py tests/test_remote_speech_m340.py tests/test_ai_runtime_m340.py
python -m pytest -q
python scripts/check_repo.py
python scripts/check_benchmark_policy.py
python scripts/check_memo_language.py
python scripts/build_previews.py
git diff --exit-code -- previews/
python scripts/runtime_browser.py
```

`runtime_browser.py` 기본 모드는 Chromium intercepted HTTPS/CSP 테스트입니다.
브라우저 navigation이 차단된 환경에서만 명시적으로 `HUB_BROWSER_BRIDGE=1`을 쓰며,
이때는 about:blank/fetch binding UI 테스트로 기록합니다. HTTP/CSP 강제 적용을 검증했다고 주장하지 않습니다.
실제 CUDA/Windows Task Scheduler/Odyssey/음성 정확도/250-case 모델 실행은 운영자가 별도 확인합니다.

### 근거

- Python SQLite context manager는 트랜잭션을 처리하지만 connection을 닫지 않습니다:
  https://docs.python.org/3.13/library/sqlite3.html#how-to-use-the-connection-context-manager
- 원격 전송/health/verbose JSON 규격은 whisper.cpp **v1.8.3** 소스를 기준으로 검토했습니다:
  https://github.com/ggml-org/whisper.cpp/blob/v1.8.3/examples/server/server.cpp
- httpx는 `trust_env=False`로 ambient proxy를 사용하지 않습니다:
  https://www.python-httpx.org/environment_variables/
