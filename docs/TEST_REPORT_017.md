# 0.1.7 검증 기록

제작 환경: Linux x86_64 / Python 3.13 / Chromium. 실제 V35, iPad Safari, 음량/스피커, 절전/재부팅, Qwen/Whisper 모델에는 접근하지 않았습니다. 아래 DB·사용자 텍스트·스크린샷은 모두 합성입니다.

## 실행한 검사

- 메모/알람/업데이트 단위 테스트: **50 passed**, 실패 0. 권한, 비공개 공유, 버전 충돌, 중복 생성/삭제 후 재시도, 1회/반복/서머타임/재시작/놓친 회차/3회 미루기, 코드 패치 실패 복원과 롤백을 포함합니다.
- 전체 기존+신규 서버 테스트: **616 passed**, 실패 0, Python 프로세스 종료 코드 0. pytest 측정 38.16초(상위 실행기 40.32초).
- 메모/알람 UI: **22 passed**, 브라우저 uncaught error 0. 실제 임시 Uvicorn/SQLite를 Python HTTP 브리지로 연결하고 브라우저 갱신 알림을 주입했습니다. 메모 저장/공유/비공개/XSS 텍스트, 초안 보존, 알람 예약/울림 상태/미루기/확인, 새로고침 재허용, 오프라인/권한 해제, 수동 테스트 음의 재생 유지, 기본 4×2 위젯 맞춤, 834·390px overflow를 검사했습니다.
- 전체 0.1.6 소스 복사본 → 0.1.7 적용 → 재적용 → 0.1.6 롤백: **15개 통과**. 기존 할 일/배치/고정 메모/키/설정/인증서·모델 대체파일 유지, 롤백 후 새 메모 테이블 보존을 확인했습니다.
- 실제 Uvicorn 루프백 HTTP: **19개 통과**, 종료 코드 0.
- 기존 UI 회귀: 일반 대시보드 **22**, 날짜별 할 일 **36**, 전체 할 일 **48**, LLM 위젯 **42**, 비서 결합 **19**, 문맥 결합 **20**, 미리보기 축소 방지 **87**개 통과. 비서/문맥 결합은 명시적 HTTP 브리지이며 나머지는 독립 예시/모의 전송 기반 검사를 포함합니다. 미리보기 65초 검사이지 장기 운용 시험은 아닙니다.
- 저장소 코드/문서/민감 파일 검사 및 미리보기 생성: 종료 코드 0.

## 환경 제약과 확인하지 않은 범위

직접 Chromium `Page.goto(http://127.0.0.1:...)`는 제작 환경 정책의 `ERR_BLOCKED_BY_ADMINISTRATOR`로 실패했습니다. 이를 성공으로 집계하지 않았습니다. UI 결합은 `HUB_BROWSER_BRIDGE=1`의 명시적 테스트 전송 계층으로 수행했습니다. CI에서는 기본 직접 HTTP 경로를 사용합니다.

WebAudio는 클릭 뒤 AudioContext가 running인지까지만 확인했습니다. **소리가 실제 iPad 스피커에서 들렸는지 검증하지 않았습니다.** 화면 잠금 알림, 네이티브 푸시, 음성으로 메모/알람 실행도 구현·시험 범위가 아닙니다.

기존 Windows CI의 긴 실행/취소 원인을 이번 기능 구현이 해결했다고 주장하지 않습니다. 최종 PR의 GitHub Actions 상태는 별도로 확인합니다. 물리 V35의 정확한 처리 지연·CPU·발열·장시간 운용도 별도 시험 사항입니다.

## 재현

```bash
python -m pytest -q
python -m pytest tests/test_life.py tests/test_life_update.py -q
python scripts/build_previews.py
python scripts/life_browser.py
# localhost 브라우저 탐색이 정책으로 차단된 환경의 대체 UI 결합 시험:
HUB_BROWSER_BRIDGE=1 python scripts/life_browser.py
python scripts/check_repo.py
```

운영 데이터를 테스트 대상으로 지정하지 마세요. 테스트 출력은 artifacts/test-results, 스크린샷은 artifacts/screenshots에 저장되며 공개 소스 패키지에서는 제외합니다.
