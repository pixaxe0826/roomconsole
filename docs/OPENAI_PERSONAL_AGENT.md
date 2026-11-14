# OpenAI Personal Agent 아키텍처 및 운영 가이드 (`pixaxe0826/roomconsole`)

[문서 홈](../README.md) · [설치](SETUP.md) · [보안](../SECURITY.md) · [Widget Protocol 1.0](WIDGET_PROTOCOL.md) · [실내 온도·공기질 센서](INDOOR_TEMPERATURE.md) · [M3.4-0 런타임](RUNTIME_M340.md)

## 1. 개요 및 활성화 경계

Room Hub의 **OpenAI Personal Agent**는 기존 로컬 `Whisper + Qwen` 구조의 결정론적 엔진(`fast_reads`, `timer_commands`, `memo_grounding`, `semantic_parser`)을 **Tier 0($0.00)**으로 재사용하면서, 관리자가 명시적으로 활성화한 경우에만 **예산 예약·정산 거버너(`BudgetGovernor`)** 하에 **GPT-6.1 Sol High (`model="gpt-6.1-sol"`, `reasoning_effort="high"`)** Structured Outputs 및 선택적 OpenAI Audio STT를 결합하는 하이브리드 비서 계층입니다.

### 1.1 명시적 활성화 (Opt-in 기본 비활성화)

신규 에이전트와 외부 클라우드 호출은 기본적으로 **비활성화**되어 있으며, 비활성 상태에서는 기존의 iPad 음성 수신함(`SpeechHub` $\to$ `LLMHub`), 관리자 LLM 인터페이스(`/api/llm/requests`), 표시 위젯 동작이 100% 그대로 유지됩니다.

| 환경변수 | DB 정책 필드 | 기본값 | 역할 |
|---|---|---|---|
| `HUB_PERSONAL_AGENT_ENABLED` | `agent_enabled` | `false` | `/api/agent/*` 실행 및 `SpeechHub` 전사 결과의 에이전트 전달 활성화 |
| `HUB_OPENAI_ENABLED` | `cloud_enabled` | `false` | Tier 1 / Tier 2 OpenAI 클라우드 플래너 호출 허용 여부 |
| `HUB_OPENAI_AUDIO_ENABLED` | `audio_cloud_enabled` | `false` | `/api/agent/audio`에서 OpenAI STT 외부 전송 허용 여부 |
| `HUB_OPENAI_INCLUDE_SHARED_NOTES` | `allow_shared_notes_in_cloud` | `false` | 디스플레이 공유 메모(`shared=true`) 제목·ID의 클라우드 프롬프트 포함 허용 (비공개 메모는 항상 제외) |
| `OPENAI_API_KEY` / `HUB_OPENAI_API_KEY` | — | 미설정 | OpenAI 전용 API 키 (**`HUB_LLM_API_KEY`를 자동 재사용하지 않음**) |
| `HUB_OPENAI_MONTHLY_BUDGET_USD` | `monthly_cap_usd` | `100.00` | 월간 비용 예약·추정 한도(USD) |
| `HUB_OPENAI_FAST_MODEL` | `fast_model` | `gpt-6.1-sol` | Tier 1 Fast 계획 모델 |
| `HUB_OPENAI_DEEP_MODEL` | `deep_model` | `gpt-6.1-sol` | Tier 2 Deep 복합 계획·브리핑 모델 |
| `HUB_OPENAI_REASONING_EFFORT` | `reasoning_effort` | `high` | 추론 강도 (`low` / `medium` / `high`) |
| `HUB_OPENAI_STT_MODEL` | `stt_model` | `gpt-4o-mini-transcribe` | 클라우드 STT 모델 (`gpt-4o-mini-transcribe`, `gpt-4o-transcribe`, `whisper-1`) |

### 1.2 설정 우선순위와 재시작 보존

1. **요청 수준 `local_only` (`mode="local_only"` 또는 `local_only=true`)**: 전역 클라우드 설정이 켜져 있어도 해당 요청(텍스트·음성·브리핑·폴백)에서 외부 AI/STT 호출을 강제로 차단합니다.
2. **DB 저장 정책 (`hub_ai_budget_policy`, `PUT /api/agent/budget/policy`)**: 관리자가 UI 또는 API로 변경·저장한 정책은 SQLite에 영속 저장되며, **서버를 재시작해도 환경변수 초기값으로 덮어쓰지 않습니다.**
3. **최초 DB 생성 시 환경변수 시드 (`policy_from_env()`)**: `hub_ai_budget_policy` 행이 없을 때만 환경변수를 읽어 초기 정책을 생성합니다.
4. **코드 기본값**: 환경변수도 지정되지 않은 경우 모든 스위치는 `false`(비활성)로 시작합니다.

