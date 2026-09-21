# 타이머 위젯 / TimerAdapter 1.0

[문서 홈](../README.md) · [Widget Protocol](WIDGET_PROTOCOL.md) · [V35 업데이트](V35_GIT_UPDATE.md)

## 범위

- 위젯 폴더/id는 `timers`, Protocol 서비스 이름은 `timer`입니다.
- 기본 크기 **2×2**, 입력은 **1~600초 정수**, **1초 단위**입니다. 기본 입력은 240초입니다.
- 모든 배치 인스턴스는 같은 서버 타이머 목록을 공유합니다. 위젯 하나에서도 여러 타이머를 시작할 수 있습니다.
- 2×2에서는 현재 타이머, 실행 수, 시작/종료를 표시합니다. `전체 N개 보기`를 눌러 기록과 각 실행 타이머를 확인·종료합니다.
- 기존 저장 레이아웃에는 자동 추가하지 않습니다. 관리자 `위젯과 배치`에서 타이머를 추가·저장하세요.
- 새 의존성, 모델 변경, Whisper 변경, 일반 Agent 구조 재작성은 없습니다.

## 시간과 상태의 기준

SQLite `hub_timers`의 UTC 시작/종료 시각이 source of truth입니다. 초마다 DB를 쓰지 않습니다. 브라우저는 서버 기준 시각과 monotonic 경과 시간으로 남은 시간을 그립니다. 실제 종료 확정은 서버가 합니다. 표시가 0이더라도 서버 갱신 전에는 `완료 확인 중`으로 표시합니다.

상태는 `running`, `expired`, `stopped` 세 가지입니다. 서버의 250ms 주기 검사와 조회 시 deadline 비교를 사용합니다. scheduler 오류는 snapshot에 드러내고 빈 목록이나 성공으로 숨기지 않습니다. 타이머가 없거나 아직 종료 시각이 아니면 scheduler는 쓰기 잠금을 잡지 않습니다.

재시작/연결 끊김 후에는 **원래 deadline**으로 복원합니다. 재시작했다고 4분을 처음부터 다시 세지 않습니다. 서버가 꺼진 동안 deadline이 지났으면 재기동/조회 시 종료 상태로 판정합니다. 시스템 시계가 크게 조정되면 wall-clock deadline에 영향을 줄 수 있습니다. 1초 설정 단위는 하드 실시간 기동/정확한 소리 재생 시각을 보장한다는 뜻이 아닙니다.

`current`는 **가장 최근에 시작한 실행 중 타이머 한 개**입니다. 같은 timestamp로 시작한 타이머도 DB sequence로 순서를 결정합니다. 종료된 타이머는 current에서 제외됩니다. iPad에서 선택한 카드나 LLM 기억을 current로 추정하지 않습니다. current가 없으면 `not_found`이며 아무것도 종료하지 않습니다.

자원 한도는 실행 중 최대 **32개**, 전체 목록은 정상 사용에서 최근 **256개**, durable mutation 영수증은 **10,000개**입니다. 실행 중 항목은 prune하지 않습니다. 영수증 한도에 도달하면 안전하게 거부하며 자동 삭제/재실행하지 않습니다. Registry의 기존 프로세스당 쓰기 영수증 한도(512개)도 유지합니다. 무한 개수를 지원한다고 표시하지 않습니다.

## 즉시 실행 권한 — 타이머만 해당

사용자가 요청한 `4분 타이머 시작`, `현재 타이머 종료`의 즉시 동작을 위해 **timer.start/stop만** 별도 확인 없이 실행합니다. 기간은 서버에서 1~600초로 검증합니다. 메모/할 일/일정/알람의 기존 confirmation은 유지합니다.

- 관리자 HTTP Protocol: 기존 관리자 인증/CSRF 뒤 timer.start/stop만 즉시 허용합니다.
- 페어링 표시 기기: 저장 레이아웃에 `timers`가 배치되어 있을 때만 좁은 `/api/timers` API에서 공유 타이머를 시작/종료할 수 있습니다.
- 표시 기기에 일반 Widget Protocol, Todo 편집, 메모/알람 관리 권한을 추가하지 않습니다. 기기 권한과 페어링 해제는 매 호출 재검증합니다.
- 음성: 기존 SpeechHub → LLMHub(mode=auto) → 서버 Router 경로를 사용합니다. 모델에 실행 컨텍스트/권한을 맡기지 않습니다.
- `context.user_confirmed`는 권한을 부여하지 않습니다.

