# M3.4-0 — 런타임 안정화 검증 기록

## 기준과 범위

기준 소스는 `9ad752f0a21a8e4fad47f5256d10fcf622101c10`입니다. 작업 시작 시 로컬
Git tree가 원격의 `bf4a9913cee0ae133a3b5a56cce0a9e881142552`와 일치함을 확인했습니다.
제품 버전은 0.1.7을 유지하며 별도 runtime patch ID는 `room-hub-m3.4-0`입니다.

아래는 개발 컨테이너의 **실제 실행 결과**입니다. Linux / Python 3.13.5 / pytest 9.0.2,
FastAPI 0.128.2 / httpx 0.28.1 / Pydantic 2.13.4 환경입니다.
모든 DB·음성·HTTP 결과는 합성 테스트용이며 운영 자료나 실제 모델을 사용하지 않았습니다.

## 실행 결과

| 검사 | 결과 |
|---|---|
| 전체 pytest | **1675 passed, 4 skipped** / 195.04초 |
| M3.4-0 신규 테스트 세 파일 | **63 passed, 1 skipped** / 2.23초 (전체 검사에 포함) |
| 신규 런타임 Chromium UI | **12/12 통과**, JavaScript 오류 없음 |
| 기존 미리보기 안정성 | **87/87 통과**, 65.303초, 첫/마지막 너비 791 동일 |
| 기존 live/browser 회귀 17개 + 신규 runtime_browser | 모두 종료 코드 0 |
| 저장소·외부 벤치마크 정책 검사 | 통과 |
| 선언형 메모 문법 검사 | 합성 양성 320 / 부정 320 통과 |
| 독립 미리보기 재생성 | 기존 previews와 diff 없음 |

전체 pytest의 네 skip은 모두 이 Linux 환경에 없는 Windows 실행 도구와 관련됩니다.
신규 PowerShell 진단 스크립트 문법 검사 한 건, 기존 PowerShell argv/orchestration 한 건,
기존 cmd.exe/PowerShell updater 두 건입니다. 실패를 skip으로 변경한 것은 아닙니다.
Windows CI에는 신규 테스트 세 파일을 추가했습니다. Linux CI에는 Python 3.14도 추가했습니다.
CI의 실제 성공 여부는 해당 PR의 Checks에서 별도로 확인해야 합니다.

실행한 회귀 스크립트:

```text
live_smoke.py                   browser_smoke.py
todo_regression.py              all_tasks_regression.py
all_tasks_transport_regression.py  todo_transport_regression.py
llm_regression.py               llm_widget_regression.py
llm_widget_live.py              assistant_browser.py
dialog_browser.py               memo_catalog_browser.py
context_browser.py              life_browser.py
fast_reads_browser.py           widget_bridge_browser.py
timers_browser.py               runtime_browser.py
preview_regression.py --seconds 65
```

로컬 브라우저 실행은 환경 제약으로 `HUB_BROWSER_BRIDGE=1`을 명시했습니다.
신규 UI 검사는 about:blank/fetch-binding + 실제 Chromium DOM/렌더링 검사이며,
HTTP/CSP 강제 적용이나 TLS의 증명이 아닙니다. 기본 스크립트/CI 경로는 intercepted HTTPS/CSP입니다.
인증·CSRF·소유권과 실제 애플리케이션 API 경계는 별도로 TestClient 합성 테스트에서 검증했습니다.

## 핵심 회귀 방지 항목

- SQLite: commit/rollback/지연 FK commit 실패 이후 close, 초기 PRAGMA 실패 close,
  GC를 끄고 연결 객체 300개를 보존한 상태에서도 DB FD 증가 없음, 관리자 연결 생성/해제 반복,
  backup API 원본과 목적 DB 모두 닫힘.
- 벤치마크: 운영과 동일한 정확한 factory만 허용. 임의 factory/subclass/위치 인자 우회와
  임시 영역 밖 DB 접근은 계속 거부. 기존 benchmark 격리·채점 테스트도 전체 pytest에 포함.
- 원격 STT: loopback-only, redirect/proxy 상속 없음, multipart 언어/beam/hint 보존,
  오류 본문 비노출, 응답 크기·형식·Unicode 검사, 전체 timeout과 취소 시 HTTP/임시 파일 정리,
  재시도와 로컬 fallback 없음.
- AI 상태: 관리자 권한, health/models 이외 접속 없음, 일반 상태 조회에서 outbound probe 없음,
  30초 캐시 만료와 설정 변경 중 probe 무효화, 모델 ID 불일치, 미확인 장치 정보 보존.
- 장애 경계: 모의 PC 연결 실패 중 core/state/작업/메모/장치 연결 관리와 타이머·알람 tick 유지.
  실패한 STT/LLM은 연결 복귀만으로 다시 실행되지 않고 신규 요청으로만 재개.
- 이관: 기본 dry-run 무변경, 명시적 apply의 원본 백업, 이전 설정/accuracy 정책 보존,
  잘못된 원격 endpoint는 쓰기 전에 거부.

## 변경하지 않은 경계

Parser, semantic/dialog/entity 해석, LLM prompt·schema·모델 기본값, capability/Adapter/확인·receipt,
벤치마크 scoring/projection/평가 데이터는 변경하지 않았습니다. Store 소스 hash는 달라지므로
과거 분석을 새 소스로 다시 작성하지 않습니다. 운영 시스템에 배포/재시작도 하지 않았습니다.

## 아직 별도 확인이 필요한 항목

실제 Windows/Task Scheduler/CUDA, Odyssey 실기기, iPad Safari/HTTPS, 실제 음성 정확도·1.2초 전사,
Qwen 토큰 속도 및 250-case 모델 벤치마크는 이번 개발 환경에서 실행하지 않았습니다.
모의 응답의 성공을 해당 결과로 대체하지 않습니다. 병합 후 [적용·롤백 안내](RUNTIME_M340.md)의
PC online/offline/recovery 및 FD 관찰 순서로 운영자가 검증합니다.

로컬 JUnit과 UI 검사 결과는 `artifacts/test-results/`, UI 캡처는 `artifacts/screenshots/`에
생성되며 소스에는 포함하지 않습니다. 이 문서는 원본 테스트 실행을 요약한 기록입니다.
