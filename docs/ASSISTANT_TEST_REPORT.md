# Room Hub 0.1.5 — 검증 범위

검증일: 2026-09-20. 실제 사용자 DB/음성/인증 키를 사용하지 않았습니다.

## 현재 완료한 검사

- Python 전체: **419 passed** (기존 343 + 새 assistant 검사 76). 실제 SQLite/FastAPI, 정상 처리·규칙·소스 검증·날짜·읽기·관리자 확인·일괄 원자성·재요청/동시 확인 중복 방지·재시작 영수증·표시 권한을 검사했습니다.
- 새 관리자/표시 UI 결합: **19 passed**. Chromium에는 실제 배포 HTML/CSS/JS를 넣고, 실제 임시 Uvicorn HTTP/SQLite를 Python fetch 브리지로 연결했습니다. 브라우저 갱신 알림은 주입했습니다. 실제 일반 Qwen은 모의 응답입니다.
- 기존 직접 전송 UI: **31 passed**, 위와 같은 명시적 HTTP 브리지, legacy 모드를 명시하여 기존 경로를 검사했습니다.
- LLM 위젯: **42 passed**, 독립 예시 HTML. 표시용 API 결합: **24 passed**, 실제 API/DB+HTTP 브리지, 모의 LLM, 갱신 알림 주입.
- 기존 대시보드: **22 passed**; 날짜별 할 일 **36**, 전송 오류 모의 **15**, 전체 할 일 **48**, 전송 오류 모의 **11**.
- 미리보기 축소 회귀: **87 passed**, 약 65초간 폭 791px 유지와 창 크기/탭 이동 검사. 장기 실행 시험은 아닙니다.
- QR/링크 유지: **29 passed**, 실제 HTTP/WebSocket을 Python 브리지로 연결.
- 새 독립 미리보기: **3 passed**, 합성 UI 상태만 사용.
- 실제 Uvicorn 루프백 HTTP: **19 passed**. 물리 V35/LAN이나 Docker 배포 시험과는 다릅니다.

서버 WebSocket의 실제 invalidate 경로는 TestClient 기반 검사로 별도 확인했습니다. QR 회귀 하네스는 Python WebSocket 클라이언트에서 서버 알림을 실제 수신한 뒤 브라우저에 전달합니다.

## 한계와 실패를 숨기지 않음

제작 환경에서 Chromium의 직접 localhost 탐색은 `ERR_BLOCKED_BY_ADMINISTRATOR`로 차단되었습니다. 이 실패를 제품의 네트워크 성공으로 간주하지 않았습니다. 문서에 명시한 HTTP 브리지/주입 방식으로 UI를 검사했습니다. 구버전 버전 문자열·빈 화면 문구에 대한 회귀 기대값은 0.1.5 문구로 갱신했습니다.

**실제 Qwen 가중치/추론, V35 성능·발열, iPad Safari, Windows 설치, Tasker·절전·재부팅, Docker·TrueNAS는 이번 작업에서 실행하지 않았습니다.** 모의 LLM의 추천 문구를 실제 모델 정확도라고 주장하지 않습니다. 같은 모델을 쓰더라도 새로운 프롬프트와 스키마의 실제 품질은 장치에서 확인해야 합니다. `assistant-selftest.sh`가 실제 설치 모델에 합성 문장 두 개를 보내는 선택 시험을 제공합니다.

핵심 사례의 명확한 추가/목록 요청은 모델을 건너뛰고 실제 서버 데이터/확인 경로로 처리하므로, 이 경로의 테스트는 모의 모델 정답에 의존하지 않습니다. 복합/반복·애매한 시간·부정/인용 명령은 실행하지 않고 다시 명확하게 요청하도록 합니다.

## 재현

```bash
python -m pytest -q
python scripts/build_previews.py
python scripts/assistant_browser.py
HUB_BROWSER_BRIDGE=1 python scripts/llm_regression.py
python scripts/llm_widget_regression.py
python scripts/llm_widget_live.py
python scripts/preview_regression.py --seconds 65
python scripts/pairing_regression.py
python scripts/live_smoke.py
python scripts/check_repo.py
```

테스트에는 `requirements-dev.txt`와 Playwright Chromium이 필요합니다. 실제 운영 DB를 대상으로 실행하지 마세요. 결과는 artifacts/test-results 아래 생성되며, 기본 소스 ZIP에서는 원본 로그를 제외합니다.

패치 적용·개인 파일/모델/설정 보존·기존 로그인 쿠키 유지·실행·롤백 검사 **21개**를 통과했습니다. 최종 ZIP 재검사 결과는 전달본의 PACKAGE_VALIDATION.md에 함께 기록합니다. 프로그램 파일 롤백이 실제 확인 실행한 할 일 변경을 되돌리는 것은 아닙니다.
