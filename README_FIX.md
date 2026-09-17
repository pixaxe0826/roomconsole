# Room Hub 0.1.1 — 연결 QR이 사라지는 문제 수정

패치 ID: **0.1.1-pairfix1**. 기능 버전 및 `/healthz` 버전은 **0.1.1을 유지**합니다.
적용 대상: 기존 Room Hub 0.1.1 / GitHub 통합본 / V35 Termux 번들의 관리자 UI.

## 원인

기존 `web/manager.js`는 연결 요청 성공 시 `#pairResult.innerHTML`에만 QR과 링크를 넣고,
그 정보를 별도 화면 상태에 저장하지 않았습니다. 서버의 `POST /api/devices/pair`는
`device.pair_created` 변경을 저장하고 WebSocket `invalidate`를 보냅니다.
관리자는 이 알림으로 상태를 조회한 뒤 `#content.innerHTML=views[tab]()`로 전체 영역을
새로 만들었습니다. 새 템플릿의 `#pairResult`는 비어 있으므로 QR과 링크가 제거되었습니다.
30초 주기 조회도 같은 전체 재렌더링을 실행하므로, 즉시 사라지지 않은 링크도 나중에
사라질 수 있었습니다. 서버의 연결 코드 생성 실패나 QR 라이브러리 설치 문제가 아닙니다.

## 수정 내용

- 연결 URL, 해당 기기 ID, 만료 시각, 입력한 이름·주소를 현재 관리자 탭 메모리에 보관.
- 표시 기기 화면 갱신 시 왼쪽 기기 목록·제어 영역만 교체. 오른쪽 QR/입력 폼은 유지.
- 관리자 내부 탭 왕복 후 유효한 QR과 폼 값 복원.
- 생성 중 버튼 비활성화와 중복 요청 차단, 탭을 떠난 뒤 도착한 응답 처리.
- 남은 시간 표시. 링크 사용/만료/연결 해제는 빈 영역 대신 명확한 상태 메시지 표시.
- 로그아웃하면 연결 정보를 메모리/화면에서 제거하고 늦은 응답을 무시.
- 새 관리자 JS 캐시 키를 HTML에 반영.

연결 토큰을 localStorage/sessionStorage에 영구 저장하지 않습니다. 브라우저 전체 새로고침,
탭 종료 후에는 새 연결 링크가 필요합니다. 관리자 내부 메뉴 이동과 전체 페이지 새로고침은 다릅니다.
10분/1회 정책은 서버에서 그대로 적용합니다. 새 링크를 만들었다고 이전 링크가 자동 취소되는 것은
아닙니다. 기존 미사용 링크를 즉시 취소하려면 해당 대기 기기를 연결 해제하세요.

## 실제로 바뀌는 파일: 2개

| 파일 | V35의 위치 | 내용 |
|---|---|---|
| `web/manager.js` | `~/room-hub/web/manager.js` | QR 상태 및 갱신 처리 |
| `web/manager.html` | `~/room-hub/web/manager.html` | JS 캐시 키 변경 |

서버 Python 코드, DB, 관리자 키, 배치, 할 일, 클라이언트, 위젯, 기존 미리보기 축소 방지 CSS,
Termux 서비스, Debian, nginx와 cloudflared 설정은 바꾸지 않습니다.

## 적용 — Windows에서 전송, V35에서 설치

### 1. Windows PowerShell (Termux가 아님)

ZIP을 Windows의 다운로드 폴더에 저장하고 실행합니다.

```powershell
scp -P 8022 "$HOME\Downloads\room_hub_pair_fix_v0.1.1.zip" "u0_a306@192.168.0.14:~/"
```

현재 전면 서버를 실행하는 창은 그대로 두고, 새 PowerShell 창에서 별도로 접속합니다.

```powershell
ssh -p 8022 u0_a306@192.168.0.14
```

### 2. V35에 SSH 접속한 Termux 셸

PRoot Debian 내부가 아니라 바깥 Termux 셸에서 실행합니다.

