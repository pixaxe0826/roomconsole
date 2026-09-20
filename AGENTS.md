# Room Hub 작업 지침

작업 전 README.md, docs/ARCHITECTURE.md, SECURITY.md, docs/TESTING.md를 읽습니다.

- 기준 기능 버전은 VERSION의 0.1.4입니다. 런타임은 Python/FastAPI + SQLite 1 worker, UI는 순수 웹입니다.
- app/는 서버, web/는 관리자·클라이언트, widgets/는 신뢰 코드 플러그인입니다.
- data/, .env, 실제 토큰·DB·음원·백업을 읽어 문서나 테스트 픽스처로 복사하지 않습니다.
- 클라이언트는 완료 전용 API만 쓸 수 있습니다. 작업 편집 권한을 무심코 확대하지 않습니다.
- 날짜 줄은 일~토, 주 이동은 ±7일, 반복 회차는 독립 상태를 유지합니다.
- 관리자 미리보기의 부모 크기를 ResizeObserver 안에서 반복 수정하지 않습니다.
- 기본 UI를 변경하면 scripts/build_previews.py로 독립 데모를 다시 생성합니다.
- 테스트 결과는 artifacts/로 저장합니다. 기본 검사: pytest, live_smoke, browser_smoke, todo_regression, todo_transport_regression, preview_regression.
- 미구현 후보는 docs/ROADMAP.md에 둡니다. 제안과 실행 기능을 구분합니다.
- GitHub 원격 생성·push·배포·라이선스 선택은 소유자의 별도 지시 없이 수행하지 않습니다.

- 전체 목록은 `Room.chronologicalTasks`를 사용합니다. 완료를 정렬 키로 넣거나 과거/완료 항목을 기본 숨김 처리하지 않습니다.
- 모든 항목이 접근 가능해야 합니다. 최초 렌더는 점진 표시하되 300개 같은 숨은 영구 잘림을 만들지 않습니다.
- 전체 목록 선택/스크롤은 기기 세션 UI 상태입니다. 다른 날짜를 선택했다고 목록을 필터링하지 않습니다.
- `scripts/all_tasks_regression.py`와 `scripts/all_tasks_transport_regression.py`를 추가 실행합니다.

- 0.1.3: 표시 기기의 마이크 녹음 업로드·자기 작업 조회/취소/재시도만 추가 허용합니다. 전사로 작업을 자동 실행하지 마세요.
- 음성 worker는 한 번에1개, 클라이언트/모델 경로 인자를 셸로 전달하지 않습니다.
- HTTPS8443은 기존8080 nginx와 별도 서비스/설정/PID입니다. 루트CA개인키/모델/runtime/data는 배포 금지입니다.
- 테스트에서 모의 ASR 출력과 실제 인식 정확도/속도를 구분하세요. 설치 시 실제 샘플 시험을 통과해야 음성을 켭니다.

## 0.1.4 LLM boundary
- app/llm.py and manager-llm.js/css implement manual, manager-only requests. Never install a model or execute tool output without separate approval.
- Store exact user/system/request snapshots; preserve historical output. Mark missing usage/timing as unknown, never text-length estimates.
- No retries at startup, after timeout, or on cancel. Requests use local loopback only and never inherit browser/API keys.
- Preserve existing speech-config.json, speech engine code, worker settings and 6T.
- Run tests/test_llm.py, scripts/llm_regression.py, existing UI and pairing/preview regressions. Mark mock LLM vs actual inference clearly.

## 0.1.4-llm-widget1
- app/llm_display.py는 opt-in 공유 레이아웃 존재를 확인한 read-only allowlist입니다.
- dispatch_attempted=1이고 started_at이 있는 항목만 표시. 미전송 기록을 실제 답변처럼 만들지 마세요.
- 원본/System/endpoint/키/reasoning/tool_calls를 표시용 DTO에 추가하지 않습니다.
- tests/test_llm_display.py, scripts/llm_widget_regression.py, scripts/llm_widget_live.py 실행.
- 실제 모델 대신 fixture를 사용한 검증은 분명하게 표시합니다.

## 0.1.6 운영 계약
- Whisper Base/ko/6T/careful(beam5)와 음성 코드·데이터는 별도 동의 없이 변경하지 않는다.
- app/clock_service.py의 기준 시각과 원문 근거를 유지한다. 모델이 날짜/완료조건을 새로 결정하지 않는다.
- UI와 assistant는 Store.query_tasks를 공유한다. DB 오류를 빈 목록으로 바꾸지 않는다.
- app/capabilities.py만 승인된 기능 등록부다. 위젯 코드나 모델 출력은 실행 권한이 아니다.
- 모든 assistant write는 관리자 confirmation+snapshot+receipt를 거친다.
- 원문/요청/모델원출력은 manager만, Client에는 검증된 최종결과만 보낸다.
- 일반대화의 품질검사는 post-response다. streaming/실기기검증으로 과장하지 않는다.

## 0.1.7 메모·알람 계약
- app/life.py는 additive SQLite 서비스다. 임의 모델 도구 권한을 추가하지 않는다.
- note는 기존 고정 문구 보존, 새 메모는 shared + 위젯 배치 opt-in만 표시한다.
- alarms 예약 CRUD는 관리자. 유효 표시 기기는 기존 회차 ack/snooze만 허용한다.
- ringing은 실제 소리 재생 증거가 아니다. 잠금/백그라운드/오프라인 보장을 하지 않는다.
- tests/test_life.py, tests/test_life_update.py, scripts/life_browser.py와 기존 테스트를 실행한다.
