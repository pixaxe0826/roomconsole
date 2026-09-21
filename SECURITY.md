# 보안과 개인정보

## 사용 경계

Room Hub 0.1.2은 개인의 신뢰할 수 있는 LAN용 초기 구현입니다. 인터넷 공개용 인증 게이트웨이, 다중 사용자 SaaS 또는 검증된 보안 제품으로 취급하지 마세요. 기본 HTTP는 암호화하지 않습니다. 공유기 포트포워딩으로 직접 공개하지 않습니다.

HTTPS 역방향 프록시를 사용할 때에는 동일 출처 API/WebSocket, 접근 통제, 허용 호스트, 인증과 네트워크 정책을 검토하고 그 환경에서만 `HUB_SECURE_COOKIE=true`를 설정합니다. IP·Host 문자열 형식 검사는 DNS rebinding이나 모든 외부 접근을 차단하는 허용 목록과 다릅니다.

## 권한

- 관리자 키: 전체 편집·기기 등록·백업·연동 키 확인.
- 표시 세션: 상태 조회, 자신의 화면 동작, 해당 작업의 완료 전용 변경.
- 입력 키: 음성/텍스트 수신 전용. 작업 생성·관리자 API 사용 권한이 아닙니다.
- 관리자 세션 12시간, 표시 세션 30일, 연결 코드 10분·1회. 무기한 자동 갱신은 없습니다.

입력의 버전 검사와 Origin·요청 헤더 검사, 쿠키 속성, 파일 크기 제한 등이 있지만, 이 사실이 보안 감사를 통과했음을 뜻하지 않습니다.

## 저장과 공개 금지

`data/`에는 DB, 실제 키, 세션·기기 연결 정보, 음성 파일과 개인 할 일이 들어갑니다. `.env`, DB 백업, 서버 콘솔 로그, 일회용 연결 링크·QR 역시 비공개로 다룹니다. 운영 데이터는 GitHub와 Releases에 올리지 않습니다.

`.gitignore`와 `scripts/check_repo.py`는 실수를 줄이는 보조 장치일 뿐 완전한 비밀정보 탐지기가 아닙니다. 이미 커밋한 키는 ignore 추가만으로 사라지지 않습니다. 노출이 의심되면 즉시 키·세션을 폐기/교체하고 관련 접근을 확인하세요.

로그인이 아닌 데이터 자체도 민감할 수 있습니다. 문제 보고에 실제 일정·메일 주소·위치·음성을 포함하지 마세요. 테스트/데모는 합성 데이터만 사용합니다.

## 플러그인과 파일 입력

위젯은 동일 출처 JavaScript이며 샌드박스가 아닙니다. `widgets/`는 정적 제공되므로 서버 비밀 키를 넣을 수 없습니다. 관리자 미리보기에서도 실행될 수 있으므로 신뢰할 수 있는 코드만 설치하세요.

파일 업로드는 10MB 제한과 MIME/UTF-8 검사를 합니다. 음원 내용의 무해성이나 실제 오디오 여부를 완전 검증하지 않으며, 원문을 임의 코드로 실행하지 않습니다. 0.1.3 이후에는 검증된 음성만 제한된 FFmpeg/Whisper 파이프라인에서 처리합니다. 입력 원문을 명령으로 바로 실행하지 마세요. 파일은 수신함에서 삭제하기 전까지 보관되고 자동 보관 기간 정책은 없습니다.

## 취약점 제보

저장소의 Security 탭에서 비공개 취약점 보고가 활성화되어 있다면 그 기능을 사용하세요. 활성화되어 있지 않다면 소유자가 정한 비공개 채널을 먼저 확인하세요. 이 패키지에는 아직 실제 담당자 주소가 지정되어 있지 않습니다. 공개 이슈에 재현용 실제 키나 운영 데이터를 남기지 마세요.

지원 범위는 현재 개발 중인 0.1.x입니다. 별도의 보안 응답 SLA나 과거 버전 유지보수 약속은 없습니다.

## 0.1.3 음성·HTTPS

