# 기존 250문항을 Personal Agent/OpenAI로 평가하기

이 커넥터는 기존 외부 `room_hub_v1` 텍스트 평가 세트를 현재 `PersonalAgentService`에 전달합니다.
기존 데이터·정답·projection·채점 함수를 바꾸지 않습니다. PR #39의 음성 전사 벤치마크와는 별개이며,
**오디오, STT 정확도, 마이크, 배포된 HTTP/UI는 측정하지 않습니다.**

`python -m benchmarks.agent`는 별도 호출자입니다. 운영 서비스에 요청하지 않고 문항별 임시 DB에
fixture를 넣어 실제 Agent·WidgetRegistry·어댑터 경로를 실행합니다. 관리자 승인을 자동으로 누르지
않으므로 일반 쓰기는 `CONFIRM`에서 끝납니다. 허용된 타이머 즉시 제어만 임시 DB에서 실행합니다.
운영 DB·저장 정책·음성 설정·systemd·PC 모델 서버는 변경하지 않습니다.

이 문서는 실행 절차입니다. 합성 테스트 통과나 `preflight` 성공은 실제 250문항 실행·유료 모델 응답의
증거가 아닙니다. 실제 평가 데이터나 결과를 이 저장소에 추가하지 마세요.

## 1. 운영 checkout과 분리하여 설치

Odyssey의 기존 서비스(`/opt/room-hub`)를 업데이트하거나 중지할 필요가 없습니다.
아래 경로가 이미 있으면 덮어쓰지 말고 새로운 빈 경로를 선택하세요.

PR 검증용 브랜치를 별도 복제할 때:

```bash
git clone --branch feat/personal-agent-benchmark --single-branch \
  https://github.com/pixaxe0826/roomconsole.git \
  /home/pixaxe/room-hub-agent-bench

cd /home/pixaxe/room-hub-agent-bench
git status --short
git log -1 --oneline

python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m benchmarks.agent --help
```

PR 병합 후에는 병합된 `main`의 CI를 확인하고 같은 명령의 `--branch` 값을 `main`으로 바꿉니다.
기존 벤치마크 checkout을 강제로 reset하거나 과거 결과를 덮어쓰지 않습니다.

데이터 위치는 기존 설치를 그대로 사용합니다. 예시는 다음 구조를 전제로 합니다.

- 데이터 부모: `/opt/room-hub-benchmark-data`
- suite: `/opt/room-hub-benchmark-data/room_hub_v1`
- 새 결과 부모: `/home/pixaxe/room-hub-agent-benchmark-results`
- 이전 Qwen 결과: `/home/pixaxe/room-hub-benchmark-ab-results` — 읽기/덮어쓰기 대상이 아닙니다.

실제 경로가 다르면 명령의 경로만 바꾸세요. 데이터가 없으면 이 커넥터가 250문항을 생성하거나
다운로드하지 않습니다. 원본 데이터 전송 절차는 [기존 벤치마크 안내](ASSISTANT_BENCHMARK.md)를 따릅니다.

## 2. 먼저 유료 호출 없이 검사

```bash
cd /home/pixaxe/room-hub-agent-bench

.venv/bin/python -m benchmarks.agent \
  --suite-root /opt/room-hub-benchmark-data \
  --results-root /home/pixaxe/room-hub-agent-benchmark-results \
  preflight --suite room_hub_v1 \
  --agent-mode auto \
  --fast-model gpt-6.1-sol --deep-model gpt-6.1-sol \
  --reasoning-effort high --timeout 90
```

`preflight`는 입력·설정 검사입니다. API 계정의 모델 접근 권한이나 실제 응답 성공을 검증하지 않습니다.
선택 문항 수·데이터 해시·실행 설정을 확인하세요. 문항의 날짜는 오늘로 바꾸지 않고 외부 suite에
저장된 기준 시각·시간대를 사용합니다. 특정 과거 날짜나 문항 수를 새로 하드코딩하지 않습니다.

`run`도 `--live`가 없으면 클라우드가 비활성입니다. Tier 0으로 처리할 수 없는 문항을
실제 모델이 답한 것처럼 평가하지 않습니다. 오프라인 결과를 OpenAI 성능으로 해석하지 마세요.
일부 문항이 미평가인 실행의 종료 코드 `3`은 전체 성공이 아니라 부분 평가/중단을 알리는 값입니다.

| `--agent-mode` | 측정 경로 |
|---|---|
| `auto` | 실제 서비스처럼 Tier 0 규칙 우선, 필요한 요청에 클라우드 계획 |
| `fast` | 초기 Tier 0을 생략하고 fast 모델 계획 요청 |
| `deep` | 초기 Tier 0을 생략하고 deep 모델 계획 요청 |
| `local_only` | 모델 호출 없이 로컬 규칙만 사용 |

`fast`/`deep`에서도 공급자 오류 후 생산 코드의 로컬 fallback이 발생할 수 있습니다.
모드 이름만 보지 말고 실제 `tier_used`와 호출 기록을 확인하세요.

