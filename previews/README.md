# 독립 미리보기

- [클라이언트](client_preview.html)
- [관리자](manager_preview.html)

PC에서 파일을 내려받아 브라우저로 엽니다. GitHub 코드 보기 화면에서는 앱으로 실행되지 않습니다.
두 파일은 외부 네트워크 없이 가상 데이터로 동작합니다. 실제 서버와 동기화하지 않고 새로 열면 변경이 사라집니다. iPad의 실제 운영은 서버 `/client`를 사용하세요.

`sample_state.json`은 생성기가 사용하는 합성 데이터이며 운영 DB에서 내보낸 자료가 아닙니다.

UI 수정 후 저장소 루트에서 `python scripts/build_previews.py`로 다시 생성합니다. 테스트 스크린샷은 `artifacts/screenshots/`에, README에 선별한 이미지는 `docs/assets/`에 둡니다.

`llm_widget_preview.html`: 4×2 LLM 응답 전용 예시 배치. 합성 요청/응답 3개를 탐색하며 실제 LLM 호출은 하지 않습니다.
