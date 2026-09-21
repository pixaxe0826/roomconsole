> 최신 변경: [라우팅 안정화 및 SSH 업데이트](ROUTING_STABILIZATION.md)를 참고하세요. 아래의 알람 set/cancel 미연결 설명과 widget-assistant-1 경로는 이전 계약 기록입니다.

# LLM fallback → Widget Protocol 연결

[문서 홈](../README.md) · [Widget Protocol 1.0](WIDGET_PROTOCOL.md) · [Fast reads](FAST_READS.md) · [V35 업데이트](V35_GIT_UPDATE.md)

## 범위와 호환성

`WidgetBridge`는 기존 `LLMHub`와 `app.widget_protocol` 사이의 연결 계층입니다.
Adapter/Registry/업무 DB/음성 worker를 교체하지 않습니다.

```text
전사 저장 → 기존 자동 Assistant 제출(mode=auto) → 정규화 / 기존 Fast Router
  ├─ 명확한 메모·할 일·일정 읽기 → WidgetRequest → Registry/Adapter → 실제 결과 포맷
  ├─ 기존 규칙 기반 쓰기·시각·날씨 → 기존 원자적/일괄 처리와 확인 절차
  └─ 미확정 Widget 요청 → 도메인·동작 후보 → JSON Schema 제약 LLM Proposal
       → 서버 검증 / 원문 근거 / Registry policy
          ├─ read → 실제 Adapter → deterministic formatter
          ├─ write → 기존 관리자 confirmation → 실제 Adapter → formatter
          └─ missing / unsupported / unavailable / error → 구조화된 상태와 안내
```

**동작 변경:** 자동 모드에서 Widget 도메인을 정할 수 없는 일반 질문은 이제
“어떤 Widget 작업인가요?”로 명확화를 요청합니다. 일반 질문을 자동 자유 대화로 보내지 않습니다.
관리자가 명시적으로 선택하는 일반 대화/legacy 모드는 Widget 요청이 아닐 때만 유지합니다.
메모·할 일·일정·알람 요청은 일반 대화/legacy를 선택해도 자유 응답으로 우회하지 않습니다.
음성 자동 전달은 계속 기존 `mode=auto`를 사용합니다. 기존 정상 규칙 명령과 UI 편집은 보존합니다.

기존 `app/capabilities.py`의 목록은 기존 규칙 엔진의 계약입니다. 새 모델 후보와 policy는
`WidgetRegistry.get_capabilities()`/실제 Operation에서 가져옵니다. 상태 응답의
`widget_capability_catalog`와 `assistant_profiles.widget_proposal`이 새 경로를 설명합니다.

## Model Proposal: 기존 WidgetRequest의 작은 projection

`WidgetProposal`은 기존 `WidgetRequest`의 필드 타입을 그대로 참조하는 Pydantic projection입니다.
독립적인 Widget 업무 스키마를 새로 정의하지 않습니다. 모델이 출력하는 필드는 네 개뿐입니다.

```json
{
  "widget": "todo",
  "action": "add",
  "target": null,
  "args": {"date": "2026-09-22", "title": "우유 사기", "time": null}
}
```

날짜는 예시입니다. 실제 요청의 날짜 근거를 서버가 먼저 확인합니다.
기존 TodoAdapter의 생성 필드는 `text`가 아니라 **`title`**입니다.
서버는 검증 후 기존 `WidgetRequest`에 protocol_version, request_id, idempotency_key와
context를 채웁니다. 모델은 이를 만들거나 permission/confirmation 값을 지정할 수 없습니다.

args는 선택된 실제 Adapter 입력 스키마에서 생성합니다. version, limit, category, priority,
notes, repeat, weekdays, enabled, timezone, pinned, shared는 모델 출력 대상에서 제외합니다.
이 필드들은 서버가 필요한 값만 결정하거나 관리자 UI에서 처리합니다. 모델이 넣으면 거부합니다.

## Domain-scoped prompt와 constrained output

도메인 단어와 동작 근거를 가벼운 규칙으로 추립니다. 실제 capability 이름·args schema·availability는
Registry에서 가져옵니다. 중앙에 두 번째 tool 목록을 유지하지 않습니다. 여러 도메인이 섞였거나
단일 동작 근거를 찾지 못하면 모든 도구를 모델에 보내는 대신 clarification을 반환합니다.
`하늘에 … 추가해`처럼 오인식 가능성이 있는 단어는 교체하지 않고 추가 동작의 domain 후보만
선택할 수 있습니다. 의도 복구와 인자 복구는 다릅니다.

