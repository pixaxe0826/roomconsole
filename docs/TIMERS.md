# Independent timer widgets — 0.1.7 / widget 1.1.0

## User-visible contract

Place multiple `timers` widgets in the saved dashboard layout. **One layout instance
ID owns one countdown.** Each 2×2 card has its own minutes/seconds fields, remaining
time, start/stop controls, and opt-in sound. All cards stay visible on the dashboard;
touching a timer does not navigate to a shared fullscreen list. Generic dashboard
keyboard/remote expansion still opens that same independent card.

The accepted duration is **1–600 integer seconds**. Minutes are 0–10 and seconds
0–59; their sum must be 1 second through 10 minutes. Invalid, decimal, negative,
zero or >600-second values are rejected, not rounded or clamped. A running card's
fields/start button are disabled. Stop it before starting another run in that card.
Other cards remain editable and continue running. After completion the same duration
can be started again. The most recent run of each placed widget restores its duration
on reload; unsent edits are local draft state and are not durable configuration.

Widget independence is **not device independence**: opening the same saved widget ID
on another paired device shows that same countdown. Different IDs on one screen have
independent countdowns. This remains a single-user, shared-layout workspace.

## Ownership and API

`hub_timers.widget_id` is a nullable, additive column. A partial UNIQUE index permits
only one `running` row per non-null widget ID. A transaction expires due rows, checks
that an explicitly requested widget exists and is a timer, checks occupancy, creates
the new run, and commits its durable request receipt. A competing/new start on an
occupied card returns 409 without resetting its deadline or creating a hidden timer.

The authenticated narrow display API remains:

| Request | Body / result |
|---|---|
| GET `/api/timers` | Shared snapshot; each item includes `widget_id` |
| POST `/api/timers/start` | `request_id`, `duration_seconds`, UI `widget_id`, optional `label` |
| POST `/api/timers/{timer_id}/stop` | `request_id`, optional `version`, UI `widget_id` |

Mutation responses retain the timer fields and add a fresh `snapshot`. The UI applies
that snapshot immediately instead of waiting for (or being blocked by) an older poll.
Scoped stop rejects a mismatched widget/run. It never resolves another widget's
current timer. Recovery controls intentionally stop a specific orphan run ID.

The existing Widget Protocol 1.0 envelope/actions stay intact:
`timer.list`, `timer.get`, `timer.start`, `timer.stop`. TimerData gains optional
`widget_id`; the model's input schema does **not** gain the ability to invent a
widget ID. UI ownership is passed through the narrow HTTP request model.

Existing unscoped voice/Protocol starts allocate the first idle placed timer card,
ordered by row, column, then ID. If all placed cards are busy, the server rejects the
start with an instruction to add a card or stop a countdown. It does not replace a
running timer or produce another hidden run. With no timer layout, trusted legacy
service/Protocol calls retain their former unassigned behavior; no display write
permission is granted without the existing layout opt-in.

`현재 타이머 종료` retains its explicit voice meaning: **the most recently started
RUNNING timer in the workspace**. A card's stop button always uses that card's actual
run ID, never this global reference. Clear phrases such as `4분 타이머 시작` and
`현재 타이머 종료` still use the existing exact rules with zero LLM calls. No model,
Whisper, prompt or generic parser changes are part of this patch.

## Migration and old timers

Startup and an authenticated layout save reconcile legacy **unassigned running**
timers to empty placed cards, oldest first. Existing IDs, original deadlines,
versions and request receipts are preserved. Bound runs never move when another
card is started, stopped, renamed or reordered. No layout is automatically added,
resized or deleted.

If there are more legacy timers than available cards, or a running timer's widget
has been removed, the run is not silently stopped/deleted. The first timer card has
an inline `이전·제거된 위젯 타이머` recovery section with countdowns and explicit stop
buttons. More cards can be added and saved to bind remaining unassigned legacy runs.
Removed *bound* IDs are not automatically rebound to unrelated cards.

Legacy unscoped request signatures remain compatible. Replay happens before slot
allocation/current resolution, so retrying a start does not select the next free
card and retrying `current` stop does not stop the next run. The receipt and mutation
are one SQLite transaction. Replay after restart is supported. A new request ID is
an intentionally new command; retries with the same ID and different scope/body
conflict. Notification failure cannot undo a committed change.