> **주의**: `run.py` 직접 실행은 `.env` 파일을 자동으로 읽지 않습니다. Python/systemd에서는 셸 `export` 또는 `EnvironmentFile=`을 설정하고, Docker에서는 `compose.yaml`의 `environment:` 매핑을 통해 프로세스에 전달해야 합니다.

---

## 2. 3-Tier 라우팅 및 `local_only` / 로컬 폴백

```text
[iPad 마이크 (/api/speech/jobs) 또는 /api/agent/audio, /api/agent/command]
       │
       ├─ (1) 요청/정책 판정: should_use_cloud(mode, local_only, category)
       │      └─ local_only 또는 클라우드 비활성/예산 차단/장애 시:
       │           • 음성: 로컬/LAN SpeechHub(Whisper) 전사 경로 재사용 (불가 시 503 구조화 오류)
       │           • 텍스트/브리핑: Tier 0 로컬 처리기 또는 로컬 브리핑 실행 (외부 호출 0회)
       │
       ├─ (2) Tier 0 로컬 빠른 경로 ($0.00, 클라우드 호출 0회)
       │      ├─ 실내 센서 읽기 (indoor.read / indoor.air)
       │      ├─ 활성 알람 이벤트 제어 (alarm.snooze / alarm.ack)
       │      ├─ 빠른 읽기 (fast_reads: clock, weather, todo.list, calendar.list, memo.read)
       │      ├─ 타이머 명령 (timer_commands: timer.start, timer.stop, timer.list)
       │      ├─ 이름 기반 메모 그라운딩 (memo_grounding: 로컬 전용 카탈로그 사용)
       │      └─ 한국어 시맨틱 파서 (semantic_parser: 단일 할 일·일정·알람·메모 변경 스테이징)
       │      * 복합 다중 의도 명령은 일부 단계만 조용히 잘라 실행하지 않고 Tier 1/2로 넘기거나
       │        로컬 전용 모드에서는 분리 요청 안내(needs_clarification)를 반환합니다.
       │
       └─ (3) Tier 1 / Tier 2 OpenAI Structured Outputs (`gpt-6.1-sol`, `reasoning_effort="high"`)
              ├─ 호출 전 BudgetGovernor.reserve_budget() 트랜잭션으로 예산 예약
              ├─ 엄격한 JSON Schema (`AGENT_PLAN_JSON_SCHEMA`, 최대 12단계 제한)
              └─ 서버 측 카탈로그·원문 근거·버전 검증 후 실행(읽기) 또는 승인 대기(쓰기)
```

---

## 3. 예산 예약·정산 원장 (`app/openai_budget.py`, `app/openai_gateway.py`)

동시 요청에서 한도를 초과하지 않도록 외부 네트워크 호출 전에 짧은 `BEGIN IMMEDIATE` 트랜잭션으로 비용을 예약합니다.

$$\text{확정 사용량(settled)} + \text{미정산 예약(reserved)} + \text{결과 불명 비용(unknown)} + \text{이번 보수적 예상 비용} \le \text{설정 한도}$$

- **응답 파싱 전 usage 우선 정산**: HTTP 4xx/5xx 오류, 모델 `refusal`, `finish_reason != "stop"`, JSON 파싱 실패가 발생하더라도 응답 본문에 `usage`가 포함되어 있으면 예외를 발생시키기 **전에** `settle_reservation()`으로 실제 토큰 비용을 먼저 원장에 기록합니다.
- **추론·캐시 토큰 이중 계산 방지**: OpenAI API 규격상 `prompt_tokens`에 `cached_tokens`가 포함되고 `completion_tokens`에 `reasoning_tokens`가 이미 포함되어 있으므로, 캐시 토큰은 차감 후 할인 단가를 적용하고 추론 토큰은 출력 토큰에 중복 합산하지 않습니다.
- **Timeout / 재시작의 보수적 처리 (`unknown`)**: 네트워크 타임아웃이나 프로세스 재시작으로 과금 여부를 확정할 수 없는 예약은 즉시 환불하지 않고 `status='unknown'`으로 전환하여 월 한도 노출액(`committed_microdollars`)에 계속 산입합니다.
- **오디오 컨테이너 및 실제 길이 검증 (`verify_audio_payload`)**: 호출자가 보낸 `duration_seconds`를 신뢰하지 않고, 외부 전송 전에 파일 크기(최대 10MB), 음수/NaN/Inf 길이, MIME/매직 헤더 일치 여부, WAV 프레임 무결성 및 실제 재생 시간(최대 300초)을 검증합니다. 공급자 `usage`가 있으면 `cost_source="provider_usage"`, 길이 기반 계산이면 `cost_source="duration_estimate"`로 구분 기록합니다.
- **청구 면책 고지**: 앱 내부 원장은 단가표(`PRICING_CATALOG_METADATA`) 기반의 보수적 추정치 및 호출 차단 장치이며, 환율·세금·공급자 정산 시점까지 포함한 실제 OpenAI 청구서 금액을 절대적으로 보장하는 것은 아닙니다.

