> 업데이트: [라우팅 안정화](ROUTING_STABILIZATION.md)에서 AlarmAdapter.set/cancel을 기존 웹 알람 서비스에 연결했습니다. 아래의 미연결 표시는 그 이전 계약 설명입니다. HTTP 쓰기 확인 경계는 동일합니다.

# Widget Communication Protocol 1.0

[문서 홈](../README.md) · [기존 브라우저 위젯 API](WIDGET_API.md) · [V35 Git 업데이트](V35_GIT_UPDATE.md)

## 목적과 이번 경계

`Caller → WidgetRequest → WidgetRegistry → WidgetAdapter → 기존 서비스/DB → WidgetResponse`.

UI 폴더 플러그인과 서비스 호출 규격은 서로 다릅니다. `widgets/note`, `widgets/todos`,
`widgets/all-todos`, `widgets/calendar`, `widgets/alarms`는 기존 브라우저 렌더러이고,
프로토콜의 `memo`, `todo`, `calendar`, `alarm`은 서버 서비스 이름입니다.
프런트 플러그인에서 Python 코드를 자동 로드하거나 실행하지 않습니다.

LLM prompt/tool/schema, Fast Router, STT, 음성 자동 전달, 기존 UI/REST 경로는 교체하지 않습니다.
`app/capabilities.py`는 계속 **비서에 허용한 기능 등록부**입니다. 새로운 registry의
`assistant_capability`는 기존 이름과의 대응 정보일 뿐, LLM에게 추가 실행 권한을 부여하지 않습니다.

실제 읽기와 기존 CRUD를 호출하는 서버 Adapter를 구현합니다. **새 HTTP 경로의 쓰기는
`needs_confirmation`에서 멈춥니다.** 이 PR에는 확인 UI/승인 endpoint/Policy Engine이 없습니다.
신뢰된 Python 호출자가 요청별 확인을 별도로 얻은 경우에만 쓰기를 실행할 수 있습니다.
기존 관리자 UI의 편집 API 및 기존 비서의 확인/receipt 경로는 그대로입니다.

## 조사한 기존 구조와 재사용

- `Store.query_tasks`: 할 일과 달력이 공유하는 실제 tasks 조회, 날짜/상태/오전·오후/개수/잘림.
- `main.py`의 기존 `add_task`, `edit_task`, `complete_task`, `delete_task`: 버전, 반복 회차,
  10,000개 제한, 변경 통지. `TaskServices`로 이 함수들을 주입하며 비즈니스 SQL을 복제하지 않습니다.
- `LifeService`: hub_notes, hub_alarms, hub_alarm_events 및 기존 생성/회차 receipt.
- `memo_snapshot`: 현재 기본 카드/최근 수정 메모. optional `widget_id`만 추가하여 명시적 위젯도
  같은 읽기 snapshot으로 조회합니다. 기존 호출의 기본 동작은 바뀌지 않습니다.
- 기존 `TaskCreate`, `TaskPatch`, `TaskCompletion`, `NoteCreate`, `NoteUpdate`, `AlarmInput` 모델 재사용.
- 기존 admin 인증/CSRF 검사와 audit 테이블/관리자 활동 이력 재사용.
- 새 테이블, 의존성, 큐, event bus, Android companion은 추가하지 않습니다.

## Request / Response

```json
{
  "protocol_version": "1.0",
  "request_id": "4d130e28-a988-4d67-9084-62e0a7a31c30",
  "idempotency_key": null,
  "widget": "todo",
  "action": "list",
  "target": null,
  "args": {"date": "2026-09-21", "status": "all", "limit": 50},
  "context": {"source": "api", "session_id": null, "user_confirmed": false},
  "extensions": {}
}
```

응답은 같은 request_id를 돌려줍니다. 잘못된 envelope에 유효 ID가 없으면 `null`이며 새 ID를 꾸미지 않습니다.
다음은 **형식 예시**이지 실제 운영 조회 결과가 아닙니다.

```json
{
  "protocol_version": "1.0",
  "request_id": "4d130e28-a988-4d67-9084-62e0a7a31c30",
  "status": "needs_clarification",
  "widget": "alarm",
  "action": "set",
  "data": null,
  "error": {"code": "MISSING_REQUIRED_ARGUMENT", "message": "필수 값을 명시해 주세요.", "field": "args.time", "retryable": false},
  "meta": {"adapter": "AlarmAdapter", "source_of_truth": "room_hub_sqlite.hub_alarms", "changed": false,
           "executed_at": null, "latency_ms": 0.0, "duplicate": false, "idempotency_scope": "none",
           "outcome_uncertain": false, "request_digest": null, "events": [], "warnings": []}
}
```

