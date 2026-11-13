# OpenAI Personal Agent Architecture ($100/Month Hybrid Budget Design)

This document specifies the **Personal AI Agent** built on top of `roomconsole` (Room Hub `0.1.7`), transitioning from a local-only `Whisper + Qwen3-0.6B` setup to a **4-Tier Hybrid Local + OpenAI Cloud Architecture** governed by a strict **$100.00/month (`100,000,000` microdollars) Budget Governor**.

---

## 1. Design Goals & Architectural Principles

1. **Full Reuse of Room Hub Domain & Safety Contracts**:
   - **Schedule & To-Do (`calendar.*`, `todo.*`)**: Reuses [app/store.py](../app/store.py) (`tasks` and `series` tables), [app/recurrence.py](../app/recurrence.py), and `TaskServices(add_task, edit_task, complete_task, delete_task)` via [app/widget_protocol/adapters.py](../app/widget_protocol/adapters.py).
   - **Alarms & Memos (`alarm.*`, `memo.*`)**: Reuses [app/life.py](../app/life.py) (`LifeService`, `hub_notes` max 200 with `pinned`/`shared`/`version`, `hub_alarms` max 64 with DST-aware `next_fire` and `days_of_week`).
   - **Timers (`timer.*`)**: Reuses [app/timers.py](../app/timers.py) (`TimerService`, `hub_timers` 1–600s countdowns).
   - **Confirmation & Optimistic Locking**: Only `timer.start` and `timer.stop` execute immediately on write; every mutation to Schedule, To-Do, Alarms, or Memos stages a preview in `hub_agent_runs` (`status = 'awaiting_confirmation'`) bound to a cryptographic `preview_sha256` digest and target entity `version`.
   - **Privacy Redaction**: Non-shared memos (`shared = false`) never leak body text to `/client` display summaries; only titles/metadata or shared memo bodies appear in display responses.

2. **Why Transition from Local Whisper + Qwen3-0.6B to Hybrid OpenAI**:
   - Local `Qwen3-0.6B` on edge hardware (e.g., LG V35 / low-power mini-PC) handles single-slot commands well when assisted by deterministic parsers, but struggles with **compound multi-domain requests** (e.g., *"내일 오전 10시 치과 일정 잡고, 8시 반에 알람 맞춰주고, 준비물 메모에 신분증 적어줘"*), **schedule conflict reasoning**, and **holistic daily briefings**.
   - Local `whisper.cpp` on mobile/edge CPUs has higher latency and occasional homophone errors on Korean/English mixed task names.
   - With a **$100/month OpenAI budget**, we do **not** need to choose between wasteful cloud-only calls and brittle small-model parsing: we keep **Tier 0 local deterministic routing** for 0ms/$0 routine actions and use **OpenAI Structured Outputs (`gpt-4o-mini` / `gpt-4o`) + OpenAI Audio STT (`gpt-4o-mini-transcribe`)** for natural language, multi-step planning, and executive daily briefings.

---

## 2. 4-Tier Hybrid Routing Architecture

```mermaid
flowchart TD
    User["User Input (Voice Audio or Natural Language Text)"] --> InputType{"Audio or Text?"}
    InputType -->|Audio| STT["Tier 1: Hybrid STT Router"]
    STT -->|Budget OK| OpenAISTT["OpenAI Audio STT (gpt-4o-mini-transcribe + task_hint vocabulary)"]
    STT -->|Hard Cap / Offline| LocalWhisper["Local SpeechHub (whisper.cpp fallback)"]
    OpenAISTT --> Tier0
    LocalWhisper --> Tier0
    InputType -->|Text| Tier0["Tier 0: Deterministic Local Fast-Path ($0, <5ms)"]

    Tier0 -->|Matches fast_reads / timer_commands / memo_grounding / semantic_parser| ExecImmediate["Immediate Read / Timer or Single-Step Staged Preview"]
    Tier0 -->|Compound, Ambiguous, or Briefing Request| BudgetCheck["BudgetGovernor.evaluate() (SQLite Microdollar Ledger)"]

    BudgetCheck -->|mode=fast or auto (Normal/Soft/Conservation)| Tier2["Tier 2: OpenAI Fast Planner (gpt-4o-mini Structured Outputs)"]
    BudgetCheck -->|mode=deep or briefing (Normal < $70)| Tier3["Tier 3: OpenAI Deep Reasoner & Briefing (gpt-4o Structured Outputs)"]
    BudgetCheck -->|Soft Cap >= $70| Tier2
    BudgetCheck -->|Hard Cap >= $100 or Offline| LocalFallback["Deterministic Local Fallback & Briefing"]

    Tier2 --> Validator["Server-Side Grounding, Conflict Detector & EntityCatalog Version Lock"]
    Tier3 --> Validator
    Validator -->|Read / Timer Only| Completed["Status: completed"]
    Validator -->|Has Mutations (todo/calendar/alarm/memo)| Staged["Status: awaiting_confirmation (preview_sha256 + 10m TTL)"]
    Staged -->|POST /api/agent/runs/{id}/confirm| Commit["Atomic Execution + hub_agent_receipts"]
```

