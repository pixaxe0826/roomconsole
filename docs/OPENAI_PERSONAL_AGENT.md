# OpenAI Personal Agent Architecture (GPT-6.1 Sol High · WidgetProtocol 1.0 · $100/Month Budget Governor)

This document specifies the **Personal AI Agent** built on top of `roomconsole` (Room Hub `0.1.7`), transitioning from a local-only `Whisper + Qwen3-0.6B` setup to a **Hybrid Local + OpenAI Cloud Architecture** powered by **GPT-6.1 Sol High (`model="gpt-6.1-sol"`, `reasoning_effort="high"`)**, aligned across all 11 Room Hub widgets and systems via **WidgetProtocol 1.0**, and governed by a strict **$100.00/month (`100,000,000` microdollars) Budget Governor** (with configurable caps such as `$5.00` for testing).

---

## 1. Full WidgetProtocol 1.0 Adapter Specification Alignment (`11` Adapters)

Every widget in `widgets/` (`alarms`, `all-todos`, `calendar`, `clock`, `llm-response`, `note`, `timers`, `todos`, `weather`) and every core Room Hub subsystem (`layout`, `settings`, `system`) is registered as a typed `WidgetAdapter` inside [app/widget_protocol/system_adapters.py](../app/widget_protocol/system_adapters.py) (`build_full_registry`):

| Adapter Name | Target Widgets / Subsystem | Source of Truth | Supported Actions (`WidgetProtocol 1.0`) | Execution Authority |
|---|---|---|---|---|
| `memo` (`FullMemoAdapter`) | `widgets/note` | `room_hub_sqlite.hub_notes` | `read`, `list`, `write`, `append`, `clear`, `delete` | `READ_CTX` (`read`, `list`), `PREVIEW_CTX` $\to$ `confirmed_digest` (`write`, `append`, `clear`, `delete`) |
| `todo` (`FullTodoAdapter`) | `widgets/todos`, `widgets/all-todos` | `room_hub_sqlite.tasks` | `list`, `get`, `add`, `update`, `complete`, `reopen`, `delete` | `READ_CTX` (`list`, `get`), `PREVIEW_CTX` $\to$ `confirmed_digest` (mutations) |
| `calendar` (`CalendarAdapter`) | `widgets/calendar` | `room_hub_sqlite.tasks` | `list`, `get`, `add`, `update`, `delete` | `READ_CTX` (`list`, `get`), `PREVIEW_CTX` $\to$ `confirmed_digest` (mutations) |
| `alarm` (`FullAlarmAdapter`) | `widgets/alarms` | `room_hub_sqlite.hub_alarms` | `list`, `events`, `set`, `update`, `cancel`, `snooze`, `ack` | `READ_CTX` (`list`, `events`), `PREVIEW_CTX` $\to$ `confirmed_digest` (mutations) |
| `timer` (`TimerAdapter`) | `widgets/timers` | `room_hub_sqlite.hub_timers` | `list`, `get`, `start`, `stop` | `READ_CTX` (`list`, `get`), `TIMER_CTX` (immediate `start`, `stop`) |
| `clock` (`ClockAdapter`) | `widgets/clock` | `room_hub_clock.os_clock` | `read`, `resolve` | `READ_CTX` |
| `weather` (`WeatherAdapter`) | `widgets/weather` | `room_hub_sqlite.kv.weather` | `read`, `refresh` | `READ_CTX` (`read`), `TIMER_CTX` (immediate `refresh`) |
| `llm-response` (`LLMDisplayAdapter`) | `widgets/llm-response` | `room_hub_sqlite.hub_agent_runs` | `list`, `read` | `READ_CTX` |
| `layout` (`LayoutAdapter`) | 8×6 Dashboard Grid & All 9 Widget Plugins | `room_hub_sqlite.kv.layout` | `read`, `update_widget`, `add_widget`, `remove_widget`, `get_data`, `set_data` | `READ_CTX` (`read`, `get_data`), `PREVIEW_CTX` $\to$ `confirmed_digest` (mutations) |
| `settings` (`SettingsAdapter`) | Hub Settings (Title, Theme, Timezone, Location) | `room_hub_sqlite.kv.settings` | `read`, `update` | `READ_CTX` (`read`), `PREVIEW_CTX` $\to$ `confirmed_digest` (`update`) |
| `system` (`SystemAdapter`) | System Overview, Paired Display Devices, Audit Log | `room_hub_sqlite.system` | `status`, `devices`, `audit` | `READ_CTX` |

