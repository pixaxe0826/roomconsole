# 설계 시 확인한 공식 문서

검토 기준: 2026-09-17. 아래 문서는 라이브러리·프로토콜의 근거이며, 이 프로젝트의 실기기 성능을 검증한 자료가 아닙니다.

- FastAPI — WebSockets: https://fastapi.tiangolo.com/advanced/websockets/
- FastAPI — Request Files: https://fastapi.tiangolo.com/tutorial/request-files/
- Open-Meteo — Weather API: https://open-meteo.com/en/docs
- Open-Meteo — Terms: https://open-meteo.com/en/terms
- TrueNAS — Installing Custom Apps: https://apps.truenas.com/managing-apps/installing-custom-apps/
- Amazon Alexa — Request and Response JSON Reference: https://developer.amazon.com/en-US/docs/alexa/custom-skills/request-and-response-json-reference.html
- Google Home — Cloud-to-cloud: https://developers.home.google.com/cloud-to-cloud

날씨 출처 표시는 실제 위젯에 포함합니다. 개인/비상업 용도를 벗어나면 공급자 이용 조건과 허용량을 다시 확인하세요. Alexa/Google Home의 제품별 전송 가능 범위와 인증은 별도 구현 시 검토해야 합니다.


## GitHub 저장소 정리 시 확인한 자료

- 로컬 소스 업로드: https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github
- GitHub Pages의 정적 호스팅 범위: https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages
- Actions 권한·SHA 고정: https://docs.github.com/en/actions/reference/security/secure-use
- Dependabot의 Actions 업데이트: https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/auto-update-actions

워크플로에 지정한 커밋은 공식 Actions 저장소 릴리스에서 확인했습니다.

| Action | 참조 릴리스 | 고정 커밋 |
|---|---|---|
| actions/checkout | v7.0.0 | `9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0` |
| actions/setup-python | v7.0.0 | `5fda3b95a4ea91299a34e894583c3862153e4b97` |
| actions/upload-artifact | v7.0.1 | `043fb46d1a93c77aae656e7c1c64a875d1fc6a0a` |

- https://github.com/actions/checkout/releases/tag/v7.0.0
- https://github.com/actions/setup-python/releases/tag/v7.0.0
- https://github.com/actions/upload-artifact/releases/tag/v7.0.1

자료 확인은 원격 CI 실행·보안 감사의 대체가 아닙니다.
