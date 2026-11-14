# Room Hub 0.1.4 설치와 소스 업데이트

[문서 홈](../README.md) · [현재 상태](CURRENT_STATUS.md) · [V35/Termux](V35_TERMUX.md)

PC를 추론 서버에서 완전히 분리하는 Odyssey 구성은 [Odyssey 단독 OpenAI 운영](ODYSSEY_OPENAI.md)을 먼저 읽으세요. 아래 일반 설치와 이전 V35 문서는 다른 배포 방식도 포함합니다.

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

## Docker/Compose 음성 전처리 제한

Odyssey 단독 전환은 기존 Ubuntu의 Python/systemd 배포를 권장합니다. 현재 저장소의 `Dockerfile`은 FFmpeg를 설치하지 않습니다. `compose.yaml`이 `HUB_AI_PROFILE`과 `HUB_FFMPEG`를 전달하더라도 iPad 압축 녹음 전처리가 자동으로 지원되는 것은 아닙니다.

호스트 Odyssey에 설치한 FFmpeg는 컨테이너 안에서 사용할 수 없습니다. 컨테이너 음성 입력을 사용하려면 필요한 라이브러리까지 포함한 FFmpeg를 이미지에 설치하고, `HUB_FFMPEG`에 **컨테이너 내부** 실행 경로를 지정한 뒤 별도로 검증해야 합니다. 호스트의 실행 파일 하나를 마운트하거나 경로만 전달해서 해결됐다고 판단하지 마세요. 현재 기본 이미지는 FFmpeg가 필요한 녹음 경로의 준비 상태가 거부될 수 있으며, 이는 PC 추론 서버가 필요하다는 뜻이 아닙니다. 텍스트 규칙·클라우드 플래너의 준비 조건은 음성 전처리와 별개입니다.

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
| `HUB_AI_PROFILE` | `legacy` | `odyssey_openai`는 PC/로컬 추론·폴백·레거시 probe를 차단. 프로세스 시작 시 고정; 외부 전송 동의는 별도 |
| `HUB_FFMPEG` | 미설정 | 음성 변환에 사용할 실행 파일의 절대 경로. Odyssey 예: `/usr/bin/ffmpeg` |
| `HUB_PERSONAL_AGENT_ENABLED` | `false` | 새 하이브리드 Personal Agent(`/api/agent/*` 및 음성 자동 연결) 초기 활성화 여부 |
| `HUB_OPENAI_ENABLED` | `false` | OpenAI 클라우드 플래너 호출 초기 허용 여부 |
| `HUB_OPENAI_AUDIO_ENABLED` | `false` | 음성의 OpenAI STT(`/v1/audio/transcriptions`) 외부 전송 초기 허용 여부 |
| `HUB_OPENAI_ALLOW_SHARED_NOTES` | `false` | 공유 메모(`shared=true`) 제목/ID를 서버 수집 클라우드 문맥에 포함할지 여부 (비공개 메모는 제외) |
| `OPENAI_API_KEY` / `HUB_OPENAI_API_KEY` | 미설정 | OpenAI 전용 API 키 (`HUB_LLM_API_KEY`를 자동 재사용하지 않음) |
| `HUB_OPENAI_MONTHLY_CAP_USD` | `100.00` | 정책 최초 생성 시 월 예산 한도(USD) |
| `HUB_OPENAI_FAST_MODEL` / `HUB_OPENAI_DEEP_MODEL` | `gpt-6.1-sol` | 최초 DB 초기화 시 Tier 1 / Tier 2 계획 모델 |
| `HUB_OPENAI_REASONING_EFFORT` | `high` | 최초 DB 초기화 시 추론 강도(`low`/`medium`/`high`) |
| `HUB_OPENAI_STT_MODEL` | `gpt-4o-mini-transcribe` | 최초 DB 초기화 시 클라우드 STT 모델 |

직접 Python 실행(`run.py`)은 `.env` 파일을 자동으로 읽지 않습니다.

- **Python / systemd**: 셸에서 `export VAR=value`(PowerShell은 `$env:VAR="value"`)를 먼저 설정하거나, systemd 유닛에 `EnvironmentFile=/path/to/.env`를 명시해야 프로세스에 전달됩니다.
- **Docker Compose**: `compose.yaml`의 `environment:` 섹션에 선언된 변수만 컨테이너로 전달됩니다.
- **설정 우선순위**: `(1) 요청의 local_only 지정` > `(2) DB에 저장된 관리자 정책(PUT /api/agent/budget/policy)` > `(3) 정책 최초 생성 시 환경변수 초기화값` > `(4) 코드 기본값(전체 비활성)`. 저장된 정책 필드는 재시작 시 환경변수 초기값으로 덮어쓰지 않습니다. API 키는 DB 정책에 저장하지 않고 실행 프로세스 환경에서 읽습니다.

