# M3.1 — 회귀 비교와 Semantic Parser 안정화

## 범위

기준은 M3 PR #23이 포함된 main `798d8a70945c04cd4e3c528b4dd4e335e5334787`입니다.
이번 변경은 새 operation/모델/DB schema/화면을 추가하지 않습니다. 일반 Assistant
할 일 조회의 pending 기본값, 명시적 completed/all, 타이머의 독립 widget ownership과
기존 confirmation/receipt 정책을 유지합니다. Semantic Parser는 **1.1.0**,
벤치마크 엔진은 **1.3.0**입니다. 원본 채점과 taxonomy 규칙 버전은 변경하지 않습니다.

사용자가 제공한 결과는 전체 성공 24/250, 공통 지원 집합 성공 24/144,
지원 문항의 모델 호출 50/144, p50 약 1.61초였습니다. 명확화 정답은 22/23,
실행 경계 정답은 98/144로 기준선보다 낮았습니다. 이것은 집계이며 **회귀 문항 ID,
실제 원인 또는 M3.1 이후 점수를 증명하지 않습니다.** 사용자 raw/trace와 실제 평가
데이터셋은 이 PR에 없습니다. GitHub 테스트에는 별도로 만든 작은 합성 입력만 씁니다.

## 1. 문항별 비교 (기존 점수 그대로)

기존 `compare-analysis`에 문항 단위 보고서를 추가했습니다. 새 모델 호출, production
import를 통한 재실행, `score`/`project` 재실행, 기존 analysis 수정은 하지 않습니다.
검증된 두 analysis의 `scored_results.jsonl`과 `taxonomy.jsonl`을 case_id로 짝짓습니다.
selected IDs, dataset/mode/taxonomy 계약뿐 아니라 각 문항의 원문·정답·메타데이터도
대조합니다. 양쪽에 critical-slot metadata가 있으면 그 정의가 달라도 거부합니다.
과거 실행에 해당 metadata가 없으면 불완전함을 표시하고 값을 추측하지 않습니다.

출력은 새 비교 폴더에만 만듭니다.

| 파일 | 용도 |
|---|---|
| comparison.json / md / html | 기존 지원 교집합 집계 + 문항 전환 요약 |
| case_diff_summary.json | 성공 전환·회귀 ID·실행/명확화 회귀 ID·경로 전환 집계 |
| case_transitions.jsonl | 모든 선택 문항의 이전/이후 저장 점수와 실제 필드 차이 |
| CASE_TRANSITIONS.md / case_transitions.html | 전체 문항별 비교 |
| REGRESSIONS.md | 기존 정답→오답, 관측 누락/미평가 전환 |
| IMPROVEMENTS.md | 기존 오답→정답으로 바뀐 차원 |
| ROUTE_TRANSITIONS.csv | 같은 case의 이전/이후 route·결과·지연 |
| PARSER_OUTCOMES.md | 새 Parser 문항을 성공/실패/미평가로 나눈 결과와 지연 |

성공 순증은 **FAIL→PASS 수 − PASS→FAIL 수**입니다. 실행 경계 순감 8건을
회귀 8문항으로 치환하지 않습니다. 미평가를 FAIL로 만들지 않고 관측 손실로 별도
표시합니다. 없는 slot과 명시 null은 구별합니다. 한 문항에 여러 차원의 회귀가 있어도
그것을 여러 개의 실패 문항으로 계산하지 않습니다.

성능 지표는 기존의 양쪽 지원·채점 교집합을 유지합니다. 안전성, 잃어버린 성공,
관측 손실 검토에는 **지원/미지원 전체 선택 문항**도 사용해 제외로 문제를 숨기지 않습니다.
현재 로그만으로 인과관계를 확정하지 않습니다. `title`/`text`, native/canonical 차이도
그대로 보고하며 채점·gold·projection을 바꾸지 않습니다.

기본 콘솔 출력은 간략한 전환 요약입니다. 기존처럼 전체 집계 JSON을 출력하려면
`--full-json`을 붙입니다. 전체 JSON은 옵션에 관계없이 comparison.json에 저장합니다.

`--fail-on-regression`은 보고서를 정상 게시한 다음, 저장 점수의 정답→오답,
관측 손실 또는 이후 오실행이 하나라도 있으면 **exit 3**을 반환합니다.
입력/무결성/기존 출력 이름 오류는 **exit 2**이며, 일반 보고서 생성 성공은 **exit 0**입니다.
회귀 gate는 진단용입니다. 안전하게 미지원 조건을 거부했는데 기존 정답 계약과 달라진
경우도 검토 목록에 남습니다. 점수 때문에 조건을 버리거나 확인 없이 실행하지 마세요.

