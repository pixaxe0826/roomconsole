# Room Hub 0.1.4 → 0.1.5 업데이트

대상: V35 Termux의 `~/room-hub`, main8088 / HTTPS8443 / SSH8022 / Qwen localhost8090. 기존 0.1.4 + LLM 위젯 포함본에서 적용합니다. 모델은 Base/6T와 Qwen3-0.6B/6T를 유지합니다. 실제 운영 데이터·인증서·모델은 이 ZIP에 없습니다.

## 1. Windows PowerShell

패치 ZIP을 다운로드 폴더에 저장한 뒤:

```powershell
scp -P 8022 "$HOME\Downloads\room_hub_assistant_update_v0.1.5_patch.zip" "u0_a306@192.168.0.14:~/"
ssh -p 8022 u0_a306@192.168.0.14
```

## 2. 이후는 V35 바깥쪽 Termux에서

Debian으로 직접 들어가지 않습니다. 원본을 먼저 검사합니다. 기존 같은 이름의 압축 해제 폴더가 있다면 내용을 확인하고 새 빈 폴더를 사용하세요.

```bash
termux-wake-lock
unzip "$HOME/room_hub_assistant_update_v0.1.5_patch.zip" -d "$HOME/room-hub-update-0.1.5"
python "$HOME/room-hub-update-0.1.5/apply_update.py" --target "$HOME/room-hub" --check
```

`READY: ... No files changed.`이면 진행합니다. `STOP`이면 진행하지 마세요. 예상과 다른 수정본을 강제로 덮어쓰지 않습니다. 패키지 파일을 기존 루트로 임의 복사하는 방법보다 검사를 권합니다.

## 3. 요청을 끝내고 Room Hub만 중지

진행/대기 중인 전사와 LLM 요청을 끝내거나 취소하세요. **Qwen room-hub-llm, SSH, nginx, cloudflared, HTTPS는 중지하지 않습니다.**

```bash
python "$HOME/room-hub/deploy/termux/stop-room-hub.py" --root "$HOME/room-hub"
```

`STOPPED: port 8088 is closed.`를 확인합니다. 정확히 확인한 Room Hub 프로세스가 종료되지 않는다는 경우에만 `--force`를 추가합니다. 다른 프로세스를 pkill하지 마세요.

정지 상태에서 개인 백업:

```bash
umask 077
tar -czf "$HOME/room-hub-private-before-0.1.5-$(date +%Y%m%d-%H%M%S).tgz" -C "$HOME/room-hub" data widgets
```

인증 키/전사문/TLS 키가 들어 있는 백업이므로 GitHub·공개 Drive 링크로 공유하지 않습니다.

## 4. 적용 및 재시작

```bash
python "$HOME/room-hub-update-0.1.5/apply_update.py" --target "$HOME/room-hub" --apply --server-stopped
sv up "$PREFIX/var/service/room-hub"
sleep 3
sv status "$PREFIX/var/service/room-hub"
curl -fsS --max-time 5 http://127.0.0.1:8088/healthz
```

성공 표시 `APPLIED: room-hub-0.1.5-assistant1`; healthz `{"status":"ok","version":"0.1.5"}`. 시작이 늦으면 잠시 기다렸다 다시 검사합니다.

```bash
tail -n 80 "$PREFIX/var/log/sv/room-hub/current"
```

소스 자동 백업은 `~/room-hub-update-backups/`에 생성되며, 출력된 BACKUP 경로를 기록하세요. data/ 전체 백업을 대신하지 않습니다.

**새 의존성 설치, setup.sh, install-speech.sh, Qwen setup/enable, 서비스 재등록, 모델 다운로드는 필요 없습니다.** Qwen runtime.py를 바꾸지 않으므로 기존 실제 엔진 시험 영수증을 유지합니다. 기존 모델 관리 도구의 허용 앱 버전 검사만 0.1.5를 허용하도록 갱신합니다. 스레드·모델·LLM 연결 및 생성 설정은 그대로입니다.

## 5. 화면 갱신

Windows의 `http://192.168.0.14:8088/manager`에서 **Ctrl+F5**. iPad의 기존 `https://192.168.0.14:8443/client`를 새로고침합니다. 유효한 기존 쿠키가 유지되면 재페어링·인증서 재설치는 없습니다. 주소와 사이트 데이터를 지울 필요도 없습니다.

## 6. 기능 확인

관리자 새 요청 모드가 `자동 분기 · 조회 / 변경 확인 / 대화`인지 확인하세요.

- `오늘 할 일에 택배 보내기 추가해?`: 날짜·제목 미리보기와 **확인한 내용 실행** 버튼. 누르기 전에는 변경 없음. 누르면 실제 추가되어 할 일 위젯 갱신.
- `오늘 남은 할 일 확인해서 보고해줘.`: 실제 DB 목록. LLM 토큰 `—`는 정상(규칙/서버 경로).
- `오늘 할 일 전부 완료 처리해`: 대상 목록을 확인하고 실행/취소. 시험용 항목으로 확인 권장.
- `저녁메뉴 추천해 줘.`: 별도 일반 대화 프롬프트로 실제 Qwen에 전송. 답변과 실제 토큰 통계.

0.1.4의 과거 '권한 없음' 응답은 기록 보존 목적상 그대로입니다. 개선 결과는 **auto로 새 요청**해 확인합니다. 기존 미전송 기록의 저장된 입력 전송은 과거 요청 그대로입니다.

관리자 화면에서만 신규 변경을 승인합니다. iPad LLM 위젯은 원문·최종 응답·실행 결과 조회 역할을 유지합니다. 기존 iPad 할 일 완료 버튼은 그대로 동작합니다.

## 7. 선택: 기존 Qwen에서 프로브

서버와 Qwen이 동작하며 다른 전사가 없는 때:

```bash
cd "$HOME/room-hub"
bash deploy/termux/assistant-selftest.sh
```

출력은 `data/assistant-model-probe.json`. 합성 입력 2개를 실제 Qwen에 보냅니다. 실제 사용자 DB/전사문은 읽지 않고, 작업이나 설정도 바꾸지 않습니다. 불명확한 모델 출력이면 FAIL/REVIEW로 보고합니다. 규칙 기반 조회·확인 기능과는 독립된 품질 시험입니다. 이 프로브는 이 패키지 제작 환경에서 실제 Qwen으로 실행하지 못했습니다.

## 8. 되돌리기

Room Hub만 정지하고:

```bash
python "$HOME/room-hub-update-0.1.5/apply_update.py" --target "$HOME/room-hub" --rollback "실제_BACKUP_폴더_경로" --server-stopped
sv up "$PREFIX/var/service/room-hub"
```

모델·Qwen 서비스·인증서는 그대로이며 소스만 되돌립니다. **이미 확인 실행한 할 일 변경은 롤백 명령으로 취소되지 않습니다.** 이전 데이터 백업 복원은 그 이후 생긴 데이터도 잃을 수 있으므로 별도로 판단하세요. 추가 테이블은 그대로 남고 구버전은 이를 사용하지 않습니다.

## 9. GitHub

패치의 `files/` 내용을 저장소 같은 경로에 병합하거나 전체 0.1.5 소스 ZIP을 비교 후 반영합니다. `data/`, runtime 모델/바이너리, 인증 키, 개인 백업은 제외합니다. 새 확장에 대한 문서는 [ASSISTANT.md](ASSISTANT.md), 검증 범위는 [ASSISTANT_TEST_REPORT.md](ASSISTANT_TEST_REPORT.md)입니다.