TimerAdapter capability의 `read_only=false`, `permission_level=control`, `requires_confirmation=false`가 예외를 명시합니다. Registry manifest의 `http_immediate_actions`는 `timer.start`, `timer.stop`만 나열합니다. 이전 문서의 'HTTP 쓰기 모두 확인 대기'는 timer를 제외한 기존 Adapter에 그대로 해당합니다.

## Adapter 규약

Protocol envelope는 1.0을 유지합니다. 새 모델 전용 tool schema를 따로 복제하지 않고 같은 Registry metadata에서 후보/제약 스키마를 만듭니다.

| action | args | target | 동작 |
|---|---|---|---|
| list | `{}` | null | 전체 실제 snapshot 조회 |
| get | `{}` | item_id 또는 current reference | 특정 타이머 조회 |
| start | duration_seconds:1..600, 선택 label(80자) | null | 새 타이머 즉시 시작 |
| stop | 선택 version(양의 정수) | item_id 또는 current reference | 해당 타이머만 즉시 종료 |

실제 시작 요청 예:

```json
{
  "protocol_version": "1.0",
  "request_id": "timer-start-001",
  "widget": "timer",
  "action": "start",
  "target": null,
  "args": {"duration_seconds": 240, "label": "타이머"}
}
```

현재 타이머 종료 요청 예:

```json
{
  "protocol_version": "1.0",
  "request_id": "timer-stop-001",
  "widget": "timer",
  "action": "stop",
  "target": {"type": "reference", "value": "current"},
  "args": {}
}
```

개별 타이머는 `{"type":"item_id","value":"실제 타이머 ID"}`로 지정합니다. start/stop 응답에는 `id`, `duration_seconds`, `label`, `state`, `started_at`, `deadline_at`, `ended_at`, `remaining_seconds`, `version`, `changed`, `duplicate`가 있습니다. list 응답은 `items`, `current_id`, `active_count`, `revision`, `server_time`, `max_active`, `scheduler_error`, `delivery`를 제공합니다.

`source_of_truth=room_hub_sqlite.hub_timers`. 실제 변경은 `timer.started`, `timer.stopped`, deadline 도달은 `timer.expired` 기존 갱신 알림으로 전달됩니다. 기존 관리자 `widget_trace`에 요청/응답/대상/LLM 호출 여부를 그대로 남깁니다. 표시 DTO에는 principal이나 영수증 키가 없습니다.

## 중복·재시도

start/stop은 **요청 ID와 호출 주체**를 해시해 업무 변경과 영수증을 같은 SQLite transaction에 기록합니다. 같은 키/같은 요청은 새 deadline을 만들거나 다른 타이머를 종료하지 않습니다. 같은 키/다른 내용은 conflict입니다. `current` resolve보다 영수증 replay를 먼저 합니다.

음성은 기존 `action_key`(재시도 계열)를 키로 사용합니다. UI는 응답 손실 시 동일 작업의 키를 유지합니다. 네트워크 오류 뒤 다시 누르는 경우라도 같은 요청의 중복 효과를 방지합니다. 타이머 데이터가 prune된 오래된 replay는 저장된 영수증으로 '이미 처리한 요청'을 응답하고 새로운 작업은 하지 않습니다.

다만 사용자가 별도 녹음을 다시 만들거나 완전히 새 요청 키로 명령하면 새 사용자 요청입니다. 이것까지 동일 명령으로 간주해 무조건 제거하지 않습니다. 이미 시작한 timer의 DB 변경은 Assistant 요청 취소나 코드 revert가 자동 undo하지 않습니다.

## 음성/LLM 연결

명확한 다음 명령은 서버 규칙으로 처리하고 **LLM을 호출하지 않습니다**.

