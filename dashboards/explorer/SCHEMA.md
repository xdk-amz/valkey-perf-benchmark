# Run file (`vpb-result`)

One file per run. A run is every scenario VPB measured with one fingerprint:
one build on one host setup with one config. In a same-host A/B the two
builds are two runs, two files, whose reps share pair ids. `vpb_result.py`
writes them, `<results>/<commit or run name>/vpb-runs/<run id>.json`; a later
invocation with the same fingerprint adds its reps to the same file.

```json
{
  "schema": "vpb-result",
  "run": {
    "id": "...", "data_set": "...", "started": "...", "finished": "...",
    "build": {"repo": "...", "ref": "...", "sha": "...", "time": "...", "version": "..."},
    "harness": {"repo": "...", "sha": "...", "config": "...", "config_inline": null},
    "fingerprint": {"hash": "...", "build": {}, "server": {}, "client": {}, "server_config": {},
                    "load_generator": "...", "network_path": "...", "pinning": "...", "io_threads": 4, "cores": 4},
    "reproduce": "<one command>"
  },
  "cells": [
    {"id": "...", "family": "...", "command": "...", "command_line": "...",
     "shape": {"type": "...", "encoding": null, "size": 16, "size_unit": "B"}, "mix": "none",
     "pipeline": 1, "clients": 50,
     "metrics": {"server_ops": {"median": 0, "min": 0, "max": 0, "cv": 0}},
     "runs": [{"rep": 1, "pair": "u3r1", "t": "...", "ok": true, "server_ops": 0, "p50_ms": 0}],
     "missing": null}
  ]
}
```

A field the run could not read is null. Run-level facts (build, harness,
hardware, io-threads) are only in `run`, never in a cell.

## Run

| Field | Meaning |
|---|---|
| `id` | Lowercase id, unique in a results repository; the file is `<id>.json`. |
| `data_set` | `official-ci`, `curated:<owner>`, `community` or `fork:<owner>/<repo>` (`--data-set`). |
| `started`, `finished` | ISO 8601 times of the first and last rep. |
| `build.repo`, `build.ref`, `build.sha` | `owner/name`, the branch or tag, the 40-hex commit. |
| `build.time` | Commit time. |
| `build.version` | Server version from `INFO server`. |
| `harness.repo`, `harness.sha` | The VPB checkout that ran. |
| `harness.config`, `harness.config_inline` | The config's path in the harness repo, and the config itself when the path does not say it all (overrides, a generated config). |
| `fingerprint.hash` | The Benchmark Fingerprint (below). |
| `fingerprint.build` | `settings` (allocator, bits, compiler, multiplexing, clock, git dirty flag from `INFO`), `args` (make arguments from VPB's build stamp), `binary_sha256`. |
| `fingerprint.server`, `fingerprint.client` | Host facts of the server's host and the client's (several client hosts: a value they agree on, else their values joined). |
| `fingerprint.server_config` | `CONFIG GET` of the performance settings and every setting the run set; secrets, paths and addresses are never asked for. |
| `fingerprint.load_generator` | The load generator's `--version`. |
| `fingerprint.network_path` | `loopback`, `same host, non-loopback address` or `network, separate hosts`, with `, TLS` when on. |
| `fingerprint.pinning` | CPUs the server and the client ran on. |
| `fingerprint.io_threads`, `fingerprint.cores` | Server io-threads and the number of server CPUs. |
| `reproduce` | One command that reruns the run. |

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

## Cell

| Field | Meaning |
|---|---|
| `id` | The scenario's id in the run (`listpack16.none.sadd_new.P1`, `SET-d16-p1-c1600`). Unique in the file. |
| `family` | Scenario family: the config's `test_name`, lowercased (`continuous` for the basic format). |
| `command` | Lowercase command or workload id. |
| `command_line` | The command template the load generator sent. |
| `shape` | `type` (`string`, `set`, `hash`, `list`, `zset`, `stream`), `encoding` (expected server encoding without TTLs), `size` (bytes, or members per key), `size_unit` (`B` or `members`). |
| `mix` | TTL or expiry mix: `none`, or the scenario's mix id (`10pct`, `exp_half`). |
| `pipeline`, `clients` | Pipeline depth per connection and total client connections. |
| `metrics` | Every metric a valid rep measured: `median`, `min`, `max` and `cv` of its valid reps. |
| `runs` | Every rep: `rep` (1, 2, ...), `pair` (shared by the two builds' reps made back to back on one host, else null), `t` (start time), `ok`, `why` when `ok` is false, and each metric's value. |
| `missing` | Why no rep is valid; null when `metrics` has any. |

## Benchmark Fingerprint

`fingerprint-fields.json` (`vpb-fingerprint-fields/3`, fingerprint v5)
names every field and its path in `run`: the build, the config, the server
settings, io-threads, load generator, network path, pinning, cores and every
host fact of the server and the client. The hash is sha256 over the canonical
JSON (keys sorted, no whitespace, UTF-8) of `{field: value}` over every field,
null included. Two runs of one build with one fingerprint are one run.

## Results repository

| Path | Holds |
|---|---|
| `data/<data set directory>/<run id>.json` | One run. `official-ci/`, `curated/<owner>/`, `community/`, `fork/<owner>/<repo>/`. |
| `metrics.json` | `{"schema": "vpb-metrics", "metrics": {<id>: {"name", "unit", "better", "bar"}}}`, every metric the runs use. |

| Metric | Name | Unit | Worse | Bar |
|---|---|---|---|---|
| `ops` | Throughput, as the client counts it | ops/s | lower | 2% and 20 ns per call |
| `server_ops` | Throughput, as the server counts it | ops/s | lower | 2% and 20 ns per call |
| `avg_ms`, `p50_ms`, `p95_ms`, `p99_ms`, `p999_ms` | Client latency | ms | higher | 2% and 0.01 ms |
| `bytes_per_key` | Memory per key after the preload | B | higher | 1% and 16 B |

## Rules a file must pass (`python -m dashboards.explorer check`)

- `schema` is `vpb-result`; `run`, each cell and each rep have exactly the
  fields above (a rep: `rep`, `pair`, `t`, `ok`, `why` exactly when `ok` is
  false, and metric values).
- `run.id` is a lowercase id, `run.data_set` a data set, `run.build.sha` 40 hex.
- `run.fingerprint.hash` equals the hash of the run's fields.
- Every metric is defined in `metrics.json` (`--metrics`); each cell's
  `metrics` summarize its valid reps; `missing` is set exactly when no metric
  was measured; rep numbers and cell ids do not repeat.
- With `--data-root`, the file is `data/<data set directory>/<run id>.json`
  and no two files hold one run id.
- No key (outside the harness config) names a host identifier: hostname, IP,
  MAC, instance id, zone, account id, serial, machine id, UUID.
- No internal identifiers anywhere: no IP or MAC addresses, instance, account,
  security group, subnet or AMI ids, zones, UUIDs, internal or cloud
  hostnames, internal links, e-mail addresses or local paths (`idscan.py`).
