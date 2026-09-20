# LG V35 / Termux 상시 서버

[문서 홈](../README.md) · [현재 상태](CURRENT_STATUS.md) · [Git 관리형 업데이트](V35_GIT_UPDATE.md)

이 문서는 현재 Room Hub 0.1.4를 LG V35에서 상시 실행할 때의 **현재 권장 구조**를 설명합니다. 특정 사용자명/IP를 가정하지 않습니다.

## 구조

```text
Android / Termux
├─ termux-services (runsvdir)
│  ├─ room-hub       → PRoot Debian → FastAPI :8088
│  └─ room-hub-https → nginx HTTPS :8443
├─ termux-wake-lock
└─ Tasker 같은 외부 부팅 자동화 → start-services.sh 호출
```

현재 Android 환경에서는 Termux:Boot 설치가 Play Protect 정책 등으로 막힐 수 있으므로, **Tasker의 Device Boot 이벤트에서 Termux RUN_COMMAND를 호출하는 방법**을 우선 권장합니다. 저장소의 runit 서비스 자체는 Termux:Boot에 의존하지 않습니다.

## 준비

바깥쪽 Termux 셸에서:

```bash
pkg update
pkg install python proot-distro termux-services unzip curl
termux-wake-lock
```

소스를 `~/room-hub`에 두었다고 가정하면:

```bash
cd ~/room-hub
bash deploy/termux/setup.sh
```

처음에는 전면 실행으로 확인합니다.

```bash
bash deploy/termux/start.sh
```

다른 LAN 장치에서 `/healthz`와 `/manager`가 열리는지 확인한 뒤 Ctrl+C로 전면 서버를 종료합니다.

## 백그라운드 서비스

```bash
bash deploy/termux/install-service.sh
sv status "$PREFIX/var/service/room-hub"
curl -fsS http://127.0.0.1:8088/healthz
```

최근 로그:

```bash
tail -n 80 "$PREFIX/var/log/sv/room-hub/current"
```

서비스 제어:

```bash
sv up "$PREFIX/var/service/room-hub"
sv down "$PREFIX/var/service/room-hub"
sv restart "$PREFIX/var/service/room-hub"
```

PRoot 자식 프로세스 종료가 지연될 수 있으므로 소스에는 `deploy/termux/stop-room-hub.py`도 포함되어 있습니다. 업데이트 전에 8088이 실제로 닫혔는지 확인하세요.


## Git 관리형 업데이트

기존 ZIP/패치 설치를 한 번 Git clone으로 전환하면 이후에는 운영 V35에서 소스를 직접 수정하지 않고, 검토·병합된 GitHub `main`만 fast-forward로 적용할 수 있습니다.

현재 설치가 Git인지 확인:

```bash
git -C "$HOME/room-hub" rev-parse --is-inside-work-tree
```

비-Git 설치의 최초 전환과 이후 업데이트/rollback은 [V35 Git 관리형 업데이트](V35_GIT_UPDATE.md)를 사용하세요. 전환 뒤 상태 확인은:

```bash
bash "$HOME/room-hub/deploy/termux/update-from-git.sh" --check
```

업데이트는:

```bash
bash "$HOME/room-hub/deploy/termux/update-from-git.sh"
```

V35는 push하지 않는 배포 clone으로 유지합니다. `data/`, `.venv-v35/`, 모델, 토큰, TLS private material은 Git에서 제외합니다.

## 재부팅 후 자동 시작 — Tasker 권장

Termux 설정에서 외부 명령을 허용하고 Tasker에 Termux RUN_COMMAND 권한을 부여한 뒤, Tasker의 **Device Boot → Wait 20~30초 → Termux command** 흐름을 사용합니다.

호출 명령은 `termux-services`의 서비스 관리자를 시작하고 wake lock을 다시 확보하면 됩니다. 예:

```bash
termux-wake-lock
. "$PREFIX/etc/profile.d/start-services.sh"
```

`room-hub`/`room-hub-https`가 enabled 상태라면 runit이 시작합니다. Android에서 Termux와 Tasker는 배터리 최적화 제외를 검토하고, 상시 서버 장치는 전원/발열 상태를 확인하세요.

`install-service.sh`가 생성하는 `~/.termux/boot/` 스크립트는 Termux:Boot 호환용 보조 파일일 뿐이며, Tasker 방식에서는 필요하지 않습니다.

## 음성 엔진

V35 전사는 별도 Debian 환경의 FFmpeg + whisper.cpp를 사용합니다.

```bash
bash deploy/termux/install-speech.sh base
```

모델과 설정은 GitHub ZIP에 포함되지 않습니다. 실제 운영에서 모델을 바꿀 때는 `speech-model.sh`/`cpu_lab.py`를 사용해 hash와 실제 엔진 smoke test를 확인하세요. 현재 실기기 기준 운영값은 multilingual Base / 6 threads이지만, 이는 특정 V35에서의 선택이며 일반 기본값이 아닙니다.

## iPad 마이크용 HTTPS

Termux nginx와 openssl 도구가 필요합니다.

```bash
pkg install nginx openssl-tool
bash deploy/termux/install-https.sh <V35-LAN-IP>
```

생성된 개인 CA를 iPad에 설치한 뒤 **인증서 신뢰 설정에서 완전 신뢰**를 활성화해야 합니다. CA private key는 `data/https/private/` 아래의 운영 비밀이므로 GitHub에 올리지 않습니다.

## 연결 문제 진단

문제가 생겼을 때 V35 화면을 먼저 깨우기보다 층별로 확인합니다.

```text
1. 다른 LAN 장치에서 ping <V35-IP>
2. TCP 8088 접근 여부
3. http://<V35-IP>:8088/healthz
4. V35 내부 curl http://127.0.0.1:8088/healthz
5. sv status room-hub
6. Room Hub 로그에서 /ws/display 또는 /ws/manager 상태
```

V35 내부 health는 정상인데 외부 LAN 접근만 실패하면 Room Hub보다 Wi-Fi/Android 네트워크 상태를 먼저 확인합니다.