---

## 4. 개인정보 보호, 대상 검증 및 권한 경계 (`admin` vs. `display`)

1. **비공개 메모 완전 제외**:
   - `shared=false`인 비공개 메모는 제목·ID·본문·개수 어느 것도 클라우드 프롬프트와 STT 힌트에 포함되지 않습니다.
   - `shared=true`(LAN iPad 표시 공유) 역시 외부 클라우드 전송 동의와 동일시하지 않으며, 기본값(`allow_shared_notes_in_cloud=false`)에서는 클라우드로 전송하지 않습니다.
2. **신뢰할 수 없는 문자열 정화 및 카탈로그·원문 근거 검증**:
   - 할 일 제목·알람 라벨 등에 포함된 프롬프트 인젝션 패턴을 정화(`sanitize_untrusted_label`)하고 `UNTRUSTED_DATA`로 격리합니다.
   - 모델이 반환한 `target_id`가 DB에 존재한다는 사실만으로 변경을 허용하지 않고, **이번 요청의 허용 카탈로그 소속 여부**, **사용자 원문(`user_text`) 내 근거 존재 여부**, **동명 항목 중복 여부(2개 이상 시 `needs_clarification`)**, **낙관적 버전(`version`)**을 모두 검증합니다.
3. **관리자(`admin`)와 페어링된 표시 기기(`display`) 분리**:
   - 서버가 세션 쿠키/토큰에서 `ExecutionContext`(`role='admin'` vs `role='display'`, `principal='display:<device_id>'`)를 직접 결정합니다.
   - `display` 기기는 허용된 명령 제출과 **자신의 실행 기록(`principal` 일치)**만 조회할 수 있으며, 다른 기기나 관리자의 실행 기록 조회(`403`), 데이터 변경 최종 승인(`POST /api/agent/confirm/{run_id}`, `403`), 예산 정책 수정(`403`), 비공개 메모 접근이 차단됩니다. 기기 연결이 해제(`revoke`)되면 즉시 접근이 거부됩니다.

---

## 5. 단일 트랜잭션 복합 변경(`unit_of_work`) 및 영속 멱등성

- **사전 실행 예약 (`hub_agent_runs`)**: 클라우드 호출이나 즉시 타이머 실행 전에 `(principal, request_id)`와 요청 본문 해시(`request_sha256`)를 `BEGIN IMMEDIATE`로 먼저 예약합니다.
  - 동일 ID + 동일 본문: 기존 실행 결과 또는 진행 상태(`running`) 반환 (`duplicate: true`).
  - 동일 ID + 다른 본문: `409 Conflict` 거절.
- **즉시 제어와 승인 필요 변경의 단계 검증 및 분리 안내**: `timer.start/stop` 같은 비-트랜잭션 즉시 제어는 계획 내 모든 단계의 검증이 통과한 뒤에만 실행되며, DB 변경(`mutations`)과 함께 포함된 경우 미리보기 및 취소 응답에서 선행 실행된 즉시 제어와 승인 대기 중인 DB 변경을 명확히 구분해 안내합니다.
- **단일 트랜잭션 승인 (`Store.unit_of_work()`)**: `confirm_run()`은 권한·미리보기 해시(`preview_sha256`)·만료(10분)·시간대·버전 검증, 모든 어댑터의 실제 DB 변경, 실행 영수증(`hub_agent_receipts`), 실행 상태(`succeeded`) 갱신을 **단 하나의 SQLite `BEGIN IMMEDIATE` 트랜잭션** 안에서 처리합니다. 두 번째 단계가 실패하면 첫 번째 단계의 DB 변경과 메모리 중복 방지 캐시도 완전히 롤백됩니다. 실시간 WebSocket 알림은 커밋 완료 후 실행되어 알림 오류가 발생해도 이미 커밋된 변경을 재실행하지 않습니다.

