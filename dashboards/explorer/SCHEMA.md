# Result record (`vpb-result/2`)

Every number on the explorer page is one result record: one metric of one
build in one benchmark cell, with its runs and how to rerun it. Everything
the explorer page shows reads only these fields. VPB writes them itself:
`vpb_result.py` turns each run into records, in `vpb-result.json` beside the
run's `metrics.json`. `vpb-result/1` files, which have every field below
except those under "Run facts", stay readable: they pass `check`, and read
together with `/2` files they take those fields as null.

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

### Run facts (`vpb-result/2`)

Read at run time, the same way by every runner: the server's `INFO` and
`CONFIG GET`, and on the server's host and the client's host one script
(uname, `/etc/os-release`, lscpu, `/proc/cpuinfo`, `/proc/meminfo`, cgroup and
sysfs files, the instance metadata for the instance type only), run locally or
over ssh. A fact a host cannot report is null, and the source's `lacks` names
each one with its reason.

| Field | Type | Meaning |
|---|---|---|
| `build_info.version` | string or null | Server version from `INFO server`. |
| `build_info.settings` | object or null | Build settings from `INFO`: allocator, bits, compiler, multiplexing API, clock, git dirty flag. |
| `build_info.args` | list or null | Make arguments, from the stamp VPB's builder leaves in the tree; null for a binary built another way. |
| `build_info.binary_sha256` | string or null | sha256 of the running server binary, read on its host. |
| `server_config` | object or null | `CONFIG GET` of the performance settings and every setting the run set; secrets, paths and addresses are never asked for. |
| `load_generator` | string or null | The load generator's `--version`. |
| `network_path` | string or null | `loopback`, `same host, non-loopback address` or `network, separate hosts`, with `, TLS` when on. |
| `pinning` | string or null | CPUs the server and the client ran on. |
| `parameters` | object or null | The scenario's own definition as it ran (a matrix cell's parameter point). |
| `host.server`, `host.client` | object | The host facts below, for the server's host and the client's (several generator hosts: a value they agree on, else their values joined). |

| Host fact | Meaning |
|---|---|
| `instance_type` | From the instance metadata (IMDSv2), else the EC2 DMI product name. |
| `arch` | `uname -m`. |
| `cpu_model` | lscpu model name, else the Arm part from `/proc/cpuinfo`. |
| `cpu_microcode` | x86 microcode, else the Arm MIDR and REVIDR registers. |
| `cpu_topology` | CPUs, sockets, cores, threads, NUMA nodes, L3. |
| `memory` | `MemTotal`, GiB. |
| `nic` | Driver and link speed of the interface carrying the benchmark traffic, or `loopback`. |
| `virtualization` | `systemd-detect-virt`: VM type or `bare metal`, and any container. |
| `cgroup_cpu_limit` | cgroup v2 `cpu.max` of the measured process, `none` when unlimited. |
| `os` | `PRETTY_NAME` from `/etc/os-release`. |
| `kernel` | `uname -r`. |
| `kernel_boot_options` | `/proc/cmdline` without root devices, paths, addresses or ids. |
| `kernel_runtime_settings` | THP and its defrag, ASLR, NUMA balancing, cpufreq governor, turbo, idle states. |

No field holds a host identifier: no hostname, address, instance id, zone,
account id or MAC.

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
  "schema": "vpb-result/2",
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

`fingerprint-fields.json` is fingerprint v4 (`vpb-fingerprint-fields/2`). It
gives every v3 field a source, adding the run facts above and the client
host's instance type, CPU model, OS and kernel. Every v3 source is unchanged
and the new ones exist only in `/2` records, so a `/1` record hashes exactly as
it did under v3.

### Data sets in a results repository

| Data set | Directory under `data/` |
|---|---|
| `official-ci` | `official-ci/` |
| `curated:<owner>` | `curated/<owner>/` |
| `community` | `community/` |
| `fork:<owner>/<repo>` | `fork/<owner>/<repo>/` |

## Rules a file must pass (`python -m dashboards.explorer check`)

- `schema` is `vpb-result/1` or `vpb-result/2`; every record has every field
  of its version, `host.server` and `host.client` exactly the host facts;
  `value` is null exactly when `missing` is set.
- `build.sha` is 40 hex; `data_set` is one of the data sets above.
- `metric`, `reproduce` and `source` resolve.
- `fingerprints` equals the fingerprints recomputed from the records.
- With `--data-root`, each file sits in its data set's directory.
- No key anywhere (outside a rerun config) names a host identifier:
  hostname, IP, MAC, instance id, zone, account id, serial, machine id, UUID.
- No internal identifiers anywhere: no IP or MAC addresses, instance, account,
  security group, subnet or AMI ids, zones, UUIDs, internal or cloud
  hostnames, internal links, e-mail addresses or local paths (`idscan.py`).
