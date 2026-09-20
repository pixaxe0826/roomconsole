# V35 Git 관리형 업데이트

이 문서는 기존 ZIP/패치 기반 V35 설치를 **GitHub main을 읽기 전용으로 따라가는 배포 clone**으로 전환하고, 이후 검토·병합된 코드만 안전하게 적용하는 절차를 설명합니다.

## 목표

개발과 운영을 분리합니다.

```text
Chat / 개발 브랜치
        ↓
GitHub Pull Request
        ↓ 사용자 검토
Merge → main
        ↓ main CI 성공 확인
V35: update-from-git.sh
        ↓
fetch → fast-forward → 검사 → 재시작 → health/version 확인
```

V35에서는 기능 개발이나 push를 하지 않습니다. 운영 전화는 `origin/main`을 소비하기만 합니다.

Git이 관리하는 것은 소스뿐입니다. 다음은 저장소에 올리지 않고 V35에 그대로 남깁니다.

- `data/`: SQLite, 토큰, HTTPS private material, 녹음/전사 기록
- `.venv-v35/`: Debian 전용 Python 환경
- `runtime/`: Whisper/Qwen 등 로컬 런타임 자산
- `.env`, `.env.*` 실제 설정
- `secrets/`, `backups/`

이 경로들은 `.gitignore` 대상입니다.

## 1. 기존 비-Git 0.1.7 설치를 한 번만 전환

현재 `~/room-hub`에서 다음 오류가 난다면 아직 Git clone이 아닙니다.

```text
fatal: not a git repository
```

먼저 Termux에 Git이 있어야 합니다.

```bash
pkg install git
termux-wake-lock
```

전환 전에 현재 서비스가 정상인지 확인합니다.

```bash
curl -fsS --max-time 5 http://127.0.0.1:8088/healthz
bash "$HOME/room-hub/deploy/termux/stt-accuracy.sh" status --verify
```

PR을 병합해 이 문서와 스크립트가 `main`에 들어간 뒤, 현재 비-Git 설치에서 `adopt-git.sh`를 확보해 실행합니다. 가장 단순한 방법은 새 clone을 임시로 만든 뒤 그 clone의 스크립트를 실행하는 것입니다.

```bash
cd "$HOME"
git clone --depth 1 --branch main https://github.com/pixaxe0826/roomconsole.git room-hub-bootstrap

bash "$HOME/room-hub-bootstrap/deploy/termux/adopt-git.sh"

rm -rf "$HOME/room-hub-bootstrap"
```

`adopt-git.sh`는 다음을 수행합니다.

1. 현재 built-in widget 소스가 GitHub와 다르면 중단합니다. 로컬/custom widget을 조용히 덮어쓰지 않습니다.
2. 최신 `main`을 새 디렉터리에 clone하고 `scripts/check_repo.py`를 실행합니다.
3. Room Hub만 정상 정지합니다.
4. 기존 `~/room-hub`를 `~/room-hub-pre-git-<timestamp>`로 옮깁니다.
5. Git clone을 다시 정확히 `~/room-hub` 위치에 둡니다.
6. `data/`, `.venv-v35/`, `runtime/`, private env/secret 경로를 새 clone으로 이동합니다.
7. 기존 runit 서비스 경로를 바꾸지 않고 다시 시작합니다.
8. `/healthz`가 살아나지 않으면 이전 설치를 자동 복원합니다.

성공 후 기존 소스 백업은 즉시 지우지 마세요. 실제 iPad/관리자/음성을 확인한 뒤 정리합니다.

## 2. 온라인과 V35가 같은지 확인

Git 관리로 전환한 뒤 언제든 다음만 실행하면 됩니다.

```bash
bash "$HOME/room-hub/deploy/termux/update-from-git.sh" --check
```

동일하면 다음 핵심 값이 일치합니다.

```text
local HEAD   == remote HEAD
local tree   == remote tree
ahead/behind == 0 0
working tree == clean
SYNCED: local tracked source is exactly origin/main.
```

