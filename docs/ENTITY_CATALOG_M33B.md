# M3.3-B — Request-local Entity Catalog / Named Memo Grounding

## Scope

This patch follows the merged M3.3-A source, not a benchmark-specific interpreter.
It adds a metadata-only, request-local catalog and bounded title-based memo
selection. Existing exact-normalized and unique literal substring matching remain
in `entity_resolver.py`; no fuzzy matching, inferred aliases, embeddings, ASR
repair, or model-generated entity IDs are added.

Version identities: app 0.1.7, Semantic Parser 1.2.0, Interaction Model / Dialog
1.0.0 (unchanged); Entity Catalog 1.0.0, Memo Grounding 1.0.0, Benchmark 1.5.0.
M3.3-A's existing 250-case no-regression result is a baseline, NOT evidence that
its manager multi-turn UX was tested on the user's device. That acceptance remains
separate until actually confirmed.

## Catalog and matching

`EntityCatalog` is an immutable snapshot of server-owned id/title/version and
small metadata fields. No body, task notes, credentials, permissions, confirmation
authority, or model prompt is copied into it. There is no persistent index or
cross-request cache: every grounding reads the current server rows.

- Todo / Calendar: the existing scoped read remains authoritative. Both use the
  same task table; no artificial domain separation or completion-state filtering
  is introduced to make a duplicate target unique. The legacy atomic write path
  and the semantic path use the same catalog/Resolver selection rule.
- Memo: `life.note_catalog_snapshot()` uses a read-only SQLite transaction with
  metadata columns only and the existing 200-note limit. Reading 201 rows detects
  an oversized store instead of treating a prefix as a complete catalog. Pinning
  and sharing do not change named-target selection. Private titles participate in
  the manager's uniqueness check; they are never sent to the model/display.
- Exact NFC/whitespace-normalized title first, then unique literal substring.
  A one-character non-exact substring is not enough. Multiple matches, malformed
  rows and truncated catalogs fail closed. No target = no default-note fallback.
- The catalog records source, scope, observation time, row count, truncation and
  content hash. The hash excludes observation time/order but includes metadata;
  it is not a signature or a snapshot of body bytes. Candidate evidence is bounded
  and private to manager/benchmark traces.

## Named memo grammar (synthetic examples)

| Request | Existing operation | Behavior |
|---|---|---|
| `포장 정리 메모 읽어줘` | memo.read | Read the unique stored note, not the default card |
| `포장 정리 메모 비워줘` | memo.clear | Existing confirmation; clear body, not delete note |
| `포장 정리 메모에 새 문구 덧붙여줘` | memo.append | Append literal text; no automatic newline insertion |
| `포장 정리 메모를 새 문구로 바꿔줘` | memo.write | Replace body of selected note; never create instead |

The prefix is a contiguous spoken title; the whole supported utterance is matched.
Title normalization uses the same Resolver as tasks. Body/text are copied from raw
source spans, preserving internal whitespace. Unsupported constraints, negation,
quoted/chained instructions and ordinal/pronoun references are not guessed.
Ordinary noun titles such as `사과 분류`, `회의 정리`, and `메모리 테스트` are not
mistaken for commands/conjunctions just because they contain a substring.

Unrecognized title-first grammar can still use the existing model proposal path,
but a server guard refuses to substitute current/latest for the unhandled title.
This guard is a refusal boundary, not another target parser. Standard old current,
latest, explicit-ID and new-note paths retain their existing validators.

This is NOT `memo.search`: include/related/search requests are not weakened to a
normal read. It is NOT memo rename/delete/list or multi-read. No operations or
permissions are added to WidgetRegistry or the legacy capability registry.

## Current/latest and privacy

`현재 메모` still means the saved note widget's default card, including legacy
fixed text. `방금 수정한 메모` still uses updated_at with the existing tie check.
Neither means the card currently selected on a particular browser. A named memo
never silently falls back to either selector.

Only after a unique metadata match does the existing MemoAdapter read the selected
body. Version/title/id are compared against the catalog to reject a rename/update
between lookup and read. Manager reads may select private notes. The existing
public allowlist rechecks shared status, note-widget opt-in and current version on
EACH saved-response projection. Unsharing, editing, deleting or removing the widget
hides stale body output. Failed/ambiguous reads cannot expose candidate titles on
the shared display. Write requests and results remain manager-only.

## Confirmation and replay

