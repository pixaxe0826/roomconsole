# M3.3-A — Interaction Model and bounded slot filling

## Scope and identity

This is a linguistic metadata and explicit missing-slot dialogue layer, not a new
execution/capability registry or an independent trained NLU. Existing single-turn
FAST_PATH, M3.2 semantic routing, legacy atomic writes, timer ownership and model
prompts remain unchanged. The app remains 0.1.7; Semantic Parser stays 1.2.0;
Interaction Model and Dialog are 1.0.0; Benchmark metadata version is 1.4.0.

`app/interaction_model.py` contains frozen `IntentSpec`/`SlotSpec` definitions.
Startup validates referenced operations and argument fields against the actual
WidgetRegistry. It never registers operations or grants permissions. Initial
request observations are `activated=false` and reuse the existing source parser;
phrase-family labels are metadata, NOT automatically learned utterance examples.
They must not be reported as an independent shadow classifier accuracy result.

## What can be continued

Only a new `auto` request that actually finishes `needs_clarification`, with a
source-grounded missing required value, can create a pending dialogue:

| Existing operation | Required values that may be filled | Retained semantics |
|---|---|---|
| todo.add | date, title | time optional |
| calendar.add | date, title | time optional; no all-day inference |
| todo.complete / reopen / delete | target_text | exact/unique server target resolution; date qualifier optional |
| alarm.set | date, exact time | existing future-time validation and manager confirmation |

Typed replies fill ONE awaited value. Missing date comes before title/time.
Source-literal titles remain open vocabulary noun phrases, but commands, negation,
quoted instructions, yes/no approval and unresolved pronouns are not treated as
safe values. Invalid replies retain known values and ask for the same field again.
Ambiguous or unsupported original requests do not become executable by guessing.
The user can cancel this information-entry dialogue with `취소` or the cancel button.
This does not delete a task, stop a timer, cancel an existing alarm or undo a write.

Examples (synthetic):

1. `내일 할 일 추가해` -> title prompt -> `합성 물품 포장` -> existing write preview.
2. `할 일 추가해` -> date prompt -> `내일` -> title prompt -> `합성 물품 포장` -> preview.
3. `내일 알람 맞춰줘` -> clock prompt -> `오후 세 시` -> existing alarm preview.
4. `완료 처리해` -> target prompt -> an exact existing title -> resolve and preview.
5. `3시` as a time reply is ambiguous; no invented AM/PM or model call.

## Explicit request and session binding

There is no global last-pending request. The manager selects a particular pending
request and sends its state digest to `POST /api/llm/requests/{rid}/dialog/reply`.
A new ordinary voice/text request is NEVER automatically attached to a pending
conversation. General pronoun/ordinal context, slot edits, multi-intent chaining,
voice confirmation and automatic second-utterance capture are outside this patch.
The first source may originate from voice recognition; FOLLOW-UP entry in this
version is typed text in the manager's selected-request form.

The owner is derived by the server from the authenticated admin session, never
accepted from request JSON. Cookie sessions are separate. Reuse of the same admin
Bearer credential represents the SAME manager identity; it is not per-device
isolation. Automatic/internal speech sources without a manager session are unbound
until an admin explicitly selects and claims that exact pending request. Subsequent
replies must have the same owner. Paired displays and ingest tokens gain no reply
or general write permission. Existing CSRF protection applies.

The dialogue has a fixed 180-second lifetime from the ORIGINAL request, maximum
six replies, and is invalidated by timezone/day rollover, source-integrity or
version mismatch. Invalid answers do not extend the lifetime. A consumed parent
accepts only an idempotent replay of its same request key/signature; another reply
requires the latest child request/digest. Concurrent replies cannot fork the parent.

## Source proof and execution boundary

`assistant_runs.record_json` stores dialogue metadata and a proof containing the
original source plus individual raw replies and server timestamps. No new tables
or dependencies are added. Each replay reconstructs typed values from original
source turns; it does not join them into a fabricated utterance. Per-slot provenance
points to the turn and normalized reply span. Domain IDs, entity versions,
permissions and confirmation tokens are NOT supplied by this dialogue proof.

Before final grounding and confirmation, stored raw source text, hashes,
timestamps and parent chain are checked against the immutable `llm_requests` rows.
Deleting or altering a source ancestor prevents a new execution. Only server reads
supply actual target IDs and versions. The existing WidgetBridge performs read-only
duplicate/target checks, freezes the preview and requires manager confirmation.
Existing digest, expiry, current target version, durable reservation and receipt
checks remain authoritative. A fixed dialogue TTL is not a replacement for the
existing preview expiry; after completion, the normal preview confirmation policy
applies. Receipt replay still precedes any attempt to retarget a completed write.

A selected adapter class/WidgetRequest in a trace is not proof of service execution.
Tests spy on actual adapter calls and check SQLite state. This is not an exactly-once
claim for a process crash during an adapter write; existing uncertain-receipt rules
remain unchanged.