`data/`, 모델, 토큰처럼 Git이 무시하는 운영 파일은 이 비교에 포함하지 않습니다. 이것이 의도한 동작입니다.

종료 코드는 다음과 같습니다.

- `0`: tracked source가 `origin/main`과 동일하고 clean
- `2`: 로컬 tracked/unignored 변경이 있음
- `3`: clean이지만 `origin/main`과 HEAD가 다름

## 3. 이후 모든 업데이트

새 기능은 다음 순서로 진행합니다.

1. Chat/개발 환경에서 새 브랜치 작성
2. GitHub PR 생성
3. CI와 diff 검토
4. 사용자가 Merge
5. **최종 main CI 성공 확인**
6. V35 바깥쪽 Termux에서:

```bash
bash "$HOME/room-hub/deploy/termux/update-from-git.sh"
```

직접 `git reset --hard origin/main`이나 force pull을 운영 절차로 사용하지 않습니다.

업데이터는 실제로 `git fetch` 후 현재 HEAD가 `origin/main`의 조상인지 확인하고 **fast-forward만 허용**합니다. 로컬 이력이 갈라졌거나 tracked 파일이 수정돼 있으면 중단합니다.

## 4. 업데이트 시 자동 보존/검증

`update-from-git.sh`는 다음 순서로 동작합니다.

1. `origin/main` fetch
2. local/remote HEAD·tree·ahead/behind 출력
3. dirty/diverged 상태 거부
4. Room Hub만 정지
5. `~/room-hub-git-backups/<timestamp>-<old-head>/`에 현재 HEAD 기록과 SQLite backup 생성
6. fetched `origin/main`으로 fast-forward
7. `scripts/check_repo.py` 실행
8. `deploy/termux/requirements-v35.txt`가 실제로 바뀐 경우에만 V35 Python 의존성 동기화
9. 서비스 재시작
10. `/healthz`의 version이 저장소 `VERSION`과 같은지 확인

일반 `requirements.txt`가 바뀌었다는 이유만으로 V35 환경을 재설치하지 않습니다. V35는 별도 검증된 `deploy/termux/requirements-v35.txt`를 사용합니다.

## 5. 자동 rollback

fast-forward 이후 저장소 검사, V35 dependency 동기화, 서비스 시작 또는 health/version 검사가 실패하면:

- Room Hub 정지
- tracked source를 이전 HEAD로 `git reset --hard <old-head>`
- V35 dependency 파일이 바뀌었던 업데이트라면 이전 pin으로 다시 동기화
- 업데이트 직전 SQLite backup 복원
- 이전 서비스를 다시 시작

합니다.

코드가 rollback된 동안 생성된 새 요청을 잃지 않도록 업데이트 중에는 서버를 정지한 채 작업합니다.

자동 rollback도 완전한 장비 snapshot은 아닙니다. 모델, HTTPS, 외부 Tasker 설정 같은 Git 비관리 자산은 이 도구가 변경하지 않으며 별도 백업 정책을 유지합니다.

## 6. V35에서 하지 않는 것

운영 V35에서는 다음을 하지 않는 것이 기본 정책입니다.

```bash
git add
git commit
git push
git checkout feature/...
git reset --hard origin/main
```

V35에서 임시로 코드를 수정하면 다음 업데이트가 의도적으로 중단됩니다. 필요한 수정은 개발 브랜치 → PR → Merge 절차로 GitHub에 먼저 반영하세요.

## 7. 현재 배포 경로를 유지하는 이유

기존 `termux-services`의 `room-hub/run`은 설치 시 `~/room-hub/deploy/termux/start.sh` 경로를 기록합니다. Git 전환 뒤에도 clone의 최종 경로를 **동일한 `~/room-hub`**로 유지하므로 다음을 다시 등록하지 않습니다.

- room-hub runit service
- room-hub-https
- nginx/8443 인증서
- Tasker boot 자동화
- Qwen 서비스
- Whisper 모델/빌드

전환 후에는 `sv status`, `/healthz`, STT verify로 실제 동작을 확인합니다.
