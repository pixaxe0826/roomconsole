# Odyssey 단독 운영 — PC 없는 OpenAI 전사·플래너

[문서 홈](../README.md) · [Personal Agent](OPENAI_PERSONAL_AGENT.md) · [설치](SETUP.md) · [배포·백업](DEPLOYMENT.md) · [보안](../SECURITY.md)

이 문서는 PR #37의 `odyssey_openai` 실행 프로필을 적용하는 절차입니다. 소스 지원과 실제 운영 서버 배포는 다릅니다. PR 병합, Odyssey 배포, 실제 API 호출과 iPad 검증을 이 문서를 작성하면서 수행했다는 뜻이 아닙니다.

아래 절차는 기존 **Ubuntu Python/systemd 배포**를 대상으로 합니다. 현재 기본 Docker 이미지에는 FFmpeg가 없습니다. Compose의 프로필·환경변수 전달만으로 iPad 압축 녹음이 지원되지는 않으며, 호스트의 FFmpeg도 컨테이너 내부에 자동으로 제공되지 않습니다. Docker를 쓰려면 FFmpeg와 의존 라이브러리를 포함한 이미지 및 컨테이너 내부 경로를 별도로 준비·검증해야 합니다. [Docker/Compose 제한](SETUP.md#dockercompose-음성-전처리-제한)을 참고하세요.

## 1. 운영 구조

| 구성 | 역할 | PC 필요 여부 |
|---|---|---|
| iPad | 기존 HTTPS 주소로 접속, 녹음 업로드, 상태·결과 표시 | 불필요 |
| Odyssey Room Hub | 인증, 음성 대기열, FFmpeg 변환·검증, 규칙 해석, 예산·승인, SQLite, 위젯 갱신 | 불필요 |
| OpenAI STT | Odyssey가 검증한 음성의 전사 | 불필요; 인터넷·API 키·전송 허용 필요 |
| OpenAI 플래너 | Odyssey의 로컬 규칙으로 처리하지 못하는 허용된 요청의 계획 | 불필요; 인터넷·API 키·전송 허용 필요 |

iPad는 OpenAI API 키나 PC 주소를 받지 않습니다. 모든 API 호출은 Odyssey의 Room Hub 프로세스에서 시작합니다. OpenAI가 반환한 계획은 실행 권한이 아니며, Odyssey가 대상·버전·권한을 검증합니다. 일반 변경은 관리자 승인 후에만 반영합니다. 명확한 조회와 기존의 제한된 타이머 경로는 Odyssey의 로컬 규칙으로 처리할 수 있습니다.

이 프로필에서는 Windows의 `llama-server`, `whisper-server`, SSH 역방향 터널과 `8090`·`8178` 포트가 필요하지 않습니다. 프로필은 다음 경로를 차단합니다.

- 기존 로컬/원격 Whisper 실행과 전송, 로컬 Qwen/llama-server 전송.
- 기존 PC 엔드포인트에 대한 연결 검사와 자동 폴백.
- 클라우드 비활성·키 누락·예산 부족·통신 오류 뒤의 PC 재접속.
- 이전에 대기하던 레거시 요청의 PC 재전송이나 클라우드로의 무단 전환.

기존 설정·작업 이력은 보존합니다. `speech-config.json`의 `backend`를 `openai`로 고치거나 기존 LLM URL을 OpenAI 주소로 바꿀 필요가 없습니다. 저장된 PC 설정이 남아 있어도 이 프로필의 실행 경로에는 사용하지 않습니다. 클라우드 연결은 공식 `https://api.openai.com/v1`을 사용하며 PC의 호환 API나 터널로 바꾸지 않습니다.

## 2. 프로필과 동의는 별개

| 설정 | 의미 | 적용 시점 |
|---|---|---|
| `HUB_AI_PROFILE=odyssey_openai` | Room Hub에서 레거시 추론·PC 연결 경로를 차단 | 프로세스 시작 시; 변경 후 재시작 필요 |
| `HUB_FFMPEG=/usr/bin/ffmpeg` | Odyssey에서 사용할 음성 변환 실행 파일의 절대 경로 | 프로세스 시작 시 |
| `OPENAI_API_KEY` 또는 `HUB_OPENAI_API_KEY` | OpenAI 전용 키 | 비공개 서비스 환경 |
| `agent_enabled` | Personal Agent 사용 | 관리자에 저장한 정책 |
| `cloud_enabled` | OpenAI 플래너 전송 허용 | 관리자에 저장한 정책 |
| `audio_cloud_enabled` | 음성의 OpenAI STT 전송 허용 | 관리자에 저장한 정책 |

프로필 기본값은 기존 동작을 보존하는 `legacy`입니다. 허용하지 않는 프로필 문자열은 시작 오류로 거부합니다. **프로필 또는 API 키만 설정해도 외부 전송 동의가 자동으로 켜지는 것은 아닙니다.** 클라우드 음성에는 세 정책이 모두 필요합니다. `local_only` 요청은 계속 외부 호출을 금지하며, 이 프로필에는 로컬 음성 엔진이 없으므로 로컬 전용 음성은 사용할 수 없습니다. 로컬 전용 텍스트 규칙 처리는 별개입니다.

`HUB_PERSONAL_AGENT_ENABLED`, `HUB_OPENAI_ENABLED`, `HUB_OPENAI_AUDIO_ENABLED`와 예산 관련 환경변수는 정책 행 최초 생성의 초기값입니다. 기존 DB가 있다면 Manager에서 저장된 정책을 바꿉니다. 이 정책을 초기화하려고 DB를 지우지 마세요. 프로필은 저장된 정책보다 바깥쪽의 실행 경계이므로, 클라우드를 끄더라도 PC 연결이 다시 켜지지 않습니다.

## 3. 소스 업데이트 전 확인

이전 운영 기록의 경로는 소스 `/opt/room-hub`, 가상환경 `/opt/room-hub/.venv`, 데이터 `/var/lib/room-hub`입니다. 실제 서버 설정이 다르면 아래 경로를 그대로 실행하지 마세요.

```bash
systemctl show room-hub.service \
  -p User -p WorkingDirectory -p FragmentPath -p EnvironmentFiles
git -C /opt/room-hub rev-parse --is-inside-work-tree
git -C /opt/room-hub branch --show-current
git -C /opt/room-hub status --short
/opt/room-hub/.venv/bin/python --version
command -v ffmpeg
df -h /opt /var
sudo du -sh /opt/room-hub /var/lib/room-hub
```

다음 조건을 모두 확인합니다.

1. PR을 검토·병합했고 배포할 `main`의 CI가 통과했습니다. 병합 전 `main`을 pull해도 PR 소스는 적용되지 않습니다.
2. `/opt/room-hub`가 해당 저장소의 Git checkout이고, 브랜치가 `main`이며 작업 트리가 깨끗합니다.
3. 실제 서비스 사용자, 데이터 경로와 가상환경이 확인됐습니다. Git/pip 명령은 해당 설치를 관리하는 계정으로 실행합니다.
4. 기존 음성·LLM 요청이 완료됐거나 명시적으로 취소됐습니다. 기존 작업을 새 ID로 재전송해 정리하지 않습니다.
5. 소스·가상환경과 데이터 전체를 백업할 공간이 있습니다. 서비스 유닛·drop-in·환경 파일도 비공개 위치에 별도로 보관합니다.
6. Odyssey에 실행 가능한 FFmpeg가 있습니다. `command -v ffmpeg`가 경로를 반환하지 않으면 서비스 중지 전에 Ubuntu 패키지로 설치하고 `ffmpeg -version`으로 확인합니다. 이 준비가 끝나기 전에는 4단계로 넘어가지 않습니다.

FFmpeg가 없는 Ubuntu 설치에서만 아래 명령을 사용합니다. 기존 FFmpeg가 있으면 불필요하게 교체하지 않습니다.

```bash
sudo apt-get update
sudo apt-get install ffmpeg
command -v ffmpeg
ffmpeg -version
```

**Git 저장소가 아니거나 로컬 수정이 있으면 아래 pull 절차를 중단합니다.** 과거 FD hotfix, 사용자 위젯과 설치별 수정이 있을 수 있습니다. `reset --hard`, `git clean`, 기존 폴더 위에 ZIP 전체 덮어쓰기를 하지 않습니다. 별도 checkout에 새 소스를 준비하고 차이를 검토한 후 설치 경로에 맞는 교체 계획을 세워야 합니다. Termux 업데이트 도구나 센서 전용 업데이트 패키지는 Odyssey 전체 업데이트 도구가 아닙니다.

## 4. 깨끗한 Git 설치의 백업과 업데이트

위 조건을 만족한 경우에만 실행합니다. Room Hub 백엔드는 4~5단계 동안 중단됩니다. HTTPS/Cloudflare/SSH 설정은 변경하지 않습니다. **소스 업데이트 직후에는 서비스를 시작하지 않습니다.** 5단계에서 `odyssey_openai` 프로필과 환경 파일을 적용한 뒤 처음 시작해야 합니다. 중간에 기본 `legacy` 프로필로 시작하면 저장된 PC 설정이나 대기 작업이 다시 활성화될 수 있습니다.

```bash
(
  set -eu
  sudo install -d -m 700 /var/backups/room-hub
  rh_backup_dir="$(sudo mktemp -d /var/backups/room-hub/pre-odyssey-openai-XXXXXXXX)"
  printf '백업 위치: %s\n' "$rh_backup_dir"

  sudo systemctl stop room-hub.service
  sudo cp -a /var/lib/room-hub "$rh_backup_dir/data"
  sudo cp -a /opt/room-hub "$rh_backup_dir/source"

  git -C /opt/room-hub pull --ff-only origin main
  /opt/room-hub/.venv/bin/python -m pip install \
    -r /opt/room-hub/requirements.txt
)
```

정상 완료해도 서비스는 의도적으로 중지 상태를 유지합니다. 그대로 5단계로 진행합니다. 오류가 나면 이후 명령이 중단되므로 원인과 복구 경로를 확인하기 전에 서비스를 시작하거나 다음 단계로 넘어가지 않습니다. 데이터에는 SQLite WAL, 토큰, 연결 정보, 설정과 음성이 포함되므로 정지 후 전체 디렉터리를 복사합니다. 백업은 비공개로 보관합니다. 소스 롤백만으로 DB 변경이나 이미 발생한 API 비용이 되돌아가지는 않습니다.

## 5. Odyssey 서비스에 실행 프로필 설정

새 소스가 설치되고 Room Hub가 중지된 상태에서 전용 환경 파일을 편집합니다. 기존 파일이면 다른 항목을 보존합니다. 아래 환경 파일과 서비스 drop-in을 모두 적용하기 전에는 Room Hub를 시작하지 않습니다.

```bash
sudo touch /etc/room-hub-openai.env
sudo chmod 600 /etc/room-hub-openai.env
sudoedit /etc/room-hub-openai.env
```

파일 내용의 예시입니다. `OPENAI_API_KEY=` 오른쪽에만 실제 키를 입력합니다. 키를 저장소·스크린샷·채팅·셸 명령행에 넣지 마세요. FFmpeg는 `command -v ffmpeg`로 확인한 Odyssey의 절대 경로를 사용합니다.

```ini
HUB_AI_PROFILE=odyssey_openai
HUB_FFMPEG=/usr/bin/ffmpeg
OPENAI_API_KEY=
```

서비스의 기존 사용자·실행 명령·포트·데이터 경로는 보존하면서 환경 파일만 추가합니다.

```bash
sudo systemctl edit room-hub.service
```

기존 drop-in을 지우지 말고 아래 항목을 추가합니다.

```ini
[Service]
EnvironmentFile=/etc/room-hub-openai.env
```

```bash
sudo systemctl daemon-reload
sudo systemctl start room-hub.service
systemctl is-active room-hub.service room-hub-https.service
curl --noproxy '*' -fsS --max-time 5 http://127.0.0.1:8088/healthz
```

직접 Python 실행은 `.env` 파일을 자동으로 읽지 않습니다. SSH 셸의 `export`는 이미 실행 중인 systemd 서비스에 적용되지 않습니다. 키 확인을 위해 환경 전체나 환경 파일 내용을 공개하지 마세요. 기존 HTTPS 주소, 인증서, Cloudflare Access와 iPad 연결 쿠키는 그대로 사용합니다. 한 DB에 두 Room Hub worker를 실행하지 않습니다.

## 6. 관리자 정책과 실제 요청 검증

Manager → LLM의 Personal Agent 패널에서 다음을 저장합니다.

1. `Personal Agent 활성화` 켜기.
2. 직접 정한 `월 예산 USD`를 입력하고 `OpenAI 클라우드 플래너 허용` 켜기.
3. 음성 외부 전송에 동의할 때 `음성 외부 OpenAI STT 전송 허용` 켜기.
4. `공유 메모 제목의 클라우드 문맥 포함 허용`은 필요할 때만 켜기.
5. `정책 적용`으로 저장.

기본 월 예산 초기값 `100`은 반드시 확인합니다. 예산 원장은 공급자 청구서나 최종 결제 상한을 보장하지 않습니다. API 계정의 모델 사용 권한·실제 비용·전사 품질은 별도 검증 대상입니다.

먼저 요청을 발생시키지 않는 상태 확인을 수행합니다. 관리자 AI 상태와 `/api/agent/status`, `/api/speech/status`에서 프로필, 세 정책, 키 설정 여부, 예산과 FFmpeg 준비 상태를 확인합니다. 이 프로필의 상태 조회와 진단은 PC 포트나 OpenAI에 추론 요청을 보내지 않습니다. `ready`는 로컬 준비 조건이며 공급자 접속·키 유효성·모델 접근·실제 인식 성공 증거가 아닙니다.

이어서 **PC의 RoomHub AI Bridge를 중지·비활성화하거나 PC를 끈 상태**에서 검증합니다. 다른 SSH 세션이나 다른 프로젝트의 AI 프로세스를 이름만으로 일괄 종료하지 않습니다.

| 검사 | 기대 결과 | 유료 호출 |
|---|---|---|
| 기존 HTTPS/iPad·위젯·데이터 | PC 없이 정상 접근·기존 배치 유지 | 없음 |
| 상태 조회와 AI 진단 | Odyssey/OpenAI 경로 표시; PC를 필수 노드로 요구하지 않음 | 없음 |
| `local_only` 텍스트 조회 | Odyssey 규칙으로 처리, 외부 호출 없음 | 없음 |
| `빠른 계획` 텍스트 조회 | 새 실행의 실제 클라우드 경로·결과 확인 | 발생 가능 |
| 짧은 iPad 음성 1건 | 업로드 → OpenAI 전사 → 에이전트 결과 | 발생 가능 |
| 음성 UI 재열기/재접속 | 같은 작업 GET 조회; 음성·계획 재전송 없음 | 새 호출 없음 |

실제 API 검사는 비용·외부 전송을 허용한 후에만 수행합니다. 실패하면 기존 job/run의 오류와 원장을 먼저 확인합니다. 새 요청 ID로 반복 호출하거나 unknown 비용을 무료로 지우지 않습니다. 일반 변경은 관리자 승인, 타이머의 좁은 예외는 기존 범위를 유지합니다.

## 7. PC 제거, 장애와 복구

PC 없이 위 검사가 성공한 뒤 Windows 예약 작업 `RoomHub AI Bridge`와 `C:\RoomHubAI`의 해당 서버 설치만 정리할 수 있습니다. 삭제 전에 작업 정의·필요한 설정을 비공개로 보관하고, 작업 비활성화 → 중지 → 재부팅 → PC 없는 검증 → 최종 삭제 순서를 사용합니다. 이 소스 업데이트는 Windows 작업·프로세스·파일을 원격으로 삭제하지 않습니다.

OpenSSH, `.ssh`, GPU 드라이버/CUDA, 다른 프로젝트와 모델은 삭제 대상이 아닙니다. Odyssey의 Room Hub/HTTPS/Cloudflare/FFmpeg와 `/var/lib/room-hub`도 삭제하지 않습니다.

인터넷 장애·API 키 누락·동의 철회·예산 부족 시 클라우드가 필요한 작업은 명시적 오류로 종료합니다. PC나 로컬 Whisper로 전환하지 않습니다. 위젯·DB·서버 타이머/알람 상태는 계속 Odyssey가 관리합니다. 이 프로필은 오프라인 음성 전사, 화면 꺼진 iPad 깨우기, 소리 전달 보장 또는 15초 응답 보장이 아닙니다.

안전하게 신규 클라우드 호출을 멈추려면 관리자에서 클라우드 정책을 끕니다. `odyssey_openai` 프로필은 유지하므로 PC 경로가 재활성화되지 않습니다. 이미 전송한 요청의 과금이나 이미 승인한 변경은 취소되지 않습니다.

예전 추론 구조로 돌아가려는 경우에만 서버를 정지하고 기존 설정·서비스를 검토한 뒤 프로필을 `legacy`로 바꾸고 재시작합니다. 저장된 레거시 설정이 다시 효력을 얻을 수 있으므로 이를 단순한 클라우드 끄기 방법으로 사용하지 않습니다. 코드/데이터 복원은 전체 백업과 이후 변경을 검토한 별도 복구 작업입니다.
