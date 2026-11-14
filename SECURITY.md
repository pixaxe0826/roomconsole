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


## Explicit dialogue continuations (M3.3-A)

Additional-information replies are manager-only and bound to an exact parent,
server-derived session owner, state digest and fixed 180-second lifetime. Identical
Bearer credentials represent one manager; only distinct cookie sessions provide
per-session separation. Unbound internal/speech requests need explicit first claim.
No new general display permissions or voice approval are introduced. Source proof
replay checks immutable per-turn text/hash/timestamps/parent links before final
grounding and confirmation. No global pending request or concatenated synthetic
utterance is used. Existing reservation/uncertain receipt and target version guards
remain in force. See [dialogue contract](docs/CLASSIC_NLU_M33.md).

## M3.4-0 원격 전사 경계

원격 전사는 명시적으로 선택한 숫자 loopback HTTP `/inference`에만 WAV를 보냅니다.
LAN/DNS/redirect/ambient proxy/자동 fallback/모델 load는 허용하지 않습니다. 기존 SSH 터널의
끝점 PC도 음원과 기존 어휘 힌트(작업 제목 포함 가능)를 받는 신뢰 경계입니다. 선택적 speech 키는
admin/ingest/LLM 키와 별개입니다. 관리자 전용 AI probe는 health/models만 조회하며 모델·GPU 인증이 아닙니다.
자세한 제한과 취소/시간초과 계약은 [M3.4-0](docs/RUNTIME_M340.md)을 참고하세요.

## OpenAI Personal Agent 보안·개인정보·권한 경계

- **기본 비활성화 (Opt-in)**: 에이전트·클라우드·음성 전송의 기본값은 모두 `false`입니다. OpenAI STT는 세 저장 정책이 모두 활성화되고 키·예산·로컬 FFmpeg 검증 조건을 충족해야 합니다. 환경변수는 정책 최초 생성의 초기값이며, 이후 저장된 DB 정책은 관리자 UI/API로 변경합니다. 환경변수만 `false`로 바꾸거나 재시작했다고 기존 정책이 비활성화되었다고 가정하지 마세요. `local_only` 요청은 OpenAI를 호출하지 않습니다.
- **키 분리**: 기존 `HUB_LLM_API_KEY`는 OpenAI 호출에 재사용하지 않으며 `OPENAI_API_KEY` 또는 `HUB_OPENAI_API_KEY`만 사용합니다.
- **비공개 메모 및 클라우드 전송 격리**: 서버가 수집하는 클라우드 카탈로그에서는 비공개 메모(`shared=false`)의 제목·ID·본문·개수를 제외합니다. STT 힌트에는 메모를 포함하지 않습니다. 디스플레이 공유(`shared=true`) 역시 외부 전송 동의가 아니며 별도 `allow_shared_notes_in_cloud` 정책이 필요합니다. 사용자가 요청 원문에 직접 입력한 민감정보까지 자동 제거한다는 뜻은 아닙니다. 민감한 요청은 `local_only`를 사용하세요.
- **프롬프트 인젝션 및 카탈로그 밖 대상 방어**: 할 일·알람·메모 문자열은 신뢰할 수 없는 데이터로 취급하여 인젝션 패턴을 제거하고, 모델이 반환한 `target_id`는 요청 카탈로그 소속 여부·원문 근거·유일성·대상 버전을 모두 검증합니다.
- **관리자와 표시 기기 분리**: `/api/agent/command`, `/briefing`, `/audio`, 실행 전체 기록, 승인·취소·예산 정책은 관리자 전용입니다. paired iPad는 기존 `/api/speech/jobs`로 음성을 제출합니다. 서버는 저장된 job owner와 연결 상태로 실행 주체를 결정하며, 표시 기기는 자신의 안전한 실행 결과만 읽습니다. POST 본문의 임의 device ID나 내부 콜백의 관리자 기본값으로 승격하지 않습니다.
- **표시 결과의 재검증**: 공용 LLM 위젯은 배치 opt-in이 필요합니다. 메모를 포함한 과거 결과와 그 결과를 다시 읽은 파생 응답은 현재 공개 여부·버전·출처를 재검사합니다. 확인할 수 없는 내용, 비공개로 전환된 메모, 메모 변경 결과는 원문 대신 마스킹된 표시용 결과를 제공합니다. 관리자 원본 기록은 별도 권한으로 유지됩니다.
- **변경 승인**: Personal Agent의 조회와 1–600초 타이머 제어 외 데이터 변경은 관리자 미리보기 승인이 필요합니다. 알람 미루기/확인은 미리보기 시 확정한 기존 이벤트 ID·버전에만 적용되며, 미래 알람에 가짜 이벤트를 만들거나 다음 발생 회차를 대신 조작하지 않습니다.
- **외부 제어 제외**: 에이전트의 `weather.refresh`, `layout.focus`, `layout.select_date`는 미지원으로 거부합니다. SQLite 배치의 원자성과 외부 HTTP/화면 명령 전달을 같은 보장으로 취급하지 않습니다. 기존 수동 관리자 UI와 날씨 스케줄러는 유지합니다.
- **중복 과금·불명 비용**: 음성 입력과 유료 호출은 소유자/요청 ID 및 입력 지문으로 선예약합니다. 중단된 요청을 동일 ID로 자동 재전송하지 않습니다. 타임아웃·취소·누락된 usage는 확인 없이 환불하거나 `$0`으로 정산하지 않으며 미정 예약액이 한도에 남습니다. 브라우저 재접속은 GET 상태 조회이며 새 모델 호출이 아닙니다.

앱 원장은 공급자 청구서와 같지 않습니다. 테스트 정의·합성 회귀 통과는 보안 감사, 실제 OpenAI 응답, 물리 iPad, 운영 배포 성공을 뜻하지 않습니다. 상세 계약과 미검증 범위는 [OpenAI Personal Agent](docs/OPENAI_PERSONAL_AGENT.md)를 참고하세요.

## Odyssey 전용 AI 실행 경계

`HUB_AI_PROFILE=odyssey_openai`는 서버 시작 시 선택하는 고정 프로필입니다. API 키나 클라우드 전송 동의와는 별개이며 기존 DB 정책을 자동으로 덮어쓰지 않습니다. 이 프로필은 기존 로컬/PC Whisper·Qwen 호출과 SSH loopback 진단을 비활성화합니다. 키 누락, 동의 철회, 예산 소진, OpenAI 오류에서도 기존 PC로 자동 우회하지 않습니다. 공식 `https://api.openai.com/v1` 이외의 대체 모델 끝점은 차단합니다.

이전 PC 전사용 작업을 클라우드로 재해석하거나 자동 재전송하지 않습니다. 기존 설정·원음·처리 기록은 유지하며 새 실행 경로만 제한합니다. 상태/진단 조회는 로컬 준비 조건만 확인하고 유료 요청을 생성하지 않습니다. 외부 서비스를 사용할 수 없으면 음성 전사는 사용할 수 없지만, 기존 위젯과 허용된 결정론적 텍스트 처리는 Odyssey에서 계속 동작할 수 있습니다. [배포·적용과 검증](docs/ODYSSEY_OPENAI.md).
