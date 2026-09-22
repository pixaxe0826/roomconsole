# External-data Assistant Benchmark

Only engine code is shipped. Supply an external parent directory with
`--suite-root` or `ROOM_HUB_BENCHMARK_DATA`. There is no repository-bundled dataset.

```bash
python -m benchmarks --suite-root /external/benchmark-data list
python -m benchmarks --suite-root /external/benchmark-data validate --suite room_hub_v1
```

[Windows transfer, V35 commands, PR #20 compatibility, scoring and limits](../docs/ASSISTANT_BENCHMARK.md)

Tiny synthetic fixtures are generated under OS temporary directories by tests;
production evaluation cases/gold/schema/fixtures must not be committed here.

## M2 offline diagnostics (engine 1.2.0)

`python -m benchmarks analyze baseline-v1-qwen --name m1-taxonomy-v1` reads
existing raw/config/metrics/trace files, not the dataset or LLM.
`python -m benchmarks verify-analysis m1-taxonomy-v1` verifies both the derived
files and immutable original run. Outputs live in `artifacts/benchmarks/analysis/`.
Supported means exposed production operations, not every argument or UI feature.
`compare-analysis` compares a fixed common supported/evaluated cohort.
See [M2 specification and V35 instructions](../docs/BENCHMARK_M2.md).
