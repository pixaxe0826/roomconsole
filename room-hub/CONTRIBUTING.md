# 기여와 개발

[README](README.md) · [구조](docs/ARCHITECTURE.md) · [테스트](docs/TESTING.md)

## 개발 환경

Python 3.11 이상으로 가상환경을 만들고 `requirements-dev.txt`를 설치합니다. 정적 UI는 빌드 도구 없이 수정합니다. 앱 실행은 `python run.py`, 단독 미리보기 재생성은 `python scripts/build_previews.py`입니다.

테스트 시 운영 서버·데이터·키와 분리된 환경을 사용하세요. 모듈 import가 기본 앱의 데이터 폴더를 만들 수 있으므로 테스트 명령은 빈 작업 복사본에서 실행하거나 `HUB_DATA_DIR`을 테스트용 임시 경로로 지정합니다.

## 지켜야 할 경계

- iPad 입력은 완료·완료 취소만 허용합니다. 관리자 API 권한을 표시 기기에 넘기지 마세요.
- 할 일의 날짜 줄은 일요일~토요일 고정이며, 주 이동은 선택한 요일을 유지합니다. 반복 생성 규칙의 주 기준과 UI 표시 주 기준을 혼동하지 마세요.
- 기존 `data/`와 사용자 위젯, 토큰을 삭제하거나 초기화하지 마세요.
- 서버는 1 worker입니다. 분산/복제 지원 없이 worker 수를 늘리지 마세요.
- `ResizeObserver`에서 관찰 대상의 폭·높이를 다시 수정하는 루프를 만들지 마세요.
- 외부 데이터 수집과 인증 키는 서버에 두고, 위젯 코드에는 비밀정보를 넣지 마세요.
- 임의 위젯의 JavaScript는 관리자 미리보기에서도 실행되는 신뢰 코드입니다. 보안 샌드박스로 설명하지 마세요.

## 변경 검증

```bash
python scripts/check_repo.py
python -m pytest -q
python scripts/build_previews.py
python scripts/live_smoke.py
python scripts/browser_smoke.py
python scripts/todo_regression.py
python scripts/todo_transport_regression.py
python scripts/preview_regression.py --seconds 65
```

브라우저는 별도 Playwright Chromium 설치가 필요합니다. 결과와 스크린샷은 `artifacts/`에 둡니다. 재생성한 `previews/*.html`과 가상 데이터는 소스와 함께 커밋하되, 운영 데이터는 커밋하지 않습니다.

## PR 작성

작은 변경 단위와 테스트 결과, 실제 시험한 환경을 적습니다. 미구현 기능은 구현되었다고 표시하지 않습니다. UI 변경은 가상 데이터 스크린샷을 첨부하고 세로·가로·축소 위젯·확대 화면을 확인합니다. 보안 문제는 공개 이슈가 아니라 [SECURITY.md](SECURITY.md)의 경로를 확인하세요.

라이선스와 외부 기여 수락 정책은 저장소 소유자가 공개 전에 결정해야 합니다. 이 문서는 기여자에게 별도 권리 양도를 요구하거나 프로젝트 라이선스를 임의로 부여하는 계약이 아닙니다.
