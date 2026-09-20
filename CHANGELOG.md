# Changelog

## 0.1.7 — 2026-09-20

- 저장형 메모: 관리자 CRUD/검색/고정, 비공개 기본값과 선택 공유. 기존 note 고정 문구 보존.
- 서버 알람: 한 번/요일 반복, 재시작·지연·DST 정책, 원자적 발생 회차와 중복 방지.
- 표시 기기의 기존 회차 확인/5분 미루기만 추가 허용. 예약/메모 편집은 관리자 전용.
- 명시적 소리 허용, 최대 2분 울림 대기, 오프라인/숨김 억제. 네이티브 알람/푸시 없음.
- 소스 검사/백업/롤백 업데이터, 신규 검증, 미리보기. STT/LLM/서비스 설정 변경 없음.


## 0.1.6 — 2026-09-20

- 공통 서버 시계/원문 날짜의 의미 검증. ISO 동등값 수용, 잘못된 날짜/조건 거부.
- 쉼표·자연어 날짜별 조회, 남은/완료 필터, 주/월 범위. SQLite 조회 근거와 잘림 표시.
- 공통 기능 등록부. 메모/알람은 미연결로 구분하며 권한 자동 부여 없음.
- 일반 대화 128토큰·샘플링 프로필, 반복/에코/한도종료 품질 거부. 원출력은 관리자 보존.
- 신규 근거/설정 UI와 테스트. 모델/Whisper Base6Tbeam5/마이크/서비스/데이터는 변경 없음.


## 0.1.5 — Assistant routing and confirmed local actions

- Auto/chat/legacy modes; raw transcript retention and cautious punctuation normalization.
- Deterministic SQLite/weather/time reads; constrained Qwen proposal fallback and separate general-chat prompt.
- Admin-only 10-minute write previews with frozen IDs/versions; transactional receipts and retry-family idempotency.
- Manager execution trace and iPad read-only final result projection; existing histories unchanged.
- Runtime/model/6-thread settings preserved; compatibility checks accept 0.1.5.
- See docs/ASSISTANT_TEST_REPORT.md for tests and unverified on-device/model scope.

# 변경 이력

현재 실행 버전은 **0.1.4**입니다. 아래는 기능 단위의 통합 이력이며, 현재 GitHub 소스에는 이전 버전의 기능이 모두 합쳐져 있습니다.

## 0.1.4 — LLM 인터페이스 및 표시 위젯

- 관리자에 LLM 요청 노트북 추가: 음성 전사문을 요청 단위로 저장하고 실제 API 입력/출력, 응답 시간, 입력/출력 토큰, 생성 속도 통계를 표시할 준비.
- LLM 연결은 기본 비활성. Qwen/llama.cpp 모델을 자동 설치하거나 자동 실행하지 않음.
- 같은 음성 입력을 여러 번 보낼 때 각 요청을 독립 기록으로 보존.
- 4×2 `llm-response` 위젯 추가: 실제 전송된 요청/최종 응답 한 쌍씩 이전/다음/최신 탐색, 확대 보기.
- 표시 기기에는 사용자 입력과 최종 응답만 최소 투영. System 프롬프트·키·내부 상세는 관리자 전용.
- 기존 Whisper, HTTPS, 할 일, 페어링, DB 세션을 유지.

## 0.1.3 — iPad 음성 입력 및 V35 로컬 전사

- iPad의 명시적 버튼 녹음, 전송 전 재생 확인, 업로드·취소·재시도 UI.
- V35에서 FFmpeg 변환 + whisper.cpp 로컬 전사 큐.
- Termux용 PRoot Debian 설치, runit 서비스, 전용 HTTPS 8443 구성 도구.
- 다국어 tiny/base/small 모델 관리 도구와 1~8 CPU thread 설정 지원.
- 전사 모니터링/비교용 CPU lab 도구 포함.
- 전사 결과를 자동 명령으로 실행하지 않고 관리자 음성 수신함에서 검토.

## 0.1.2 — 전체 할 일 목록

- `all-todos` 위젯 추가: 과거·현재·미래·완료 포함 모든 등록 회차를 날짜/시간순으로 표시.
- 관리자 할 일 화면 기본 범위를 모든 날짜로 변경하고 완료 필터·점진적 목록 표시 추가.
- 기존 날짜별 `todos` 위젯은 유지.

## 0.1.1 — 주간 할 일과 iPad 완료 체크

- 날짜별 할 일의 날짜 줄을 일요일~토요일로 고정.
- 이전/다음 주, 오늘 복귀 추가.
- iPad 표시 세션에 완료/완료 취소만 쓰기 권한 허용.
- 관리자 미리보기 반복 축소 문제와 QR 연결 링크 갱신 문제 수정.

## 0.1.0 — 초기 버전

- iPad 클라이언트와 웹 관리자.
- 시계·날씨·할 일·달력·메모 위젯.
- SQLite, 반복 일정, 위젯 격자 배치, 표시 기기 페어링, 원격 화면 제어.
- 텍스트/음성 파일 수신함과 기본 백업·배포 도구.