System prompt의 핵심:
- widget/action/target/args JSON Proposal 하나만 반환.
- 현재 후보 동작만 사용. 최종 답변·Widget 상태·실행 결과·성공 문장 생성 금지.
- 사용자 text를 원문에서 복사. 누락은 null, unknown 금지.
- 숫자·날짜·시간·ID 복구/추정 금지. SQL/shell/권한 필드 금지.

현재 도메인의 필요한 args schema, 오늘/내일 ISO 날짜, 설정 timezone만 추가합니다.
실제 메모 본문·할 일 목록·DB 구조는 prompt에 넣지 않습니다. reasoning 설명을 요구하지 않습니다.

요청 설정은 `temperature=0`, `max_tokens=min(기존 설정,64)`, non-thinking입니다.
긴 본문/복잡한 Proposal은 64토큰에서 잘릴 수 있습니다. `finish_reason != stop`이면 거부하고
실행하거나 자동으로 길이를 늘려 재시도하지 않습니다. 일반 대화 설정/저장된 모델은 바꾸지 않습니다.

### 확인한 llama-server API와 검증 경계

저장소 `deploy/termux/qwen-runtime/upstream.json`의 기준은 llama.cpp **b6000**,
commit `4762ad7316dcdec20016ab5985fb46a27902204d`입니다. 이 commit의
`tools/server/utils.hpp`에 있는 `oaicompat_chat_params_parse()`가 아래 중첩 필드를
실제로 읽는 것을 확인했습니다. README의 요약 예시만으로 API 형태를 추측하지 않았습니다.

```json
{
  "response_format": {
    "type": "json_schema",
    "json_schema": {
      "name": "widget_request_proposal",
      "strict": true,
      "schema": {"anyOf": ["<요청별 실제 capability schema>"]}
    }
  }
}
```

위 schema 내부는 설명용 자리표시자이며 실제 wire에는 JSON Schema object가 들어갑니다.
args의 date/start/end/time은 원문에서 별도로 검증된 값과 null만 허용하도록 제약합니다.
예를 들어 전사문이 `오늘 오전 2시 40분 아람 맞춰줘`이면 시간은 `["02:40", null]`이며
LLM이 이를 `12:40`으로 복구하도록 허용하지 않습니다. 서버 검증도 별도로 다시 수행합니다.

**저장소의 pin과 실제 V35 바이너리 상태는 다릅니다.** 개발 환경에서 V35 바이너리나 실제 Qwen을
실행하지 않았습니다. 지원하지 않는 response_format에 대해 자유 응답으로 자동 우회하지 않습니다.
모델/API 오류는 기존 failed 요청 이력에 기록합니다.

실기기에서 설치 변경 없이 확인하는 선택적 진단 도구:

```bash
# GET /v1/models + /props만 사용. 추론은 실행하지 않음.
"$PREFIX/bin/python" "$HOME/room-hub/scripts/probe_widget_decoding.py"

# 명시적으로 1회 합성 constrained generation을 수행. Widget 작업/DB 변경 없음.
"$PREFIX/bin/python" "$HOME/room-hub/scripts/probe_widget_decoding.py" --generate
```

기본 주소/모델은 현재 저장소의 `http://127.0.0.1:8090/v1`,
`Qwen3-0.6B-Q5_K_M.gguf`입니다. 커스텀 alias/port를 사용했다면 실제 설정에 맞는 `--model` /
`--base-url`을 지정하세요. 리터럴 loopback만 허용하고 redirect/proxy는 사용하지 않습니다.
기본 결과의 schema_probe=not_run은 constrained decoding 성공을 의미하지 않습니다.
`--generate`는 일부러 JSON과 다른 출력을 요청해도 제약된 합성 상수가 반환되는지 검사하는
단일 호환성 시험일 뿐, 모든 args schema/작업 정확도/실기기 latency를 보장하지 않습니다.
props의 경로·템플릿·API 키·모델 원문은 진단 출력에 포함하지 않습니다.

## Validation / source grounding

모델 출력은 전체 JSON object로 엄격 파싱합니다. code fence, 앞뒤 설명, 중복 JSON key,
nonfinite number, 잘린 출력, tool_calls/refusal, 과도한 크기는 거부합니다.

1. 기존 WidgetRequest projection과 실제 input model로 검증.
2. 고정한 domain/action 후보와 현재 Registry metadata를 비교.
3. 날짜·시간·제목·본문·ID를 원문/기존 clock/실제 source와 대조.
4. Registry의 read_only/requires_confirmation/permission_level/availability로 policy 적용.