```bash
unzip -o "$HOME/room_hub_pair_fix_v0.1.1.zip" -d "$HOME/room-hub-pairfix"
bash "$HOME/room-hub-pairfix/apply.sh" "$HOME/room-hub"
```

정상 완료 시:

```text
Applied: Room Hub 0.1.1-pairfix1
Updated: .../room-hub/web/manager.js
Updated: .../room-hub/web/manager.html
Backup: .../room-hub-patch-backups/pairfix1-...
```

두 파일은 자동 백업됩니다. 백업 위치는 프로젝트 폴더 밖 `~/room-hub-patch-backups/`입니다.
이미 적용했으면 `Already applied`라고 표시하고 다시 교체하지 않습니다.

설치 도구는 알려진 0.1.1 파일 또는 이번 패치 파일인지 SHA-256으로 검사합니다.
직접 수정한 코드 등 해시가 다른 파일은 `STOP`으로 중단합니다. 임의로 검사를 우회해 덮어쓰지
말고 변경 내용을 비교하세요. `PATCH_MANIFEST.json`에는 원본과 새 파일의 해시가 들어 있습니다.

### 3. 서버 재시작은 필요 없음

이번에는 정적 관리자 파일만 바뀝니다. 제공된 서버는 정적 파일을 요청할 때 읽으므로
현재 `start.sh` 서버 또는 서비스는 계속 실행해 두면 됩니다. 별도 실제 서버 프로세스를 유지한
상태에서 교체하고 새 HTML/JS가 제공되는 것도 시험했습니다.

`setup.sh`나 `install-service.sh`를 다시 실행하지 마세요. 데이터 재가져오기, `.venv-v35` 삭제,
V35 재부팅도 필요하지 않습니다. 이 패치는 서비스를 신규 등록하지 않습니다.

### 4. Windows 관리자 브라우저 갱신

```text
http://192.168.0.14:8088/manager
```

관리자에서 `Ctrl+F5` 또는 `Ctrl+Shift+R`로 강력 새로고침합니다.
기존 브라우저 쿠키를 지울 필요는 없습니다. 새 JS 파일의 주소는 다음과 같습니다.

```text
/static/manager.js?v=0.1.1-pairfix1
```

다시 **표시 기기 → 새 화면 연결**에서 서버 주소를 `http://192.168.0.14:8088`로 확인하고
연결 링크를 만드세요. QR 아래에 `남은 시간 9:xx`가 나타납니다. 30초 이상 기다려도
QR이 유지되는지 확인한 뒤 iPad로 스캔하거나 링크를 열면 됩니다.

정상 연결이 확인되면 QR 대신 **화면이 연결되었습니다**가 나타납니다. 이는 정상 처리입니다.
아직 연결하지 않았는데 자동 갱신만으로 빈 화면이 되는 현상과는 다릅니다.

## 수동 교체

자동 설치를 쓰지 않으려면 위의 두 파일을 백업한 뒤 같은 경로에 복사하고 관리자 브라우저를
새로고침합니다. `manager.html`만 바꾸거나 `manager.js`만 바꾸기보다는 둘을 함께 적용하세요.

복구는 설치 로그의 백업 폴더 안 `web/manager.js`, `web/manager.html`을 원래 경로로 복사하면
됩니다. DB나 인증 키를 되돌릴 필요가 없습니다.

## 검증 및 제한

자세한 결과는 `docs/PAIRING_FIX_TEST_REPORT.md`와 함께 넣은 JSON에 있습니다.
실제 V35나 사용자 PC에 원격 접속해서 적용한 것은 아니며, 패치를 전송하고 설치해야 반영됩니다.
물리 iPad의 카메라 스캔, Safari, Termux/PRoot 재부팅까지 시험한 결과는 아닙니다.

개발자용 `scripts/pairing_regression.py`는 Room Hub 저장소의 `scripts/`로 복사하고
개발 의존성을 준비한 별도 테스트 환경에서 실행할 수 있습니다. 운영 V35에서 테스트를
실행할 필요는 없습니다. 독립 관리자 HTML 미리보기는 별도 다운로드로 제공되며 운영 파일이 아닙니다.