## 3. 키와 외부 전송 동의

유료 실행에는 `--live`, `--max-cost-usd`, `--max-calls`를 명시해야 합니다.
이는 **평가 문장과 생산 코드가 구성한 fixture 메타데이터를 OpenAI에 보내는 실행**입니다.
민감한 내용이 포함되었는지 사용자가 로컬에서 확인한 후 동의하세요. 정답·채점 조건은 모델에
전달하지 않습니다. fixture의 비공개 메모를 공개 메모로 바꾸거나 운영 데이터를 가져오지 않습니다.
공개 fixture 메모 제목을 클라우드 카탈로그에 넣으려면 `--allow-shared-notes`로 별도 동의합니다.
기본값은 비활성이며 운영 UI의 저장된 동의를 가져오지 않습니다. 어느 값이든 비공개 메모는
클라우드 카탈로그에서 제외됩니다. 사용자가 요청 문장에 직접 넣은 내용까지 자동 제거한다는 뜻은 아닙니다.

키는 `OPENAI_API_KEY` 또는 `HUB_OPENAI_API_KEY` 환경변수로만 전달합니다. 운영 서비스에서 사용하는
키를 재사용하려면 사용자가 이 테스트 셸에 직접 입력합니다. 키를 채팅·명령 인자·문서·결과 파일에
붙여 넣거나 운영 `.env`/systemd 설정을 출력·통째로 가져오지 마세요. `sudo -E`도 필요 없습니다.

현재 Bash 셸에서 숨겨서 입력하려면:

```bash
set +x
IFS= read -r -s -p 'OpenAI API key: ' OPENAI_API_KEY
printf '\n'
export OPENAI_API_KEY
```

## 4. 소규모 확인 후 전체 평가

먼저 새 이름으로 5문항만 실행합니다. 아래 한도는 예시이며 비용을 보장하는 견적이 아닙니다.

```bash
.venv/bin/python -m benchmarks.agent \
  --suite-root /opt/room-hub-benchmark-data \
  --results-root /home/pixaxe/room-hub-agent-benchmark-results \
  run --suite room_hub_v1 --name openai-auto-smoke-01 \
  --agent-mode auto \
  --fast-model gpt-6.1-sol --deep-model gpt-6.1-sol \
  --reasoning-effort high --timeout 90 \
  --limit 5 --live --max-cost-usd 1 --max-calls 5
```

`auto`는 실제 서비스처럼 Tier 0 규칙을 우선 사용합니다. 5문항이 모두 로컬에서 처리되면
OpenAI 호출은 0회일 수 있습니다. `--live`를 넣었다는 사실만으로 실제 유료 호출 성공을 주장하지 마세요.
실제 호출 횟수·공급자 오류·usage·미정 비용을 결과에서 확인합니다.

유료 커넥터 자체를 확인하려면 선택적으로 별도 **fast 3문항** 실행을 사용할 수 있습니다.
이는 위 auto 실행과 별도 비용 한도이며, 운영 auto의 성능 결과와 합치지 않습니다.

```bash
.venv/bin/python -m benchmarks.agent \
  --suite-root /opt/room-hub-benchmark-data \
  --results-root /home/pixaxe/room-hub-agent-benchmark-results \
  run --suite room_hub_v1 --name openai-fast-smoke-01 \
  --agent-mode fast \
  --fast-model gpt-6.1-sol --deep-model gpt-6.1-sol \
  --reasoning-effort high --timeout 90 \
  --limit 3 --live --max-cost-usd 1 --max-calls 3
```

소규모 결과를 확인한 뒤 전체 suite를 선택합니다. suite가 250문항일 때에만 250문항 평가입니다.

```bash
.venv/bin/python -m benchmarks.agent \
  --suite-root /opt/room-hub-benchmark-data \
  --results-root /home/pixaxe/room-hub-agent-benchmark-results \
  run --suite room_hub_v1 --name openai-auto-full-01 \
  --agent-mode auto \
  --fast-model gpt-6.1-sol --deep-model gpt-6.1-sol \
  --reasoning-effort high --timeout 90 \
  --live --max-cost-usd 5 --max-calls 250
```

`--max-calls 250`은 모델 전송 예약/시도 상한이며 문항 수나 실제 청구 건수가 아닙니다.
거부된 HTTP 요청이나 전송 직전 중단도 포함될 수 있고, 전송 기록만으로 공급자 수신을 증명하지 않습니다.
한 문항이 0회 또는 복수의 모델 호출을 만들 수 있습니다. 비용·호출 상한으로 중단되면 나머지는
미평가이며 성공으로 채우지 않습니다.
완료된 결과를 확인하기 전에는 같은 평가를 반복하지 마세요. 새 실행 이름은 새 비용 한도를 시작하며,
이전 실행에 쓴 금액을 자동으로 합산하는 계정 전체 한도가 아닙니다.

