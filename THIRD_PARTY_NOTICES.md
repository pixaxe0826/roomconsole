# 의존성과 외부 서비스

이 저장소는 다음 라이브러리를 설치하여 사용합니다. 라이브러리 코드를 vendor하거나 전체 라이선스를 대신 재지정하지 않습니다. 실제 버전은 requirements 파일을 기준으로 하고, 각 프로젝트의 라이선스와 의존성 공지를 확인하세요.

| 구성 | 공식 프로젝트/문서 |
|---|---|
| FastAPI | https://github.com/fastapi/fastapi |
| Uvicorn | https://github.com/Kludex/uvicorn |
| HTTPX | https://github.com/encode/httpx |
| python-multipart | https://github.com/Kludex/python-multipart |
| qrcode | https://github.com/lincolnloop/python-qrcode |
| tzdata | https://github.com/python/tzdata |
| pytest (개발) | https://github.com/pytest-dev/pytest |
| Playwright (개발) | https://github.com/microsoft/playwright-python |
| Python / SQLite | https://www.python.org/ / https://www.sqlite.org/ |
| GitHub 공식 Actions | .github/workflows/ 안의 actions/* 참조 |

날씨는 Open-Meteo를 이용하며 화면에 출처를 표시합니다. 이용 범위와 호출량에 맞는 약관을 별도로 확인하세요.

- https://open-meteo.com/en/docs
- https://open-meteo.com/en/terms

데모 일정·날씨는 scripts/build_previews.py에서 생성한 가상 데이터입니다. 문서 이미지는 이 가상 데이터 화면에서 캡처합니다. 폰트 바이너리는 배포하지 않고 시스템 폰트를 사용합니다.

이 문서는 프로젝트 자체에 대한 LICENSE가 아닙니다. 프로젝트 라이선스는 소유자가 아직 선택하지 않았습니다.

## 선택적 V35 음성 구성

- whisper.cpp v1.8.3, ggml-org: MIT; 설치 시 upstream 라이선스가 소스와 함께 내려옵니다.
- Whisper 모델은 upstream 모델 배포의 라이선스/이용 조건을 확인하세요. 이 소스 ZIP에는 모델이 없습니다.
- FFmpeg, nginx, OpenSSL은 사용자 시스템 패키지로 설치하며 각 배포판/빌드의 라이선스가 적용됩니다.
- iPad 마이크에는 WebKit MediaRecorder/getUserMedia를 사용합니다. Apple Intelligence나 Siri 서비스가 아닙니다.
- 공개 jfk.wav는 whisper.cpp 소스의 설치 후 실행 점검용 샘플입니다. 소스 배포 ZIP에는 포함하지 않습니다.
