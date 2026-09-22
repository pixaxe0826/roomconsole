# M3 — Supported Semantic Parser V1

## 범위와 승인된 의미

M3는 현재 Assistant operation을 자연어에 연결하는 **제한된 단일 요청 파서**입니다.
일반적인 할 일 조회의 기본 상태는 `pending`, 명시적인 완료 조회는 `completed`,
명시적인 전체 상태 조회는 `all`입니다. UI·Store·Protocol API의 기본 `all`은 바꾸지 않습니다.
`전체 날짜`는 날짜 범위이므로 그 말만으로 완료 상태 조건을 `all`로 바꾸지 않습니다.
날짜를 생략한 일반 목록 조회는 기존 요청 기준 오늘 정책을 유지합니다.

기존 operation 등록/입출력 스키마, DB, UI, 타이머, Whisper, 모델 설정/프롬프트,
외부 데이터셋과 M1/M2 결과는 변경하지 않습니다. Semantic Parser 버전은 `1.0.0`이고
제품 VERSION은 `0.1.7`을 유지합니다. 벤치마크는 기존 1.2.0에 관측 필드만 추가합니다.
이 문서는 구현 범위이지 실제 V35의 정확도·속도 상승을 증명하는 보고서가 아닙니다.

## 처리 경계

```text
검증된 전사/텍스트 원본과 요청 시각
  → 기존 FAST_PATH (보존)
  → 기존 exact timer/alarm 및 정확한 atomic legacy write (보존)
  → Semantic Parser (전체 문장을 해석할 수 있는 제한된 문법)
      EXACT → 기존 WidgetProposal 검증 → 서버 대상 조회 → 기존 Registry
      MISSING / UNSUPPORTED → 구체적 clarification, 모델로 값 추측하지 않음
      문법 밖 → 기존 constrained Qwen fallback / 안전 규칙
  → read: 실제 Adapter 결과로 응답
  → write: 미리보기 → 관리자 확인 → 기존 digest/version/receipt 검사 → Adapter
```

Parser는 ID/version/permission/confirmed_digest 또는 실행 결과를 만들지 않습니다.
`SemanticFrame`은 불변 객체로 operation, source-only arguments, target 문자열,
정규화된 원문 기준 span, 시간 근거와 확신 상태를 기록합니다. ID나 권한은 포함하지
않습니다. 사용 직전에 저장 원문·시각에서 frame을 다시 계산해 일치 여부를 검사합니다.
모델을 쓰지 않는 제안도 기존 validation/Registry의 확인 절차를 거칩니다.

기존의 정확한 legacy write는 atomic confirmation과 중복 경고를 보존합니다.
기능명만 benchmark 정답에 맞추기 위해 `todo.create`를 `todo.add`로 바꾸거나 다른
엔진으로 우회하지 않습니다. `text`/`title` 투영, 원본 scoring과 taxonomy 규칙은 그대로입니다.

## 해석 예시 (테스트용 문장)

| 입력 | 처리 |
|---|---|
| 오늘 할 일 보여줘 | 오늘 pending 목록, 기존 FAST_PATH, 모델 0회 |
| 오늘 완료한 할 일 보여줘 | 오늘 completed 목록 |
| 오늘 전체 할 일 보여줘 | 오늘 all 목록 |
| 오늘부터 내일까지 할 일 보여줘 | 양 끝 날짜 포함 pending 목록 |
| 2월 28~29일 일정 알려줘 | 요청 기준 연도의 유효한 범위, calendar 기본 all |
| 이번 주 수요일에 장비 포장 할 일 추가해 | ISO 월요일 기준 해당 수요일·원문 제목 → 관리자 확인 |
| 내일 오후 5시까지 장비 전달 할 일 추가해 | 명시 날짜/17:00/제목 → 기존 TaskCreate 시간 필드 |
| 도서 포장 끝냈어 | 단일 대상 complete, 확인 전에는 변경 없음 |
| 부품 주문 다시 미완료로 바꿔줘 | 기존 reopen, 실제 대상/버전 확인 |
| 도서 포장 할 일 삭제해 | 명령 경계의 `할 일`, `삭제해`만 제거하여 대상 선택 |
| 도서 포장 할 일 읽어줘 | 서버가 제목으로 실제 ID를 찾은 뒤 get |
| 내일 할 일 하나 추가해 | 날짜 근거 유지, 제목 재질문 |
| 도서 포장 할 일 추가해 | 없는 날짜를 오늘로 만들어 쓰지 않고 재질문 |