시간·날짜 슬롯 및 target의 missing placeholder unknown/none/null/n/a/빈 값은 null로 바꿉니다.
사용자 본문 자체의 unknown은 바꾸지 않습니다. malformed 인자는 안전한 구조화 오류/clarification으로
처리하고 Pydantic 원문·DB exception을 사용자 답변으로 노출하지 않습니다.

날짜는 기존 요청 reference_at/timezone과 resolve_source_dates/resolve_range를 재사용합니다.
제안된 날짜가 원문과 다르면 거부합니다. 쓰기의 명시적 날짜를 모델이 누락했어도 임의 보완하지 않습니다.
시간은 기존 parse_clock 결과와 일치해야 합니다. 이 PR은 기존 parser의 `12시 40분`처럼
시와 숫자 분 사이 공백을 처리하는 최소 수정을 포함합니다. `오전 12시 40분`은 `00:40`,
`오후 12시 40분`은 `12:40`입니다. 미지원 한글 분·범위·대략적 시각은 추측하지 않습니다.

**그럴듯하지만 잘못된 전사문을 음성 원본 없이 알아낼 수는 없습니다.** 원래 발화가 12시인데
전사문이 2시이면 서버가 증명할 수 있는 값은 전사문 기준 2시뿐입니다. 이를 12시로 교정하지 않으며,
불확실한 슬롯은 null/clarify, 실행 가능한 쓰기의 전사 숫자는 관리자 preview에서 다시 확인합니다.

모델 ID는 원문에 그대로 있는 ID만 허용합니다. target/version은 실제 Adapter 조회 결과로 확정합니다.
현재/최근 메모는 기존 memo_snapshot 정책이고 브라우저 선택 상태는 여전히 알 수 없습니다.
ID 없는 task 변경은 명시된 날짜의 실제 목록에서 원문에 나온 제목이 정확히 하나일 때만 해석합니다.
필터·범위·반복·공유·일괄 변경을 새 fallback에서 조용히 생략해서 실행하지 않습니다.
기존 규칙으로 이미 안전하게 처리하던 일괄 작업은 기존 경로로 유지됩니다.

## Confirmation / durable replay

새 읽기는 Registry.execute(읽기 권한)로 즉시 실제 Adapter를 호출합니다.
새 쓰기는 **confirmed_digest 없이** Registry에 검증해 needs_confirmation에서 멈춥니다.
기존 관리자 상세의 확인 버튼과 `/api/assistant/{id}/confirm`을 재사용합니다.

preview는 WidgetRequest/operation digest/대상 ID·version·title/시간대/만료 시각에 결합됩니다.
서버가 같은 preview hash, 미만료, 변하지 않은 날짜·metadata를 검사한 뒤에만
ExecutionContext에 실제 확인 증거를 넣습니다. 모델/HTTP context.user_confirmed는 증거가 아닙니다.
현재 확인 만료는 10분, 요청 기준 시각 유효성은 기존 15분/날짜·timezone 검사입니다.

기존 `assistant_effects`에 action_key의 실행 예약을 먼저 commit합니다. 확인 요청이 중복되거나
retry family가 재실행돼도 새 mutation을 반복하지 않습니다. 이미 처리된 요청은 receipt를 반환합니다.
Adapter의 비즈니스 transaction과 이 ledger는 같은 transaction이 아닙니다. 따라서 service 실행과
최종 receipt 사이 crash는 **reserved/uncertain**이며, 성공/실패를 추측하거나 자동 재실행하지 않습니다.
실행 중 취소로 “변경을 취소했다”고 거짓 표시하지 않습니다. 불확정 상태는 실제 Widget에서 확인해야 합니다.
기존 규칙 기반 쓰기의 단일 transaction/일괄 처리 ledger는 그대로입니다. 새로운 exactly-once 보장은 없습니다.

성공 문장은 실제 `WidgetResponse.status=success` 이후 서버 formatter만 작성합니다.
AlarmAdapter.list는 실제 웹 알람을 조회하지만 set/cancel은 여전히 unavailable입니다.
시간 없는 set은 먼저 clarification으로 안내하고 “알람을 설정했습니다”는 출력하지 않습니다.

## 요청 기록 / privacy / latency

기존 관리자 assistant journal에 `widget_trace`를 추가합니다.

- domain, available_capabilities, parsed_proposal, schema_validation, policy_result.
- widget_request, widget_response, adapter, source_of_truth, resolved_target.
- latency_ms: stt_ms, router_ms, llm_ms, adapter_ms, total_ms.