## Manager UI and API

After updating, make a NEW incomplete request in automatic mode. Existing old
requests are not retroactively given dialogue state. Its detail shows the known
values, the missing value, expiry and an additional-information field. Send only
the requested value. On completion the new child detail displays the EXISTING
confirmation button; merely filling the slot does not modify business data.
The runtime-only `manager-dialog.js` add-on preserves the existing notebook code.
Unsent field text is kept through periodic refresh. Static standalone previews
intentionally omit this live-only panel; they have no authenticated pending state.
`dialog_browser.py` tests the exact production add-on over the test HTTP bridge. A consumed root links to the
next request. The generic 'retry original request' action is disabled for dialogue
children because their raw source is only a slot reply, not a standalone command.

Manager-authenticated API body (use a fresh request ID; keep it for network retries):

```json
{
  "request_id": "dialog-reply-client-unique-001",
  "text": "합성 물품 포장",
  "expected_state_sha256": "<GET parent dialog.state_sha256>"
}
```

Read the new `id`/`dialog` from the 202 response. Invalid typed values create a child
still asking for the same value. A stale state, expired dialogue or changed source
returns 409; a different owner returns 403. Unknown authority/slot fields return
422. A successful HTTP response does not imply a business write: inspect status
and use the normal confirmation workflow. A conflict from an adapter can remain
an application-level failure in a 200 confirmation envelope, as before.

## Measurement and regression policy

The existing 250-case suite is a SINGLE-TURN measurement; no `context.*` capability
is registered or artificially scored as supported by this patch. Store original
M1/M3.2 analyses unchanged. The new benchmark metadata records interaction/dialog
versions and hash, not a claim that every case used the feature. Runtime traces
carry shadow observations and safe dialogue metadata when present. Scoring,
projection, support definitions and taxonomy are unchanged.

Acceptance has two separate parts:

- Existing 250-case baseline: preserve the prior supported cohort and individual
  successes, no newly observed false execution; inspect per-case differences.
- Synthetic multi-turn HTTP/UI tests: required values, cancellation, raw proof,
  expiry, isolation, concurrency and manager-only confirmation. These are not
  V35/Qwen/ASR accuracy measurements and must not be merged into the 250-case rate.

Calendar time-unspecified vs all-day policy, dynamic entity catalog/memo grounding,
ASR grammar/second-pass, free-form context and new search/multi operations remain
separate future work. In particular this patch does not fix missing-time calendar
expectations by inferring meaning from the event title.

## Validation commands

```bash
python -m pytest -q tests/test_interaction_model_m33.py tests/test_dialog_state_m33.py
python -m pytest -q
python scripts/check_repo.py
python scripts/check_benchmark_policy.py
python scripts/live_smoke.py
python scripts/dialog_browser.py
python scripts/assistant_browser.py
python scripts/build_previews.py
```

Run the new test modules in Windows portability CI, the full suite on Linux and
the new UI test alongside the unchanged Chromium regression suite. Actual browser
networking/V35/Safari/model checks still belong to device acceptance.

## V35 acceptance after merge

Outer Termux (not Windows PowerShell):

```bash
bash "$HOME/room-hub/deploy/termux/update-from-git.sh"
bash "$HOME/room-hub/deploy/termux/update-from-git.sh" --check
bash "$HOME/room-hub/deploy/termux/benchmark.sh" verify-analysis m32-taxonomy-v1
bash "$HOME/room-hub/deploy/termux/benchmark-data.sh" verify room_hub_v1
```

Check Interaction Model 1.0.0, Dialog 1.0.0 and Benchmark 1.4.0 in their files.
Semantic Parser intentionally remains 1.2.0. Test the manager scenarios above with
non-important synthetic tasks before running the external suite. No dataset
re-upload or model reinstallation is required.

```bash
bash "$HOME/room-hub/deploy/termux/benchmark.sh" run --suite room_hub_v1 --mode full --name m33a-smoke-01 --llm local --limit 3
bash "$HOME/room-hub/deploy/termux/benchmark.sh" run --suite room_hub_v1 --mode full --name m33a-parser-v1-qwen --llm local
bash "$HOME/room-hub/deploy/termux/benchmark.sh" analyze m33a-parser-v1-qwen --name m33a-taxonomy-v1
bash "$HOME/room-hub/deploy/termux/benchmark.sh" verify-analysis m33a-taxonomy-v1
bash "$HOME/room-hub/deploy/termux/benchmark.sh" compare-analysis m32-taxonomy-v1 m33a-taxonomy-v1 --name m32-vs-m33a-case-v1
```

Use new result names, never overwrite prior runs. Do not analyze old raw results
with changed production bytes; compare the preserved analyses. The first three
suite cases are a harness smoke, not a multi-turn feature test.