`TaskCreate.time`에 명시 시각을 연결할 뿐 별도의 deadline/event duration 필드를
추가하지 않습니다. 기존 Calendar/Todo 공용 저장 구조도 변경하지 않습니다.

### 시간 정책과 범위 제한

기존 `clock_service`와 `parse_clock`을 사용합니다. 오늘/내일/모레, 명시 날짜,
이번 주 요일, 월/주 범위, 명시적인 `부터…까지`/`~` 범위와 오전/오후·24시간 시각을
원문에서 추출합니다. 연도 생략은 요청 기준 연도, 주는 ISO 월요일 기준입니다.
동일 월 범위의 종료 `29일`은 명시된 시작 월/연도를 상속하지만 뒤집힌 범위를
다음 해로 임의 보정하지 않습니다. 잘못된 날짜, 복수 날짜/시각, 모호한 AM/PM은 거부합니다.

**특정 시각 이전/이후 필터는 이번 릴리스에서 실행하지 않습니다.** 기존 `TaskQuery`에는
날짜·완료상태·오전/오후만 있고 `before/after` 인자가 없습니다. 새 파서는 그 조건을
인식하고 설명을 반환하지만 일별 목록으로 축소하지 않습니다. 기존 Adapter 계약을
동결한 M3에서 이 기능을 넣었다고 주장하지 않습니다.

`10분 뒤`, `한 시간 뒤`는 고정 요청 instant에서 UTC elapsed-time으로 계산합니다.
기존 분 단위 Task/Alarm 시간으로 **정확히 표현 가능한 경우에만** 후보를 만들며,
초·마이크로초를 버리거나 반올림하지 않습니다. 일반적인 실제 요청처럼 초가 있으면
정확한 날짜/시각 재질문으로 끝날 수 있습니다. Alarm은 기존 future/DST 검증과 관리자
확인을 계속 거칩니다. 타이머로 대신 실행하거나 잠금화면 소리 보장을 추가하지 않습니다.

생략형 다중 턴, 대명사/순서/지난 결과 참조, search/move/rename/일괄 새 동작,
미지원 필터와 recurrence는 확장하지 않습니다. 기존 bulk operation은 기존 경로에 남깁니다.
복합·부정·조건·인용 안전 규칙을 전역 삭제하지 않습니다. 검증 완료한 시간 span만
안전 검사에서 분리하므로 `부터` 단어를 지워 범위를 버리는 방식이 아닙니다.

## 단일 요청 Entity Resolver

정확한 정규화 제목 → 유일한 literal substring 순서입니다. 한 글자 substring, fuzzy
수정, ASR 추측, 모델이 제안한 ID는 사용하지 않습니다. `보고서`가 여러 항목에 맞으면
하나를 임의로 선택하지 않습니다. Parser가 반환한 것은 target_text이고 서버 조회 결과의
ID/version만 기존 WidgetRequest에 넣습니다.

날짜가 명시되지 않은 제목 요청은 오늘로 가정하지 않고 실제 전체 날짜 항목을 검사합니다.
서로 다른 날짜/완료상태의 같은 제목은 여전히 여러 대상입니다. 상태를 먼저 필터링해
편한 대상 하나로 만들지 않습니다. 날짜를 명시한 경우에만 그 날짜로 좁힙니다.
최대 10,000개 조회가 잘리면 유일성을 증명하지 못하므로 명확화를 요구합니다.
DB 오류는 빈 목록/성공으로 바꾸지 않습니다. 후보 설명은 최대 5개만 기록합니다.

