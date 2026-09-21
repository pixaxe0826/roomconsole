# 명확한 요청은 Rule, 불명확한 요청은 제한된 LLM

기준: PR #15 병합 main `5e00a3cceaa28d2d945ffcd164aaafe951fbbeb4`.
앱 버전 0.1.7, Protocol 1.0 유지. DB migration/추가 dependency/모델 변경 없음.

## 경로

- **EXACT**: 기존 메모·할 일·일정 읽기는 Fast-path 유지. 명확한 알람 목록/생성/ID 취소는 서버가 WidgetRequest를 만들며 모델을 호출하지 않습니다.
- **CANDIDATE**: 도메인이 미확정인 내용 읽기는 `memo.read/todo.list/calendar.list/alarm.list`만 제시합니다. 도메인이 있고 동작 표현만 낯설면 그 도메인의 capability만 제시합니다. 모델은 JSON 제안 또는 명시적 null abstention을 반환합니다.
- **UNSAFE / 누락**: 부정·조건·인용·복합 요청, 임의의 시간/ID 복구는 실행하지 않습니다. 필수 날짜/시간/대상이 누락된 명확한 알람은 모델로 지어내지 않고 다시 묻습니다.

후보 선정과 실행 권한은 별개입니다. 동작이 불명확한 메모 요청의 모델이 쓰기/삭제를 선택해도 원문에 해당 변경 동작의 근거가 없으면 확인 미리보기조차 만들지 않습니다. 모델은 실제 본문을 생성하지 않으며 Adapter가 읽은 값만 최종 출력합니다. 원문 전사는 교체하지 않습니다.

### 재현 입력

| 입력 | 경로 / 결과 |
|---|---|
| 현재 메론 내용 읽어줘. | CANDIDATE → 제한된 읽기 Proposal → 실제 MemoAdapter 또는 명확화 |
| 현재 메모내용 그리핑 해줘. | CANDIDATE → memo 후보만 → 실제 조회 또는 명확화 |
| 현재 메모 고리따 해줘. | CANDIDATE → memo 후보만; 모델이 변경을 추측해도 원문 근거 검사에서 거부 |
| 현재 메모 내용 읽어줘. | 기존 FAST_PATH, 모델 0회 |
| 오늘 오후 일정 확인해줘. | 기존 FAST_PATH, 모델 0회 |
| 내일 아침 7시 25분 알람 맞춰. | EXACT, 07:25/내일, 모델 0회, 관리자 확인 대기 |
| 내일 하늘에 우유 사기 추가해. | 기존 제한된 LLM fallback 유지 |
| 내일 7시 25분 알람 맞춰. | 오전/오후 재질문, 변경 없음 |

모의 LLM으로 경로와 검증을 테스트하는 것이며 실제 Qwen이 모든 오전사를 정확히 해석한다는 보장이 아닙니다. `메론`을 `메모`로 전역 치환하지 않습니다. 모델은 null로 의도 판단을 포기할 수 있습니다. 필수값과 숫자를 복구할 수 없으면 다시 묻는 것이 정상입니다.

## 알람 연결 범위

AlarmAdapter는 기존 `LifeService.create_alarm()`과 `delete_alarm()`을 사용합니다.
`set`은 기존 미래 시각/DST/개수 제한 검사를 재사용하며 확정된 서버 timezone으로 저장합니다. label을 말하지 않았을 때의 서버 기본 이름은 `알람`이며 미리보기에 표시됩니다.

`cancel`은 **알람 ID와 버전에 묶인 예약 삭제**입니다. `알람 ID <실제 ID> 취소해줘`를 지원하고 ID 없는 취소는 묻습니다. 기존 이벤트도 기존 서비스의 삭제 계약을 따릅니다. 시간/제목의 fuzzy 검색으로 임의의 알람을 취소하지 않습니다.

항상 기존 관리자 confirmation/digest/receipt를 거칩니다. HTTP context의 user_confirmed=true는 승인 권한이 아닙니다. Rule-based라는 이유로 음성 입력이 DB를 즉시 변경하지 않습니다. 예약 생성 후 실제 소리가 났다고 주장하지 않습니다.

이는 **Room Hub 활성 브라우저 웹 알람**입니다. Android OS 알람/잠금 화면 깨우기/백그라운드·오프라인 소리를 추가한 것이 아닙니다. iPad 페이지에서 소리를 허용해야 합니다.

Bridge 계약은 widget-assistant-2입니다. 업그레이드 전에 만든 확인 대기 요청은 원문으로 새 요청을 만들어 확인하세요. 기존 완료된 기록은 보존합니다. 후보/실행 판단은 기존 상세 패널의 confidence, route_reason, widget_trace에 표시합니다.

## Windows 한 파일 업데이트

[deploy/windows/V35_Update.bat](../deploy/windows/V35_Update.bat)는 제공된 V35ssh.bat의 다음 값을 유지합니다.

