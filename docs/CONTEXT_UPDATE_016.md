# Room Hub 0.1.6 업데이트 — Base / 6T / Beam 5 보존

대상: 기존 0.1.5 + STT 정확도 패치, Qwen runtime 연결, iPad HTTPS 운영 환경.
패치 ID: `room-hub-0.1.6-grounded-context1`.

Whisper `app/speech.py`, `app/stt_accuracy.py`, 음성 입력 Client, 모델, `data/speech-config.json`, `data/stt-accuracy.json`을 교체하지 않습니다. 원본검사는 원문 파일의 알려진 해시와 비교하며 Git/Windows의 CRLF는 수용합니다. 알 수 없는 로컬 수정은 강제로 덮어쓰지 않습니다.

## 1. Windows에서 전송

ZIP을 다운로드 폴더에 저장하고 **Windows PowerShell**에서:

```powershell
scp -P 8022 "$HOME\Downloads\room_hub_context_update_v0.1.6_patch.zip" "u0_a306@192.168.0.14:~/"
ssh -p 8022 u0_a306@192.168.0.14
```

이후 명령은 **V35 바깥쪽 Termux 셸**에서 실행합니다. Debian에 직접 들어가지 않습니다.

## 2. 현재 설정 확인과 압축 해제

```bash
termux-wake-lock
bash "$HOME/room-hub/deploy/termux/stt-accuracy.sh" status --verify
```

`ggml-base.bin / language ko / threads 6 / profile careful / beam_size 5`인지 확인합니다. 이 명령과 패치는 모델·스레드·프로필을 바꾸지 않습니다.

```bash
unzip "$HOME/room_hub_context_update_v0.1.6_patch.zip" \
  -d "$HOME/room-hub-update-0.1.6"
python "$HOME/room-hub-update-0.1.6/apply_update.py" \
  --target "$HOME/room-hub" --check
```

같은 압축 해제 폴더가 있다면 이전 자료인지 확인하고 새 빈 폴더에 풉니다. `READY`와 `STT: ... checked: true ... threads:6 ... beam:5` 확인 후 진행합니다. 이때는 서버를 켜 두어도 됩니다. `STOP`이면 적용하지 마세요. 소스 checkout처럼 설정이 없는 곳에서는 `checked:false`로 표시하지만 **현재 V35 운영 폴더에서라면 경로를 다시 확인**하세요. 사용자 지정 HUB_DATA_DIR라면 이 도구를 실행하는 셸에도 동일한 절대 경로를 지정해야 합니다. 키를 보내지 않습니다.

## 3. Room Hub만 정지, 데이터 백업

진행 중/대기 중인 STT·LLM 요청을 끝내거나 취소합니다. 새 녹음은 잠시 멈춥니다.

```bash
python "$HOME/room-hub/deploy/termux/stop-room-hub.py" --root "$HOME/room-hub"
```

`STOPPED: port 8088 is closed.`를 확인합니다. 도구가 정확한 Room Hub PID를 확인했는데 정지 지연인 경우에만 `--force`를 추가해 다시 실행합니다.

```bash
python "$HOME/room-hub/deploy/termux/stop-room-hub.py" --root "$HOME/room-hub" --force
```

SSH·nginx·cloudflared·HTTPS8443·`room-hub-llm`은 중지하지 않습니다. `pkill python/proot` 같은 전체 종료는 하지 않습니다.

정지 상태에서:

```bash
umask 077
tar -czf "$HOME/room-hub-private-before-0.1.6-$(date +%Y%m%d-%H%M%S).tgz" \
  -C "$HOME/room-hub" data widgets
```

백업이 정상 완료된 뒤 진행합니다. 이것은 DB·키·전사문·인증서를 포함하는 **개인 백업**입니다. GitHub/공개 링크에 업로드하지 마세요.

## 4. 적용과 재시작

```bash
python "$HOME/room-hub-update-0.1.6/apply_update.py" \
  --target "$HOME/room-hub" --apply --server-stopped
```

`APPLIED: room-hub-0.1.6-grounded-context1`, `BACKUP: ...`가 표시됩니다. 기존 소스는 `~/room-hub-update-backups/`에 보존합니다. 이미 적용됐다면 `ALREADY APPLIED`. 설정 파일의 SHA도 전후 비교합니다.

```bash
sv up "$PREFIX/var/service/room-hub"
sleep 3
sv status "$PREFIX/var/service/room-hub"
curl -fsS --max-time 5 http://127.0.0.1:8088/healthz
```