확인 미리보기에는 실제 대상 제목·날짜·시각을 표시합니다. 새 create는 같은 날짜·제목·
시간이 존재하면 중복 경고를 유지하고, 중복 조회가 잘린 경우 미확정임을 표시합니다.
기존 버전/digest/만료/시간대 검증은 유지합니다. 요청 retry의 durable receipt를
새 대상 조회보다 먼저 확인하여 삭제된 이름으로 새 항목이 만들어져도 재삭제하지 않습니다.

## 관측과 평가 보존

새 경로는 `routing.route=SEMANTIC_PARSER` 및 parser version/source evidence를 남깁니다.
벤치마크 runtime은 production이 기록한 frame/parser_ms/entity evidence를 방어 복사합니다.
기존 exact fast read를 새 Parser 성과로 표시하지 않습니다. NLU 경계는 서버 대상 조회 전,
Decision은 최종 Adapter 실행 전, Full은 실제 격리 Adapter까지입니다. 확인을 자동 누르지
않으며 조회를 통해 얻은 ID는 모델 추출 정확도로 해석하지 않습니다.

이번 변경은 registry metadata를 확장하지 않습니다. 실제 지원 subset의 ID 수는
이전 M1의 불완전한 alias 기록과 새 run의 projection receipt 차이도 영향을 줄 수 있으므로
`compare-analysis`의 고정 교집합과 alias provenance를 함께 검토합니다. 실제 144개가
유지됐는지는 외부 corpus 결과로 확인해야 합니다. Gold/score를 고쳐 수치를 맞추지 않습니다.

M2 taxonomy rules/hash는 유지합니다. 따라서 M2의 primary 분류가 새 Parser 내부의
모든 단계를 자동으로 구분한다고 가정하지 마세요. `semantic_frame`, `entity_resolution`,
`SEMANTIC_PARSER` 경로와 trace 근거를 함께 읽습니다.

## 병합 후 사용자 V35 테스트

PR 검토·병합 → **병합 main CI 성공 확인** → 기존 V35 Git updater로 코드 업데이트.
데이터셋은 이미 설치된 외부 데이터를 그대로 사용합니다. 수동 source 덮어쓰기/원본
수정/모델 재설치가 필요하지 않습니다. 아래는 **바깥 Termux** 명령입니다.

```bash
bash "$HOME/room-hub/deploy/termux/update-from-git.sh"
bash "$HOME/room-hub/deploy/termux/update-from-git.sh" --check
bash "$HOME/room-hub/deploy/termux/benchmark.sh" verify-analysis m1-taxonomy-v1
bash "$HOME/room-hub/deploy/termux/benchmark-data.sh" verify room_hub_v1
```

예전 `baseline-v1-qwen/`와 `analysis/m1-taxonomy-v1/`는 그대로 보존합니다.
**M3 app으로 옛 raw를 analyze하지 마세요.** source mismatch가 정상 보호 동작입니다.
이전에 만든 analysis의 verify/비교는 현재 app 내용과 무관하게 가능합니다.

