# M2 — 지원 계약과 실패 원인 분석

M2는 **측정된 결과를 설명하는 단계**입니다. 운영 Parser/Router/Adapter, 모델/Whisper,
원본 데이터셋, M1의 채점 정의를 바꾸지 않습니다. 기존 엔진 1.1.0에서 외부 데이터셋을
분리했으므로 이번 엔진 버전은 **1.2.0**, 진단 규칙 버전은 **1.0.0**입니다.
제품 `VERSION`은 바꾸지 않습니다.

## 1. 확정된 계약

Supported는 **현재 Assistant가 노출한 사용 가능한 operation**입니다. UI나 DB에서
가능한 일이라는 이유만으로 지원으로 세지 않습니다. `WidgetRegistry`와 기존 Assistant
registry를 함께 읽되, 오래된 registry의 미지원 설명이 새 Bridge의 실제 등록을 덮어쓰지
않습니다. Calendar가 내부적으로 Todo 코드를 공유해도 두 domain을 서로 별칭으로 합치지
않습니다.

이 지표는 operation 수준입니다. `calendar.list`가 등록돼 있어도 모든 시간 필터나
말투가 지원된다는 뜻은 아닙니다. 등록된 기능이 특정 인자를 지원하지 않는 문제는 원본
실패로 남습니다. M2는 운영 규칙을 고쳐 이를 숨기지 않습니다.

M1 폴더에는 **파일을 추가하지도, 수정하지도, 삭제하지도 않습니다.** 입력 폴더의 모든
일반 파일을 해시로 기록하고 분석 전후 동일함을 확인합니다. 파생 결과는 별도
`artifacts/benchmarks/analysis/<name>/`에만 생성합니다. 기존 이름은 덮어쓰지 않습니다.

## 2. 사용자가 먼저 해야 할 것

이 기능의 PR을 검토·병합한 뒤 **병합된 main의 CI 성공**을 확인하고 기존 Git updater로
V35 코드를 갱신합니다. 데이터셋 재전송, 250문항 재실행, 모델 설정 변경은 필요 없습니다.

원본 결과는 V35의 다음 위치에 있어야 합니다.

```text
~/room-hub/artifacts/benchmarks/baseline-v1-qwen/
  config.json
  progress.json
  metrics.json
  raw_results.jsonl
  trace/*.json
  summary.md / summary.html / ...
```

채팅에 복사한 요약만으로는 자동 분석을 실행할 수 없습니다. 원본 config/raw/metrics/
progress 네 파일이 필수이며, trace가 없으면 해당 문항의 원인 근거가 부족하다고 표시합니다.
원본 실행은 COMPLETED 또는 PARTIAL_EVALUATION이어야 하고 선택된 모든 문항의 처리 기록이
있어야 합니다. 중단된 실행이나 집계 불일치는 정상 기준선으로 수리하지 않고 거부합니다.

## 3. V35에서 M1 전체 결과 분석

**바깥 Termux**에서 아래를 각각 실행합니다. wrapper는 기존 roomhub PRoot와
`.venv-v35`만 사용합니다. 분석 명령에서는 dataset 폴더를 bind하거나 읽지 않습니다.
서비스 설치·중지·재시작, Qwen 호출, 운영 DB 접근은 수행하지 않습니다.

```bash
# 이미 생성한 M1의 250문항 결과를 읽습니다. run 명령이 아닙니다.
bash "$HOME/room-hub/deploy/termux/benchmark.sh" \
  analyze baseline-v1-qwen --name m1-taxonomy-v1

# 분석 결과의 해시와 원본 파일 불변성을 다시 확인합니다.
bash "$HOME/room-hub/deploy/termux/benchmark.sh" \
  verify-analysis m1-taxonomy-v1

# 사람이 읽을 진단 요약
sed -n '1,260p' "$HOME/room-hub/artifacts/benchmarks/analysis/m1-taxonomy-v1/summary.md"

# 지원 operation 안의 실패와 근거
sed -n '1,240p' "$HOME/room-hub/artifacts/benchmarks/analysis/m1-taxonomy-v1/SUPPORTED_FAILURES.md"
```

`verify-analysis`는 `valid: true`, `original_files_unchanged: true`,
`derived_files_unchanged: true`를 확인합니다. M1 전체 run에서는 원본
`overall_task_success`가 17/250이고 `false_execution`이 0/250인지 확인하세요.
**Supported 지표가 얼마인지는 실제 raw/trace 분석 전에는 정하지 않습니다.**

