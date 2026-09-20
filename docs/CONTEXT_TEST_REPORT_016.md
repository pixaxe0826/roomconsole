# Room Hub 0.1.6 검증 보고서

기준: 0.1.5 전체 소스 + 기존 STT 정확도 코드. 제작 환경: Linux x86_64 / Python3.13 / Chromium. 사용자 운영 서버·실제 Qwen·마이크에 접근하지 않았습니다. 시험 DB, 모델 응답, 스크린샷 데이터는 합성입니다.

## 실행 결과

| 검사 | 결과 |
|---|---:|
| 전체 pytest | **566 passed**, 실패 0 |
| 위 전체 수에 포함된 새 시간/의미/조회/권한/품질 테스트 | **86개** |
| 위 전체 수에 포함된 새 업데이트 도구 테스트 | **20개** |
| 새 문맥/데이터/품질 UI 결합 | **20개 통과** |
| 기존 비서 UI 결합 재검사 | **19개 통과** |
| 일반 UI | **22개 통과** |
| 날짜별 할 일 UI | **36개 통과** |
| 전체 할 일 UI | **48개 통과** |
| 전체 목록 모의 전송 오류 | **11개 통과** |
| 날짜별 완료 모의 전송 오류 | **15개 통과** |
| LLM 응답 위젯 | **42개 통과** |
| 페어링 QR 유지 | **29개 통과** |
| 미리보기 축소 방지 | **87개 통과** |
| 실제 Uvicorn + 루프백 HTTP | **19개 통과** |
| 실제 기존 소스 복사본 패치/DB·키·쿠키 보존/롤백 | **21개 통과** |

테스트 개수는 검증 강도의 보장이 아니며 서로 다른 검사 범위를 합쳐 실기기 검증으로 부르지 않습니다. 일반/기존 브라우저 UI는 독립 HTML, 모의 transport, 명시적 Python HTTP bridge를 사용하는 시나리오가 섞여 있습니다.

## 실제로 확인한 것

- 쉼표·구어체의 pending 조회가 실제 SQLite를 읽고 모델을 부르지 않음.
- ‘내일’→동등 ISO 날짜 수용; 다른 날짜·상태·대상·범위 거부.
- 한 날짜/주/월의 계산, 윤년·연말·시간대·자정·오래된 요청 검증.
- 작업 0개와 DB 실패 구분; 첫 50개와 실제 전체 개수/잘림 표시.
- 새 조회는 변경된 데이터를 읽고 과거 요청은 예전 스냅샷 유지.
- 변경은 계속 관리자 확인 후 적용. 확인 충돌·반복·중복 실행 방지 유지.
- 반복 모델 원출력은 관리자에만 보존, Client에는 품질 안내. 통계를 조작하지 않음.
- 단순 현재 시각은 실행 시각, 날짜 표현은 요청 기준 의미를 보존.
- 기능 목록·시간대·원문→해석 날짜·조회 근거가 실제 배포 관리자 UI에 표시.
- 기존 관리자 미리보기 폭 **791px**가 **65.442초** 동안 유지됨. 장시간 운용 검증은 아님.

## 브라우저 네트워크 제약과 실패를 숨기지 않음

`scripts/llm_regression.py`의 직접 `Page.goto(http://127.0.0.1:...)`는 실행 환경의 `ERR_BLOCKED_BY_ADMINISTRATOR`로 중단됐습니다. 해당 검사는 통과로 집계하지 않았습니다.

별도의 `scripts/context_browser.py`, `scripts/assistant_browser.py`는 배포 HTML/JS를 읽어 브라우저에 로드하고 **Python fetch bridge로 실제 임시 Uvicorn/SQLite에 연결**했습니다. WebSocket 무효화 알림은 시험 하네스가 주입합니다. 이는 실제 Safari·LAN·WebSocket 전 과정 시험이 아닙니다. Qwen 응답은 모의 백엔드입니다. 새 UI20개와 기존 비서19개는 이 방식으로 통과했습니다.

## 보존 범위

다음 파일의 원본과 새 버전 SHA256을 대조하여 **바이트 동일**을 확인했습니다.

- `app/speech.py`
- `app/stt_accuracy.py`
- `web/client.html`
- `web/client.js`
- `web/client.css`
- `deploy/termux/stt_accuracy_cli.py`
- `deploy/termux/stt-accuracy.sh`

- `web/speech.js`
- `web/speech.css`

기존 사용자 설정의 보존은 합성 `speech-config.json`(Base/ko/6T), `stt-accuracy.json`(careful=5), 키·HTTPS·모델 대체파일로 패치/롤백 검사했습니다. **실제 사용자의 설정을 읽거나 변경한 것은 아닙니다.** 실제 V35에서는 적용 도구가 설정을 읽어 같은 조건을 확인합니다. 조건이 다르면 변경하지 않고 중단합니다.

## 하지 않은 검증

실제 Qwen 생성·한국어 전반 정확도, V35 CPU/RAM/온도, 실제 STT Base6Tbeam5 성능, iPad Safari·물리 Wi-Fi, Termux/PRoot 설치·재부팅, Docker/TrueNAS, GitHub Actions는 이번 환경에서 시험하지 않았습니다. OS 시각의 NTP 정확도를 보장하지 않습니다. 신뢰된 OS 시간을 요청마다 읽고 시간대 변환만 합니다.

반복 가드는 응답 후 검사이며 streaming 조기 중단이 아닙니다. 메모·알람 실제 기능과 다중 턴 대화는 구현하지 않았습니다.

## 재현

```bash
python -m pytest -q
python scripts/build_previews.py
python scripts/context_browser.py
python scripts/assistant_browser.py
python scripts/browser_smoke.py
python scripts/todo_regression.py
python scripts/all_tasks_regression.py
python scripts/all_tasks_transport_regression.py
python scripts/todo_transport_regression.py
python scripts/pairing_regression.py
python scripts/llm_widget_regression.py
python scripts/preview_regression.py --seconds 65
python scripts/live_smoke.py
python scripts/context_patch_regression.py --package /path/to/patch --baseline /path/to/015-with-stt
python scripts/check_repo.py
```

개발 테스트 의존성은 requirements-dev.txt를 사용하며 운영 V35에 이를 설치할 필요는 없습니다. 원시 실행 기록은 artifacts/test-results/에 생성되며 운영 개인 데이터처럼 공개 저장소에서 제외합니다.

[업데이트](CONTEXT_UPDATE_016.md) · [기능](CONTEXT_016.md)