```text
4분 타이머 시작
타이머 4분 시작해줘
1분 30초 타이머 시작
600초 타이머 시작
네 분 타이머 시작해 줘
현재 타이머 종료
마지막 타이머 중지
현재 타이머 남은 시간 알려줘
```

0초, 601초, 음수/소수, 시간 누락은 명확화로 처리합니다. 알람과 timer의 도메인을 분리했습니다. 명확한 timer 요청의 진단은 `EXISTING_RULE / EXACT / llm_called=false`입니다.

정규 문법 밖의 timer 요청만 기존 constrained LLM fallback에 해당 후보를 노출합니다. 모델은 실제 상태/완료 문장이 아닌 Proposal만 생성합니다. 서버는 원문에 있는 duration과 start/stop 의도, 실제 target ID 또는 current 근거를 다시 검사한 뒤 Adapter를 실행합니다. 모델이 임의 숫자나 ID를 생성하면 실행하지 않습니다. 기존 temperature/max_tokens/모델 설정은 바꾸지 않습니다. 다른 timer 표현의 폭넓은 의미 이해/실기기 Qwen 정확도 개선은 이번 범위가 아닙니다.

일시 정지/재개/기간 변경/예약 시작/여러 개 일괄 종료/반복 timer는 아직 지원하지 않습니다. 요구를 생략해서 실행하지 않고 확인을 요청합니다.

## 브라우저 완료 알림

소리는 **각 브라우저에서 `소리 허용`을 누른 경우만** 유한한 짧은 알림음을 재생합니다. 음소거, 오프라인, 숨겨진 페이지, 관리자 iframe 미리보기에서는 울리지 않습니다. 서버 연결/화면 권한이 끊기면 소리를 멈춥니다. 처음 조회한 과거 종료 기록은 다시 울리지 않습니다.

이것은 Android/iOS OS 타이머, 푸시, 잠금 화면 기동 기능이 아닙니다. 화면/브라우저 종료·백그라운드·절전에서 소리를 보장하지 않습니다. DB `expired`는 실제 소리 재생 증거가 아닙니다. 새로고침 후에는 소리 허용을 다시 눌러야 합니다.

## 검증

```bash
python -m pytest -q tests/test_timers.py tests/test_timer_bridge.py
python scripts/timers_browser.py
```

합성 clock, 실제 SQLite/Registry/FastAPI/Uvicorn, 카운터가 있는 모의 LLM/STT로 경계/병행/중복/재시작/권한과 UI를 검사합니다. 실기기 Whisper 인식 정확도나 Qwen generation/알림음 검증은 별도입니다. `HUB_BROWSER_BRIDGE=1`은 네트워크가 제한된 개발환경에서만 기존 명시적 Python HTTP bridge와 주입 WS 알림을 사용합니다. CI는 기본 직접 Chromium HTTP/WS 경로입니다.

## 적용 / rollback

PR 검토·Merge 후 main CI가 성공했는지 확인하고 기존 수정 BAT 또는 바깥 Termux에서:

```bash
bash "$HOME/room-hub/deploy/termux/update-from-git.sh"
bash "$HOME/room-hub/deploy/termux/update-from-git.sh" --check
curl -fsS --max-time 5 http://127.0.0.1:8088/healthz
echo
sv status "$PREFIX/var/service/room-hub"
```

별도 pip/model 설치는 필요 없습니다. 최초 실행에서 additive timer 테이블 3개만 생성합니다. VERSION=0.1.7 유지이므로 Git SHA와 widget 목록으로 적용을 확인합니다. 관리자/표시 페이지 새로고침 후 타이머를 레이아웃에 추가하세요. 저장된 다른 위젯/메모/알람 데이터는 수정하지 않습니다.

되돌리기 전 실행 timer를 종료하고 타이머 위젯을 레이아웃에서 제거하세요. Revert PR 검토·Merge 뒤 같은 updater로 배포합니다. 타이머 테이블은 남겨두며 과거 데이터/영수증을 자동 삭제하지 않습니다. 다시 기능을 배포해도 원래 deadline을 사용합니다. 전체 DB를 오래된 백업으로 복원하면 그 이후 요청 영수증도 소실되므로 오래된 음성 요청을 무조건 재전송하지 마세요.