```bash
# 먼저 하네스/기존 경로의 작은 점검. 앞 3개가 M3 문장을 반드시 포함하진 않습니다.
bash "$HOME/room-hub/deploy/termux/benchmark.sh" run --suite room_hub_v1 --mode full --name m3-smoke-disabled-01 --llm disabled --limit 3
bash "$HOME/room-hub/deploy/termux/benchmark.sh" run --suite room_hub_v1 --mode full --name m3-smoke-qwen-01 --llm local --limit 3

# 실제 Qwen baseline 비교. 모델·threads·timeout·token limit·전원/온도 조건을 유지하세요.
bash "$HOME/room-hub/deploy/termux/benchmark.sh" run --suite room_hub_v1 --mode full --name m3-parser-v1-qwen --llm local
bash "$HOME/room-hub/deploy/termux/benchmark.sh" analyze m3-parser-v1-qwen --name m3-taxonomy-v1
bash "$HOME/room-hub/deploy/termux/benchmark.sh" verify-analysis m3-taxonomy-v1
bash "$HOME/room-hub/deploy/termux/benchmark.sh" compare-analysis m1-taxonomy-v1 m3-taxonomy-v1 --name m1-vs-m3-parser-v1
```

보고서 출력:

```bash
sed -n '1,280p' "$HOME/room-hub/artifacts/benchmarks/analysis/m3-taxonomy-v1/summary.md"
```

비교 명령이 출력한 report 경로의 `comparison.md`를 확인합니다. 기존 이름은 덮어쓰지
않습니다. 미지원/미평가 수와 고정 비교 집합, alias 차이, 오실행, 기존 성공 문항의
회귀를 먼저 읽은 뒤 전체/지원 성공, 불필요 clarification, LLM 호출률, p50/p95를 봅니다.
M3 목표 수치는 실기기 실행 전에는 달성으로 보고하지 않습니다.

### 실제 UI 확인 (테스트용 제목 사용)

관리자에서 `장비 포장 검증용` 항목을 하나 준비한 다음 `장비 포장 검증용 끝냈어`를
요청합니다. **확인 전 미변경 → 실제 제목/날짜 확인 → 관리자 확인 후에만 완료**가 정상입니다.
서로 다른 날짜에 동일 테스트 제목을 두 개 만들면 날짜 없는 요청은 재질문해야 합니다.
`내일 우산 점검 검증용 할 일 추가해`는 모델 호출 없이 확인 대기가 되어야 합니다.
일반 할 일/완료한 할 일/전체 할 일을 조회해 pending/completed/all이 구분되는지 확인합니다.
두 독립 타이머도 서로 다른 시간으로 시작하고 한쪽만 종료해 기존 동작을 점검합니다.
테스트 항목은 사용자가 확인하여 정리하고, 실제 중요 항목으로 삭제 테스트하지 마세요.

## 개발 검사

```bash
python -m pytest -q tests/test_semantic_parser_m3.py tests/test_entity_resolver_m3.py tests/test_m3_assistant_regression.py tests/test_benchmark_m3.py
python -m pytest -q tests/test_timers.py tests/test_timer_bridge.py tests/test_timer_instances.py
python -m pytest -q
python scripts/check_repo.py
python scripts/check_benchmark_policy.py
python scripts/live_smoke.py
```

기존 LLM 악성/불완전 proposal 테스트는 새 exact grammar가 가로채지 않는 `…등록해 줄래`
형태로 입력만 바꿔 **실제 fallback을 계속 검증**합니다. 테스트를 제거하거나 모델 분기를
전체 monkeypatch하지 않습니다. 기존 implicit todo status 기대값만 승인된 pending으로
갱신했고 UI/Calendar/explicit-all 기대값은 유지합니다. CI에 M3 테스트를 추가하며 기존
검사·타임아웃을 줄이거나 늘리지 않습니다.

실제 V35/PRoot, iPad Safari, 음성 인식 정확도, 실제 Qwen의 250개 결과와 장시간 발열은
사용자 장비에서 별도 검증해야 합니다. 로컬 synthetic test 성공은 benchmark 점수 상승이
아닙니다. 원본/출력 SHA256는 무결성 확인이지 원본 진실성 인증/전자서명이 아닙니다.

Rollback은 검토된 revert PR → main CI → 기존 updater입니다. 승인하여 실행한 task 변경은
코드 rollback으로 되돌아가지 않습니다. 운영 DB/외부 데이터/M1/M2 결과를 삭제하지 않습니다.