---

## 6. 12개 WidgetProtocol 1.0 통합 어댑터

| 번호 | 도메인 (`widget_id`) | 어댑터 클래스 | 지원 동작 (`action`) | 비고 |
|---|---|---|---|---|
| 1 | `memo` | `FullMemoAdapter` | `read`, `list`, `write`, `append`, `clear`, `delete`, `pin`, `share` | 비공개 메모 클라우드/디스플레이 격리 |
| 2 | `todo` | `FullTodoAdapter` | `list`, `get`, `add`, `update`, `complete`, `reopen`, `delete` | 단일/반복 할 일 및 버전 검증 |
| 3 | `calendar` | `FullCalendarAdapter` | `list`, `get`, `add`, `update`, `delete` | 일정 관리 및 시간 충돌 감지 |
| 4 | `alarm` | `FullAlarmAdapter` | `list`, `events`, `set`, `update`, `cancel`, `delete`, `snooze`, `ack` | 예약 알람 및 활성 이벤트 미루기/확인 (승인 게이트 적용) |
| 5 | `timer` | `TimerAdapter` | `list`, `get`, `start`, `stop` | 카운트다운 타이머 즉시 제어 |
| 6 | `clock` | `ClockAdapter` | `read`, `resolve` | 현재 시각·요일·시간대 및 상대 날짜 해석 |
| 7 | `weather` | `WeatherAdapter` | `read`, `refresh` | 날씨 스냅샷 조회 및 갱신 |
| 8 | `indoor` | `IndoorSensorAdapter` | `read`, `air`, `devices` | 실내 온도·습도·기압 및 먼지 ADC·전압 읽기 전용 조회 (`ok`/`stale`/`offline`/`sensor_error`/`waiting` 구분, 추가 폴링 및 임의 PM2.5/AQI 환산 없음) |
| 9 | `llm-response` | `LLMResponseAdapter` | `list`, `read`, `latest`, `get` | `llm_requests` + `hub_agent_runs` 공통 표시 투영 |
| 10 | `layout` | `LayoutAdapter` | `read`, `update_widget`, `add_widget`, `remove_widget`, `get_data`, `set_data`, `focus`, `select_date` | 전체 `Layout` 8×6 경계·겹침·최대 12개·설치 위젯 종류 검증 및 빈 슬롯 탐색 |
| 11 | `settings` | `SettingsAdapter` | `read`, `update` | 허브 제목·테마·위치·시간대 설정 |
| 12 | `system` | `SystemStatusAdapter` | `status`, `overview`, `devices`, `audit` | 전체 작업·메모·알람·위젯 요약, 기기 목록 및 감사 로그 |

---

## 7. 단계별 시각 관측 및 미구현 범위 (`NOT_IMPLEMENTED`)

- 각 실행(`hub_agent_runs`)은 `timestamps` 객체에 `recorded_at`, `uploaded_at`, `transcribed_at`, `planned_at`, `awaiting_confirmation_at`, `confirmed_at`, `executed_at`, `displayed_at`을 구분 기록합니다.
- 음성 시작부터 응답 표시까지 15초 이내는 설계 목표치이며, 네트워크·모델 부하에 따라 달라질 수 있는 미검증 목표치입니다. 관리자 승인 대기 시간(`awaiting_confirmation_at` $\to$ `confirmed_at`)은 자동 처리 시간과 별도로 구분됩니다.
- **미구현 항목 (`NOT_IMPLEMENTED`)**:
  - 음성 합성(TTS) 출력: `NOT_IMPLEMENTED` (시각 대시보드 및 텍스트 응답 전용)
  - 항상 켜진 호출어(Wake word) 감지: `NOT_IMPLEMENTED` (사용자 명시적 마이크 버튼 탭 방식 유지)
  - 자동 브리핑 백그라운드 스케줄러: `NOT_IMPLEMENTED` (사용자/관리자 명시적 브리핑 요청 시 생성)
  - 멀티턴 장기 기억 DB: `NOT_IMPLEMENTED` (단일 요청 + 현재 시점 구조화 컨텍스트 방식)
  - 관리자 승인 없는 자율 DB 쓰기 실행: `NOT_IMPLEMENTED` (안전 정책상 모든 DB 변경은 관리자 확인 필수)