| Tier | Engine / Model | Latency | Unit Cost | Handled Workloads |
|---|---|---|---|---|
| **Tier 0** | Local `fast_reads`, `timer_commands`, `memo_grounding`, `semantic_parser` | `< 5 ms` | **$0.00** | Direct schedule/todo/alarm/memo reads, exact countdown timers (`타이머 3분`), named memo lookups, unambiguous single-intent commands |
| **Tier 1** | `gpt-4o-mini-transcribe` (or `gpt-4o-transcribe`) + local `SpeechHub` fallback | `~350 ms` | **$0.003 / min** | Voice command transcription seeded with active task/alarm/memo vocabulary hints from [app/stt_accuracy.py](../app/stt_accuracy.py) |
| **Tier 2** | `gpt-4o-mini` (`json_schema` strict mode) | `~500 ms` | **$0.15 / 1M in**, **$0.60 / 1M out** | Multi-step compound commands across `todo`, `calendar`, `alarm`, `memo`, `timer`; natural relative dates; entity resolution |
| **Tier 3** | `gpt-4o` (`json_schema` strict mode) | `~1.1 s` | **$2.50 / 1M in**, **$10.00 / 1M out** | Holistic morning/evening daily briefings (`mode="briefing"`), multi-constraint schedule reorganization (`mode="deep"`), conflict resolution |

---

## 3. $100/Month Budget Governor & Token/Audio Economics

### 3.1 Realistic Monthly Usage Simulation vs. $100 Cap

Even under heavy daily personal usage, the hybrid architecture uses only a small fraction of the $100/month cap while providing huge headroom for deep reasoning:

| Daily Activity | Volume / Day | Monthly Volume | Model Used | Monthly Cost (USD) |
|---|---:|---:|---|---:|
| Routine reads & timers (Tier 0) | 50 cmds/day | 1,500 cmds/mo | Local deterministic | **$0.00** |
| Voice commands (Tier 1 STT, avg 12s each) | 60 clips/day (12 min/day) | 360 min/mo | `gpt-4o-mini-transcribe` ($0.003/min) | **$1.08** |
| Compound / NL commands (Tier 2, 1.5K in / 350 out) | 80 cmds/day | 2,400 calls/mo | `gpt-4o-mini` | **$1.04** |
| Deep schedule planning & briefings (Tier 3, 2.5K in / 800 out) | 15 calls/day | 450 calls/mo | `gpt-4o` | **$6.41** |
| **Baseline Heavy Personal Usage Total** | **205 interactions/day** | **~6,150/mo** | **Hybrid Tier 0–3** | **~$8.53 / month** |
| **Maximum Capacity at $100/mo** | — | — | Up to **6,000+ `gpt-4o` deep calls** or **33,000+ audio mins** | **$100.00 cap** |

### 3.2 Microdollar Ledger & 4-State Budget Policy ([app/openai_budget.py](../app/openai_budget.py))

All usage is tracked in SQLite (`hub_ai_budget_policy` and `hub_ai_usage_ledger`) using **exact integer microdollars** ($1\,\text{USD} = 1{,}000{,}000\,\mu\$$) to avoid floating-point drift, including the 50% discount on OpenAI `cached_tokens`:

- **`NORMAL` (`$0.00 – $69.99` / `< 70%`)**:
  - Full access to `gpt-4o` (deep/briefing), `gpt-4o-mini` (fast), and `gpt-4o-mini-transcribe` / `gpt-4o-transcribe`.
- **`SOFT_CAP` (`$70.00 – $89.99` / `70% – 90%`)**:
  - Automatically downshifts `gpt-4o` and `o3-mini` requests to `gpt-4o-mini`, and `gpt-4o-transcribe` to `gpt-4o-mini-transcribe`.
