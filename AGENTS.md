# Room Hub 작업 지침

작업 전 README.md, docs/ARCHITECTURE.md, SECURITY.md, docs/TESTING.md를 읽습니다.

- 기준 기능 버전은 VERSION의 0.1.1입니다. 런타임은 Python/FastAPI + SQLite 1 worker, UI는 순수 웹입니다.
- app/는 서버, web/는 관리자·클라이언트, widgets/는 신뢰 코드 플러그인입니다.
- data/, .env, 실제 토큰·DB·음원·백업을 읽어 문서나 테스트 픽스처로 복사하지 않습니다.
- 클라이언트는 완료 전용 API만 쓸 수 있습니다. 작업 편집 권한을 무심코 확대하지 않습니다.
- 날짜 줄은 일~토, 주 이동은 ±7일, 반복 회차는 독립 상태를 유지합니다.
- 관리자 미리보기의 부모 크기를 ResizeObserver 안에서 반복 수정하지 않습니다.
- 기본 UI를 변경하면 scripts/build_previews.py로 독립 데모를 다시 생성합니다.
- 테스트 결과는 artifacts/로 저장합니다. 기본 검사: pytest, live_smoke, browser_smoke, todo_regression, todo_transport_regression, preview_regression.
- 미구현 후보는 docs/ROADMAP.md에 둡니다. 제안과 실행 기능을 구분합니다.
- GitHub 원격 생성·push·배포·라이선스 선택은 소유자의 별도 지시 없이 수행하지 않습니다.
