# Grounded read Fast-path (0.1.7 stabilization)

## Scope and sources

Only `MEMO_READ`, `TODO_LIST`, and `CALENDAR_QUERY` gain a deterministic fast lane.
The existing assistant request API/history, authenticated manager boundary,
write previews/confirmation/receipts, ASR, model, and framework remain in place.
No dependency or database schema change is required.

```
explicit submitted transcript -> existing normalization -> anchored read grammar
 -> frozen clock / saved widget / updated_at resolver -> existing SQLite services
 -> deterministic formatter -> existing assistant history / final response
```

Clear reads bypass the model queue, including while another model request or
another transcription is active. No model load, probe, HTTP call, or second model
formatting pass is performed. The unrelated active model request is not cancelled.
A transcript is still submitted through the existing manager workflow; this does
not automatically execute microphone recordings or grant display write access.

`auto`, `chat`, and legacy direct-send modes all recognize these clear reads
before model dispatch. Nonmatching inputs keep the old mode behavior. Existing
unambiguous write rules and server-only clarification safeguards remain intact.

## Recognition

`app/read_patterns.py` holds anchored Korean patterns, separate from reads.
`app/fast_reads.py` reuses `command_semantics.known_read` and the existing clock
resolver rather than replacing previously working read rules. Quoted, chained,
negated, or additional-filter expressions do not loosely match the new grammar.

| Input examples | Plan | Source |
|---|---|---|
| 현재 메모 읽어줘 / 메모 내용 읽어줘 / 현재 메모에 남아있는 내용 읽어줘 / 메모 뭐라고 적혀 있어 | MEMO_READ: current | Saved default note widget / `hub_notes` |
| 방금 메모에 작성한 내용 읽어줘 / 방금 적은 메모 보여줘 / 아까 작성한 메모 읽어줘 | MEMO_READ: last_modified | `hub_notes.updated_at` |
| 오늘 할 일 확인해줘 / 오늘 할 일 뭐 있어 / 내일 할 일 알려줘 | TODO_LIST | `Store.query_tasks` / `tasks` |
| 오늘 일정 알려줘 / 오늘 오후 일정 확인해줘 / 오늘 오전 일정 알려줘 / 내일 일정 알려줘 | CALENDAR_QUERY | Same `tasks` source as the current calendar widget |
| 오늘 할 일 / 오늘 오후 일정 | Dated brief query | Same read services |

There is **no external calendar integration** in this change. The existing
calendar displays Room Hub task dates/times, so calendar queries use that same
source, not invented meetings or Google Calendar data.

## Resolving "current" and "just now"

The browser's selected note card (`viewState.noteId`) is not persisted or sent to
the server today. There is no authoritative `active_widget` or `last_action` to
reuse for that selection. This patch does not fabricate one or introduce a new
session-state system.

- **Current:** first note widget in the saved layout, and that widget's default
  card. The same precedence as `widgets/note/widget.js` is used: a nonempty
  `widget_data:note.text` (nullish fallback to instance config text) first,
  otherwise the first shared note ordered by pinned, updated_at, id.
- **Just now / earlier:** newest stored note by `updated_at`, independent of
  pinned order. This is the most recently saved/edited note, not a guessed event
  from model memory. If newest timestamps tie, ask for clarification. If there
  are no saved note rows, use the current/default widget card and label that
  fallback honestly. Legacy widget text has no per-card modified timestamp.
- No current note widget: normal clarification, not a model answer. Empty notes:
  honest empty result. Multiple widgets: default saved layout order is explicit.
- Responses label the **default card**, not the card the iPad is presently showing.
  `resolved_context.active_card_known=false` records this limitation.

The manager can read the latest private note. A placed LLM reply widget must not
turn that into a display leak: `llm_display` rechecks note sharing, version and
note-widget presence on each fetch. Private, revoked, deleted, edited or removed
sources are replaced with a generic manager-only message in the display DTO.
The manager's original history remains unchanged. No display submission/notes
CRUD authority is added.

## Date and period policy

Use the request-created reference clock and configured timezone from
`clock_service`, including today/tomorrow near midnight. Existing stale-request
and changed-timezone checks are retained.

- Morning: `00:00 <= time < 12:00`.
- Afternoon: `12:00 <= time < 24:00` (includes evening; no fuzzy daypart guessing).
- Untimed tasks are not assigned a period. The reply explicitly counts those
  excluded from the requested date/status range.
- SQL period filtering occurs **before** count/LIMIT in the same read snapshot.
- Date-only queries include untimed tasks. Existing pending/completed/all and
  date/range aliases remain supported.
- The current task schema has a single time, not event duration/overlap semantics.
- At most 50 task rows are formatted, with an explicit count/truncation notice;
  no hidden claim that a partial list is complete. Memo body is not summarized.

Source/DB exceptions are failures, not an empty list and not a model fallback.

## Fallback and optional values

Nonmatching personal-task requests retain the old structured parser; general
conversation retains the old local chat path. Existing explicit unsupported or
unsafe requests may still clarify without calling the model. For example,
`내일 하늘에 우유 사기 추가해.` does not match a read intent: it follows the
existing parser when the model is enabled, then validation/confirmation.
No fuzzy rewrite from `하늘` to `할 일` is introduced.

