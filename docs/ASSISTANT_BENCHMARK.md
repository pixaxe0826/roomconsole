# Assistant Benchmark Harness V1

This is an opt-in, text-only caller of Room Hub's **existing assistant**, not another
router or an assistant improvement. It changes no production prompt, rule, model,
permission, UI, speech configuration or database. Reports are generated in Korean.

## Start here

Use the existing Python environment with `requirements-dev.txt` for development.
No new dependency is added. From the repository root:

```bash
python -m benchmarks list
python -m benchmarks validate --suite room_hub_v1
python -m benchmarks run --suite room_hub_v1 --mode full --name baseline-v1 --llm disabled
python -m benchmarks run --suite room_hub_v1 --mode full --name baseline-v1-qwen --llm local
python -m benchmarks compare baseline-v1-qwen parser-v1-qwen
```

`--llm disabled` deliberately does **not** fake an LLM answer. Deterministic routes
execute; requests requiring a model are `NOT_EVALUATED`. Their absence is not counted
as model misunderstanding. A successful CLI exit means the evaluation completed,
not that the assistant passed every case or that the entire suite was evaluable.
Check coverage and `run_status` in `config.json` and the summary.

Global options precede the command:

```bash
python -m benchmarks --results-root artifacts/benchmarks run --suite room_hub_v1 --name trial
python -m benchmarks --suite-root /path/to/suites validate --suite external_v1
python -m benchmarks run --suite room_hub_v1 --tag temporal --name temporal-only
python -m benchmarks run --suite room_hub_v1 --source-type paraphrase --name paraphrases
python -m benchmarks run --suite room_hub_v1 --case B056 --name one-case
python -m benchmarks run --suite room_hub_v1 --category canonical --name canonical
python -m benchmarks run --suite external_v1 --split holdout --name holdout
python -m benchmarks run --suite room_hub_v1 --suite external_v1 --name paired
```

Repeated tags are AND filters. Repeated case/category selectors select any matching
value. Multiple suites produce separate `<name>-<suite>` runs. Existing result
folders are never silently overwritten. Use another name for a repeat. Explicit
sessions must include all prerequisite turns beginning with turn 1.

## Shared production path

```text
manifest / cases + fixtures
  | explicit projection (NO expected, ID, parent, tags or answer)
  v
separate worker -> disposable FastAPI application (NO lifespan/server)
  -> LLMHub._insert (shared post-transcript text entry)
  -> production detect / normalizer / rules
  -> WidgetBridge or AssistantEngine
  -> production ChatBackend / validators / source grounding / policy
  -> reviewed Fake*Adapter -> real service implementation, TEMPORARY SQLite
  -> production WidgetResponse / formatter
  v
observation -> parent-only projection / scorer -> raw files / Korean reports
```

`LLMHub.submit()` normally checks a stored transcript and calls `_insert()`. The
benchmark calls this same post-transcript entry with synthetic metadata. It does
not exercise microphones, speech integrity checks, browser authentication or the
HTTP submission surface. Queued work invokes the **same** `_execute()` consumer
inline rather than starting a background worker. No polling scheduler, weather
fetch, ASR job, alarm audio or web server is started.

Instance observation hooks wrap existing prepare, validate, ground, query, stage
and finish functions. They do not implement text interpretation. The test-clock
patches live only in the worker process and are restored after each runtime.
These private seams are intentionally version-coupled: a production refactor must
keep the harness tests passing or update the boundary explicitly. Do not silently
replace the core with an approximate benchmark implementation.

## Isolation and Fake Adapters

The worker receives only text, reference clock, explicit session state, synthetic
fixture state and connection configuration. It never imports the scorer or loads
the suite directory. The parent loads answers and evaluates returned observations.

Every independent input gets a new application, SQLite file, execution ledger,
model notebook, widget registry and clock. Python imports can be reused to avoid
startup dominating evaluation; business state is not reused. Cases with explicit
`session_id` and ascending `turn` retain one isolated runtime. General multi-turn
understanding is **not** thereby added to the production assistant.

FakeMemoAdapter, FakeTodoAdapter, FakeCalendarAdapter, FakeAlarmAdapter and
FakeTimerAdapter inherit the actual adapter interface, schema, authorization and
service code. Their dependencies point only to the disposable application. A
path guard refuses any storage path other than the owned temporary SQLite file,
including lifecycle access. Unknown new adapter types fail closed until their
side-effect isolation is reviewed; a new benchmark dataset cannot authorize them.