- **`CONSERVATION` (`$90.00 – $99.99` / `90% – 100%`)**:
  - Forces `gpt-4o-mini` with a strict `max_output_tokens <= 384` ceiling and `gpt-4o-mini-transcribe`.
- **`HARD_CAP_REACHED` (`>= $100.00` / `100%`)**:
  - Blocks all outbound OpenAI HTTP calls (`allowed_cloud = False`) before network dispatch and seamlessly falls back to Tier 0 deterministic local parsing + local Whisper/Qwen.

---

## 4. Safety, Conflict Detection & Confirmation Protocol

1. **Strict Schema Enforcement (`AGENT_PLAN_SCHEMA`)**:
   - OpenAI responses use `response_format: {"type": "json_schema", "json_schema": {"strict": True, ...}}`.
   - Every step in a plan must belong to the closed capability set (`todo.list`, `todo.add`, `todo.complete`, `todo.edit`, `todo.delete`, `calendar.list`, `calendar.add`, `calendar.edit`, `calendar.delete`, `memo.read`, `memo.write`, `memo.edit`, `memo.delete`, `alarm.list`, `alarm.set`, `alarm.update`, `alarm.delete`, `timer.status`, `timer.start`, `timer.stop`, `weather.read`, `time.now`, `briefing.daily`).
2. **Server-Side Entity Resolution & Optimistic Locking**:
   - The LLM never invents raw SQLite UUIDs or row versions unless grounded. `PersonalAgentService._stage_mutation` resolves targets through [app/entity_catalog.py](../app/entity_catalog.py) (`EntityCatalog`) or exact live snapshot rows, capturing `id` and `version`.
   - When the user confirms via `POST /api/agent/runs/{run_id}/confirm`, the server verifies `hmac.compare_digest(preview_sha256)` and checks that every target row's `version` still matches the previewed version (returning HTTP `409` on concurrent edits).
3. **Automatic Schedule Conflict Detection (`detect_schedule_conflicts`)**:
   - When staging `calendar.add` or `todo.add` with a `time_of_day`, `PersonalAgentService` inspects existing occurrences on the target date. Any overlapping or adjacent (`<= 30 min`) scheduled item or active alarm generates an explicit warning in the confirmation preview.

---

## 5. HTTP API Reference (`/api/agent/*`)

All endpoints require the manager `admin` session or Bearer `HUB_ADMIN_TOKEN`.

| Method & Path | Status | Description |
|---|---:|---|
| `GET /api/agent/status` | `200` | Agent version, OpenAI key readiness, active models, and live monthly budget summary |
| `GET /api/agent/budget?month=YYYY-MM` | `200` | Detailed monthly spend in USD & microdollars, breakdown by category & model, and daily ledger |
| `PUT /api/agent/budget/policy` | `200` | Update monthly cap (`monthly_cap_usd`), soft/conservation thresholds, or preferred models |
| `GET /api/agent/snapshot` | `200` | Grounded workspace snapshot (today/upcoming schedule, todos, alarms, memos, timers, weather) |
| `POST /api/agent/command` | `202` | Submit a natural language command (`mode`: `auto`, `fast`, `deep`, `briefing`, `local_only`) |
| `POST /api/agent/briefing` | `202` | Generate a holistic daily briefing (plus optional follow-up mutation proposals) |
| `POST /api/agent/audio` | `202` | Upload voice audio (`multipart/form-data`), transcribe with vocabulary hints, and run agent |
| `GET /api/agent/runs` | `200` | List recent agent runs (`status`, `tier`, `model`, `cost_microdollars`, `preview`) |
| `GET /api/agent/runs/{run_id}` | `200` | Get full run details including step results, staged preview, and confirmation receipt |
| `POST /api/agent/runs/{run_id}/confirm` | `200` | Confirm and atomically execute staged mutations using `{"preview_sha256": "..."}` |
| `POST /api/agent/runs/{run_id}/cancel` | `200` | Cancel a staged run in `awaiting_confirmation` state |

---

## 6. Configuration & Quick Start

Set your OpenAI API key in the environment before launching Room Hub (see [.env.example](../.env.example)):

```powershell
$env:OPENAI_API_KEY = "sk-..."
$env:HUB_OPENAI_MONTHLY_BUDGET_USD = "100.00"
python run.py
```

- Credentials are read strictly from environment variables (`OPENAI_API_KEY`, `HUB_OPENAI_API_KEY`, or `HUB_LLM_API_KEY`) and are never written to SQLite or returned by any API endpoint.