`success`, `needs_confirmation`, `needs_clarification`, `not_found`, `invalid_request`,
`permission_denied`, `conflict`, `unavailable`, `error`를 사용합니다. `accepted`/`running`은
후속 비동기 확장용 예약 값이며 현재 작업 queue는 없습니다. 클라이언트는 미지원 status/version을
성공으로 해석하면 안 됩니다. 1.0만 허용하며 확장 정보는 명시적 `extensions` 공간을 사용합니다.

`args`는 capability별 스키마로 검증하며 알 수 없는 필드는 거부합니다. 요청 크기는 64KiB 이하입니다.
시간 없음은 `null`; `"unknown"` 같은 문자열은 생성하지 않으며 HH:MM 슬롯에 들어오면
구조화된 `invalid_request`로 거부합니다. 필수 값의 누락/null은 `needs_clarification`으로 반환합니다.
Memo 본문에 일반 텍스트로 적힌 unknown을 바꾸는 전역 문자열 치환은 하지 않습니다.
Pydantic exception의 원본 input/context나 DB 오류 원문을 wire 응답에 내보내지 않습니다.

## 실제 Adapter와 capability

| Adapter | 구현한 동작 | 조건과 실제 source |
|---|---|---|
| MemoAdapter | read, write, append, clear | LifeService/hub_notes 및 기존 kv 고정 문구 |
| TodoAdapter | list, get, add, complete, reopen, delete | 기존 tasks/series 및 기존 CRUD |
| CalendarAdapter | list, get, add, update, delete | 같은 tasks/series; 외부 달력이 아님 |
| AlarmAdapter | list | 기존 LifeService 예약 조회만; 스케줄러 실행/소리 재생을 유발하지 않음 |

`alarm.set/cancel`은 input/output 및 `control` metadata를 가진 **unavailable 선언**입니다.
필수 인자가 없는 alarm.set은 정상 clarification, 유효한 set/cancel은 unavailable을 반환합니다.
Android 알람 제어 또는 새 예약 제어 권한이 생기는 것이 아닙니다.

각 capability는 action/description/input_schema/output_schema/read_only/requires_confirmation/
permission_level/idempotent/idempotency_scope/availability/target_types/target_required를 제공합니다.
input_schema는 `args`, output_schema는 성공 응답 `data`의 JSON Schema입니다. target 규칙은 별도 metadata입니다.
쓰기 필드의 조건부 필요 여부(예: memo 수정의 version)는 validate_slots에서도 검사합니다.

### Target / Context

- `item_id`: 실제 memo ID 또는 task 회차 ID. ID 없는 get/update/complete/delete는 clarification.
- `widget_id`: memo.read가 **실제 저장된 note 위젯 인스턴스**를 확인해서 기본 카드를 읽습니다.
- `reference: current`: 기존 기본 note 위젯의 기본 카드. target 생략도 같은 정책입니다.
- `reference: last` 또는 `last_modified`: 기존 updated_at 최신 메모. 동률은 clarification.
- `reference: active/selected`: 서버가 브라우저의 선택 카드를 알지 못하므로 clarification.
- context의 active_widget/selected_item/last_action/session_id는 향후 resolver용 힌트이지 권한이나 확정 상태가 아닙니다.
- date_ref today/tomorrow, period morning/afternoon을 context에 표현할 수 있지만 이번 Adapter는
  이를 날짜로 암묵 변환하지 않습니다. 조회 args는 **실제 ISO date 또는 start/end**를 받습니다.
  args.period는 기존 morning/afternoon SQL 필터를 그대로 사용할 수 있습니다.
- 메모 위젯이 배치되지 않아 current를 알 수 없으면 임의 메모를 선택하지 않습니다. 실제 item_id를 사용하세요.

### 쓰기 의미

- memo.write: target 없음은 새 메모 생성(title/body 필요). item_id는 기존 본문 교체(body/version 필요).
  title/shared/pinned 생략은 기존 값을 보존합니다. 새 메모 shared 기본값은 false입니다.
- memo.append: item_id/version/text 필요. text를 그대로 연결하며 암묵 줄바꿈/요약을 넣지 않습니다.
- memo.clear: item_id/version 필요. 항목 자체는 남기고 body만 빈 문자열로 바꿉니다.
- todo/calendar.add: 기존 TaskCreate와 반복 생성 규칙을 사용합니다. time=null 허용.
- complete/reopen: version이 있는 단일 회차의 원하는 완료 상태를 지정하며 토글하지 않습니다.
- calendar.update: 기존 TaskPatch 모델을 사용합니다. time=null로 시간을 지울 수 있습니다.
- delete: item_id/version으로 지정한 **한 회차만** 지웁니다. future/series/bulk 삭제는 노출하지 않습니다.
- 기존 정적 고정 메모는 item_id가 없으므로 이 프로토콜로 직접 수정하지 않습니다.