100개 실행은 이름을 따로 지정합니다. 전체 결과와 혼동하지 마세요.

```bash
bash "$HOME/room-hub/deploy/termux/benchmark.sh" \
  analyze baseline-unique-01 --name m1-unique-taxonomy-v1
bash "$HOME/room-hub/deploy/termux/benchmark.sh" \
  verify-analysis m1-unique-taxonomy-v1
```

같은 `--name`으로 재실행하면 오류로 종료하는 것이 정상입니다. 필요할 때 새 이름
`m1-taxonomy-v2`를 사용하고 이전 분석은 보존합니다. 업데이트한 코드가 M1의 production
내용과 다르면 분석은 거부됩니다. 오류를 무시하거나 M1 config의 SHA를 수정하지 마세요.

## 4. Windows에서 기존 결과 분석

Windows의 `F:\room console\roomconsole-main`에는 M2 코드가 있어야 합니다. **V35 결과
폴더 전체를 PC의 별도 결과 루트로 복사한 경우에만** 다음처럼 실행합니다. 이 명령은
V35에 자동 접속하지 않으며 F: 원본 데이터셋도 읽지 않습니다. Python은 기존 개발
requirements가 설치된 환경을 사용합니다.

```powershell
Set-Location -LiteralPath 'F:\room console\roomconsole-main'
$Results = 'F:\room console\BenchmarkResults'
# $Results\baseline-v1-qwen\에 V35 원본 결과 전체가 있을 때만 실행
python -m benchmarks --results-root $Results analyze baseline-v1-qwen --name m1-taxonomy-v1
python -m benchmarks --results-root $Results verify-analysis m1-taxonomy-v1
```

`--results-root`는 전역 옵션으로 명령 앞에 둡니다. V35 실행 때 저장한 capability schema와
로컬 dependency가 다르면 catalog 검사로 중단될 수 있습니다. 그 경우 V35에서 동일 환경으로
분석하거나 오류와 dependency 정보를 확인하세요. source/계약 확인을 끄는 옵션은 없습니다.

## 5. 파생 파일과 읽는 순서

```text
artifacts/benchmarks/analysis/m1-taxonomy-v1/
  analysis.json                  분석 출처·규칙·원본/현재 소스 구분
  metrics.json                   지원 범위·분류·보조 지표
  original_metrics.json          원본 metrics 복사, 재채점 아님
  support_snapshot.json          생산 코드에서 읽은 지원 계약
  input_integrity.json           원본 폴더 파일별 SHA256/크기
  analysis_integrity.json        분석 산출물별 SHA256/크기
  taxonomy.jsonl                 primary 1개와 secondary flags/근거
  scored_results.jsonl           고정 cohort 비교용 저장 결과 복사
  summary.md / summary.html      한국어 보고서
  cases.csv                      스프레드시트용 분석 표
  SUPPORTED_FAILURES.md           지원 operation의 실패
  UNSUPPORTED_CAPABILITIES.md     미등록/미노출/사용 불가 계약
  UNDETERMINED.md                 근거 부족 또는 projection 확인 대상
```

먼저 summary에서 원본 전체 수치가 보존됐는지 보고, Supported-only와 Primary 분류를
확인한 다음 개별 사례와 `taxonomy.jsonl`의 evidence를 읽습니다. 분석 출력에는 원문이나
기대값이 포함될 수 있으므로 **dataset처럼 로컬 전용**입니다. GitHub/PR/CI artifact에
실제 사용자 결과를 올리지 않습니다. 소스에는 작은 합성 테스트만 포함합니다.

## 6. 지원 snapshot과 M1 호환

metadata 전용 subprocess가 실제 registry를 읽습니다. `app.main`을 import하지 않으며,
SQLite 연결·socket 연결·서비스 호출을 금지한 상태에서 catalog를 구성합니다. snapshot은
시간이나 모델 출력을 포함하지 않아 같은 production/dependency에서 deterministic합니다.

`production_source_hash`는 M1 runner와 동일하게 `app/**/*.py`의 경로와 바이트를 해시합니다.
M2가 benchmark 파일만 변경해 Git SHA가 바뀌어도 app 내용이 같으면 분석할 수 있습니다.
반대로 production hash가 다르면 기본적으로 중단합니다. 실행 당시 trace에 저장된
`runtime_catalog`가 있으면 새 metadata catalog와도 대조합니다. 이것은 임의 코드 실행이나
과거 branch checkout, 외부 네트워크 fetch를 수행하는 기능이 아닙니다.

