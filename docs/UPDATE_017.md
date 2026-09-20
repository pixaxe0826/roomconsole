# 0.1.7 메모·알람 업데이트 / V35 적용·롤백

대상은 기존 **Room Hub 0.1.6**입니다. 패치 ID는 `room-hub-0.1.7-notes-alarms1`입니다. 새 설치 ZIP을 운영 폴더 전체에 덮어쓰지 마세요. 먼저 현재 `/healthz`가 0.1.6인지 확인합니다. 다른 버전이나 알 수 없는 소스 수정은 이 업데이트 도구가 중단합니다.

## 1. Windows PowerShell에서 ZIP 전송

아래 IP/사용자명은 이전에 사용한 값입니다. V35 주소가 바뀌었다면 두 명령에 현재 값을 적용하세요.

```powershell
scp -P 8022 "$HOME\Downloads\room_hub_notes_alarms_v0.1.7_patch.zip" "u0_a306@192.168.0.14:~/"
ssh -p 8022 u0_a306@192.168.0.14
```

이후는 **V35 바깥쪽 Termux 셸**입니다. Debian 안에서 실행하지 않습니다. 기본 프로젝트 위치는 `~/room-hub`, DB/설정은 그 안의 `data/`입니다. 별도의 HUB_DATA_DIR를 사용한다면 바깥쪽 셸에서도 실제 접근 가능한 절대 경로를 지정하고 그 디렉터리도 별도로 백업하세요. 아래 기본 data/ 백업을 사용자 지정 DB의 백업으로 착각하지 마세요.

## 2. 현재 상태 확인과 사전 검사

```bash
termux-wake-lock
curl -fsS --max-time 5 http://127.0.0.1:8088/healthz
bash "$HOME/room-hub/deploy/termux/stt-accuracy.sh" status --verify
unzip "$HOME/room_hub_notes_alarms_v0.1.7_patch.zip" -d "$HOME/room-hub-update-0.1.7"
python "$HOME/room-hub-update-0.1.7/apply_update.py" --target "$HOME/room-hub" --check
```

압축 해제 대상은 새 빈 폴더여야 합니다. `READY`면 알려진 이전 소스와 파일 해시가 일치합니다. `STOP`이면 강제로 덮어쓰지 마세요. CRLF 개행은 허용하지만 사용자 소스 수정은 무시하지 않습니다. 이 단계에서는 파일과 DB를 변경하지 않습니다.

Whisper Base/한국어/6T/careful Beam5, Qwen/8090, HTTPS/8443은 재설정하지 않습니다. 업데이터는 설정 파일 해시만 전후 확인하며 가중치를 읽거나 다운로드하지 않습니다. 모델/엔진/마이크 코드도 교체하지 않습니다.

## 3. Room Hub만 정지하고 개인 데이터 백업

진행 중인 녹음·STT·LLM 요청을 마무리하고 새 요청을 잠시 멈춥니다.

```bash
python "$HOME/room-hub/deploy/termux/stop-room-hub.py" --root "$HOME/room-hub"
```

`STOPPED: port 8088 is closed.` 확인 후:

```bash
umask 077
tar -czf "$HOME/room-hub-private-before-0.1.7-$(date +%Y%m%d-%H%M%S).tgz" -C "$HOME/room-hub" data widgets
```

이 파일은 **개인 DB·키·녹음·인증서를 포함할 수 있습니다. GitHub에 올리지 마세요.** 백업 실패 시 적용하지 않습니다. SSH, 기존 8080 홈페이지, HTTPS 서비스, Qwen 서비스는 중지하지 않습니다. `pkill python`, `pkill proot` 같은 일괄 종료를 사용하지 마세요.

## 4. 적용하고 다시 시작

```bash
python "$HOME/room-hub-update-0.1.7/apply_update.py" --target "$HOME/room-hub" --apply --server-stopped
sv up "$PREFIX/var/service/room-hub"
sleep 3
sv status "$PREFIX/var/service/room-hub"
curl -fsS --max-time 5 http://127.0.0.1:8088/healthz
bash "$HOME/room-hub/deploy/termux/stt-accuracy.sh" status --verify
```

