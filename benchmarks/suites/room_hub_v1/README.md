# Room Hub Assistant Benchmark V1 dataset

The user-supplied corpus has 250 text cases: 100 unique instructions, 150
paraphrases, and 52 annotated capabilities. These capabilities describe the
**evaluation target**, not 52 features claimed implemented by Room Hub. Audio is
not included. The fixture clock is `2026-09-21T09:00:00+09:00`, Asia/Seoul.

`all_250.jsonl`, `fixtures.json`, `unique_tasks_registry_v1.json` and `SOURCES.md`
are preserved byte-for-byte. `provenance.json` records their hashes and the input
ZIP hash. The manifest adds generic loader paths. `schema.json` adds a small typed
slot-schema dialect; this validates annotation shape, not model interpretation.
`projection.json` describes explicit representation aliases only. No case/input
special handling is in executable benchmark code.

## Files

- `manifest.json`: suite/version, clock/timezone, case files, schema, fixtures,
  optional registry and projection, optional expected counts.
- `all_250.jsonl`: one case per line, with ID/input/expected/source type/parent/tags.
- `fixtures.json`: synthetic todos/calendar/memos/alarms/session; optional named
  `snapshots` can be referenced with a case's `fixture_id`.
- `schema.json`: required fields, status values, capability slot schemas and
  fixture-ID slot references. Supported type/enum/format/properties/required/
  additionalProperties/items are a documented subset, not full JSON Schema.
- `unique_tasks_registry_v1.json`: original task register, including counts.
- `projection.json`: native capability/slot names, target ID fields, unordered
  list slots. This file cannot supply expected slot values to the assistant.

## Add / remove / replace

Add a case line with a distinct ID/input, valid expected object and source type.
`expected` requires `domain`, `capability`, `slots`, `policy`, `status` and boolean
`adapter_should_execute`. `policy` accepts EXECUTE/CONFIRM/CLARIFY/CANCEL. A
paraphrase must name an existing unique case in `parent_id`; a unique case has no
parent. Update optional manifest and registry counts, bump the suite version,
and run validation. Removing a parent also requires removing or reparenting its
paraphrases. Do not modify Python code just to add or remove cases.

Optional fields include `split` (defaults to development), `category` (otherwise
variant_style), `critical_slots`, `response_assertions` (paths within the actual
WidgetResponse), `fixture_id`, and `session_id` with a positive `turn`. A selected
session must include all prerequisite turns from 1. State persists only in that
session; the tool does not add conversational context understanding to the core.

For another suite, make a sibling directory with a manifest and its declared
files. The default case filename is `cases.jsonl`; it is not tied to this corpus.
An optional registry may be omitted. New capability annotations are accepted
without changing the production registry; unsupported features then remain
observable limitations. Use the existing fixture storage shape or add a reviewed
fixture/adapter binding when a genuinely new external service is introduced.
Do not load arbitrary Python from dataset folders.

```bash
python -m benchmarks list
python -m benchmarks validate --suite room_hub_v1
python -m benchmarks run --suite room_hub_v1 --source-type paraphrase --name paraphrase-check
python -m benchmarks --suite-root /path/to/suites run --suite holdout_v1 --split holdout --name holdout-check
```

Validation rejects duplicate IDs/inputs/JSON keys, invalid parents, missing fields,
wrong typed slots, malformed capabilities/policies/statuses, missing fixture IDs,
naive reference clocks, escaping paths, malformed sessions and declared count
mismatches. Inputs and gold never flow together to the worker. Replace a suite by
replacing its declared files and version; remove it by deleting its directory.

## Interpretation limits

V1 expects default pending-only todos in some cases; the current application can
correctly implement its own all-status default yet fail that benchmark contract.
Several cases ask for context, cancellation and multi-action capabilities absent
from this production revision. Those annotations are not rewritten to inflate
scores. Native and projected outputs make these mismatches inspectable.

The prose `expected_response` is advisory. There are no original structured answer
fact assertions, so Task Success does not prove every sentence or returned list
item correct. The production app combines calendar/todo rows and does not store
calendar end times; no benchmark-only solution is injected. See the main
[benchmark guide](../../../docs/ASSISTANT_BENCHMARK.md) for this mapping and metrics.
