# M3.3-B.1 — Declarative memo routing / precision recovery

## 범위와 버전

기준 main은 `23a64c42f91a6d66fe73406daca75d78891e8f6d` (PR #27 병합)이다.
외부 사례의 [실제 코드 검토](NLU_REFERENCE_REVIEW.md)를 바탕으로 메모 해석의 충돌을
문장 유형·명시적 적용 순서·typed capture로 분리한다. 새 범용 NLU나 프레임워크 전체를
설치하는 작업은 아니다.

- Memo Grammar 1.0.0 (신규)
- Memo Grounding 1.1.0
- Benchmark 1.5.1 (문법 버전/hash metadata)
- 앱 0.1.7, Semantic Parser 1.2.0, Entity Catalog 1.0.0, Interaction Model/Dialog 1.0.0 유지

## 바뀌는 해석

| 문장 유형 / 합성 예 | 처리 |
|---|---|
| 가장 최근에 편집한 노트 내용 보여줘 | 기존 `last_modified` Fast Path. 제목으로 검색하지 않음 |
| 합성 통관이라고 해 둔 메모 내용 알려줘 | `합성 통관` 원문 구간만 named title로 사용 |
| 합성 점검이라고 지금 메모에 한 줄 덧붙여줘 | named title로 가로채지 않고 기존 모델·원문 검증 경로로 반환 |
| 합성 표식이 들어간 노트 찾아줘 | `MEMO_SEARCH_UNSUPPORTED`, 기본 메모를 대신 읽지 않음 |
| 그 노트 내용 보여줘 | `CONTEXT_REFERENCE_UNSUPPORTED`, 실제 대화 참조 기능을 구현했다고 주장하지 않음 |

기존 단순 named read/clear/append/write 및 current/latest 선택은 유지한다. 최근 수정
조회는 pin 순서가 아니며, 수정 시각이 같은 메모가 여러 개면 기존 재질문을 유지한다.
명명 wrapper는 `X라는`, `X라고 해둔`, `X라고 이름 붙인` 범위다. `X라고 써둔/적어둔`
표현은 본문 내용을 말할 수도 있으므로 제목이라고 추측하지 않는다.

내용 우선 append는 **이번에도 Qwen을 호출할 수 있다**. 모델의 원문에 없는 본문·대상·
권한은 기존 validator가 거부한다. `한 줄`을 임의 newline로 바꾸지 않는다. 이 회귀를
복구하기 위해 write confirmation을 우회하거나 새로운 current-write executor를 만들지 않는다.

## 선언형 분류와 추적

`app/memo_language.py`의 불변 `SentenceFamily`가 문장 전체 구조, stage, action,
`named/delegate/block` 결정 및 selector를 정의한다. `LanguageMatch`는 원문 offset을
보존한다. 같은 stage에서 의미가 충돌하면 `MEMO_GRAMMAR_AMBIGUOUS`로 거부한다.
임의 사용자 regex/YAML/plugin 로딩, fuzzy 일치, ASR repair는 없다.

요청 기록의 `memo_language`에는 family/stage/disposition/문법 hash/낮은 우선순위 후보와
`claimed`가 남는다. `claimed=false`는 이 모듈이 named/guard 경로를 설치하지 않았다는
뜻이며, 기존 Fast Path나 LLM이 실제로 처리했는지는 `routing`으로 확인한다.
메모 plan에도 family와 grammar hash를 저장해 원문 재검증 시 문법 변화를 감지한다.
따라서 업데이트 전 확인 대기 중인 named memo 요청은 새 요청으로 다시 확인해야 할 수 있다.
과거 기록을 자동 재해석·실행하지 않는다.

## 유지한 경계

기존 카탈로그/실제 ID·version 조회, private-note 공개 재검사, confirmed write 및 durable
receipt를 그대로 사용한다. 검색/대명사 거부는 이전의 잘못된 기본 메모 읽기를 복원하지
않는다. Runtime과 CLI scorer를 복제하거나 외부 projection/gold/taxonomy/support를
변경하지 않는다. 실제 데이터셋과 원본 결과는 여전히 Git 밖에 둔다.

M3.3-A typed dialogue에는 새 memo grammar를 실행기로 연결하지 않는다. 제목을 묻는
슬롯에 메모 명령을 넣어도 다른 메모를 읽지 않고 기존 슬롯 질문을 유지하는 통합 검사를
추가했다. Memo multi-turn, 자동 음성 follow-up, 일반 context reference는 후속 범위다.

## 검증 명령

```bash
python scripts/check_memo_language.py --json artifacts/test-results/memo-grammar.json
python -m pytest -q tests/test_memo_language_m33b1.py
python -m pytest -q
python scripts/check_repo.py
python scripts/check_benchmark_policy.py
python scripts/memo_catalog_browser.py
python scripts/dialog_browser.py
```

CI는 기존 모든 검사와 timeout을 유지하고, Windows smoke에 새 test module을 추가한다.
문법 조합 검사는 합성 검증이지 실제 V35/Qwen 인식률이 아니다.

## 사용자 병합 후 V35

PR 검토·병합 및 main CI 확인 뒤 **바깥 Termux**에서 실행한다. 모델/데이터 재설치 불필요.

```bash
bash "$HOME/room-hub/deploy/termux/update-from-git.sh"
bash "$HOME/room-hub/deploy/termux/update-from-git.sh" --check
grep -n '^GRAMMAR_VERSION' "$HOME/room-hub/app/memo_language.py"
grep -n '^MEMO_GROUNDING_VERSION' "$HOME/room-hub/app/memo_grounding.py"
cat "$HOME/room-hub/benchmarks/__init__.py"
```

위 표의 합성 문장을 중요하지 않은 임시 메모로 먼저 확인한다. 최근 메모 선택, 명명
wrapper, 내용 우선 append의 확인 전 미변경, 검색·대명사 거부를 각각 구분해서 본다.
M3.3-A 실기기 다중 턴 인수가 아직 미확인이라면 별도로 확인한다.

```bash
bash "$HOME/room-hub/deploy/termux/benchmark.sh" verify-analysis m33b-taxonomy-v1
bash "$HOME/room-hub/deploy/termux/benchmark-data.sh" verify room_hub_v1
bash "$HOME/room-hub/deploy/termux/benchmark.sh" run --suite room_hub_v1 --mode full --name m33b1-smoke-01 --llm local --limit 3
bash "$HOME/room-hub/deploy/termux/benchmark.sh" run --suite room_hub_v1 --mode full --name m33b1-parser-v1-qwen --llm local
bash "$HOME/room-hub/deploy/termux/benchmark.sh" analyze m33b1-parser-v1-qwen --name m33b1-taxonomy-v1
bash "$HOME/room-hub/deploy/termux/benchmark.sh" verify-analysis m33b1-taxonomy-v1
bash "$HOME/room-hub/deploy/termux/benchmark.sh" compare-analysis m33b-taxonomy-v1 m33b1-taxonomy-v1 --name m33b-vs-m33b1-case-v1
bash "$HOME/room-hub/deploy/termux/benchmark.sh" compare-analysis m33a-taxonomy-v1 m33b1-taxonomy-v1 --name m33a-vs-m33b1-case-v1
```

새 결과 이름을 사용하고 기존 폴더를 덮어쓰지 않는다. 새로운 코드로 과거 raw run을
다시 analyze하지 말고 보존된 analyses를 비교한다. 신규 회귀 여부, 기존 성공의 개별 보존,
지원 집합, 오실행, 실제 selector/title/text 및 Adapter 호출을 검사한다. 이전 안전하지 않은
기본 메모 읽기와 비교해 나타나는 거부 변화는 문항별로 검토하고 gate 자체를 완화하지 않는다.
Memo ID 관측 매핑 문제가 남아도 이번 패치에서 점수를 높이기 위해 scorer를 바꾸지 않는다.