```
HOST=192.168.0.14
PORT=8022
USER=u0_a306
```

**사용자가 PR을 검토·Merge한 뒤 BAT 한 파일을 실행**합니다. 원래의 V35ssh.bat은 대화형 SSH 접속용으로 그대로 두어도 됩니다. BAT는 .ps1이나 별도의 로컬 저장소가 필요 없으며 Windows OpenSSH Client와 기본 PowerShell을 사용합니다.

1. SSH 연결 가능 여부 확인(5초 제한).
2. 기존 인증으로 `ssh -T ... bash -s` 접속. 비밀번호나 첫 호스트 키 확인이 필요하면 정상적으로 묻습니다.
3. 운영 저장소가 main/clean/올바른 origin인지 확인하고 origin/main fetch.
4. **fetch한 Git commit의 updater와 supervisor**를 임시 폴더에 추출. 기존 tracked 운영 코드를 직접 수정하지 않습니다. 이전 updater를 실행한 채 스스로 덮어쓰는 문제를 피합니다.
5. 해당 merge commit의 **main push CI 성공을 확인**합니다. 진행 중이면 서비스 정지 없이 최대 10분 기다립니다. 실패·취소·API 오류·시간초과면 배포하지 않습니다. 공개 GitHub API만 사용하며 토큰을 저장하지 않습니다.
6. 기존 updater의 정상 절차: SQLite backup → Git fast-forward → 검사 → 필요한 V35 의존성만 적용 → 재시작 → health 확인. 새 main이 CI 확인 후 바뀌면 서비스 정지 전에 중단합니다.
7. HEAD/tree/clean과 `/healthz` status/version, 실제 room-hub runit **run PID가 3회 유지**되는지 확인합니다. 로그 서비스의 run 상태만 보고 성공하지 않습니다.
8. `~/room-hub-git-backups/verified-<ID>.json` 결과 보관 → SSH 종료 → 성공 시 5초 뒤 창 종료. 실패는 종료코드 1과 함께 창을 남깁니다.

비밀번호 인증이면 연결 시 비밀번호 한 번은 입력해야 합니다. 완전 무인 실행은 기존 SSH 키와 ssh-agent 설정이 되어 있을 때 가능합니다. 키를 생성·복사하거나 비밀번호를 BAT에 넣지 않으며 호스트 키 검증도 끄지 않습니다. 다른 VPN/LAN이 없으면 이 사설 주소는 외부 모바일망에서 바로 접근되지 않습니다.

네트워크가 끊겼다고 원격 업데이트까지 중단됐다는 뜻은 아닙니다. 실패하면 V35에서 상태를 먼저 확인하고, 강제 reset/반복 실행하지 마세요. supervisor 사이에는 OS 파일 잠금이 있지만 사용자가 별도로 실행하는 다른 배포 도구까지 잠그는 전역 시스템은 아닙니다. 확인 3회는 짧은 기동 검사이지 장시간 운용 보장이 아닙니다.

### 수동 경로

기존 updater가 정상 적용된 V35 바깥쪽 Termux:

```bash
bash "$HOME/room-hub/deploy/termux/update-from-git.sh"
bash "$HOME/room-hub/deploy/termux/update-from-git.sh" --check
curl -fsS --max-time 5 http://127.0.0.1:8088/healthz
echo
sv status "$PREFIX/var/service/room-hub"
```

이번 패치에는 requirements 변경이 없습니다. 관리자 Ctrl+F5 후 실제 메모 조회와 알람 확인 미리보기를 테스트하세요. 알람 생성은 확인 후 기존 알람 메뉴에 실제로 생겼는지 대조합니다.

## 테스트 / rollback

`tests/test_routing_confidence.py`는 위 입력, model-call counter, 실제 SQLite/Adapter, 원문 보존, 개인정보, 모델 abstain, 무근거 변경 거부, 시간/오전/오후, 확인/중복/버전 충돌을 테스트합니다. 기존 회귀 전체도 유지합니다.

`tests/test_remote_update.py`는 CI SHA/상태, main 변경 중단, 실패 health, 잘못된 Git HEAD, 로그만 run인 경우, PID 재시작을 검사합니다. Windows CI에서는 **실제 CMD/PowerShell + 로컬 TCP + 합성 ssh.exe**로 BAT 종료 코드, UTF-8/LF payload와 인자를 검사합니다. 실제 V35 인증/SSH/기기 배포를 대신한 시험은 아닙니다.

되돌릴 때는 Revert PR을 검토·Merge하고 같은 Git updater로 배포합니다. 기존 메모/일정/알람 데이터는 스키마를 바꾸지 않아 유지됩니다. 코드 revert는 생성/취소한 알람을 되돌리지 않습니다. 새 bridge의 대기 중 confirmation은 이전 버전에서 다시 사용하지 말고 새 요청으로 만드세요. 민감한 조회 이력을 공유하기 전에 기존 표시 정책을 확인하세요.
