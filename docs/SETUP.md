# Room Hub 0.1.4 설치와 소스 업데이트

[문서 홈](../README.md) · [현재 상태](CURRENT_STATUS.md) · [V35/Termux](V35_TERMUX.md)

## 일반 Python 실행

Python 3.11 이상이 필요합니다. 프런트엔드에 Node/npm 빌드는 없습니다.

Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py
```

Linux/macOS:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run.py
```

기본 관리자 주소는 `http://localhost:8088/manager`입니다. LAN의 iPad에서는 서버의 실제 LAN 주소를 사용합니다.

## 주요 환경 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `HUB_HOST` | `0.0.0.0` | 직접 Python 실행 바인딩 |
| `HUB_PORT` | `8088` | 서버 포트 |
| `HUB_DATA_DIR` | 프로젝트 `data/` | DB·키·음성·런타임 설정 |
| `HUB_WIDGET_DIR` | 프로젝트 `widgets/` | 위젯 소스 |
| `HUB_ADMIN_TOKEN` | 자동 생성 | 직접 지정 시 비밀값 |
| `HUB_INGEST_TOKEN` | 자동 생성 | 음성/외부 입력용 별도 키 |
| `HUB_SECURE_COOKIE` | `false` | HTTPS 프록시 환경에서만 `true` 검토 |

직접 Python 실행은 `.env`를 자동으로 읽지 않습니다. Compose는 YAML에 전달하도록 정의된 값만 사용합니다.

## iPad 연결

관리자 **표시 기기 → 새 화면 연결**에서 서버의 실제 주소를 기준으로 10분·1회용 링크/QR을 생성합니다. 연결 후 표시 세션은 브라우저 쿠키로 유지됩니다. iPad는 할 일 완료/완료 취소와 읽기 전용 탐색만 수행하고, 생성·내용 수정·삭제는 관리자 전용입니다.

마이크 녹음은 브라우저 보안 정책상 신뢰된 HTTPS가 필요합니다. V35 배포는 별도 8443 nginx 프록시/개인 CA 설치 도구를 제공합니다. [V35/Termux](V35_TERMUX.md)

## 기존 운영 데이터를 새 소스로 옮길 때

이 GitHub ZIP은 **데이터 이전 패키지가 아닙니다**. 운영 데이터는 소스와 분리해서 보존하세요.

1. 기존 서버를 중지합니다.
2. `data/`, 사용자 위젯, 실제 환경 설정을 별도 백업합니다.
3. 새 소스 폴더에 의존성을 새로 설치합니다. `.venv`를 다른 OS/장치에서 복사하지 않습니다.
4. 기존 `HUB_DATA_DIR`을 그대로 사용하거나, 서버 정지 상태에서 `data/` 전체를 새 위치로 복사합니다.
5. 한 DB에 두 Room Hub 서버를 동시에 연결하지 않습니다.
6. 새 서버의 `/healthz`, 관리자, iPad를 확인합니다.

모델 가중치와 V35 빌드 산출물은 `runtime/` 아래의 로컬 자산이며 GitHub source archive에서 제외됩니다.

## 현재 V35 운영값과 소스 기본값

현재 실기기 운영 기준은 Whisper multilingual Base / 6 threads입니다. 이는 런타임 설정이고 소스 기본 설치값과 동일하다는 뜻은 아닙니다. 모델/threads를 바꾸려면 전사 대기열이 비어 있을 때 제공된 Termux 모델/CPU 도구로 변경하고, 동일 녹음으로 성능을 다시 측정하세요.
