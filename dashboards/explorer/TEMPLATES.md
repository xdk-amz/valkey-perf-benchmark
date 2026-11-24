# Templates

| Template | Page | Inputs | Shows |
|---|---|---|---|
| Single run | `web/index.html`, `web/templates/single-run.js` | scenario; run (one run file; runs of one commit are told apart by their setup, such as io-threads); metric (throughput, p50 latency, p99 latency, memory per key) | The run's Benchmark Fingerprint, with its fields in a drawer. The run's absolute medians, in the sections the scenario defines (set commands: no TTLs, with TTLs, expired but still stored; others: none): one panel per command with a column per shape, mixes grouped inside each shape, on a log axis in absolute units from a reference line at the smallest shape. A column opens its cell: reps, spread, the rerun command. |
| Compare | `web/compare.html`, `web/templates/compare.js` | series A (any run file, and a slice); series B (any run file, and a slice). The slice is a value of the dimension the run's scenario declares as variable (set commands: TTL mix); scenarios that declare none have no slice. B's slice defaults to A's; the bar, relative % (default 2) and absolute ns per call (default 20) | Both runs' Benchmark Fingerprints and the fields they differ in besides the build. One Fail / Pass / Exceed total over the cells on both series: Fail is B slower than A beyond both parts of the bar with every pair the same sign, Exceed the same for faster, Pass everything else. B vs A throughput on the cells both series have, matched on scenario and cell id with the slice left out: one panel per command with a row per shape, each with the % change and change per call, the bar band, the pairs and its verdict. Cells on only one series are listed once under Not on both. A row opens its cell: both series' reps side by side and each run's rerun command. |

The pages load `data/manifest.json` (the runs and what tells them apart) and
then only the run files they show; Compare loads two. Benchmark Fingerprint:
the `run.fingerprint.hash` of the file (`SCHEMA.md`); a null field shows "not
recorded".