- Every step planned by the Personal Agent is converted into a canonical `WidgetRequest(protocol_version="1.0", widget=..., action=..., target=..., args=...)`.
- Read steps execute immediately through `await registry.execute(req, READ_CTX)`.
- Immediate control steps (`timer.start`, `timer.stop`, `weather.refresh`) execute immediately through `await registry.execute(req, TIMER_CTX)`.
- Mutation steps are pre-validated through `await registry.execute(req, PREVIEW_CTX)`, which validates the Pydantic input schema and slot constraints (`validate_slots`) and returns `status="needs_confirmation"` with `meta.request_digest = request_digest(req)`.
- Upon manager confirmation (`POST /api/agent/runs/{run_id}/confirm`), the server verifies both the batch `preview_sha256` and each step's `request_digest(req)`, constructs `ExecutionContext(confirmed_digest=request_digest(req))`, and executes each mutation through `await registry.execute(req, confirm_ctx)`.

---

## 2. 4-Tier Hybrid Routing Architecture (GPT-6.1 Sol High)

```mermaid
flowchart TD
    User["User Input (Voice Audio or Natural Language Text)"] --> InputType{"Audio or Text?"}
    InputType -->|Audio| STT["Tier 1: Hybrid STT Router"]
    STT -->|Budget OK| OpenAISTT["OpenAI Audio STT (gpt-4o-mini-transcribe + task_hint vocabulary)"]
    STT -->|Hard Cap / Offline| LocalWhisper["Local SpeechHub (whisper.cpp fallback)"]
    OpenAISTT --> Tier0
    LocalWhisper --> Tier0
    InputType -->|Text| Tier0["Tier 0: Deterministic Local Fast-Path ($0, <5ms)"]

    Tier0 -->|Matches fast_reads / timer_commands / memo_grounding / semantic_parser| ExecImmediate["Immediate WidgetProtocol Execution or Single-Step Staged Preview"]
    Tier0 -->|Compound, Multi-Widget, or Briefing Request| BudgetCheck["BudgetGovernor.gate() (SQLite Microdollar Ledger)"]

    BudgetCheck -->|Allowed Cloud (NORMAL / SOFT_CAP)| SolHigh["Tier 2/3: GPT-6.1 Sol High (model=gpt-6.1-sol, reasoning_effort=high, strict json_schema)"]
    BudgetCheck -->|HARD_CAP_REACHED or Offline| LocalFallback["Deterministic Local Fallback & Briefing"]

    SolHigh --> Validator["WidgetProtocol 1.0 Validation (PREVIEW_CTX), Conflict Detector & Version Lock"]
    Validator -->|Read / Immediate Control Only| Completed["Status: succeeded"]
    Validator -->|Has Mutations| Staged["Status: awaiting_confirmation (preview_sha256 + request_digest)"]
    Staged -->|POST /api/agent/runs/{id}/confirm| Commit["WidgetRegistry.execute(req, confirmed_digest) + hub_agent_receipts"]
```

| Tier | Engine / Model | Latency | Unit Cost | Handled Workloads |
|---|---|---|---|---|
| **Tier 0** | Local `fast_reads`, `timer_commands`, `memo_grounding`, `semantic_parser` | `< 5 ms` | **$0.00** | Direct schedule/todo/alarm/memo reads, exact countdown timers (`타이머 3분`), named memo lookups, unambiguous single-intent commands |
| **Tier 1** | `gpt-4o-mini-transcribe` (or `gpt-4o-transcribe`) + local `SpeechHub` fallback | `~350 ms` | **$0.003 / min** | Voice command transcription seeded with active task/alarm/memo vocabulary hints from [app/stt_accuracy.py](../app/stt_accuracy.py) |
| **Tier 2 / 3** | **GPT-6.1 Sol High** (`model="gpt-6.1-sol"`, `reasoning_effort="high"`, `json_schema` strict mode) | `~8–18 s` | **$2.50 / 1M in**, **$10.00 / 1M out** | Multi-step compound plans across all 11 Room Hub widget/system adapters, schedule conflict reasoning, and holistic daily briefings (`mode="briefing"`) |

