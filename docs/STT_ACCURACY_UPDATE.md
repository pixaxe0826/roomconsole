# Room Hub 0.1.5 — Whisper 전사 정확도 패치

패치 ID: `room-hub-0.1.5-stt-accuracy1` · 작성: 2026-09-20

## 1. 범위

현재 작동하는 **Whisper multilingual Base / 한국어 / 6스레드**를 그대로 사용합니다. 앱 버전은 0.1.5 그대로입니다. 모델이나 엔진을 다운로드·재컴파일하지 않습니다.

바꾸는 것은 Whisper의 **요청별 디코딩 정책**입니다. 신규 기본값은 `balanced`: beam size 3, best-of 1, 짧은 한국어 어휘 힌트입니다. 마이크 권한·MediaRecorder·코덱·gain·녹음 시작/종료·FFmpeg 변환·VAD·Qwen 프롬프트·라우터·실행 권한은 바꾸지 않습니다. Temperature fallback은 기존 엔진 기본값을 유지합니다. 외부 2차 전사는 자동으로 수행하지 않습니다. 기존 temperature fallback에 의한 엔진 내부 재시도와 별도 오디오 재전사는 다릅니다.

**정확도 향상을 목표로 하는 시험 패치이며 향상을 보장하지 않습니다.** Beam과 힌트는 지연과 잘못된 문맥 편향을 증가시킬 수도 있습니다. 실제 V35와 한국어 녹음의 비교 결과로 유지 여부를 결정하세요.

## 2. 구현

| 프로필 | Beam | 힌트 | 용도 |
|---|---:|---|---|
| `legacy` | 1 | 없음 | 기존 CLI 동작으로 복귀 |
| `hint` | 1 | 고정 어휘 + 제한된 작업 제목 | 지연이 크면 비교할 가벼운 설정 |
| `balanced` | 3 | 동일 힌트 | 이번 기본값 |
| `careful` | 5 | 동일 힌트 | 추가 실험용. 자동 적용하지 않음 |

고정 어휘는 `할 일, 오늘, 내일, 남은 할 일, 추가해 줘, 완료 처리해, 완료 취소해, 보여줘.`입니다. 여기에 서버 시간대 기준 **오늘·내일의 미완료 제목**을 최대 4개 추가합니다. 전체 힌트는 220 UTF-8 bytes 이내이고, 이름 중간을 자르지 않으므로 실제 포함 개수는 더 적을 수 있습니다. 중복·긴 제목·제어문자·마크업 등은 제외하며 메모·계정·인증 키·먼 미래 기록은 넣지 않습니다.

명령문을 강제로 출력하거나 전사 결과의 단어를 사후 치환하는 기능이 아닙니다. Whisper `.txt`가 최종 전사문이며 LLM 보정은 하지 않습니다. 힌트 때문에 실제로 말하지 않은 제목이 끼어들 수 있으므로 비교가 필요합니다.

상세 결과는 `-oj`(일반 JSON)와 `-ls`(토큰 점수)를 사용합니다. `-ojf`는 토큰 타임스탬프 계산까지 켜므로 이번에는 사용하지 않습니다. 토큰 `p`, 평균 log p는 **디코더 점수이지 정답일 확률·단어 정확도·검증된 confidence가 아닙니다.** 점수만으로 결과를 선택하거나 작업을 실행하지 않습니다.

`data/stt-accuracy.json`은 별도 설정입니다. **기존 `data/speech-config.json`은 한 바이트도 바꾸지 않습니다.** 기존 model toolkit의 `WhisperRunner.transcribe()` 인터페이스도 legacy 동작을 유지합니다. 기존 `speech-model.sh compare`는 새 힌트/beam 비교 도구가 아니므로 이번 실험에는 아래 전용 비교 명령을 사용하세요.

전사 성공 시 `speech_diagnostics` 테이블에 해당 시도의 원본 결과·힌트 스냅샷·프로필·점수·시간을 저장합니다. 기존 텍스트를 나중에 수동 수정해도 진단 원본은 유지합니다. 음성 항목 삭제 시 진단도 FK cascade로 삭제됩니다. 관리자만 기존 전사 작업 API로 상세 진단을 볼 수 있고, 표시 기기에는 제목 힌트나 점수를 전달하지 않습니다. 기존 UI는 그대로입니다.