작업이 끝나면 테스트 셸의 키를 제거합니다.

```bash
unset OPENAI_API_KEY
```

별도로 `HUB_OPENAI_API_KEY`를 설정했다면 그 테스트용 값도 제거하세요. 서비스에 적용된 키나 저장 정책을
이 명령이 바꿔 주는 것은 아닙니다.

## 5. 결과와 비용 해석

결과는 새 `<results-root>/<name>/` 아래에 저장합니다. 이미 있는 실행 이름은 덮어쓰지 않습니다.

- `summary.md` / `summary.html`: 실행 범위와 요약.
- `agent_config.json`: 데이터·소스 식별값과 명시한 모델/라우팅 설정.
- `agent_raw_results.jsonl` / `agent_metrics.json`: 문항별 관측·채점과 집계.
- `agent_trace/`: 원래 Agent 계획·실제 registry 결과·어댑터 진입 기록.
- `budget.json` / `budget_journal.jsonl`: 실행 한도·관측 비용·선예약/미정 내역.

결과와 trace에는 평가 문장·fixture·모델 출력이 포함될 수 있습니다. 비공개로 보관하고 Git/PR에
첨부하지 마세요. 앱 가격표와 provider usage에 따른 계산은 실제 OpenAI 청구서와 다릅니다.
모르는 비용을 0달러로 정산하지 않습니다. 사용량 누락·공급자 오류·중단 시 자동 재시도하지 않으며,
이미 전송된 요청은 클라이언트가 중단해도 공급자에서 처리·과금될 수 있습니다.

모델이 응답했지만 JSON/구조/그라운딩이 잘못된 경우와 호출 자체가 불가능한 경우를 구분합니다.
후자는 정확도 분모에서 제외될 수 있으므로 **선택 수·평가 수·미평가 수와 사유를 항상 함께 확인**합니다.
오실행 지표도 실제 어댑터 진입·상태 변경 증거와 함께 읽어야 합니다.
잘못된 모델 응답 뒤 로컬 fallback으로 요청이 처리되면 플랫폼 작업 점수는 성공일 수 있습니다.
그것은 모델 응답의 성공이 아니므로 공급자 오류·fallback 횟수도 함께 확인하세요.
`llm_ms`와 `gateway_ms`는 예산 원장·부모 체크포인트 ACK 시간을 포함합니다.
`transport_ms`는 ACK 이후의 공급자 전송 구간이며, 어떤 값도 iPad에서 말하기 시작한 시점의
전체 음성 응답 지연으로 해석하지 않습니다.

## 6. 이전 Qwen 결과와의 비교 한계

같은 suite 해시·문항·기준 시각·기존 gold를 사용하더라도 실행 엔진은 바뀌었습니다.
현재 Agent의 `auto` 점수는 로컬 규칙과 OpenAI 계획을 합친 플랫폼 점수이지 OpenAI 단독 언어 이해력은
아닙니다. 배포 서버 부하·HTTP·ASR 지연도 포함하지 않습니다.

기존 V1 채점은 단일 capability/slots를 기대합니다. 이 커넥터는 실제 Agent의 계획이 한 단계이고,
생산 검증을 통과한 canonical `WidgetRequest`도 하나일 때만 기존 표현으로 투영합니다.
복합 계획에서 첫 단계나 정답과 맞는 단계만 고르지 않습니다. 여러 단계·단계 없는 명확화·검증 실패는
원래 계획을 보존하고 `projection_status`로 한계를 표시합니다. 이 경우 임의의 capability를 채우지
않으므로 전체 엄격 점수에서는 실패할 수 있습니다. 정상적인 모델 출력의 표현 차이를 곧바로
모델의 능력 부족이라고 단정하지 마세요.

기존 `false_execution` 채점도 단일 capability 일치를 요구합니다. 투영할 수 없는 복합 계획이 타이머를
실행하면 이 엄격한 조건 때문에 보수적인 오실행 플래그가 붙을 수 있습니다. 원래 계획·개별 요청·
실제 어댑터 진입·상태 변화를 확인하기 전에는 이 플래그만으로 운영 보안 위반을 확정하지 마세요.

과거의 **지원 144문항 분모는 새 Agent에 자동으로 적용되지 않습니다.** 기존 M2의 지원 등록부·taxonomy는
레거시 WidgetBridge 경로용입니다. 새 결과를 기존 `analyze`/`compare-analysis`에 넘기거나 파일명을
레거시 `config.json`/`raw_results.jsonl`로 바꾸지 마세요. 지원 범위 평가는 별도로 구현해야 하며,
현재는 선택한 전체 문항의 원래 계약 점수와 실제 평가 범위·투영 한계를 함께 보고합니다.

기존 `python -m benchmarks` 명령과 과거 결과는 그대로 보존합니다. 다른 엔진의 결과를 비교하려면
데이터 해시·선택 ID·정책/모델 설정·투영 가능 범위·미평가 문항을 명시하고 같은 문항끼리 비교해야 합니다.