## Clock and refresh fixes

The countdown is derived from the persisted UTC deadline and a server-time sample,
plus browser monotonic elapsed time. It is not implemented by subtracting one from
a counter on each interval. Rendering updates about every 200 ms; the existing
server expiry loop runs about every 250 ms and snapshots also derive due status.
There are no per-second database writes. Server process/clock/network scheduling
means this is not a hard-real-time timer or guaranteed alarm delivery.

The previous client re-anchored its clock when it reused an identical cached
snapshot on reconnect, effectively adding the cache age back to the countdown.
Identical/older samples now cannot reset the anchor. Invalid timestamps are ignored.
A successful start/stop provides an immediate snapshot; older in-flight reads cannot
roll it back. Foreground/visibility and page-cache restoration request a fresh sample,
including a trailing refresh if a pre-resume poll was already outstanding. Client
wall-clock/monotonic discrepancies request resynchronization, not an invented time.
Server wall-clock changes and restarts still use the persisted UTC deadline contract.

The display no longer reconstructs all timer fields merely because
`remaining_seconds` changed. Draft values are keyed by instance ID and input focus
is restored to the same minutes/seconds field on a necessary dashboard redraw.

## Sound, permissions and limits

Sound is disabled by default and enabled separately for each card in that browser.
Only an authorized, visible, connected foreground page may attempt the short WebAudio
chime. Initial historical expired rows, embedded manager previews, hidden/offline
pages and muted cards do not chime. `expired` is not proof that audio played. This is
not an OS alarm, wake-up mechanism, background push or lock-screen guarantee.

Only timer start/stop retain the existing immediate-control exception. Notes,
tasks, calendar and alarm mutation confirmation policies are unchanged. Pairing,
revocation, authenticated actor scoping, Origin/CSRF checks and layout opt-in remain.
The global limits are 32 active timers and 10,000 durable request receipts. History
retains recent 256 runs plus each currently placed card's latest run (at most the
layout's 40-card bound); running entries are never pruned. Receipt exhaustion rejects
new work instead of deleting receipts and accidentally replaying old commands.

## Validation

```bash
python -m pytest -q tests/test_timers.py tests/test_timer_bridge.py tests/test_timer_instances.py
node scripts/timer_transport_regression.js
python scripts/timers_browser.py
python scripts/build_previews.py
python -m pytest -q
python scripts/check_repo.py
python scripts/live_smoke.py
```

The browser test covers two independent 2×2 cards (240/90 seconds) on one screen,
independent drafts/start/stop, 1/600 boundaries, invalid values, per-card sound opt-in,
reload/server-clock synchronization, escaped titles, and inline orphan recovery.
1194×834, 1024×768 and 834×1194 are checked. Unit tests also cover races, receipts,
restart, the actual pre-instance SQLite migration and preservation of the other card.
The deterministic Node test exercises the **production** clock/transport code.

`HUB_BROWSER_BRIDGE=1` is an explicit restricted-environment test mode: production UI
uses real Python HTTP/API/SQLite but injected browser invalidations. CI uses the normal
direct Chromium HTTP/WS path. Neither path claims V35, actual Qwen/Whisper, iPad Safari,
physical audio or long-duration sleep testing.

## Apply and rollback

Review/merge the timer PR, confirm the **merged main** CI is green, then run the existing
`deploy/windows/V35_Update.bat`. Its SSH target and safe Git backup/fast-forward/restart/
health verification are unchanged. Do not merge the separate benchmark PR to apply this
fix. Alternatively, in the outer Termux shell:

```bash
bash "$HOME/room-hub/deploy/termux/update-from-git.sh"
bash "$HOME/room-hub/deploy/termux/update-from-git.sh" --check
curl -fsS --max-time 5 http://127.0.0.1:8088/healthz
```

Reload the display and manager after updating. The timer manifest is now 1.1.0 and
client script URLs use the `timers2` cache key. Existing placed timer widgets become
independent without recreating the layout. Add extra 2×2 timer widgets only as needed.
Check 4:00 on A and 1:30 on B, stop A and confirm B continues, then test reload.

Rollback through a reviewed revert PR and the same updater. Do not delete the additive
column, tables, receipts, operational data or model files. Code rollback does not undo
already started/stopped runs and old UI returns to the old shared-list behavior.