## 3. Windows에서 파일 전송

패키지 ZIP을 다운로드 폴더에 저장하고 **Windows PowerShell**에서 실행합니다.

```powershell
scp -P 8022 "$HOME\Downloads\room_hub_stt_accuracy_v0.1.5_patch.zip" "u0_a306@192.168.0.14:~/"
ssh -p 8022 u0_a306@192.168.0.14
```

이후는 **바깥 Termux 셸**입니다. Debian 안으로 직접 들어가지 않습니다.

```bash
termux-wake-lock
unzip "$HOME/room_hub_stt_accuracy_v0.1.5_patch.zip" -d "$HOME/room-hub-stt-accuracy-update"
python "$HOME/room-hub-stt-accuracy-update/apply_update.py" --target "$HOME/room-hub" --check
```

`READY: ... source/tool files; official base verified; ko/6T. No files changed.`를 확인합니다. 모델 파일 크기와 공식 base SHA256, 서버 버전, 기존 소스 해시, 활성 ko/6T를 검사합니다. 모델 읽기만 수행하며 다운로드하지 않습니다. `STOP`이면 다음 단계로 진행하지 마세요. 기존 압축 해제 폴더가 있으면 무작정 병합하지 말고 빈 폴더를 사용하세요.

## 4. Room Hub만 정지·백업·적용

진행 중·대기 중 전사를 모두 완료하거나 취소합니다. Qwen 추론도 마무리하세요.

```bash
python "$HOME/room-hub/deploy/termux/stop-room-hub.py" --root "$HOME/room-hub"
```

`STOPPED: port 8088 is closed.`를 확인합니다. 도구가 정확한 Room Hub 프로세스를 확인했지만 정지 지연이 발생한 경우에만:

```bash
python "$HOME/room-hub/deploy/termux/stop-room-hub.py" --root "$HOME/room-hub" --force
```

SSH·nginx·Cloudflare·Qwen·HTTPS 서비스는 중지하지 않습니다. 정지 후 개인 자료 백업:

```bash
umask 077
tar -czf "$HOME/room-hub-private-before-stt-accuracy-$(date +%Y%m%d-%H%M%S).tgz" -C "$HOME/room-hub" data widgets
```

이 백업은 토큰·음성을 포함할 수 있으므로 공개하거나 GitHub에 올리지 마세요.

```bash
python "$HOME/room-hub-stt-accuracy-update/apply_update.py" --target "$HOME/room-hub" --apply --server-stopped
```

`APPLIED: room-hub-0.1.5-stt-accuracy1`과 `BACKUP:` 경로를 확인합니다. 소스는 `~/room-hub-update-backups/stt-accuracy1-...`에 백업합니다. 지정 플래그만 믿지 않고 8088 정지와 V35 서버 잠금을 검사합니다. 적용 중 예외가 발생하면 이미 쓴 파일을 원래 상태로 되돌립니다. 기존 운영 데이터와 키·모델은 삭제하지 않습니다.

## 5. 실제 엔진 기능 시험

서버를 아직 시작하지 않은 상태에서:

```bash
cd "$HOME/room-hub"
bash deploy/termux/stt-accuracy.sh selftest
```

기존 Whisper 저장소의 공개 `samples/jfk.wav`로 현재 엔진의 beam3·prompt·점수 출력을 실제 실행합니다. 이때만 샘플 언어가 영어이며 **운영의 한국어 설정은 바꾸지 않습니다.** 성공 기준:

```text
STT CHECK PASSED: actual installed engine; beam3 + prompt + score output. Korean accuracy is not tested here.
```

원래 엔진/모델이 준비된 환경에서만 성공합니다. 이 검사는 한국어 인식 품질·속도 검증이 아닙니다. 샘플이 없거나 실패하면 임의 음성으로 성공한 것처럼 표시하지 않습니다. 실패 상태를 확인하거나 우선 기존 방식으로 복귀할 수 있습니다.

