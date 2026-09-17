# 테스트와 소스 검증

[문서 홈](../README.md) · [이번 패키지의 실제 결과](TEST_REPORT.md)

## 환경

Python 3.11 이상으로 만든 별도 가상환경에서:

```bash
python -m pip install -r requirements-dev.txt
python scripts/build_previews.py
```

운영 서버를 테스트 대상으로 쓰지 않습니다. 테스트는 임시 DB와 합성 입력을 사용합니다. `app.main`의 모듈 import는 기본 앱의 데이터 폴더를 생성할 수 있으므로, 빈 소스 작업 복사본에서 실행하거나 `HUB_DATA_DIR`을 테스트 전용 경로로 설정하세요. CI는 `artifacts/runtime-data`를 사용하며 이 폴더는 소스·검증 artifact에 포함하지 않습니다.

Windows에서는 UTF-8 텍스트 처리를 위해 필요하면 `$env:PYTHONUTF8='1'`을 지정합니다. CI에는 이 환경 변수가 설정되어 있습니다.

## 검사별 명령

| 검사 | 명령 | 무엇을 확인하는가 |
|---|---|---|
| 저장소 | `python scripts/check_repo.py` | 필수 파일, 소스 경로·대표 비밀정보 패턴, UTF-8/JSON/Python 문법, 버전, 상대 문서 링크 |
| 스테이징 | `python scripts/check_repo.py --staged` | Git index의 실제 새 내용에 운영 파일·키가 포함되는지 보조 검사 |
| 서버·패키징 | `python -m pytest -q` | 기존 서버·완료 권한과 추가 소스 ZIP 정책 |
| 실제 HTTP | `python scripts/live_smoke.py` | 별도 Uvicorn 프로세스를 루프백 HTTP로 호출 |
| UI | `python scripts/browser_smoke.py` | 클라이언트·관리자 데모 UI |
| 할 일 UI | `python scripts/todo_regression.py` | 일~토·주간 이동·완료·확대·관리자 데모 동기화 |
| 완료 통신 모의 | `python scripts/todo_transport_regression.py` | 실제 클라이언트 코드 + 모의 HTTP/WS의 충돌·실패·타임아웃 |
| 크기 회귀 | `python scripts/preview_regression.py --seconds 65` | 관리자 미리보기 크기 유지·리사이즈·탭 왕복 |

브라우저 검사에는 Chromium이 필요합니다.

```bash
python -m playwright install chromium
# Linux의 시스템 라이브러리도 필요한 환경:
python -m playwright install --with-deps chromium
```

시스템 Chromium을 사용할 때는 `CHROMIUM_PATH`를 실제 실행 파일로 지정할 수 있습니다. 기본적으로 기존 시스템 Chromium/Chrome을 찾거나 Playwright의 브라우저를 사용합니다.

완료 통신 모의 검사는 각 시나리오에서 `about:blank`로 이동한 뒤 새 fixture 문서를 주입하여 이전 시나리오의 타이머·리스너·전역 상태가 남지 않도록 분리했습니다. 앱 코드가 아니라 테스트 실행 환경을 정리하는 변경입니다.

## 출력 파일

원본 결과는 **`artifacts/test-results/`**, 검사 중 화면 캡처는 **`artifacts/screenshots/`**로 보냅니다. 두 폴더는 Git과 소스 ZIP에서 제외합니다. `docs/TEST_REPORT.md`에는 검토한 결과만 요약합니다.

```bash
python -m pytest -q --junitxml=artifacts/test-results/backend.xml
python scripts/check_repo.py --json artifacts/test-results/repository.json
```

JUnit·디버그 로그는 커질 수 있고 요청 내용 등을 포함할 수 있으므로 기본 소스 배포에 넣지 않습니다. README 이미지가 필요하면 `python scripts/capture_docs.py`로 합성 데이터 화면 세 장을 `docs/assets/`에 생성합니다. 운영 화면은 캡처하지 않습니다.

## GitHub Actions

`.github/workflows/ci.yml`에는 다음 작업을 정의했습니다.

- Ubuntu Python 3.11/3.13, Windows Python 3.12에서 서버·패키징 테스트와 HTTP 스모크.
- Ubuntu Chromium에서 UI·완료 통신 모의·65초 미리보기 회귀.
- Docker Compose 설정 검사, 이미지 빌드, 비루트 프로세스·health endpoint 검사.

`.github/workflows/source-archive.yml`은 수동 실행으로 소스 ZIP과 체크섬을 생성합니다. 배포·Release 생성·원격 서버 변경은 하지 않습니다. 최초 업로드 후 실제 Actions 실행 결과를 확인해야 합니다.

Actions는 공식 저장소의 커밋 SHA에 고정하고 `contents: read`, checkout 자격 증명 비지속 옵션을 사용합니다. 워크플로는 GitHub-hosted runner를 대상으로 작성했습니다. Enterprise/self-hosted 환경의 별도 정책·호환성은 확인하지 않았습니다.

## 결과 해석

Chromium `set_content` 검사는 실제 배포 서버에 브라우저를 접속시키는 E2E 검사가 아닙니다. 통신 모의 검사는 실제 LAN을 시험한 것이 아니며, 루프백 HTTP도 iPad/Safari를 시험한 것이 아닙니다. Docker CI 정의가 있는 것과 로컬에서 Docker를 실제 실행한 것은 구분합니다.

실기기에서는 첫 연결, 세션 만료, Safari 가로/세로·홈 화면, Wi-Fi 끊김·복구, 서버 재시작, 장시간 사용, 실제 백업 복원, 사용할 NAS의 ACL을 별도로 확인하세요.