Missing `time` and `date_ref` are JSON null. A small before-validator normalizes
only empty/unknown/none/null/n-a placeholders (the spelling `n/a`, case-insensitive)
to None. Other invalid clocks still fail strict validation and produce the normal
`needs_clarification` state, not a Pydantic traceback to the user. An explicit
source time omitted by a proposal also clarifies rather than silently dropping
it. Optional missing time is permitted for an untimed task; missing required
source/title information still clarifies. A write remains confirmation-only.

`memo.read` is registered as server read-only and excluded from the LLM parser's
schema/prompt: a model proposal cannot use it to choose arbitrary memo data.

## Diagnostics

Existing `assistant_runs.record_json` carries a `routing` object; no new table:

- `route`: FAST_PATH / LLM_FALLBACK / EXISTING_RULE (old server-only rules).
- `route_reason`, `resolved_intent`, `resolved_context`, `source_of_truth`.
- `llm_called`, `llm_seconds`, `router_seconds`, `fast_path_seconds`.

The existing lowercase `assistant.route` values remain for UI/history compatibility.
Fast reads keep `dispatch_attempted=0`, no provider calls/raw output, empty token
metrics and 0 LLM seconds. The admin detail panel shows the route/source/intent
and `0초 · 토큰 N/A`. Resolved context is included in the existing debug details.
Historical records are not reclassified. General model generations keep their
actual provider-reported metrics; text length is never used as a token estimate.

## Validation and measurement

Use a development or CI checkout, **not the production V35 data directory**:

```bash
HUB_DATA_DIR=artifacts/runtime-data python -m pytest -q tests/test_fast_reads.py
HUB_DATA_DIR=artifacts/runtime-data python scripts/fast_reads_browser.py
HUB_DATA_DIR=artifacts/runtime-data python scripts/benchmark_fast_reads.py --iterations 30
```

Tests use synthetic notes/tasks and a model test double/call counter. They cover
all seven requested cases, anchored negative examples, missing/invalid times,
no-LLM modes, busy model/transcription isolation, source failure, date/period
boundaries, latest edited memo/ties, privacy revocation and existing writes/chat.
The new UI test uses real synthetic Uvicorn HTTP/SQLite through the repository's
explicit Python browser bridge, with injected invalidations. It does not claim
native iPad networking/physical Safari testing. CI also runs existing direct
browser, server, container and preview regressions.

The benchmark records API and router/read/format p50/p95/max in
`artifacts/test-results/fast-read-latency.json`, with an asserted zero model count.
Its container/CI results exclude STT, actual V35 hardware and network latency.
Sub-second V35 post-STT latency is a goal, not a measured guarantee; SQLite locks,
phone thermal load and storage can add delay. No STT/model parameter is changed.

## V35 deployment and rollback

Review/merge this PR, then confirm the **merged main** CI. Existing path:
`$HOME/room-hub`, outer Termux; service `$PREFIX/var/service/room-hub`.

Normal application:

```bash
bash "$HOME/room-hub/deploy/termux/update-from-git.sh"
bash "$HOME/room-hub/deploy/termux/update-from-git.sh" --check
curl -fsS --max-time 5 http://127.0.0.1:8088/healthz
echo
sv status "$PREFIX/var/service/room-hub"
```

If this phone is still at the updater version that rolled back after printing
`HEAD^{{tree}}` (last reported `4138e5a...`), load the repaired updater **from the
fetched Git object**, not by editing a tracked production file. This also works
on an already corrected installation:

```bash
(
  set -eu
  umask 077
  : "${PREFIX:?Use the outer Termux shell}"
  ROOT="$HOME/room-hub"
  test "$(git -C "$ROOT" branch --show-current)" = main
  test -z "$(git -C "$ROOT" status --porcelain --untracked-files=normal)"
  git -C "$ROOT" fetch origin main
  updater="$(mktemp "$HOME/.room-hub-updater.XXXXXX.py")"
  trap 'rm -f "$updater"' EXIT
  git -C "$ROOT" show origin/main:deploy/termux/update_from_git.py > "$updater"
  ROOM_HUB_ROOT="$ROOT" "$PREFIX/bin/python" "$updater"
)
```

Then run the normal `--check`, health and service checks above. Do not perform a
bare pull first and then assume the updater's ALREADY CURRENT branch restarts
anything. The existing updater owns stop/SQLite backup/fast-forward/restart/health.
No dependency installation is needed. Refresh the admin page; the manager asset
cache key changes. App version remains 0.1.7; verify commit and route diagnostics.

No new schema migration: reverting this PR through a reviewed Git revert PR
removes the new behavior without deleting notes/tasks/alarms. Existing history
JSON with extra fields is tolerated. Old code cannot apply the new memo-history
privacy projection, so disable the shared LLM response widget before reverting
(or remove sensitive new memo query history in the manager); do not expose private
memo results to old display code. This is distinct from the updater's automatic
failed-deployment rollback, which restores its pre-update source/DB snapshot.