```bash
bash deploy/termux/stt-accuracy.sh profile legacy
sv up "$PREFIX/var/service/room-hub"
```

시험 성공 시 새 기본 프로필은 balanced입니다. 다음으로 서버를 다시 올립니다.

```bash
sv up "$PREFIX/var/service/room-hub"
sleep 3
sv status "$PREFIX/var/service/room-hub"
curl -fsS --max-time 5 http://127.0.0.1:8088/healthz
bash "$HOME/room-hub/deploy/termux/stt-accuracy.sh" status --verify
```

`healthz`는 **0.1.5**로 유지됩니다. 상태는 `ggml-base.bin`, `threads: 6`, `language: ko`, `profile: balanced`, `beam_size: 3`이어야 합니다. 기존 Manager/iPad 주소와 인증서를 그대로 사용하세요. 화면이 재연결되지 않을 때만 새로고침합니다. **재페어링·라이브러리 설치·모델 다운로드·서비스 재등록은 필요 없습니다.**

## 6. 사용과 기록

iPad에서 평소처럼 녹음·전송하면 다음 작업부터 새 프로필을 씁니다. 과거 성공한 전사문을 자동 재계산하지 않습니다. 새 결과도 기존 음성 수신함에 들어가며 LLM은 종전의 수동 전송/확인 정책을 따릅니다.

최근 진단 요약(원문·힌트는 콘솔에 출력하지 않음):

```bash
cd "$HOME/room-hub"
bash deploy/termux/stt-accuracy.sh recent
bash deploy/termux/stt-accuracy.sh recent --export
```

내보내기는 `~/room-hub/data/stt-accuracy-tests/latest-diagnostics.json`에 저장합니다. **개인 전사문과 선택한 할 일 제목이 포함됩니다.** 공유 전 내용을 확인하세요.

현재 UI는 별도 진단 패널을 추가하지 않았습니다. 관리자 API에서는 기존 `GET /api/speech/jobs/{id}`의 `stt_diagnostics`로도 확인할 수 있습니다. 표시 기기는 이 필드를 받지 않습니다.

## 7. 같은 음성으로 비교 — 권장

성공/실패했던 원본 녹음을 수신함에서 삭제하지 않은 상태로 보존합니다. 진행 중·대기 중 전사를 정리하고 4절의 정지 명령으로 Room Hub만 정지합니다. Qwen은 켜 두어도 되지만 비교 중 새 요청을 보내지 마세요.

```bash
cd "$HOME/room-hub"
bash deploy/termux/stt-accuracy.sh compare --latest --profiles legacy,hint,balanced --runs 1
```

같은 마지막 음성 파일을 **기존 beam1/힌트 없음 → beam1/힌트 → beam3/힌트**로 처리합니다. 오늘·내일의 힌트 스냅샷은 비교 시작 시 한 번만 고정합니다. 결과는 수신함이나 LLM에 보내지 않고 별도 보고서에 저장합니다. 음성/모델/엔진 해시, 원본 결과, 프로필별 변환·로딩 포함 추론시간·RTF·점수를 기록합니다.

더 넓은 탐색도 시험할 수 있습니다.

```bash
bash deploy/termux/stt-accuracy.sh compare --latest --profiles balanced,careful --runs 1
```

`--runs 2`는 다음 회차의 순서를 역전합니다. 단발 시간은 캐시·온도·순서 영향을 받습니다. 비교가 끝나거나 취소되면 서버를 다시 시작하세요.

```bash
sv up "$PREFIX/var/service/room-hub"
```

**Windows PowerShell**에서 보고서 받기:

```powershell
scp -P 8022 "u0_a306@192.168.0.14:~/room-hub/data/stt-accuracy-tests/latest-compare.zip" "$HOME\Downloads\"
```

압축을 풀고 `report.html`을 엽니다. 정확한 정답 문장 파일을 별도로 지정하지 않았다면 CER는 계산하지 않습니다. 모델 점수를 정확도 점수로 대신하지 않습니다.

