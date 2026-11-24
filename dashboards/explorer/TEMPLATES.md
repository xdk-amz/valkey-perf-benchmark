# Templates

| Template | Page | Inputs | Shows |
|---|---|---|---|
| Single run | `web/index.html`, `web/templates/single-run.js` | scenario; run (one run file; runs of one commit are told apart by their setup, such as io-threads); metric tab | The run's title, then the metric tabs. The run's absolute medians, in the sections the scenario defines (set commands: no TTLs, with TTLs, expired but still stored; others: none): one panel per command with a column per shape, mixes grouped inside each shape, on a log axis in absolute units from a reference line at the smallest shape. A column opens its cell: reps, spread, the rerun command. A Fingerprint section closes the page: the run's identity (build, commit, run date, harness) and its Benchmark Fingerprint, with its fields in a drawer. |
| Compare | `web/compare.html`, `web/templates/compare.js` | series A (any run file, and a slice); series B (any run file, and a slice). The slice is a value of the dimension the run's scenario declares as variable (set commands: TTL mix); scenarios that declare none have no slice. B's slice defaults to A's; metric tab; the bar, relative % and absolute part in the metric's bar unit (default: the metric's bar in metrics.json) | The comparison's title, then the metric tabs. One Fail / Pass / Exceed total over the cells on both series: Fail is B worse than A in the metric's direction (`better` in metrics.json) beyond both parts of the bar with every pair the same sign, Exceed the same for better, Pass everything else; a cell without the metric on either side is no data. B vs A in the tab's metric on the cells both series have, matched on scenario and cell id with the slice left out: one panel per command with a row per shape, each with the % change and the absolute change (per call for throughput), the bar band, the pairs and its verdict. Cells on only one series are listed once under Not on both. A row opens its cell: both series' reps side by side and each run's rerun command. A Fingerprint section closes the page: both runs' identity, their Benchmark Fingerprints and the fields they differ in besides the build. |

The pages load `data/manifest.json` (the runs and what tells them apart) and
then only the run files they show; Compare loads two. Benchmark Fingerprint:
the `run.fingerprint.hash` of the file (`SCHEMA.md`); a null field shows "not
recorded".

Metric tabs, on both pages in the same place, under the title (on Compare, above the Total table): TPS (the server-side rate when the runs have it, else the client's), p50, p99 and memory per key
(B). The selected tab drives every chart, table and verdict on the page and stays in the URL (`m=tps`, `p50`,
`p99`, `mem`).