표시 기기는 자신의 음성 작업만 읽고 취소/재시도할 수 있습니다. 다른 기기 전사문·원음은 관리자 권한이 필요합니다.
마이크는 명시적인 버튼과 같은 출처에서만 사용합니다. 원본 녹음은 관리자 수신함 삭제 전까지 저장됩니다.
전사문은 외부/불확실 입력이며 자동 도구 실행 명령이 아닙니다. HTML 이스케이프하여 출력합니다.
인증서 개인 키와 CA 키는 운영 data/에만 생성합니다. CA를 임의로 공개하거나 공용 기기에 설치하지 마세요.
CA는 이 사이트 밖의 인증서도 서명할 수 있으므로 키 노출 시 즉시 신뢰 해제·새 CA 재배포가 필요합니다.
HTTPS 인증서 경고를 우회하거나 Play Protect/브라우저 보안을 끄는 설치 절차는 제공하지 않습니다.
runtime/의 실행파일·모델, data/의 원음·전사·인증서는 저장소 배포 금지입니다.
음성 전처리기는 외부 파일을 파싱하므로 FFmpeg/OS 보안 업데이트도 유지하세요.

## 0.1.4 LLM request notebook
Full LLM APIs are manager-only; the display cookie and ingest key cannot access raw histories/configuration. The opt-in display projection is described below. Only loopback numeric addresses at a dedicated non-reserved port are accepted. No redirects, no environment proxy, no shell or tool execution. Responses are escaped text, bounded to 512KiB. Request/response snapshots and exports are private data, including copies of transcripts whose original voice may later be deleted. API authentication key is read only from HUB_LLM_API_KEY, not the UI or database. Cancellation only abandons this app's HTTP request; backend computation may continue. No model is installed by this source update.

## 0.1.4-llm-widget1: 클라이언트의 제한된 LLM 조회

기존 `/api/llm/*`는 관리자 전용입니다. `/api/display/llm/{id}`는 유효한 표시/관리자
세션과 공유 배치의 `llm-response` 존재를 모두 요구합니다. 모든 paired display가 전송된
사용자 입력·최종 출력만 볼 수 있는 opt-in 공유이며 기기별로 LLM 이력을 나누지 않습니다.
System/원시 JSON/endpoint/reasoning/tool_calls/키/성능 통계는 서버의 allowlist 투영에서 제외합니다.
미전송 입력도 제외합니다. GET으로 모델을 실행하거나 데이터를 수정하지 않습니다.
[상세 범위와 오프라인 한계](docs/LLM_WIDGET.md)를 참고하세요.


## 0.1.5 confirmed assistant actions

Every assistant write requires an authenticated manager confirmation, including create. Read-only display credentials gain no new mutation ability. Schema validity is not authority: source grounding, explicit scope, exact server-resolved targets, immutable preview digest/expiry/timezone and optimistic versions are all checked. Actions are allowlisted SQLite operations, not model-provided SQL, shell or arbitrary URLs.

Task mutations and execution receipts are committed in a single SQLite transaction; receipt key follows a retry family. Deleting the visible notebook does not delete the execution ledger, which prevents accidental replay but retains a brief result containing task IDs/titles. Treat DB backups as private. Rollback of program files does not undo already confirmed mutations.

The new mode is automatic routing, not automatic writes or automatic audio sending. Imported legacy records are never reinterpreted or silently reexecuted. Generic chat cannot query data or change tasks. Stored task content is never treated as a system instruction. See [assistant scope](docs/ASSISTANT.md).

## 0.1.7 확장
메모/알람 예약 CRUD는 관리자 전용입니다. 메모는 기본 비공개이고 `shared` 및 note 위젯 배치로만 공개됩니다. 기존 고정 메모 문구는 이미 공개된 위젯 설정이므로 별도로 유지합니다. 알람 위젯 배치는 예약/활성 회차를 연결 화면 모두에 공유하는 opt-in입니다. 표시 기기는 버전·요청 ID가 검증된 기존 발생 회차의 확인 또는 5분 미루기만 가능합니다. 실제 소리 재생·시스템 알람·Web Push를 보장하지 않습니다. 새 테이블/내보내기에도 개인 정보가 있으므로 백업/DB를 공개하지 마세요.

## Timer-only immediate control (0.1.7 timer patch)

`timer.start/stop` are an explicit exception to confirmation-only mutations: bounded
1–600-second local countdowns may execute immediately after authenticated server
validation. The existing note/task/calendar/alarm confirmation checks are unchanged.
Paired displays may call only `/api/timers` start/stop when a timer widget is installed;
this does not grant general Protocol or other CRUD access. A placed widget ID owns
one running timer; scoped controls cannot target another card. The same ID remains
shared across displays in this single-user workspace. Duration/target grounding, actor-scoped durable idempotency,
CSRF, revocation and resource caps remain enforced. Foreground audio is opt-in and
is not an OS alarm or a guarantee of background delivery. See [TIMERS](docs/TIMERS.md).