정답 파일은 UTF-8로 V35 프로젝트의 `data/`에 두고 비교 명령에 `--reference-file /opt/room-hub/data/stt-reference.txt`를 추가합니다. 이 경로는 명령이 실제로 실행되는 PRoot 경로입니다. CER는 NFC/casefold 후 공백·문장부호를 제외한 문자 편집거리/정답 문자수이며, 삽입 오류가 많으면 1을 넘을 수 있습니다. 날짜/부정/숫자 오류도 직접 확인하세요.

## 8. 너무 느리거나 품질이 나빠지면

진행 중 작업이 끝난 뒤 아래 중 하나를 사용합니다. **설정만 바꾸는 경우 서버 재시작은 필요 없습니다.** 작업이 시작할 때 설정을 고정합니다.

```bash
# Beam1 + 같은 문맥 힌트만 유지
bash "$HOME/room-hub/deploy/termux/stt-accuracy.sh" profile hint

# 기존 Beam1, 힌트 없음으로 복귀
bash "$HOME/room-hub/deploy/termux/stt-accuracy.sh" profile legacy

# Beam3로 재설정
bash "$HOME/room-hub/deploy/termux/stt-accuracy.sh" profile balanced

# 제목 문맥이 오히려 왜곡하는지 분리 시험: 고정 어휘만
bash "$HOME/room-hub/deploy/termux/stt-accuracy.sh" profile balanced --task-titles off
```

`--task-titles on`으로 다시 켤 수 있습니다. 별도 옵션 없이 프로필만 바꾸면 제목 포함 설정은 유지합니다. 어떤 프로필이든 Base·6스레드는 유지합니다. 이전 `speech-model.sh restore`는 모델/스레드까지 다른 설정으로 복원할 수 있으므로 이번 목적에는 사용하지 않습니다.

소스 전체 되돌리기는 서버 정지 후, 설치 시 출력된 실제 백업 폴더로 실행합니다.

```bash
python "$HOME/room-hub-stt-accuracy-update/apply_update.py" --target "$HOME/room-hub" --rollback "$HOME/room-hub-update-backups/stt-accuracy1-실제번호" --server-stopped
sv up "$PREFIX/var/service/room-hub"
```

패치 후 수정된 파일/프로필이 있으면 무조건 덮어쓰지 않고 중단합니다. 이때 단순 동작 복귀에는 `profile legacy`가 적합합니다. 진단 테이블은 남겨 두며 모델/DB/키는 되돌리기에서도 지우지 않습니다.

## 9. 무엇을 포함하지 않았나

- 마이크 프로필, 녹음 품질·입력 필터, VAD 변경: 없음.
- LLM 문장 복구, fuzzy matching, 새로운 명령 자동 실행: 없음.
- 자동 2-pass 및 후보 자동 채택: 없음. 먼저 비교하여 실제 오류율과 지연을 확인합니다.
- Beam3가 반드시 빠르거나 정확하다는 보장: 없음.
- 실제 모델 가중치·사용자 DB·토큰·녹음·인증서: 패키지에 없음.

## 10. GitHub 반영

패키지 `files/` 아래를 저장소의 같은 경로로 반영합니다. 버전 번호는 0.1.5로 유지합니다. 운영 `data/stt-accuracy.json`, 진단 보고서·비교 ZIP·백업·모델은 커밋하지 마세요. 문서와 테스트는 소스에 포함할 수 있습니다.

## 11. 기술 근거

현재 엔진인 whisper.cpp v1.8.3 CLI 소스에서 `--beam-size`, `--prompt`, `--output-json`, `--log-score`, beam>1의 beam-search 선택과 token timestamp 경로를 확인했습니다.

- https://github.com/ggml-org/whisper.cpp/blob/v1.8.3/examples/cli/cli.cpp
- https://github.com/ggml-org/whisper.cpp/blob/v1.8.3/include/whisper.h
- https://github.com/openai/whisper/blob/main/whisper/transcribe.py

제작 환경에서는 모델 다운로드가 DNS 오류로 실패했습니다. 기능 시험의 인식 출력은 모의 CLI이고 음성은 합성 파형입니다. 실제 V35에서 기존 모델을 실행하는 5절의 시험과, 한국어 같은 파일 비교를 통해 확인하세요.
