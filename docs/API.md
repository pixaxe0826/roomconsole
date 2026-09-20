# HTTP / WebSocket API v1

기본 주소 예시 `http://192.168.0.20:8088`. 실제 IP로 바꿉니다. 공개 인터넷용 URL이 아닙니다.

## 인증

관리자: `Authorization: Bearer <ADMIN_KEY>` 또는 로그인 후 `room_admin` HttpOnly 쿠키.
표시 기기: 연결 링크 교환 후 `room_display` HttpOnly 쿠키. 상태 조회·디스플레이 WS와 완료 전용 PATCH만 허용. 일반 작업 수정·삭제·생성 권한은 없습니다.
음성 어댑터: `Authorization: Bearer <INGEST_KEY>`. 음성 수신 POST만 허용.

브라우저의 변경 요청은 `X-Room-Request: 1`을 보냅니다. 다른 출처의 Origin은 거부합니다. 관리자 키·입력 키를 일반 조회 URL의 query에 넣지 않습니다. 연결 코드만 URL fragment로 전달하고 교환 직후 지웁니다.

## 주요 경로

| Method | 경로 | 내용 |
|---|---|---|
| GET | /healthz | 공개 상태 확인 |
| GET | /client, /manager | 앱 화면 |
| POST | /api/auth/login | `{"token":"..."}` 관리자 세션 |
| POST | /api/auth/logout | 현재 관리자 쿠키 세션 종료 |
| GET | /api/auth/me | 현재 역할 |
| GET | /api/state | 표시용 스냅샷 |
| GET | /api/admin/overview | 등록 기기·수신함·감사 기록 |
| POST | /api/devices/pair | 이름으로 일회용 연결 링크 생성 |
| POST | /api/devices/claim | `{"code":"..."}` 링크 교환 |
| DELETE | /api/devices/{id} | 표시 권한 회수 |
| GET | /api/admin/qr?url=... | 관리자 전용 연결 QR SVG |
| POST | /api/tasks/preview | 반복 날짜·개수 미리보기, 저장 안 함 |
| POST | /api/tasks | 작업·회차 생성, 201 |
| PATCH | /api/tasks/{id} | 관리자 전용, version 포함 단일 회차 수정 |
| PATCH | /api/tasks/{id}/completion | 표시 기기/관리자, version + completed만 허용 |
| DELETE | /api/tasks/{id}?scope=one&version=1 | one / future / series 삭제 |
| PUT | /api/layout | version 포함 전체 배치 저장 |
| PUT | /api/settings | 전체 허브 설정 저장 |
| POST | /api/weather/refresh | 서버 날씨 갱신 |
| POST | /api/commands | 온라인 화면 원격 제어 |
| POST | /api/widgets/reload | 폴더 검색 갱신 |
| PUT | /api/widgets/{type}/data | 서버 위젯 데이터 저장 |
| GET | /api/widgets/{type}/data | 위젯 데이터 읽기 |
| POST | /api/voice/text | 전사 텍스트 수신, 202 |
| POST | /api/voice/upload | multipart 파일 수신, 202 |
| GET | /api/voice/{id}/audio | 관리자 원본 파일 다운로드 |
| PATCH | /api/voice/{id} | status / 선택 text 검토 |
| DELETE | /api/voice/{id} | 원본 포함 삭제 |
| GET | /api/admin/integration | 입력 키·기능 경계 확인 |
| GET | /api/admin/export | JSON 파일 |
| GET | /api/admin/backup | SQLite 백업 파일 |

## 반복 생성 예

```json
{
  "title":"책 20페이지 읽기",
  "date":"2026-09-17",
  "time":"21:00",
  "category":"study",
  "priority":"normal",
  "notes":"완료 상태는 날짜마다 따로 관리",
  "repeat":{
    "frequency":"weekly",
    "interval":1,
    "until":"2026-10-31",
    "weekdays":[0,2,4]
  }
}
```

frequency: none/daily/weekdays/weekly/monthly. 요일은 월=0~일=6. 종료일을 포함해 생성합니다. 월간은 원래 날짜를 월말에 맞추어 보정합니다. 최대 1,827일, 전체 작업 10,000개. 주간에서 weekdays가 비어 있으면 시작 요일을 사용합니다.

수정 예 `{"version":1,"completed":true}`. 시간을 지우려면 `"time":null`. 나머지 필수 데이터의 null은 거부합니다. 현재 version과 다르면 409이므로 최신 스냅샷을 읽고 사용자에게 충돌을 알려야 합니다.

## 표시 기기의 완료 변경 (0.1.1)

```http
PATCH /api/tasks/<id>/completion
Content-Type: application/json
X-Room-Request: 1
Cookie: room_display=<HttpOnly cookie supplied by browser>

{"version": 1, "completed": true}
```

`completed`는 엄격한 JSON boolean, `version`은 1 이상의 정수입니다. 문자열이나 추가 필드는 422입니다.
`true`는 완료, `false`는 완료 취소입니다. 현재 값을 반전시키는 toggle API가 아니므로 전송 중복으로 이중 반전되지 않습니다.
정상 변경 시 해당 회차의 version을 1 증가시키고 갱신 이벤트를 연결된 관리자/표시 기기에 보냅니다.
같은 version에서 이미 원하는 완료 상태라면 값을 바꾸지 않고 반환합니다. 과거 version의 재요청은 409입니다.
제목/날짜/메모/반복 규칙은 수정하지 않습니다. 삭제는 404, 인증 만료/취소는 401입니다.
기존 기기 세션에 적용되며 DB 스키마 변경이나 재페어링은 필요하지 않습니다.