원본 모델 답변/요청은 기존 `assistant.calls`/response_raw에 보존합니다. 실패한 자연어 답변은
관리자 원문 이력일 뿐 최종 답변/실행 결과가 아닙니다. 표시 기기에는 이런 내부 정보가 전달되지 않습니다.
Memo fallback은 제출 직후부터 private 경계를 적용합니다. 메모 변경 입력/결과는 표시용 LLM 위젯에서
마스킹하고, 메모 읽기는 기존 공유 여부/원문 버전/위젯 존재를 동적으로 재검사합니다.

stt_ms는 기존 음성 작업 elapsed가 있을 때만 기록합니다. router_ms는 준비/도메인/schema 생성 포함,
llm_ms는 실제 generate 호출 wall time, adapter_ms는 Registry 결과 latency 합입니다.
**total_ms는 ASR 이후 요청 처리 시간이며 STT·대기열·사람의 확인 대기 시간은 포함하지 않습니다.**
실제 모델 미호출은 llm_called=false, llm_ms=0, 토큰 N/A입니다. 모델 time/token은 provider 값을
기존 정책대로 표시하고 문자열 길이로 토큰을 추정하지 않습니다.

## 테스트와 성능

```bash
PYTHONDONTWRITEBYTECODE=1 HUB_DATA_DIR=artifacts/runtime-data python -m pytest -q tests/test_llm_widget_bridge.py
PYTHONDONTWRITEBYTECODE=1 HUB_DATA_DIR=artifacts/runtime-data python -m pytest -q
python scripts/check_repo.py
python scripts/build_previews.py
python scripts/widget_bridge_browser.py
python scripts/benchmark_fast_reads.py --iterations 30
```

CASE1~8 및 숫자/시간/ID 변조, dynamic schema, raw JSON, 실제 Adapter 오류, 확인 전 미실행,
동시 확인·stale version·취소·crash 이후 불확정 ledger·개인정보·기존 규칙을 검증합니다.
브라우저 도구는 합성 데이터, 실제 Uvicorn/SQLite/Registry, counted fake model과 기존 명시적
Python HTTP bridge/주입 WS 알림을 사용합니다. 실제 V35/iPad/Safari/ASR/Qwen 테스트가 아닙니다.
Fast-read 벤치마크는 합성 TestClient/SQLite의 후처리 측정이며 네트워크/STT를 제외합니다.
출력 토큰 cap은 64이고, 실기기 fallback 지연/정확도는 별도 실측 대상입니다.

## V35 배포와 rollback

새 DB schema/dependency/model 설치는 없습니다. 버전은 0.1.7 유지입니다.
사용자가 PR을 검토·Merge하고 **병합된 main CI 성공**을 확인한 뒤 바깥 Termux에서:

```bash
bash "$HOME/room-hub/deploy/termux/update-from-git.sh"
bash "$HOME/room-hub/deploy/termux/update-from-git.sh" --check
curl -fsS --max-time 5 http://127.0.0.1:8088/healthz
echo
sv status "$PREFIX/var/service/room-hub"
```

기존 updater가 fetch/fast-forward/DB 백업/검사/restart/health를 수행합니다. 수동 pip 설치,
Tasker/nginx/HTTPS 재등록은 하지 않습니다. 관리자 Ctrl+F5 후 명확한 읽기, 미확정 Widget 제안,
확인 전 미실행과 실제 확인 후 결과를 시험하세요. 기존 전사 결과는 수정하지 않습니다.

**Rollback 전:** 공유된 `llm-response` 위젯을 관리자 레이아웃에서 제거하거나, 민감한 새 메모
변경/조회 요청 이력을 관리자에서 삭제하세요. 이전 코드는 새 protocol_private 이력의 표시 차단을
알지 못합니다. 코드 revert만으로 민감한 이력이 삭제되는 것은 아닙니다.

GitHub에서 이 PR의 Revert PR을 만들고 검토·Merge한 후, 위와 동일한 updater/--check/health/sv
명령을 실행하면 이전 동작으로 돌아갑니다. 운영 V35에서 git reset --hard/수동 코드 편집을 하지 않습니다.
실패한 배포의 자동 rollback은 기존 updater 정책을 따릅니다. 코드 rollback은 이미 확인 후 실행한
실제 메모·할 일 변경을 취소하지 않습니다. 남은 reserved/uncertain 기록을 수동 재전송으로 우회하지 마세요.

후속 검증은 V35에서 실제 build_info/제약 출력 probe와 원문 숫자 확인, 명확한 읽기 fast_path_seconds,
대표 복잡 문장의 Proposal 정확도·필요 토큰 수를 측정하는 것입니다. 증거 없이 모델을 교체하거나
출력 한도를 자동 확장하지 않습니다.
