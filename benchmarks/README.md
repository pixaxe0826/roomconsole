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