정상 `{"status":"ok","version":"0.1.6"}`. 시작 지연이면 조금 기다려 다시 확인합니다. 계속 실패하면:

```bash
tail -n 80 "$PREFIX/var/log/sv/room-hub/current"
```

새 패키지 설치·pip install·Whisper/Qwen 재컴파일·runtime setup·모델 다운로드·nginx 변경은 없습니다. `.env`, 모델, 인증서, 서비스, Tasker는 그대로 둡니다. 데이터베이스 스키마는 변경하지 않습니다.

마지막 STT 보존 확인:

```bash
bash "$HOME/room-hub/deploy/termux/stt-accuracy.sh" status --verify
```

## 5. Manager와 Client

Windows: `http://192.168.0.14:8088/manager`에서 Ctrl+F5. iPad: 기존 `https://192.168.0.14:8443/client` 새로고침(클라이언트 코드 자체 변경 없음). 재페어링·인증서 설치·토큰 삭제는 필요 없습니다. 기존 쿠키 만료 시에만 평소 로그인/연결 절차를 사용하세요.

‘자동 분기 · 조회 / 변경 확인 / 대화’를 선택합니다. 과거 응답은 덮어쓰지 않습니다. ‘현재 설정으로 새 요청’ 또는 수신함의 새 전송을 사용하세요. **일반 대화/기존 직접 전송 모드는 DB 도구를 실행하지 않는 의도적인 모드**입니다.

## 6. 확인할 요청

- `오늘, 남은 할 일 확인해` → pending만, 실제 DB 개수, 모델 미호출.
- `내일 뭐 해야 돼?` → 내일 미완료 목록. 임의 생활 계획 생성 아님.
- `내일 할 일 브리핑해 줘` → 내일 전체 목록. 미완료만 원하면 ‘남은’을 포함.
- `9월 21에 남은 일 알려줘` → 기준 연도 9월21일, 실제 목록.
- `지금 몇 시야?` → V35 OS의 현재 한국 시각.
- `오늘 할 일에 016 시험 추가해` → 승인 전 미등록, 관리자 승인 후 반영.
- `저녁 메뉴 추천해 줘` → Qwen 일반 대화, 새 128토큰/샘플링 프로필. 정확도/내용은 실제 모델에서 확인 필요.
- `메모 저장해 줘` / `알람 설정해` → 미지원 안내. 성공처럼 응답하지 않음.

관리자에서 요청 로컬 기준일, 원문→해석 날짜, `room_hub_sqlite`, pending/all, count/returned, 조회 시각을 확인합니다. 로그의 UTC와 현지 시각을 같이 표시하며, UTC 날짜만 보고 현지 기준 날짜가 틀렸다고 해석하지 마세요.

선택 실제 Qwen 합성 입력 검사(개인 DB 변경 없음):

```bash
cd "$HOME/room-hub"
bash deploy/termux/assistant-selftest.sh
```

결과 `data/assistant-model-probe.json`. 기본 응답·형식 확인이지 전체 한국어 정확도 보장은 아닙니다.

## 7. 되돌리기

먼저 Room Hub를 정지합니다. 적용 로그의 **실제 BACKUP 경로**를 사용하세요. 다음 경로는 예시이며 그대로 복사하지 않습니다.

```bash
python "$HOME/room-hub-update-0.1.6/apply_update.py" \
  --target "$HOME/room-hub" \
  --rollback "$HOME/room-hub-update-backups/실제_BACKUP_폴더" \
  --server-stopped
sv up "$PREFIX/var/service/room-hub"
```

소스만 복원합니다. 새로 실행/승인된 실제 작업은 취소되지 않으며 기존 DB·모델·STT 설정을 덮어쓰지 않습니다. 이후 소스를 직접 바꿨거나 백업 해시가 다르면 롤백을 중단합니다. 새 모듈만 제거하고 원래 파일을 복원하므로 기존 음성 정확도 패치도 유지합니다.

## 8. GitHub

패치의 `files/` 내용만 저장소의 같은 상대 경로에 병합하거나 전체 `room_hub_github_v0.1.6.zip`을 별도 폴더에서 검토 후 커밋합니다. 운영 V35에 전체 ZIP을 새 설치하지 마세요. `data/`, 키, `.env`, `runtime/`, 모델, 녹음, 개인 백업은 업로드 금지입니다. installer는 기존 개인 설정을 읽어 검사할 뿐 저장소에 포함하지 않습니다.

[변경 상세](CONTEXT_016.md) · [검증](CONTEXT_TEST_REPORT_016.md)