`HUB_AI_PROFILE`은 위 DB 정책과 별도의 프로세스 실행 경계입니다. `odyssey_openai`에서는 클라우드 정책을 끄거나 키·예산이 없더라도 레거시 PC 경로가 켜지지 않습니다. 기존 `speech-config.json`/LLM 설정을 보존하며 사용하지 않습니다. `local_only`는 계속 외부 호출을 막습니다. 프로필 이름을 잘못 지정하면 시작을 거부합니다. 프로필을 바꾸려면 서비스를 재시작해야 합니다.

### 선택적 Personal Agent 적용

1. 소스 업데이트 전에 기존 데이터·설정과 현재 소스 버전을 별도로 백업합니다. `HUB_DATA_DIR`을 유지하며, 정책을 다시 시드하려고 운영 DB를 삭제하지 않습니다.
2. 새 소스의 의존성을 설치하고 서버를 정상 재시작합니다. 기존 Whisper 설정과 모델은 자동 교체하지 않습니다. PC 없는 운영은 `odyssey_openai` 프로필을 선택하며, 기존 설정 파일의 enabled/backend 값을 직접 바꿀 필요가 없습니다.
3. 관리자 LLM 화면의 Personal Agent 패널에서 저장된 정책을 확인합니다. 먼저 에이전트만 활성화하고 `local_only` 텍스트 조회로 기존 데이터 연결을 확인할 수 있습니다.
4. 외부 호출을 원할 때만 서버의 비공개 환경에 `OPENAI_API_KEY` 또는 `HUB_OPENAI_API_KEY`를 설정합니다. 월 한도를 정한 뒤 클라우드 허용을 별도로 저장합니다.
5. iPad 녹음을 OpenAI로 보내려면 **에이전트·클라우드·음성 외부 전송 세 스위치 모두**를 켜야 합니다. 키·예산과 실행 가능한 로컬 FFmpeg도 필요합니다. 브라우저 녹음은 서버가 형식·길이를 검증한 WAV로 변환한 뒤에만 전송합니다.
6. 관리자 `/api/agent/status`와 표시 세션 `/api/speech/status`에서 적용 상태를 확인합니다. `ready`는 로컬 준비 조건을 뜻하며, 유료 API·모델 접근이나 실제 전사 성공을 확인한 결과가 아닙니다.
7. iPad는 기존 마이크 버튼 → 녹음 확인 → 직접 전송 흐름을 사용합니다. 전사 후 조회·타이머는 실행될 수 있으며, 그 밖의 변경은 관리자 미리보기 승인 후에만 반영됩니다. 대기·오류 시 새 유료 요청을 반복하지 말고 기존 작업 상태와 비용 원장을 확인합니다.

기존 정책은 관리자 UI/API로 바꿉니다. `.env`만 수정하거나 서버만 재시작해서 저장된 정책이 바뀐다고 가정하지 마세요. 클라우드를 끄면 새 외부 호출을 막지만 이미 전송한 요청의 처리·과금이나 이미 승인한 데이터 변경을 취소하지는 않습니다. [상세 적용·권한·검증 범위](OPENAI_PERSONAL_AGENT.md)를 참고하세요.

## iPad 연결

관리자 **표시 기기 → 새 화면 연결**에서 서버의 실제 주소를 기준으로 10분·1회용 링크/QR을 생성합니다. 연결 후 표시 세션은 브라우저 쿠키로 유지됩니다. iPad의 기존 완료/완료 취소·타이머·발생 알람 제어 권한은 유지합니다. Personal Agent는 자신의 음성 작업에서 요청을 제출하고 결과를 읽을 수 있지만, 일반 데이터 변경의 최종 승인은 관리자 전용입니다.

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

## 이전 V35 운영값과 소스 기본값

이전 V35 실기기 기록은 Whisper multilingual Base / 6 threads입니다. 이는 과거 런타임 설정이고 Odyssey OpenAI 프로필의 요구사항이 아닙니다. V35의 모델/threads를 바꾸려면 전사 대기열이 비어 있을 때 제공된 Termux 모델/CPU 도구로 변경하고, 동일 녹음으로 성능을 다시 측정하세요.