---

## 3. Budget Governor & Microdollar Ledger ([app/openai_budget.py](../app/openai_budget.py))

All usage is tracked in SQLite (`hub_ai_budget_policy` and `hub_ai_usage_ledger`) using **exact integer microdollars** ($\text{USD } 1.00 = 1{,}000{,}000\,\mu\$$), including cached prompt token discounts and `reasoning_tokens` reporting:

- **Production Default Cap**: `$100.00 / month` (`100,000,000` microdollars).
- **Configurable Test Cap**: Can be set dynamically via `BudgetPolicy(monthly_cap_usd=5.0, fast_model="gpt-6.1-sol", deep_model="gpt-6.1-sol", reasoning_effort="high")` or `PUT /api/agent/budget/policy`.
- **4-State Budget Transitions**:
  - `NORMAL` (`< 70%` of cap): Full access to `gpt-6.1-sol` (`reasoning_effort="high"`).
  - `SOFT_CAP` (`70% – 90%` of cap): Automatic model/effort optimization when `auto_downgrade=True`.
  - `CONSERVATION` (`90% – 100%` of cap): Compact output token ceiling.
  - `HARD_CAP_REACHED` (`>= 100%` of cap): Blocks outbound cloud calls (`allowed_cloud=False`) before network dispatch and falls back to Tier 0 deterministic rules.

---

## 4. HTTP API Reference (`/api/agent/*`)

All endpoints require the manager `admin` session or Bearer `HUB_ADMIN_TOKEN`.

| Method & Path | Status | Description |
|---|---:|---|
| `GET /api/agent/status` | `200` | Agent version, OpenAI key readiness, active models (`gpt-6.1-sol`, `reasoning_effort=high`), registered adapters, and budget summary |
| `GET /api/agent/adapters` | `200` | Full `WidgetProtocol 1.0` manifest of all 11 registered widget/system adapters and their input/output JSON schemas |
| `GET /api/agent/adapters/health` | `200` | Live health check across all 11 registered `WidgetAdapter` instances |
| `POST /api/agent/adapters/execute` | `200` | Direct `WidgetProtocol 1.0` execution endpoint for read/immediate-control/preview requests |
| `GET /api/agent/budget?month=YYYY-MM` | `200` | Detailed monthly spend in USD & microdollars, breakdown by category & model, and recent ledger entries |
| `PUT /api/agent/budget/policy` | `200` | Update monthly cap (`monthly_cap_usd`), thresholds, `fast_model`, `deep_model`, or `reasoning_effort` |
| `GET /api/agent/snapshot` | `200` | Grounded workspace snapshot (clock, settings, layout, weather, tasks, conflicts, alarms, timers, memo catalog) |
| `POST /api/agent/command` | `202` | Submit a natural language command (`mode`: `auto`, `fast`, `deep`, `briefing`, `local_only`) |
| `POST /api/agent/briefing` | `202` | Generate a holistic daily briefing (`GPT-6.1 Sol High` synthesis + deterministic snapshot) |
| `POST /api/agent/audio` | `202` | Upload voice audio (`multipart/form-data`), transcribe with vocabulary hints, and run agent |
| `GET /api/agent/runs` | `200` | List recent agent runs (`status`, `tier_used`, `model_used`, `cost_microdollars`, `preview`) |
| `GET /api/agent/runs/{run_id}` | `200` | Get full run details including step results, staged `WidgetRequest` preview, and confirmation receipt |
| `POST /api/agent/runs/{run_id}/confirm` | `200` | Confirm and atomically execute staged mutations using `{"preview_sha256": "..."}` |
| `POST /api/agent/runs/{run_id}/cancel` | `200` | Cancel a staged run in `awaiting_confirmation` state |