`APPLIED: room-hub-0.1.7-notes-alarms1`과 `BACKUP: ...` 경로를 보관하세요. 이미 동일하게 적용되었으면 `ALREADY APPLIED`입니다. 정상 health 응답의 version은 `0.1.7`입니다. 첫 시작 때 새 메모·알람 테이블만 추가합니다.

새 pip 패키지 설치, 모델 다운로드, Whisper/Qwen 재컴파일, 인증서 재설치, nginx/Tasker/runit 재등록은 필요 없습니다. 운영 패치는 필요한 프로그램 파일과 새 문서만 교체합니다. 전체 소스 ZIP에 있는 compose/배포 안내 변경을 운영 서버에 재배포할 필요는 없습니다. 서버 로그가 필요할 때만 다음으로 확인하고, 로그를 공유하기 전 개인 정보를 지우세요.

```bash
tail -n 80 "$PREFIX/var/log/sv/room-hub/current"
```

## 5. 화면과 위젯 설정

Windows의 기존 `/manager`에서 **Ctrl+F5**를 누릅니다. 왼쪽에 **메모**, **알람** 메뉴가 생깁니다. iPad는 기존 HTTPS `/client`를 새로고침합니다. 기존 쿠키가 유효하면 재페어링할 필요가 없습니다.

관리자 **위젯과 배치**에서 메모와 알람을 추가하고 저장합니다. 기본 크기는 각각 4×2입니다. 기존 배치를 자동으로 밀거나 초기화하지 않으므로 빈 공간이 없으면 위젯 크기/행 수를 조절하세요. 기존 메모 위젯은 같은 `note` ID로 업그레이드됩니다.

메모를 작성해 저장한 뒤 **iPad에 표시**를 켠 메모만 화면에서 확인하세요. 비공개 메모는 관리자에서만 보입니다. 알람을 2~3분 뒤로 예약하고 실제 iPad에서 **소리 허용·테스트**를 누른 다음 테스트 음과 기기 음량을 확인하세요. 화면을 열어 둔 채 울림 → 5분 미루기 → 확인·종료까지 확인합니다. 새로고침하면 다시 소리를 허용해야 합니다.

이것은 열린 대시보드용 알람입니다. **화면 잠금·Safari 종료·오프라인·V35 절전 중의 시스템 알람/푸시는 아닙니다.** 음성으로 메모나 알람을 등록하는 도구도 이번에는 추가하지 않았습니다. [기능과 시간 정책](NOTES_ALARMS.md)

## 6. 되돌리기

Room Hub만 다시 정지하고 적용 당시 출력된 **실제 BACKUP 경로**를 사용합니다. 다음 `실제_BACKUP_폴더`를 그대로 복사하지 마세요.

```bash
python "$HOME/room-hub/deploy/termux/stop-room-hub.py" --root "$HOME/room-hub"
python "$HOME/room-hub-update-0.1.7/apply_update.py" --target "$HOME/room-hub" --rollback "$HOME/room-hub-update-backups/실제_BACKUP_폴더" --server-stopped
sv up "$PREFIX/var/service/room-hub"
curl -fsS --max-time 5 http://127.0.0.1:8088/healthz
```

프로그램 파일만 이전 바이트/권한으로 복원합니다. 새 메모·알람 테이블과 사용자가 저장한 데이터는 삭제하지 않습니다. **0.1.6으로 돌아간 동안 새 알람 스케줄러는 실행되지 않습니다.** 재업데이트 시 늦은 예약 처리 정책을 적용하므로 기본 시스템 알람을 별도로 유지하세요. 업데이트 후 파일을 별도로 수정했다면 롤백은 중단합니다. 전원 중단 시 남은 `installing` 영수증과 백업을 보존하고 검사 후 같은 롤백 명령을 사용합니다.

## 개발자 재현

전체 소스에는 `scripts/build_life_package.py`, `scripts/apply_life_update.py`, 관련 단위/브라우저 테스트가 있습니다. 운영 V35에 개발 의존성을 설치할 필요는 없습니다. 이 패키지는 [검증 기록](TEST_REPORT_017.md)에 적힌 환경에서 검사했으며 물리 V35/iPad의 소리·온도·절전은 별도 확인 사항입니다.