M1에는 suite projection 사본이 기록되지 않았습니다. M2는 입력 원문이나 expected에서
별칭을 추측하지 않고, production `assistant_capability`와 저장된
`actual.native_capability → actual.capability` 관계만 복구합니다. 별칭이 불명확하거나
상충하면 거부합니다. 관측되지 않은 별칭 때문에 어떤 정답 이름이 미등록으로 남을 수
있으므로 `alias_provenance`를 검토하세요. 이것만으로 제품에 의미적으로 동등한 API가
없다고 단정하면 안 됩니다.

새 `run`은 측정 시작 전에 `support_snapshot.json`과 이름 변환 receipt를 기록하고,
부모 채점 프로세스에 `scoring_context.critical_slots`를 보존합니다. 이 자료는 실행
worker나 모델에 전달하지 않습니다. 기존 M1 raw/score/metrics를 고치지 않습니다.

## 7. 실패 분류 계약

평가 가능한 실패당 primary 하나, 기존 실패 flag는 secondary 전부 보존합니다. 성공과
미평가에는 primary가 없습니다. 미지원이나 미평가라는 이유로 false execution 등 안전
flag를 지우지 않습니다. 분류 규칙은 순서가 있고 `taxonomy_rules_hash`로 고정합니다.

| 분류 | 판정 의미 |
|---|---|
| UNSUPPORTED_CAPABILITY | 기대 operation이 등록·노출·가용 계약에 없음 |
| ROUTING_FAILURE | 선행 후보 제외/비모델 분기 불일치의 기록된 근거 |
| LLM_PROPOSAL_FAILURE | 실제 전송과 잘못된 모델 제안/형식 거부 근거 |
| CAPABILITY_FAILURE | 기능 불일치는 확인되나 발생 단계 미확정 |
| CONTEXT_FAILURE | 지원 요청의 문맥/참조 필드 불일치 증상 |
| TEMPORAL_FAILURE | 날짜/시간/기간 필드 불일치 또는 선행 grammar 제약 |
| ENTITY_GROUNDING_FAILURE | 실제 대상·결과 ID 필드 불일치 증상 |
| SLOT_EXTRACTION_FAILURE | 다른 critical 값 불일치 증상 |
| POLICY_FAILURE | 기능·critical 값은 맞고 확인/실행 판단이 불일치 |
| ADAPTER_FAILURE | action 경계 진입 근거가 있으나 결과·경계가 불일치 |
| RESPONSE_ASSERTION_FAILURE | 명시된 응답 사실 assertion 불일치 |
| UNDETERMINED | 원인 근거 부족 또는 관측/표현 변환 경계 확인 필요 |

`route=LLM_FALLBACK`만으로 모델 오류라고 판정하지 않습니다. 선행 grammar가 정답 날짜를
허용하지 않았거나 모델 값이 맞았는데 최종 slot에서 사라진 경우를 구분합니다. 원본에
명시적인 `title`이 있는데 채점 `text`만 빠진 경우도 원본 점수를 수정하지 않고 projection
확인 대상으로 둡니다. ID는 서버가 결정하는 값이며 모델이 생성해야 할 문자열로 취급하지
않습니다.

각 진단에는 `evidence_level`이 있습니다. `contract`는 등록 근거, `trace`는 단계 기록,
`result`는 저장 결과의 불일치 **증상**, `insufficient`는 원인 미확정입니다. Primary는
개발 우선순위 분류이지 유일한 인과관계의 증명이 아닙니다. M1에 빠진 raw/critical 경계
정보는 추측으로 채우지 않습니다. 안전 규칙을 해제하라는 의미도 아닙니다.

## 8. 지표와 비교

기존 `score()`/`project()` 정의를 바꾸거나 M1을 재채점하지 않습니다. 원본 metrics가 저장
score의 집계와 일치하는지 먼저 확인한 뒤 지원 subset을 **동일한 저장 점수**로 집계합니다.
Supported 성공 수는 전체 성공 수보다 클 수 없습니다. 지원 범위 분모에는 선택된 문항을,
정확도 분모에는 실제 채점된 문항을 사용하며 미평가는 따로 표시합니다.