`GET /api/state`의 `capabilities.task_completion`이 true일 때 클라이언트가 완료 UI를 활성화합니다.
저장 중에는 중복 탭을 차단하고, 오류 시 자동 재실행하지 않습니다. 응답 유실은 저장 결과가 불명확하므로
최신 스냅샷을 조회하여 상태를 확인합니다. 완료 변경 요청은 12초에 중단하고 UI 잠금을 해제합니다.

**요일 인덱스 주의:** 화면 날짜 줄은 일~토이지만, 기존 반복 생성 API의 weekdays는 여전히 월=0~일=6입니다.
이미 저장한 반복 작업을 이동하거나 다시 생성하지 않습니다.

## 음성 전사 JSON

```json
{
  "schema_version":"1",
  "request_id":"adapter-20260917-0001",
  "source":"custom-adapter",
  "text":"내일 할 일에 산책하기 추가해 줘",
  "locale":"ko-KR",
  "metadata":{"provider_request_id":"example"}
}
```

전사 텍스트는 최대 16,000자입니다. `source + request_id`가 멱등 키입니다. 같은 정규화 내용의 재전송은 `duplicate:true`; 다른 내용이면 409입니다. 요청에 대해 해시를 저장하며 원문을 자동 실행하지 않습니다.

```json
{"id":"...","status":"pending_review","duplicate":false}
```

업로드:

```bash
curl -X POST 'http://192.168.0.20:8088/api/voice/upload' \
  -H 'Authorization: Bearer <INGEST_KEY>' \
  -F 'request_id=device-0001' -F 'source=custom-adapter' \
  -F 'locale=ko-KR' -F 'file=@sample.wav;type=audio/wav'
```

10MB 이하 오디오: audio/wav, audio/x-wav, audio/wave, audio/mpeg, audio/mp4, audio/x-m4a, audio/webm, audio/ogg, audio/flac, audio/aac. 오디오 원본은 `awaiting_transcription`으로 보관됩니다. UTF-8 TXT는 디코딩하여 text와 같은 수신함으로 보냅니다. 이 경로 자체는 자동 전사하지 않습니다. 0.1.3의 관리자가 큐에 넣거나 iPad가 별도 음성 API를 사용하면 로컬 전사합니다. MIME 검사는 완전한 음원 유효성 검증이 아닙니다. 본문 전체 제한 11MB입니다.

## WebSocket

- `/ws/display`: 표시용 쿠키 또는 관리자 쿠키.
- `/ws/manager`: 관리자 쿠키.
- 동일 출처만 사용. 브라우저 WS URL에 키를 붙이지 않습니다.

서버 → 브라우저: `hello`, `invalidate`, `pong`, `command`, `revoked`, 관리자용 `device_ack`.
브라우저 → 서버: `ping`, `presence`, `ack`.

```json
{"type":"invalidate","revision":42}
```

수신 시 `/api/state`를 다시 조회합니다. 연결 단절 시 데이터 변경을 로컬에서 확정하거나 서버로 쓰지 않습니다.

```json
{"action":"expand","widget_id":"calendar","device_id":null}
```

위 본문을 POST `/api/commands`로 보내면 서버가 명령 ID·전송 시각·30초 만료를 추가하여 온라인 화면에 전달합니다. action은 home/expand/select_date/reload. select_date에는 `date`가 필요합니다. device_id를 비우면 모든 표시 연결로 보냅니다. 브라우저는 중복 ID를 무시합니다. ACK는 렌더 처리 확인이며 사용자 열람 확인이 아닙니다. reload는 즉시 새 페이지를 열기 때문에 ACK를 보장하지 않습니다. 오프라인 큐는 없습니다.

## 0.1.3 iPad 음성 업로드와 로컬 전사

[SPEECH_API.md](SPEECH_API.md)를 참고하세요. 별도 기기 소유권/대기열/취소/재시도 경로를 사용합니다.

## 0.1.4 LLM 인터페이스
관리자 전용 API와 입력/출력·측정 규약은 [LLM_INTERFACE.md](LLM_INTERFACE.md)를 참고하세요.

## LLM 응답 위젯 (0.1.4 추가 기능)

기존 state에 `llm_display` 메타데이터 인덱스와 capability가 추가됩니다.
새 `GET /api/display/llm/{id}`는 인증된 표시 세션으로 선택한 한 건의 사용자 입력·최종 출력만
조회합니다. 공유 위젯 미배치=403, 미전송/삭제/없는 항목=404. 기존 관리자 API 권한 변경 없음.
[정확한 필드·공유 범위](LLM_WIDGET.md)


## 0.1.5 assistant extension

`POST /api/llm/requests` accepts `mode: auto|chat|legacy`; omitted mode remains legacy for compatibility. UI explicitly submits auto by default. Retry accepts optional mode and inherits the assistant family for write idempotency.

`POST /api/assistant/{id}/confirm` is admin-only, with `X-Room-Request: 1` as existing same-origin mutations require. Body: `{"preview_sha256":"64-hex value from current preview"}`. Read the current request detail first. A 409 means stale/expired/cancelled/changed input: obtain a new preview, do not blindly replay. Effects and receipts commit atomically; duplicate confirmations do not repeat effects. Display-only and ingest credentials cannot call this endpoint.

Additional statuses: awaiting_confirmation and needs_clarification. Detail contains assistant (raw/normalized/route/proposal/validation/preview/calls/tool_result). Display projection still excludes these private fields and shows only original input, final output, status and public timestamps. See [ASSISTANT.md](ASSISTANT.md).