No non-timer write is automatic. The selected ID/version enters the existing
preview, permission validation, confirmation expiry and durable receipt path.
Source plans are recomputed from original raw text, not model guesses; confirmation
checks the immutable notebook source hash as well. A stale entity version blocks
mutation. Completed effect receipts are checked before querying a catalog again:
retrying an old clear/write must not target a newly created note with the same name.
Existing uncertain-receipt handling remains unchanged; this is not a new
exactly-once guarantee across a process crash.

Memo slot filling is not introduced. Incomplete/ambiguous memo targets require a
new explicit request. Existing M3.3-A task/alarm dialogues continue through their
same typed proof and confirmation path.

## Measurements

Named memo plans use the `ENTITY_CATALOG` route, separate from the M3.2
`SEMANTIC_PARSER` route. Trace includes `stages_memo_plan`,
`stages_entity_catalog`, and existing `stages_entity_resolution`. Runtime catalog
reads use the same owned temporary Store in benchmark mode. NLU mode stops before
catalog reads; decision/full use normal grounding and existing action boundaries.
Catalog reads are internal read-only grounding, not newly registered adapter
operations. Model prompts and payloads are unchanged.

Support/scoring/projection/taxonomy/raw evaluation files are not modified. The
external 250-case suite and its previous analyses remain immutable. Expected
support cohort is the same 144 operations-exposed cases, but actual score/cohort
must be rechecked on V35. Do not claim Memo 12/12 or an accuracy/latency gain from
synthetic tests; legacy memo selector/projection mismatches may remain.

## Verification

```bash
python -m pytest -q tests/test_entity_catalog_m33b.py tests/test_memo_grounding_m33b.py tests/test_benchmark_catalog_m33b.py
python -m pytest -q
python scripts/check_repo.py
python scripts/check_benchmark_policy.py
python scripts/memo_catalog_browser.py
python scripts/dialog_browser.py
python scripts/widget_bridge_browser.py
python scripts/assistant_browser.py
python scripts/live_smoke.py
```

All data are synthetic and temporary. The browser script uses real loopback
HTTP/SQLite with the explicit browser fetch bridge; it is not physical V35,
Safari, microphone, real Qwen inference or a full networking/security audit.

## Device acceptance after user merge

Review/merge the PR and wait for main CI. Outer Termux:

```bash
bash "$HOME/room-hub/deploy/termux/update-from-git.sh"
bash "$HOME/room-hub/deploy/termux/update-from-git.sh" --check
grep -n '^CATALOG_VERSION' "$HOME/room-hub/app/entity_catalog.py"
grep -n '^MEMO_GROUNDING_VERSION' "$HOME/room-hub/app/memo_grounding.py"
cat "$HOME/room-hub/benchmarks/__init__.py"
```

Check 1.0.0 / 1.0.0 / 1.5.0 respectively. No model or dataset reinstallation.
In the manager create temporary memo `M33B 포장 정리` with a recognizable body and
another default/pinned note. Test named read without model, clear with no change
until confirmation, duplicate titles requiring clarification, and private-note
redaction from the paired display. Use synthetic unimportant content only. Keep
M3.3-A dialogue testing separate; test a missing title followed by a typed reply
and normal confirmation as well.

```bash
bash "$HOME/room-hub/deploy/termux/benchmark.sh" verify-analysis m33a-taxonomy-v1
bash "$HOME/room-hub/deploy/termux/benchmark-data.sh" verify room_hub_v1
bash "$HOME/room-hub/deploy/termux/benchmark.sh" run --suite room_hub_v1 --mode full --name m33b-smoke-01 --llm local --limit 3
bash "$HOME/room-hub/deploy/termux/benchmark.sh" run --suite room_hub_v1 --mode full --name m33b-parser-v1-qwen --llm local
bash "$HOME/room-hub/deploy/termux/benchmark.sh" analyze m33b-parser-v1-qwen --name m33b-taxonomy-v1
bash "$HOME/room-hub/deploy/termux/benchmark.sh" verify-analysis m33b-taxonomy-v1
bash "$HOME/room-hub/deploy/termux/benchmark.sh" compare-analysis m33a-taxonomy-v1 m33b-taxonomy-v1 --name m33a-vs-m33b-case-v1
```

Use new result names. Do not reanalyze old runs with changed production bytes.
Preserve all prior analyses. Inspect case-level losses, false execution, unchanged
support set, named memo successes, route/call counts and latency; do not change the
scoring rules just to pass the new feature. Three-case smoke is only a harness
check, not a catalog or privacy acceptance test.