## Python interface / 신뢰 경계

```python
from app.widget_protocol import ExecutionContext, WidgetRequest

# 실제 세션 인증 이후 서버 코드가 만드는 객체. LLM/HTTP JSON에서 만들지 않는다.
authority = ExecutionContext(principal="admin", role="admin", permissions=frozenset({"read"}))
request = WidgetRequest(request_id="query-001", widget="todo", action="list",
                        args={"date": "2026-09-21"})
response = await app.state.widget_protocol.execute(request, authority)
```

`registry.get(name)`, `list_widgets()`, `get_capabilities(name)`, `manifest()`, `execute()`를 제공합니다.
Adapter SPI는 name, capabilities(), validate_request(), execute(), get_state(), health()입니다.
낮은 계층의 Adapter는 `AdapterResult` 또는 안전한 `ProtocolFault`를 반환/발생시키고,
**caller는 항상 registry.execute()를 통해** 균일한 WidgetResponse·replay·audit 처리를 받습니다.
각 Adapter.execute에도 권한/확인 검사가 있어 실수로 SPI를 직접 호출해도 쓰기 승인을 우회하지 않습니다.
단, 직접 SPI 호출은 registry 중복 방지/로그를 거치지 않으므로 일반 caller 경로가 아닙니다.

실제 쓰기에는 `ExecutionContext.confirmed_digest == request_digest(request)`가 추가로 필요합니다.
이는 **기존/향후 승인 계층이 실제 확인을 얻었다는 서버 내부 증거를 전달하는 자리**입니다.
해시 계산 자체는 승인이 아닙니다. LLM 출력 또는 클라이언트 주장을 받아 이 필드를 채우면 안 됩니다.
이번 HTTP handler는 이 값을 절대 설정하지 않습니다. digest에는 operation/target/args/version/확장 값이 결합됩니다.
`context.user_confirmed=true`, `source=voice`, 임의 session_id로 권한은 바뀌지 않습니다.

새 Adapter는 Python에서 WidgetAdapter를 상속하고 Operation 목록/기존 service binding을 제공한 뒤
`registry.register(adapter)`로 명시적으로 등록합니다. 문자열 기반 dynamic import/eval/SQL/URL 실행은 없습니다.
Protocol은 source 상태를 조회하는 것이지 LLM memory를 참조하거나 답변 문장을 생성하는 계층이 아닙니다.

## HTTP interface

기존 관리자 Bearer 또는 room_admin 세션 인증을 사용합니다. 신규 키, 공개 endpoint, CORS 허용을 추가하지 않습니다.
표시 세션/ingest 키는 새 API를 쓸 수 없으며 기존 UI의 제한된 완료/알람 회차 조작은 그대로 유지합니다.

| Method / 경로 | 역할 |
|---|---|
| GET /api/widget-protocol/schema | Request/Response/Capability/Event JSON Schema |
| GET /api/widget-protocol/widgets | 서비스와 capability 목록 |
| GET /api/widget-protocol/widgets/{name} | 특정 서비스 metadata |
| GET /api/widget-protocol/health | Adapter별 실제 DB 접근 가능 여부; 본문/경로 없음 |
| POST /api/widget-protocol/requests | 공통 envelope 실행/검증 |

HTTP status: success 200, confirmation 409, clarification 422, invalid 400, not_found 404,
conflict 409, unavailable 503, internal error 500. 미인증은 401, 64KiB 초과는 413.
전역 CSRF/Origin/body 제한은 기존 middleware가 먼저 처리하므로 그 단계의 오류는 기존 transport 형식입니다.
잘못된 JSON/중복 JSON key/non-finite number도 구조화된 오류로 거부합니다.

관리자에 로그인한 같은 출처 브라우저의 개발자 콘솔에서 읽기 확인 예:

```javascript
const clock = await fetch('/api/clock').then(r => r.json());
const reply = await fetch('/api/widget-protocol/requests', {
  method: 'POST',
  headers: {'Content-Type': 'application/json', 'X-Room-Request': '1'},
  body: JSON.stringify({protocol_version: '1.0', request_id: 'manual-check-' + Date.now(),
    widget: 'todo', action: 'list', args: {date: clock.today}, context: {source: 'web-ui'}})
}).then(r => r.json());
console.log(reply);
```

개인 메모/일정 응답이나 세션/토큰을 공개 이슈에 붙이지 마세요. HTTP 메모 읽기는 관리자 데이터이며
기존 표시용 LLM DTO로 자동 복사하지 않습니다.