The production task model combines todos and calendar entries. The fixture mapper
preserves that fact; it does not create benchmark-only calendar duration support
or domain filtering. Calendar fixture end times therefore remain unrepresented.
The fixture's active memo seeds the default pinned shared card. Other session
context (previous lists, pending dialogs, ordinal targets) is recorded but is not
magically interpreted by the current single-turn core. No manager confirmations
are clicked automatically. A valid write preview remains CONFIRM; it is not a
fake successful mutation. Existing immediate timer controls remain immediate only
inside the temporary service.

## Modes

| Mode | Boundary and scoring |
|---|---|
| `nlu` | Stop at proposal/WidgetRequest validation, before adapter execution; capability/slots/route/LLM. |
| `decision` | Allow real validation, source-grounding and confirmation preview; block final Widget Adapter execution. |
| `full` | Run the complete text path against sandbox adapters; also compare policy, final status, execution boundary and optional structured response assertions. |

Grounding reads in Decision mode may consult the fixture to validate a target.
Legacy non-Widget service reads can be computed by the existing stage method; they
are side-effect-free and remain distinct from final Widget Adapter execution.
NLU and Decision do not report full Task Success or pretend a final action happened.
A target resolved only inside an adapter will not be fabricated at an earlier
boundary; earlier-mode slot scores may consequently be lower.

## Reproducibility and trace

The suite supplies a timezone-aware `reference_datetime`; the V1 clock is fixed at
`2026-09-21T09:00:00+09:00`. Business dates, receipt timestamps and timer deadlines
use the test clock. Latencies use real monotonic/performance time. They exclude
fixture construction, subprocess startup and report generation. Cumulative worker
wall time is retained separately; nested stage timings must not be added together.

Configuration records suite/version/content hash, selected IDs, application Git
commit/tree, dirty state, production and harness source hashes, Python/platform,
and component source hashes. Semantic Parser and Proposal Repair are disabled in
this baseline. Current context scope and its unimplemented general-session layer
are explicit in the trace. If a future PR adds those components, update their
exposed layer metadata while keeping the same production call path.

Only `--llm local` enables network inference, using the unchanged production
ChatBackend and its loopback-only endpoint validation. The existing model defaults
are `http://127.0.0.1:8090/v1` and `Qwen3-0.6B-Q5_K_M.gguf`. Temperature is 0. The
production capability path can apply its existing token cap; the actual request
payload in each trace is authoritative, not merely the CLI upper bound. Seed,
context size and quantization remain null when the endpoint has not supplied
verifiable metadata. They are not guessed from a filename. There is no model
installation, HTTP proxy inheritance, automatic retry or second LLM implementation.

Raw model responses, parsed proposals, source-grounding failures, original routing,
actual adapter requests/results, business-state differences and final text are
preserved. Unexposed stage durations are null, not invented zero-cost performance.
A disabled-model attempt and an actual HTTP dispatch are counted separately.

## Scoring contract

`projection.json` maps **representations**, not language: for example the native
`todo.create` name to the suite's `todo.add`, title to text, a resolved single-day
start/end pair to date. It never fills a value from expected answers or reparses
input text. Native and projected values are both retained.

Slot equality is type-strict: strings are not integers and booleans are not
numbers. Explicit null matches a missing value; optional unordered lists are
configured as data. All expected slots are critical unless a case supplies
`critical_slots`. Full Task Success requires capability, critical slots, policy,
final status and the expected adapter-execution boundary; action bundles, strict
route assertions and structured response assertions must also match when supplied.
`expected_response` prose is advisory, not exact-match success criteria. Add
`response_assertions` for required source facts, e.g. `{"data.id":"memo.main"}`
against the returned WidgetResponse. The original V1 corpus contains no such
structured response assertions, so it does not exhaustively score answer content.

Metrics include capability/slot/policy accuracy; exact task/mode success; correct
clarification; false write/control execution; validator-observed unsupported
arguments; structural model hallucination; actual/requested/blocked LLM calls;
route distribution; unique/paraphrase, metadata domain/category/tag/split groups;
parent-group consistency and group-wide success; and latency mean/p50/p90/p95/p99/max.
A consistent group can be consistently wrong: its all-success metric is separate.