## 2. Source-only arbitration

기존 SemanticFrame의 EXACT/MISSING/UNSUPPORTED 표기는 호환을 위해 유지하고,
다음과 같은 관측용 outcome/reason을 추가합니다.

| outcome | 처리 |
|---|---|
| EXACT | 원문 계획만 확정. 기존 검증·서버 대상 확인·정책으로 이동 |
| PARTIAL | 명시적으로 빠진 제목/날짜/대상을 사용자에게 질문 |
| AMBIGUOUS | 시각·상태·연속 제목의 불확실성을 질문. 모델에게 추측시키지 않음 |
| UNSUPPORTED | 부정/복합/미지원 조건 등을 축소하지 않고 안내 |
| NO_MATCH | 파서 문법 밖. 기존 안전 검사와 제한 LLM 경로에 판단을 돌려줌 |

NO_MATCH는 무조건 LLM 호출이 아닙니다. 기존 unsafe/date guard가 거부하는 입력은
여전히 거부합니다. EXACT도 실행 권한이나 DB 대상의 유일성을 증명하지 않습니다.
`execution_eligibility`는 기존 검사 결과를 기록할 뿐 별도 권한 우회 계층이 아닙니다.
마지막 Registry 결과도 policy_outcome으로 남깁니다.

## 3. 재현해 수정한 동작

- `내일 3시쯤 검사 도구 포장 할 일 추가해`: 모호한 시각이 제목으로 섞이지 않습니다.
  `쯤/경/정도/무렵` 시각과 `약/대략` 시각은 정확한 시간 재질문으로 처리합니다.
- 시각 해석이 실패해도 독립적으로 확정한 날짜/evidence는 보존합니다. 상충하는
  날짜 둘 중 하나를 선택하지 않고, 충돌한 clock/period를 확정값으로 남기지 않습니다.
- `내일 할 일 좀 추가해`, `하나 좀`처럼 요청 보조어만 있는 경우 제목이 없다고
  질문합니다. `좀 더 읽기` 같은 실제 제목 속 표현은 전역 치환하지 않습니다.
- `할 일 내일 남은 항목들만 보여줘`, `완료한 것 보여줘`, 날짜 범위의 `남은 목록`은
  문법이 완결된 collection query로 처리하고 임의 제목 조회로 바꾸지 않습니다.
- `완료했어/완료했어요/다 했어/다 했어요`, `없애줘` 등의 제한된 단일 대상 동사를
  기존 complete/delete operation에 연결합니다. 부정·조건·복합·전체 대상 guard 유지.
- 날짜 구간 전체를 먼저 소비한 후 단일 날짜를 검사합니다. 시작 endpoint만 읽어
  범위를 단일 날짜로 축소하지 않습니다. 새로운 시간 필터/상대 시각 반올림은 없습니다.
- 대상 ID는 기존 서버 조회에서만 가져옵니다. 정규화 exact → 유일 literal substring,
  중복/잘림/미발견 거부를 유지하고 중복 ID를 가진 비정상 서버 행도 거부합니다.
- `field_provenance`는 source target/create title과 server-resolved title을 구분합니다.
  기존 field 이름이나 gold를 바꿔 점수를 올리지 않습니다.

`parser_enabled/parser_version`의 run metadata가 과거 baseline 상수였던 것도 수정했습니다.
이 값은 **설치된 parser**의 존재와 버전을 뜻합니다. 각 문항에서 실제 사용했는지는
routing/semantic frame/trace를 확인해야 합니다. 기존 결과 파일은 갱신하지 않습니다.

## 4. 적용 및 사용자 검사 — 바깥 Termux

사용자 PR 검토/병합 → 병합 main CI 성공 → 기존 Git updater 순서를 유지합니다.
이전 버전의 확인 대기 요청은 새 버전에서 계획 재검증에 실패할 수 있습니다. 업데이트
전에 필요한 미리보기를 검토하거나 취소하고, 실패한 이전 요청은 새로 입력하세요.
이미 실행한 동작은 업데이트/rollback이 취소하지 않습니다.