## Replay, errors, events, observability

- 읽기는 캐시하지 않습니다. 같은 request_id로 다시 읽어도 최신 source를 조회합니다.
- 쓰기는 principal + idempotency_key(없으면 request_id), 정확한 operation digest로 프로세스 내 중복을 막습니다.
  같은 키/같은 값은 기존 결과를 반환하고 duplicate=true, changed=false, events=[]입니다.
  같은 키/다른 값은 conflict이며 새 실행이 없습니다.
- 최대 512개 기록을 유지하고 가득 차면 신규 쓰기를 unavailable로 거부합니다. 오래된 키를 조용히 버려 재실행하지 않습니다.
- **프로세스 재시작 이후 exactly-once를 보장하지 않습니다.** 일반 write의 idempotent=false는
  무조건 재호출해도 된다는 약속을 하지 않기 때문이며, idempotency_scope=process로 범위를 명시합니다.
  memo 생성은 기존 LifeService의 durable receipt도 재사용합니다. 업데이트/삭제는 기존 version 검사도 유지합니다.
- 실패/취소가 commit 이후인지 확정 못하면 changed=null, outcome_uncertain=true로 응답/기록하고
  같은 키의 자동 재실행을 막습니다. 이 경우 실제 상태를 읽고 판단해야 합니다.
- 성공 이벤트는 기존 서비스가 이미 저장/통지한 변경을 설명합니다. 새 bus/queue/delivery 보장은 없으며
  메모 본문·일정 전체는 event.data에 넣지 않습니다.
- audit의 widget.protocol 레코드는 request_id/widget/action/adapter/source/status/latency/changed/error_code,
  **인자 이름만** 기록합니다. 인자 값, 본문, 제목, 세션, 원본 exception은 새 프로토콜 로그에 남기지 않습니다.
  기존 CRUD 자체의 기존 audit 방식은 유지됩니다.
- audit 실패가 성공한 쓰기를 rollback/재실행시키지 않습니다. meta.warnings에 AUDIT_UNAVAILABLE을 표시합니다.
- 읽기 audit는 전역 revision을 올리거나 전체 화면 invalidate를 발생시키지 않습니다.

## 테스트 / V35 적용 / rollback

```bash
python -m pytest -q tests/test_widget_protocol.py
python -m pytest -q
python scripts/check_repo.py
python scripts/live_smoke.py
python scripts/browser_smoke.py
python scripts/life_browser.py
```

합성 DB/HTTP로 memo/todo/calendar 실제 데이터, 누락/null/미지원, registry 확장,
confirmed 서버 CRUD, stale version, 중복/동시성/불확정 결과, 개인정보/기존 권한/기존 API를 검증합니다.
정상 Fast-path 및 전사 자동 전달은 기존 전체 회귀 테스트로 검증합니다.
실제 V35/iPad, 장시간 사용, 외부 네트워크는 별도 확인 대상입니다.

사용자 review/merge 후 **병합된 main CI 성공**을 확인하고, 확인된 V35 바깥 Termux 경로에서:

```bash
bash "$HOME/room-hub/deploy/termux/update-from-git.sh"
bash "$HOME/room-hub/deploy/termux/update-from-git.sh" --check
curl -fsS --max-time 5 http://127.0.0.1:8088/healthz
echo
sv status "$PREFIX/var/service/room-hub"
```

기존 updater가 fetch/fast-forward/SQLite 백업/검사/restart/health 확인을 수행합니다.
수동 pip 설치, 모델 설치, nginx/인증서/Tasker 재등록은 없습니다. 앱 VERSION은 0.1.7 유지이므로
HEAD/tree/SYNCED 및 관리자 Protocol schema endpoint로 패치 적용을 구분하세요.

서버 배포 실패의 자동 rollback은 기존 updater 정책을 따릅니다. 이 독립 PR을 GitHub에서 revert하고
그 revert PR을 검토·merge한 뒤 같은 updater로 배포하면 새 Adapter/API만 제거됩니다.
이 코드 revert는 **이미 신뢰된 caller가 확인·실행한 실제 CRUD를 취소하지 않습니다.**
새 schema migration은 없어 제거 후 기존 UI/비서가 기존 DB를 계속 사용합니다.

후속 LLM/Rule/MCP bridge는 registry.manifest()에서 스키마를 얻고 registry.execute()를 호출하면 됩니다.
반드시 원문·대상·권한·실제 확인을 검증하는 별도 경계를 거쳐야 하며, 모델에 admin 키나
ExecutionContext 생성 권한을 주지 않습니다. 기존 confirmation broker와의 통합은 후속 PR입니다.
