# Assistant Benchmark: external datasets, PR #20 compatible

벤치마크 프로그램만 Git으로 배포합니다. 실제 평가 입력·정답·fixture·schema·manifest·
projection·registry는 사용자의 외부 폴더에 두며 이 저장소에 추가하지 않습니다.
이 문서는 데이터 전송/실행 방법입니다. ChatGPT가 사용자의 F: 드라이브나 V35를
직접 읽거나 전송·배포했다는 의미가 아닙니다.

## 1. 적용 순서와 경로

기존 PR #19는 데이터셋을 포함한 이전 main 기반입니다. 그 브랜치를 운영 checkout으로
바꾸거나 ZIP으로 덮어쓰지 마세요. 최신 main(PR #20 포함)에서 만든 데이터 없는 교체 PR을
검토·병합하고 **병합된 main의 CI 성공**을 확인한 뒤 기존 V35 Git updater를 실행합니다.
동일한 새 소스가 Windows에도 있어야 전송 helper의 공유 검증 코드를 실행할 수 있습니다.

```text
GitHub roomconsole                         benchmark engine / helpers / tests / docs only
F:\room console\Benchmark\room_hub_benchmark_v1_250   authoritative local dataset
V35 ~/room-hub-benchmark-data/room_hub_v1/   explicitly verified copy
V35 ~/room-hub/artifacts/benchmarks/         run results (Git ignored)
V35 ~/room-hub/data/                        production data: never a benchmark input
```

`--suite-root`는 suite를 담은 부모 폴더입니다. 최종 V35 구조는 다음과 같습니다.

```text
~/room-hub-benchmark-data/
  room_hub_v1/
    manifest.json
    <case JSON/JSONL, schema, fixtures and optional referenced metadata>
    TRANSFER.json
```

원본 경로 자체에 manifest가 있거나, 바로 아래 하나의 suite 폴더에 manifest가 있으면
helper가 해당 폴더를 찾아 `room_hub_v1/` 한 단계로 설치합니다. 후보가 여러 개라면
`-Suite`와 일치하는 하위 폴더를 선택하거나 정확한 manifest 폴더를 `-SourcePath`로 지정합니다.
깊이를 추측해 무제한 탐색하거나 `room_hub_v1/room_hub_v1/`로 복제하지 않습니다.

## 2. Windows 준비: OpenSSH와 기존 Python

Windows PowerShell 5.1 이상, Windows OpenSSH의 `ssh`/`scp`, **Python 3.11 이상**이
필요합니다. 전송은 Python 표준 라이브러리와 기존 timezone data만 사용합니다. Windows
Python에 Asia/Seoul timezone data가 없다면 기존 Room Hub 개발 환경의 Python을
`-PythonExe`로 지정하세요. helper는 패키지·모델을 설치하지 않습니다.

```powershell
Get-Command ssh, scp, python
python --version
```

SSH 포트 기본값은 8022이며 `-SshPort`로 변경할 수 있습니다. 실제 사용자명/호스트는
정하지 않았습니다. 아래 `<V35-HOST>`와 `<TERMUX-USER>`를 실제 값으로 바꾸세요.
처음 연결하는 호스트라면 다음 명령으로 기기에 표시된 호스트 키 fingerprint를 대조한 뒤
known_hosts에 등록합니다. 전송 helper는 `StrictHostKeyChecking=yes`를 사용하며
검증을 끄거나 암호/개인키 내용을 저장하지 않습니다. hostname/IPv4/SSH config alias를
지원하며 이번 helper는 IPv6 문자열을 직접 받지 않습니다.

```powershell
ssh -p 8022 "<TERMUX-USER>@<V35-HOST>"
# 확인 후 exit로 Windows PowerShell에 복귀
```

## 3. 공식 전송 방법

Windows의 **새 roomconsole 소스 루트**에서 실행합니다. 이 경로는 데이터 폴더가 아닙니다.
source의 공백은 따옴표와 native argument 배열로 보존합니다.

```powershell
.\deploy\windows\Benchmark_Data_Push.ps1 `
  -SourcePath 'F:\room console\Benchmark\room_hub_benchmark_v1_250' `
  -Suite room_hub_v1 `
  -SshHost '<V35-HOST>' `
  -SshUser '<TERMUX-USER>' `
  -SshPort 8022
```

`-SourcePath`를 생략하면 위 F: 경로를 사용합니다. 호스트/사용자 생략 시 실행 중 질문합니다.
다른 Python은 `-PythonExe 'C:\path with space\python.exe'`, SSH 키는
`-IdentityFile 'C:\Users\YOU\.ssh\id_ed25519'`로 지정할 수 있습니다. 키 내용은 읽어
로그에 출력하지 않으며 OpenSSH가 정상적으로 사용합니다.

전송 전 로컬 검사만 하고 패키지를 보관하려면:

```powershell
.\deploy\windows\Benchmark_Data_Push.ps1 `
  -SourcePath 'F:\room console\Benchmark\room_hub_benchmark_v1_250' `
  -Suite room_hub_v1 -PrepareOnly
```

`prepared.json`과 `bundle.zip`은 출력에 표시된 임시 폴더에 남습니다. 실제 원본은 변경하지
않습니다. `-PrepareOnly` 없이 실행한 경우에는 성공/실패 후 로컬 임시 패키지만 제거합니다.

전송 흐름은 **원본 검사 → 해시 고정 ZIP 생성 → ZIP 자체 재검증 → 원격 .incoming/token
생성 → SCP → 원격 ZIP SHA256 및 파일별 크기/해시 확인 → schema/digest 확인 → 확정**입니다.
SSH/SCP가 실패하거나 파일이 누락되면 정상 suite 디렉터리로 승격하지 않습니다.

이미 같은 이름이 있으면 거부합니다. 명시적인 교체는 같은 명령에 `-Force`를 추가합니다.
기존 폴더가 이 helper로 설치되고 무결성이 확인될 때만 교체하며 이전 복사본을
`.backups/`에 보존합니다. 사용자 임의 폴더나 변조된 이전 폴더는 `-Force`로도 지우지 않습니다.
동일 파일시스템의 rename을 사용하지만 기존 폴더를 백업하고 새 폴더로 바꾸는 사이에는
짧은 미존재 구간이 있을 수 있습니다. 여러 디렉터리를 한 번에 교환하는 커널 수준의
원자 연산이라고 주장하지 않습니다. 교체 중 새 실행은 시작하지 마세요.

## 4. 수동 SCP: 검증 패키지만 전송

단순 `scp -r`로 최종 suite 폴더를 덮어쓰는 방식은 권장하지 않습니다. helper와 같은
단계적 전송을 수동으로 실행할 수 있습니다. 다음은 Windows 소스 루트에서 실행합니다.

```powershell
$Target = '<TERMUX-USER>@<V35-HOST>'
$Port = 8022
$Token = [Guid]::NewGuid().ToString('N')
$Out = Join-Path $env:TEMP "room-hub-manual-$Token"
python -m benchmarks.transfer prepare `
  --source 'F:\room console\Benchmark\room_hub_benchmark_v1_250' `
  --output $Out --suite room_hub_v1
if ($LASTEXITCODE -ne 0) { throw 'Local validation failed' }
$Meta = Get-Content -LiteralPath (Join-Path $Out 'prepared.json') -Raw -Encoding UTF8 | ConvertFrom-Json
('exec bash "$HOME/room-hub/deploy/termux/benchmark-data.sh" prepare ' + $Token) |
  ssh -p $Port -o StrictHostKeyChecking=yes $Target 'bash -s'
if ($LASTEXITCODE -ne 0) { throw 'Remote staging failed' }
scp -P $Port -o StrictHostKeyChecking=yes (Join-Path $Out 'bundle.zip') `
  "${Target}:room-hub-benchmark-data/.incoming/$Token/bundle.zip"
if ($LASTEXITCODE -ne 0) { throw 'SCP failed; do not promote' }
('exec bash "$HOME/room-hub/deploy/termux/benchmark-data.sh" install ' + $Token + ' ' + $Meta.archive_sha256) |
  ssh -p $Port -o StrictHostKeyChecking=yes $Target 'bash -s'
if ($LASTEXITCODE -ne 0) { throw 'Remote validation/promotion failed' }
```

이 수동 방법은 패키지/검증 파일을 로컬 `$Out`에 보관합니다. 원격 실패 시 `.incoming/`에
남은 특정 token은 격리 상태이며 suite 목록에 포함되지 않습니다. 실패 원인을 확인한 뒤
해당 token만 정리하세요. `.import-lock`이 남으면 실행 중인 전송이 없는지 확인 후 정리하고,
다른 전송과 경쟁하면서 lock을 강제로 제거하지 마세요.

## 5. V35 실행 (바깥 Termux)

wrapper가 기본 외부 경로를 `/opt/benchmark-data`에 bind하고 --suite-root를 자동 전달합니다.

```bash
bash "$HOME/room-hub/deploy/termux/benchmark-data.sh" verify room_hub_v1
bash "$HOME/room-hub/deploy/termux/benchmark.sh" list
bash "$HOME/room-hub/deploy/termux/benchmark.sh" validate --suite room_hub_v1

# 먼저 소규모: 임의 case ID를 추측하지 않고 앞 3개만 선택
bash "$HOME/room-hub/deploy/termux/benchmark.sh" run --suite room_hub_v1 \
  --mode full --name smoke-disabled-01 --llm disabled --limit 3
bash "$HOME/room-hub/deploy/termux/benchmark.sh" run --suite room_hub_v1 \
  --mode full --name smoke-qwen-01 --llm local --limit 3

# 고유 업무만, 이어서 전체. 실제 문항 수는 외부 manifest/파일로 검증
bash "$HOME/room-hub/deploy/termux/benchmark.sh" run --suite room_hub_v1 \
  --mode full --name baseline-unique-01 --llm local --source-type unique
bash "$HOME/room-hub/deploy/termux/benchmark.sh" run --suite room_hub_v1 \
  --mode full --name baseline-v1-qwen --llm local
```

실제 사용자 세트는 이 구현 작업에서 읽지 않았습니다. V1을 의도했다면 validate에서
`cases:250`, `valid:true`와 예상 version/hash를 확인해야 합니다. 250개로 하드코딩하지
않았습니다. `--limit 3` 문항이 모두 규칙 처리되면 local 옵션이어도 실제 LLM 호출은 0회일
수 있습니다. trace의 `transport_attempted`/집계 LLM 횟수를 확인하세요.

기본 모델 서버는 `http://127.0.0.1:8090/v1`, model ID는 `Qwen3-0.6B-Q5_K_M.gguf`입니다.
실제 설정이 다르면 확인한 `--endpoint`/`--model`을 지정합니다. 원격 LAN endpoint는 기존
loopback-only 계약상 거부됩니다. 필요할 때만 기존 `HUB_LLM_API_KEY` 환경을 사용하고
명령줄/데이터셋에 키를 쓰지 마세요. wrapper가 서비스 설정 파일의 키를 자동 로드하지는
않습니다. `--llm local`은 모델 설치/서버 시작 명령이 아닙니다.

기본 `--timeout 180` (10~600), `--max-tokens 256` (16~1024), temperature 0이며
실제 capability 처리 경로는 기존 토큰 상한을 적용할 수 있습니다. trace의 실제 payload가
기준입니다. timeout/model/token 설정은 비교 실행 간 같게 유지합니다.

외부 경로를 바꾸려면 wrapper 옵션을 command **앞**에 둡니다.

```bash
bash "$HOME/room-hub/deploy/termux/benchmark.sh" \
  --data-root "$HOME/another-benchmark-data" validate --suite another_suite
# 또는 ROOM_HUB_BENCHMARK_DATA 환경변수 (바깥 Termux 경로)
```

wrapper의 `--suite-root`는 고정 guest 경로 `/opt/benchmark-data`만 허용합니다.
일반 Python CLI에서는 원하는 외부 부모 폴더를 지정합니다.

```bash
python -m benchmarks --suite-root /external/benchmark-data list
python -m benchmarks --suite-root /external/benchmark-data validate --suite room_hub_v1
python -m benchmarks --suite-root /external/benchmark-data run --suite room_hub_v1 \
  --mode nlu --name nlu-01 --llm local
```

일반 CLI에서 --suite-root 또는 `ROOM_HUB_BENCHMARK_DATA`가 없으면 데이터 명령을 거부합니다.
`compare`와 도움말은 데이터 없이 사용할 수 있습니다. `--results-root`도 command 앞에 둡니다.
`--case`/`--category`/`--tag`는 반복 가능하며 tags는 모두 포함(AND), categories/IDs는 선택
목록 내 하나(OR), 필터 종류 사이는 AND입니다. `--source-type`과 `--split`도 유지합니다.
다중 `--suite`는 suite별 결과 이름을 분리합니다. 세션 중간 turn만 선택하면 거부합니다.

## 6. 격리와 PRoot 읽기 전용의 한계

PRoot의 일반 `--bind host:guest`는 경로 매핑이며 **커널 수준 read-only mount를 보장하지
않습니다**. 지원하지 않는 `:ro` 옵션이나 chmod만으로 root/동일 UID의 쓰기까지 차단했다고
주장하지 않습니다. 현재 방어는 data-only loader의 읽기 전용 사용, 원본/출력 경로 분리,
심볼릭 링크·junction·hardlink/경로 탈출 거부, 전송 inventory 및 실행 직전/파싱 후 해시 검사입니다.
악의적인 동일 계정 프로세스의 파일 변경을 막는 OS 격리 경계는 아닙니다.

실제 interpreter worker에는 입력 text, 명시적 세션, 고정 시각, 합성 fixture 및 연결 설정만
전달합니다. 정답·문항 ID·태그·suite 파일 경로는 전달하지 않습니다. 원본은 수정하지 않고,
실행 결과는 별도 디렉터리에 씁니다. 일반 서버의 lifespan/HTTP/ASR/알람 음향을 시작하지 않습니다.
온도·전원·부하를 기록하고 실제 음성/LLM 요청과 동시에 성능 측정을 하지 마세요.

## 7. 실제 Core 및 PR #20 타이머 호환

`LLMHub._insert` 이후 기존 `_execute`, rules, WidgetBridge/AssistantEngine, ChatBackend,
validation/grounding/정책/Registry/응답 생성 경로를 사용합니다. 별도 NLU/router는 만들지 않습니다.
FakeMemo/Todo/Calendar/Alarm/TimerAdapter는 실제 서비스를 상속하며 임시 SQLite로 연결합니다.
새로운 Adapter는 격리 검토 전 거부합니다. SQLite 트랜잭션 의미는 보존하고 벤치마크 연결만
context 종료 시 닫습니다. 생성 실패/정상 종료 모두 관찰 훅·환경·factory를 복구합니다.

독립 문항마다 DB/실행 ledger/Registry/clock을 재생성합니다. 명시적 session_id+turn만
세션 안에서 상태를 유지하며 범용 대화 문맥을 새로 해석해주는 기능은 아닙니다.

PR #20 호환 변경: 외부 `fixtures.json`에 `layout`이 있으면 **기존 production Layout**으로
검증하고 실제 widget ID를 유지합니다. `timers`에는 id, label, duration_seconds, state,
started_at/deadline_at, ended_at, version, widget_id를 명시할 수 있습니다. 시간은 timezone을
포함한 ISO 문자열 또는 epoch 숫자입니다. 기존 레코드는 실제 TimerService.reconcile_widgets로
빈 위젯에 연결하며 deadline/receipt를 재작성하지 않습니다. layout을 주지 않은 세트에는
타이머 위젯을 임의 생성하지 않습니다. 이전 no-layout 동작과 새 위젯별 동작을 구분합니다.

테스트로 서로 다른 widget_id의 독립 시작(240/90초), 한 위젯 busy/다른 위젯 idle,
모두 busy 거부, scoped stop 소유권, 요청 replay, workspace current=마지막 시작 타이머,
legacy 3개/위젯2개의 overflow와 원래 deadline 유지가 확인되어야 합니다.
모델 입력에 widget_id 권한을 추가하지 않고 optional 출력은 그대로 관찰합니다.
운영 타이머·음성 코드·UI·모델/프롬프트는 이 전환에서 변경하지 않습니다.

## 8. 모드, 채점, 결과

| 모드 | 경계 |
|---|---|
| nlu | Proposal/WidgetRequest 검증 경계에서 중단; capability/slot/route 측정 |
| decision | 실제 validation/grounding/confirmation preview까지, 최종 Adapter 실행 차단 |
| full | sandbox 서비스 실행 및 policy/status/execution boundary까지 채점 |

이전 non-Widget stage 내부의 부작용 없는 조회는 앞선 모드에서도 계산될 수 있습니다.
관리자 confirmation은 자동 클릭하지 않습니다. 정상 write preview는 CONFIRM이지 가짜 실행
성공이 아닙니다. 즉시 timer.start/stop만 기존 예외를 유지합니다.

schema는 문서화된 slot dialect(타입/enum/required/properties/items/date/time)이지 전체
JSON Schema 구현은 아닙니다. `projection.json`은 언어 재해석 없이 기능명/필드 표현만 대응합니다.
정답에서 누락 slot을 채우지 않습니다. optional projection/registry는 manifest에서 명시해야
digest/채점에 포함됩니다. 원본에 필요한 metadata가 없으면 prepare가 거부합니다. 파일 구조를
추측하거나 실제 250문항의 정답/schema/projection을 Git에 넣어 보충하지 않습니다.

case_files가 없는 legacy manifest는 cases.jsonl/all_250.jsonl/cases.json 중 하나만 존재할 때
읽습니다. 명시적 case_files가 가장 안전합니다. 직접 검사된 manifest·schema·fixtures·cases·
projection·registry의 canonical JSON SHA256을 `dataset_hash`로 기록합니다. ZIP SHA256은
전송 bytes 검사용으로 별개이며 README/SOURCES/provenance는 inventory의 파일 SHA256으로
검증합니다. 이 해시는 변조 탐지 수단이지 서명/평가 정답의 정확성 보증은 아닙니다.

config.json과 summary에는 suite/version/총 case_count/선택 IDs/dataset hash/생산·벤치마크
Git SHA/소스별 hash/mode/model/endpoint가 기록됩니다. 둘이 같은 checkout이므로 두 Git SHA는
동일한 것이 정상입니다. dirty 여부도 보존하며 확인하지 못한 모델 seed/context/quantization을
파일명에서 추측하지 않습니다.

capability/slot/policy, mode/task success, clarification, 잘못된 쓰기/제어 실행,
구조적 hallucination, LLM 실제/시도/차단, route, unique/paraphrase, 그룹 일관성·전체 성공,
평균/p50/p90/p95/p99/max latency를 기록합니다. 입력/model 비가용과 하네스 오류는 정확도
분모에서 제외하고 coverage를 표시합니다. 실행 종료코드 0은 모든 문항 정답을 뜻하지 않습니다.
관측 오실행 0건은 미평가 경로의 안전성 보증이 아닙니다.

Full 성공은 capability·critical slots·policy·status·실행 경계가 맞아야 합니다.
`expected_response` 자연어는 안내이며 exact-match 정답이 아닙니다. 필수 응답 사실 검사가
필요한 세트는 `response_assertions`를 명시해야 합니다. 지원하지 않는 기능/문맥/CANCEL 등은
실패로 관찰될 수 있으며 모델 정확도와 제품 지원 범위 차이를 구분하세요.

```text
artifacts/benchmarks/<run-name>/
  summary.html / summary.md / FAILURES.md
  metrics.json / cases.csv / raw_results.jsonl / failures.jsonl
  config.json / progress.json / trace/
```

문항마다 checkpoint, 중단 시 missing을 성공으로 처리하지 않음, 기존 run 이름 덮어쓰기 금지.
자동 resume는 구현하지 않았으므로 재실행에는 새 이름을 사용하세요. summary/CSV는 escape하고
보고서 작성용 LLM을 호출하지 않습니다. 개인정보를 테스트한 결과/trace는 Git에 올리지 않습니다.

```bash
bash "$HOME/room-hub/deploy/termux/benchmark.sh" compare baseline-v1-qwen parser-v1-qwen
```

기본 compare는 dataset hash/mode/선택·채점 ID가 다르면 거부합니다. --allow-incompatible은
공통으로 평가된 문항의 부분 비교이지 전체 개선 주장이 아닙니다. 비교 보고서는 별도
`<before>-vs-<after>/`의 JSON/Markdown/HTML로 저장합니다. 실제 모델·하드웨어·온도/부하 조건도
동일하게 유지하고 no-LLM subset과 전체 local 결과를 개선율로 비교하지 마세요.

## 9. 검증·Git·롤백

CI는 실제 suite 없이 `tests/benchmark_cases.py`에서 생성한 작은 합성 세트를 OS temp 폴더에
만들어 loader/전송/채점/보고서를 검증합니다. 새 eval corpus를 다운로드하지 않습니다.

```bash
python -m pytest -q tests/test_benchmark.py tests/test_benchmark_external.py tests/test_benchmark_wrappers.py
python -m pytest -q tests/test_timers.py tests/test_timer_instances.py tests/test_timer_bridge.py
python -m pytest -q
python scripts/check_repo.py
python scripts/check_benchmark_policy.py
bash -n deploy/termux/benchmark.sh deploy/termux/benchmark-data.sh
```

Windows CI는 실제 PowerShell parser/native argv와 stub SSH/SCP orchestration을 실행합니다.
실제 Windows→V35 네트워크, PRoot, 물리 기기/실제 Qwen 검증과 구분합니다.
데이터 명령은 repo 내부/ancestor/Git tree를 거부하고 특정 dataset 경로는 .gitignore와
tracked-path CI 검사로 차단합니다. JSON 전체 무시 규칙으로 정상 widget manifest를 깨지 않습니다.

깨끗한 교체 PR은 실제 평가 데이터 커밋을 조상으로 포함하지 않습니다. 그러나 기존 PR #19,
이전 branch/PR refs/과거 전송 workflow/캐시/복제본의 이미 공개된 데이터까지 GitHub 전체에서
지워졌다는 뜻은 아닙니다. 단순 PR 종료나 branch 제거만으로 전역 삭제를 보장하지 않습니다.

롤백은 검토된 revert PR → 병합 main CI → 기존 updater입니다. 외부 dataset/results는 코드
업데이트와 독립적입니다. 운영 data/나 모델은 삭제하지 않습니다. 이 PR은 생산 DB migration을
추가하지 않으며 PR #20의 타이머 스키마를 되돌리지 않습니다.

## M2: dataset/LLM 없이 기존 결과 분석

엔진 1.2.0은 기존 채점/운영 동작을 유지하고 `analyze`, `verify-analysis`,
`compare-analysis`를 추가합니다. M1 폴더에는 파일을 쓰지 않으며 별도의
`artifacts/benchmarks/analysis/<name>/`에 진단을 생성합니다.

```bash
bash "$HOME/room-hub/deploy/termux/benchmark.sh" analyze baseline-v1-qwen --name m1-taxonomy-v1
bash "$HOME/room-hub/deploy/termux/benchmark.sh" verify-analysis m1-taxonomy-v1
```

데이터 재전송/250문항 재실행/모델 서버 시작은 필요 없습니다. 원본 config/raw/metrics/
progress가 필수이며 trace는 원인 근거에 사용합니다. 지원 여부는 실제 Assistant
계약에서 판정하며 없는 근거를 추측하지 않습니다. production app hash 불일치는
거부하고 전체 Git SHA의 benchmark-only 변경은 허용합니다.
상세한 검증 순서·오류·비교 범위는 [M2 사양서](BENCHMARK_M2.md)를 참고하세요.