```bash
bash "$HOME/room-hub/deploy/termux/update-from-git.sh"
bash "$HOME/room-hub/deploy/termux/update-from-git.sh" --check
bash "$HOME/room-hub/deploy/termux/benchmark.sh" verify-analysis m1-taxonomy-v1
bash "$HOME/room-hub/deploy/termux/benchmark.sh" verify-analysis m3-taxonomy-v2
```

### 먼저 기존 M1→M3의 실제 회귀 ID 추출 (추론 0회)

```bash
bash "$HOME/room-hub/deploy/termux/benchmark.sh" compare-analysis m1-taxonomy-v1 m3-taxonomy-v2 --name m1-vs-m3-case-audit-v1
sed -n '1,240p' "$HOME/room-hub/artifacts/benchmarks/analysis/m1-vs-m3-case-audit-v1/REGRESSIONS.md"
sed -n '1,240p' "$HOME/room-hub/artifacts/benchmarks/analysis/m1-vs-m3-case-audit-v1/PARSER_OUTCOMES.md"
```

기존 m1-vs-m3-parser-v2에 덮어쓰지 않습니다. analysis 파일을 읽는 비교는 현재 app
hash와 옛 app hash의 일치를 요구하지 않습니다. 반면 **현재 app으로 옛 raw를 analyze하면
source mismatch가 정상**입니다. 보존한 analysis끼리 비교하세요.

### 동작 확인 및 새 benchmark

중요하지 않은 테스트용 할 일을 사용합니다. 모호한 시간/제목 없는 요청은 확인
미리보기 없이 재질문해야 합니다. 실제 제목을 주고 완료를 요청하면 preview를 보여주고,
관리자 확인 전에는 상태를 바꾸지 않아야 합니다. 중복 제목·오래된 버전·재시도와
두 독립 타이머도 기존처럼 검사합니다.

```bash
bash "$HOME/room-hub/deploy/termux/benchmark-data.sh" verify room_hub_v1
bash "$HOME/room-hub/deploy/termux/benchmark.sh" run --suite room_hub_v1 --mode full --name m31-smoke-01 --llm local --limit 3
bash "$HOME/room-hub/deploy/termux/benchmark.sh" run --suite room_hub_v1 --mode full --name m31-parser-v1-qwen --llm local
bash "$HOME/room-hub/deploy/termux/benchmark.sh" analyze m31-parser-v1-qwen --name m31-taxonomy-v1
bash "$HOME/room-hub/deploy/termux/benchmark.sh" verify-analysis m31-taxonomy-v1
bash "$HOME/room-hub/deploy/termux/benchmark.sh" compare-analysis m3-taxonomy-v2 m31-taxonomy-v1 --name m3-vs-m31-case-v1
bash "$HOME/room-hub/deploy/termux/benchmark.sh" compare-analysis m1-taxonomy-v1 m31-taxonomy-v1 --name m1-vs-m31-case-v1
```

필요하면 compare-analysis에 `--fail-on-regression`을 추가합니다. exit 3이어도
보고서는 생성됩니다. 같은 이름 재실행은 거부되므로 기존 보고서를 확인하거나 새 이름을
사용합니다. 데이터셋 재전송·모델 설치는 불필요하며 M1/M2/M3 파일을 수정하지 않습니다.

## 인수 기준과 한계

합성 단위/통합/CI 성공을 실제 외부 250문항 성공으로 주장하지 않습니다.
실제 필요한 명확화 1건 및 실행 경계 순감 8건의 구체적 원인은 aggregate만으로
확정하지 못했습니다. 새 문항 비교에서 해당 ID/trace를 확인한 다음 사용자 V35 실행으로
복구 여부를 판정해야 합니다. 기존 24건 성공의 보존도 case 단위로 확인합니다.

안전·명확화·조건 보존·기존 성공 회귀를 먼저 판단합니다. 같은 지원 cohort에서 정확도와
속도를 비교하고, 모델 호출 감소를 위해 필요한 fallback까지 막지 않습니다. p50 등
성능 수치는 반복 실기기 측정과 전원/온도/부하 기록 없이 확정하지 않습니다.
실제 데이터셋/정답/원본 결과/키/모델은 GitHub에 포함하지 않습니다.

이 문서의 동작 예는 합성 테스트입니다. actual trace가 없어 원인 미정인 사례는
그대로 검토 대상으로 남깁니다. 신규 capability/context/time-filter 확장은 후속 단계입니다.
