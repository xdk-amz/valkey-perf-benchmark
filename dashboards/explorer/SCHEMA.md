# Result record (`vpb-result/1`)

Every number on the explorer page is one result record: one metric of one
build in one benchmark cell, with its runs and how to rerun it. Everything
the explorer page shows reads only these fields. VPB writes them itself:
`vpb_result.py` turns each run into records, in `vpb-result.json` beside the
run's `metrics.json`.

## Record

| Field | Type | Meaning |
|---|---|---|
| `data_set` | string | Where the result came from: `official-ci`, `curated:<owner>`, `community` or `fork:<owner/repo>`. Set by the run (`--data-set`), and checked against the file's directory in a results repository. |
| `source` | string | Id of the source file entry the record came from (see "Data file"). |
| `build.repo` | string | Repository the build was made from, `owner/name`. |
| `build.ref` | string | Branch, tag or PR ref the commit was taken from. |
| `build.sha` | string | Full 40-hex commit. |
| `build.time` | string or null | Commit time, ISO 8601. |
| `family` | string | Scenario family: the config or study that defines the scenario (`continuous`, `set commands`, `matrix`, `core scaling`, `replica capacity`). |
| `scenario` | string | The source's own scenario id, kept for lookup. Not a filter. |
| `command` | string | Lowercase command or workload id (`get`, `sadd_new`, `get/set 80:20`). |
| `command_line` | string or null | The command template the load generator sent, when the source records it. |
| `shape.type` | string | Value type: `string`, `set`, `hash`, `list`, `zset`, `stream`. |
| `shape.encoding` | string or null | Expected server encoding of the key without TTLs (`listpack`, `intset`, `hashtable`); null when not recorded. A TTL mix can change it (an intset with member TTLs becomes a hashtable), and the shape keeps the configured one so mixes stay comparable. |
| `shape.size` | number or null | Value size in bytes for strings and fields, or members per key for collections; null when not recorded. |
| `shape.size_unit` | string | `B` or `members`. |
| `mix` | string | TTL or expiry mix: `none`, or the source's mix id (`10pct`, `exp_half`, `key ttl 1-5 s`). |
| `pipeline` | integer | Pipeline depth per connection. |
| `clients` | integer | Total client connections. |
| `io_threads` | integer or null | Server io-threads; null when not recorded. |
| `workload` | string | Anything else that defines the cell: key count, key distribution, cluster or replica layout. Empty when the other fields say it all. |
| `hardware.server` | string or null | Server instance type, or null when not recorded. |
| `hardware.cores` | integer or null | Server cores the run used, when the source records it. |
| `hardware.client` | string or null | Client host instance type. A filter, never part of a cell's identity. |
| `hardware.arch` | string or null | CPU architecture. |
| `harness.name` | string | `valkey-perf-benchmark`, or the other harness that produced the result. |
| `harness.repo` | string | Harness repository, `owner/name`. |
| `harness.sha` | string or null | Harness commit (the VPB version). |
| `metric` | string | Metric id, defined in the data file's `metrics` table (below). |
| `value` | number or null | Median of the valid runs; null when the cell is missing. |
| `n` | integer | Valid runs behind `value`. |
| `spread` | object or null | `min`, `max` and `cv` (coefficient of variation) of the valid runs. |
| `runs` | list | Each run: `v` value, optional `p` pair id, `t` time, `ok` false with `why` for an invalid run. A run with no per-run value (the source keeps only the median) is absent, and `n` still counts it. |
| `missing` | string or null | Why the cell has no value. A missing cell is never shown as zero. |
| `reproduce` | string | Key into the data file's `reproduce` table (below). |

### Cell identity

Two records are the same cell when every one of these matches:
`data_set`, `family`, `harness`, `command`, `shape`, `mix`, `pipeline`,
`clients`, `io_threads`, `workload`, `hardware.server`, `hardware.cores`,
`metric`, `build`. A comparison (a later step, not on the page yet) drops one
of them from the identity and compares the records that differ only in it.
`hardware.client` and `scenario` are never part of the identity.

### Pairs

When a source runs the two sides of a comparison back to back on the same
host, the runs carry the same pair id `p`. A comparison uses the pairs when
both sides share at least 3 of them; otherwise it uses every valid run.

### Metrics table

| Id | Name | Unit | Worse | Bar |
|---|---|---|---|---|
| `ops` | Throughput, as the client counts it | ops/s | lower | 2% and 20 ns per call |
| `server_ops` | Throughput, as the server counts it | ops/s | lower | 2% and 20 ns per call |
| `avg_ms`, `p50_ms`, `p95_ms`, `p99_ms`, `p999_ms` | Client latency | ms | higher | 2% and 0.01 ms |
| `bytes_per_key` | Memory per key after the preload | B | higher | 1% and 16 B |

A change is outside the bar only when it clears both parts and the runs agree
on its direction: all pairs move the same way, or, unpaired, the two run
ranges do not overlap. Everything else passes.

### Reproduce block

| Field | Meaning |
|---|---|
| `harness` | `name`, `repo` URL and `sha` to check out. |
| `build` | `repo` URL and `sha` of the build under test. |
| `config` | The config to run: `path` in the harness repo plus `overrides`, or the whole config `inline`. |
| `steps` | Shell lines that rerun the cell, with `<server-ip>` and similar placeholders for what differs per host. Empty when the source has no rerun command. |
| `setup` | Hosts, pinning and server settings the steps assume, in one line. |
| `gaps` | What the source did not record, so the rerun may differ. |

## Data file

One JSON file per source (one data set, whole runs):

```json
{
  "schema": "vpb-result/1",
  "sources": [{"id": "...", "name": "...", "data_set": "...", "url": "...", "lacks": ["..."]}],
  "metrics": {"ops": {"name": "...", "unit": "ops/s", "better": "higher", "bar": [2, 20, "ns per call"]}},
  "reproduce": {"<key>": {"harness": {}, "build": {}, "config": {}, "steps": [], "setup": "", "gaps": []}},
  "records": [{"data_set": "...", "...": "..."}],
  "fingerprints": [{"family": "...", "repo": "...", "ref": "...", "sha": "...", "hash": "..."}]
}
```

`reproduce` is a table so the many metrics of one run share one block;
`record.reproduce` names its entry. `fingerprints` has one entry per run
(scenario family, branch, commit) with its Benchmark Fingerprint; the
writer recomputes it whenever it writes the file.

### Benchmark Fingerprint

sha256 over the canonical JSON (keys sorted, no whitespace, UTF-8) of the
fields in `fingerprint-fields.json` that the run's records carry. A field's
value is the records' value when they all agree, else the sorted list of
their distinct values. Fields with no source in the record are not hashed
and show "not recorded". `fingerprint.py` and `web/common.js` compute the
same hash.

### Data sets in a results repository

| Data set | Directory under `data/` |
|---|---|
| `official-ci` | `official-ci/` |
| `curated:<owner>` | `curated/<owner>/` |
| `community` | `community/` |
| `fork:<owner>/<repo>` | `fork/<owner>/<repo>/` |

## Rules a file must pass (`python -m dashboards.explorer check`)

- Every record has every field above; `value` is null exactly when `missing` is set.
- `build.sha` is 40 hex; `data_set` is one of the data sets above.
- `metric`, `reproduce` and `source` resolve.
- `fingerprints` equals the fingerprints recomputed from the records.
- With `--data-root`, each file sits in its data set's directory.
- No internal identifiers anywhere: no IP addresses, instance, account,
  security group, subnet or AMI ids, internal hostnames, internal links,
  e-mail addresses or local paths (`idscan.py`).