추가 지표: 지원 Unique/Paraphrase·Domain·Route, 기능이 맞은 문항의 slot 일치,
날짜/시간·대상 ID slot, 되물은 요청의 precision과 불필요 명확화 비율, 모든 선택 구성원을
보존한 그룹 성공/일관성, 실제 모델 호출 문항의 LLM 지연·미호출 문항의 총 지연.
지원되지 않은 그룹 구성원을 빼서 그룹 전체 성공으로 만들지 않습니다.

기존 전체 비교는 `compare` 그대로입니다. M2의 지원 범위 비교는 다음과 같습니다.

```bash
# 나중에 새 Parser 결과도 별도 analyze로 생성한 뒤에만 실행
bash "$HOME/room-hub/deploy/termux/benchmark.sh" \
  compare-analysis m1-taxonomy-v1 parser-taxonomy-v1 --name m1-vs-parser-taxonomy
```

dataset hash/mode/selected IDs/지원 정의/진단 규칙 버전과 hash가 같아야 합니다. 두 결과에서
**모두 지원하고 채점한 동일 ID 교집합**으로 성능을 비교하고 지원 분류 변경과 미평가 제외를
별도로 표시합니다. alias 근거가 달라졌는지도 표시합니다. 부분집합 비교를 전체 개선이라고
말하지 않습니다. 하드웨어·발열·모델 설정이 같은지 사용자 확인은 여전히 필요합니다.

M3에서 app이 바뀐 후에는 과거 raw에 새 계약을 덮어씌우지 말고, M2 단계에서 만든 기존
analysis를 보존해 새 실행의 analysis와 비교하세요. `verify-analysis`는 현재 app 버전과
무관하게 보존된 파일의 무결성을 검증할 수 있습니다.

## 9. 무결성, 오류, 제한

SHA256 manifest는 변경 검출이지 서명/작성자 인증이 아닙니다. 과거 파일이 분석 이전부터
진본인지까지 증명하지는 못합니다. 분석 중 모든 입력 파일과 production source를 재검사하며,
완성 전 결과는 임시 폴더에 두고 성공 시에만 publish합니다. 기존 결과는 덮어쓰지 않습니다.

경로 탈출·링크/junction/hardlink·특수 파일·비정상 JSON·중복 키·무한 숫자는 거부합니다.
기본 상한은 파일당64MiB, 실행512MiB, 파일20,000개, 결과10,000행입니다. M1은 요약뿐 아니라
원본 디렉터리 전체가 있어야 합니다. path가 `data/`, 모델/운영 설정 영역에 해당하면 기존
result path 정책이 거부합니다. 부분 실행은 모든 선택 문항 기록이 있을 때 미평가 구분을
유지하며, 진행 중·중단 run은 M1 완료로 승격하지 않습니다.

| 오류 | 조치 |
|---|---|
| no such command analyze | M2가 병합·V35 업데이트됐는지 확인 |
| requires config/raw/metrics/progress | 요약 텍스트 대신 원본 결과 폴더를 사용 |
| production source differs / catalog mismatch | M1 app/dependency와 대조, SHA를 고치거나 우회하지 않음 |
| metrics differ | raw/metrics가 같은 실행인지 확인; 자동 수리하지 않음 |
| already exists | 기존 분석 유지, 새 --name으로 실행 |
| original inputs changed / integrity mismatch | 변경 경위 확인, 원본 백업과 대조 |

사용자는 기존 M1 raw/trace 폴더를 보존한 채 분석과 verify를 실행하고 summary·지원 실패·
미등록 계약·UNDETERMINED 결과를 검토합니다. 실제 M1 파일은 개발 환경에 제공되지 않아
이 PR의 로컬 검증은 합성 결과로 수행하며, V35에서 실제 17/250 보존 여부를 확인해야 합니다.

## 10. 개발 검증과 범위

```bash
python -m pytest -q tests/test_benchmark_taxonomy.py
python -m pytest -q tests/test_benchmark.py tests/test_benchmark_external.py tests/test_benchmark_wrappers.py
python -m pytest -q tests/test_timers.py tests/test_timer_bridge.py tests/test_timer_instances.py
python -m pytest -q
python scripts/check_repo.py
python scripts/check_benchmark_policy.py
bash -n deploy/termux/benchmark.sh
```

운영 기능 회귀, 외부 데이터 정책, 지원 catalog 재현성, trace 원인 경계, 원본 불변성,
source mismatch, 악성 경로/JSON, 전체/지원 분모 불변성, offline CLI와 wrapper, Windows
휴대성을 확인합니다. 시간 제한이나 기존 검사 항목을 줄여 통과시키지 않습니다.