False execution means entering an incorrect mutation service, not merely producing
a proposal or confirmation preview. Business changes without a logged mutation
are also flagged. An unevaluated model branch cannot establish safety of what the
model would have returned. Structural hallucination detection uses unsupported
model capability/argument and source-grounding rejection evidence; it is **not**
a complete semantic hallucination judge. Unnecessary LLM calls conservatively
means an observed production EXACT route nevertheless dispatched the model.
Advisory route hints are not silently treated as an oracle of future parser ability.

Accuracy denominators exclude model/transport unavailable cases and harness errors.
Coverage is always shown. `rate` objects contain numerator `correct`, denominator
`total` and `rate`; for safety/error rates, the numerator counts observed violations,
not desirable successes. A 0% observed violation rate is not a security certification.

## Results and comparisons

Each run writes `raw_results.jsonl`, `cases.csv`, `metrics.json`, `failures.jsonl`,
`trace/`, `config.json`, `progress.json`, `summary.md`, `summary.html`, and
`FAILURES.md`. Output is checkpointed after every observed case. An interrupted
worker never turns missing outputs into passes. Traces may contain input/model
text; default output is under Git-ignored `artifacts/benchmarks/`. Do not commit
results from real or private data. CSV formula text and HTML input are escaped.

The deterministic Korean summary contains overall/source-group/domain/category
scores, routes, LLM, safety, timings, failures, representative examples, comparison
status and measurement-based next priorities. It does not call a report-writing LLM.

`compare` writes JSON/Markdown/HTML and lists improved/regressed cases. Dataset hash,
mode, selected IDs and evaluated IDs must agree by default. `--allow-incompatible`
is only a clearly labelled comparison of shared evaluated cases, not evidence of
whole-system improvement. Compare like modes and the same hardware/thermal/load
conditions. Do not compare the no-LLM development subset to an all-model run and
claim accuracy or speed improved. Parser PRs should retain the baseline suite,
run a separately named post-change model-backed baseline, and add holdout suites.

## V35 after user review / merge

First verify the merged main CI, then use the existing Git updater. The benchmark
does not deploy itself or bypass Git. In the **outer Termux shell**, after the
usual updater and source-identity check:

```bash
bash "$HOME/room-hub/deploy/termux/benchmark.sh" validate --suite room_hub_v1
bash "$HOME/room-hub/deploy/termux/benchmark.sh" run --suite room_hub_v1 --mode full --name baseline-v1-qwen --llm local
```

The wrapper uses the existing `roomhub` PRoot, `/opt/room-hub` binding and
`.venv-v35/bin/python`; it installs nothing and does not stop services. Results
appear under `~/room-hub/artifacts/benchmarks/baseline-v1-qwen/`. Avoid concurrent
voice/model requests and record device temperature/power/load for meaningful
latency comparison. Authentication, when configured, comes only from the existing
`HUB_LLM_API_KEY` environment mechanism; never paste it into a suite or command.
If the deployed model ID/endpoint differs, pass the actual `--model`/`--endpoint`.
No real V35 model result is claimed by development CI or mocked provider tests.

## Validation and rollback

```bash
python -m pytest -q tests/test_benchmark.py tests/test_repo_packaging.py
python -m pytest -q
python scripts/check_repo.py
python -m benchmarks validate --suite room_hub_v1
bash -n deploy/termux/benchmark.sh
```

CI validates the suite and includes the harness tests in Linux's full suite and
Windows's existing portability job. Existing checks/timeouts are not reduced.
Tests cover real-core entry, gold projection, fresh fixtures, shared explicit
sessions, path isolation, modes, scoring, latencies, reports and comparison.
Synthetic provider tests are **not** real Qwen accuracy tests.

Rollback is a reviewed revert PR plus the existing Git updater after its merge.
The benchmark has no production schema migration or service changes. Its result
folders are independent, can be archived or explicitly removed, and are not
required to run the application. Never delete the production `data/` directory.

## Portable SQLite cleanup

The isolated worker installs an owned SQLite connection factory before application
creation, including its import-time disposable app. Transaction contexts retain
SQLite commit/rollback behavior and close their handle on exit. This prevents
exception tracebacks from retaining open files on Windows. The factory rejects
paths outside the temporary tree and is restored on Runtime.close(); deployed
application connections and business semantics are not modified.
