#!/usr/bin/env python3
"""Export a benchmark results directory as static benchmark-dashboard data.

Reads ``metrics.json`` and the per-second ``timeseries.jsonl`` that a run with
``per_second_sampling`` writes, and writes the files the static dashboard in
``dashboards/static/view.html`` reads, in the shape of the original
dubey02/valkey-data-tiering ``benchmark_dashboard/data`` directory:

  <dataset>.csv                 one row per second, columns in DASHBOARD_COLUMNS
  <dataset>-get-latency.csv     client latency of the first read process
  <dataset>-set-latency.csv     client latency of the first write process
  <dataset>-server-latency.txt  server latency per command over the measured run
  manifest.json                 tabs (test groups) and their datasets

A dataset is one scenario run: ``<group>_<scenario>``, with ``-run<N>`` added
for the second and later runs. Rates compare each sample with the previous
sample of the same run, so the first row of a run has zero rates, as the
original collector reported. Server latency comes from the difference between
the first and last LATENCY HISTOGRAM readings of the run, so it covers only the
measured phase, at the histogram's power-of-two microsecond resolution.

  python utils/dashboard_export.py results/<commit> dashboards/static/data
"""

import argparse
import csv
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Linux USER_HZ, the unit of /proc clock tick counters
CLOCK_TICKS_PER_SEC = 100.0
SECTOR_BYTES = 512
MIB = 1048576

# Per-second columns, in the order of the original metrics-collector.sh CSV.
DASHBOARD_COLUMNS = [
    "timestamp",
    "elapsed_sec",
    "used_memory",
    "used_memory_rss",
    "maxmemory",
    "keyspace_hits",
    "keyspace_misses",
    "ops_per_sec",
    "total_commands_delta",
    "total_num_items_spilled_to_ext_storage",
    "total_num_items_fetched_from_ext_storage",
    "completion_read_ok",
    "dram_value_hits",
    "kbc_fetching_block",
    "num_items_spilling_to_ext_storage",
    "blocked_clients",
    "cpu_user",
    "cpu_sys",
    "valkey_cpu_user",
    "valkey_cpu_sys",
    "valkey_cpu_total",
    "asio_cpu_pct",
    "disk_hit_pct",
    "mem_hit_pct",
    "mem_frag_ratio",
    "disk_read_iops",
    "disk_write_iops",
    "disk_read_mb",
    "disk_write_mb",
    "disk_read_merges_ps",
    "disk_write_merges_ps",
    "disk_r_await_ms",
    "disk_w_await_ms",
    "disk_aqu_sz",
    "disk_util_pct",
    "disk_in_flight",
    "disk_req_sz_kb",
    "throttle_total_throttled",
    "throttle_queued_clients",
    "throttle_current_rate",
    "throttle_allowed_tps",
    "spill_submitted_count",
    "spill_serialized_count",
    "mean_spill_ram",
    "inflight_spill_ram_bytes",
]

# INFO fields copied as they are. Each column lists the names it is read from,
# first match wins, so the data-tiering-unstable ext_storage_* names and the
# prototype names land in the same column.
INFO_GAUGES = {
    "used_memory": ["used_memory"],
    "used_memory_rss": ["used_memory_rss"],
    "maxmemory": ["maxmemory"],
    "keyspace_hits": ["keyspace_hits"],
    "keyspace_misses": ["keyspace_misses"],
    "ops_per_sec": ["instantaneous_ops_per_sec"],
    "total_num_items_spilled_to_ext_storage": [
        "ext_storage_total_num_items_spilled_to_storage",
        "total_num_items_spilled_to_ext_storage",
    ],
    "total_num_items_fetched_from_ext_storage": [
        "ext_storage_total_num_items_fetched_from_storage",
        "total_num_items_fetched_from_ext_storage",
    ],
    "completion_read_ok": ["completion_read_ok"],
    "dram_value_hits": ["dram_value_hits"],
    "kbc_fetching_block": ["kbc_fetching_block"],
    "num_items_spilling_to_ext_storage": [
        "ext_storage_num_items_spilling_to_storage",
        "num_items_spilling_to_ext_storage",
    ],
    "blocked_clients": ["blocked_clients"],
    "mem_frag_ratio": ["mem_fragmentation_ratio"],
    "throttle_total_throttled": ["throttle_total_throttled"],
    "throttle_queued_clients": ["throttle_queued_clients"],
    "throttle_current_rate": ["throttle_current_rate"],
    "throttle_allowed_tps": ["throttle_allowed_tps"],
    "spill_submitted_count": ["spill_submitted_count"],
    "spill_serialized_count": ["spill_serialized_count"],
    "mean_spill_ram": ["mean_spill_ram"],
    "inflight_spill_ram_bytes": ["inflight_spill_ram_bytes"],
}

CLIENT_LATENCY_FIELDS = [
    "test",
    "rps",
    "avg_latency_ms",
    "min_latency_ms",
    "p50_latency_ms",
    "p95_latency_ms",
    "p99_latency_ms",
    "max_latency_ms",
]

SERVER_PERCENTILES = [("p50", 0.5), ("p90", 0.9), ("p99", 0.99), ("p99.9", 0.999)]

# Commands the framework and sampler send themselves, left out of server latency
INTERNAL_COMMANDS = {
    "info",
    "ping",
    "dbsize",
    "latency|histogram",
    "config|get",
    "config|set",
    "client|setinfo",
    "flushall",
    "ft._list",
}


def _num(value: Any) -> Optional[float]:
    """Return value as a float, or None when it is not a plain number."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _info(info: Dict[str, str], names: Iterable[str]) -> Optional[float]:
    for name in names:
        if name in info:
            return _num(info[name])
    return None


def _rate(cur: Optional[float], prev: Optional[float], interval: float) -> float:
    """Per-second change of a cumulative counter, 0 when it cannot be computed."""
    if cur is None or prev is None or interval <= 0 or cur < prev:
        return 0.0
    return (cur - prev) / interval


def _cpu_split(cur: dict, prev: dict) -> Tuple[float, float]:
    """Host user and system CPU % between two process_cpu readings.

    proc_stat cpu is user nice system idle iowait irq softirq steal, per proc(5).
    """
    c = (cur or {}).get("proc_stat", {}).get("cpu")
    p = (prev or {}).get("proc_stat", {}).get("cpu")
    if not c or not p or len(c) < 8 or len(p) < 8:
        return 0.0, 0.0
    total = sum(c[:8]) - sum(p[:8])
    if total <= 0:
        return 0.0, 0.0
    user = (c[0] + c[1]) - (p[0] + p[1])
    system = (c[2] + c[5] + c[6]) - (p[2] + p[5] + p[6])
    return 100.0 * user / total, 100.0 * system / total


def _worker_threads_cpu(cur: dict, prev: dict, interval: float) -> float:
    """CPU % of every server thread except the main thread (lowest tid)."""
    c = (cur or {}).get("threads") or {}
    p = (prev or {}).get("threads") or {}
    if not c or interval <= 0:
        return 0.0
    main = min(c, key=int)
    ticks = 0.0
    for tid, t in c.items():
        if tid == main:
            continue
        before = p.get(tid, {"utime": 0, "stime": 0})
        ticks += (t["utime"] + t["stime"]) - (before["utime"] + before["stime"])
    return max(0.0, 100.0 * ticks / CLOCK_TICKS_PER_SEC / interval)


def _disk(cur: dict, prev: dict, interval: float) -> Dict[str, float]:
    """iostat-style numbers from two /sys/block/<dev>/stat readings.

    Fields: reads, read merges, sectors read, ms reading, writes, write merges,
    sectors written, ms writing, in flight, io_ticks, time_in_queue, per
    kernel Documentation/block/stat.rst.
    """
    out = {
        k: 0.0
        for k in (
            "disk_read_iops",
            "disk_write_iops",
            "disk_read_mb",
            "disk_write_mb",
            "disk_read_merges_ps",
            "disk_write_merges_ps",
            "disk_r_await_ms",
            "disk_w_await_ms",
            "disk_aqu_sz",
            "disk_util_pct",
            "disk_in_flight",
            "disk_req_sz_kb",
        )
    }
    c = (cur or {}).get("stat")
    if not c or len(c) < 11:
        return out
    out["disk_in_flight"] = float(c[8])
    p = (prev or {}).get("stat")
    if not p or len(p) < 11 or interval <= 0:
        return out
    d = [c[i] - p[i] for i in range(11)]
    if any(v < 0 for v in d):
        return out
    out["disk_read_iops"] = d[0] / interval
    out["disk_write_iops"] = d[4] / interval
    out["disk_read_mb"] = d[2] * SECTOR_BYTES / MIB / interval
    out["disk_write_mb"] = d[6] * SECTOR_BYTES / MIB / interval
    out["disk_read_merges_ps"] = d[1] / interval
    out["disk_write_merges_ps"] = d[5] / interval
    out["disk_r_await_ms"] = d[3] / d[0] if d[0] else 0.0
    out["disk_w_await_ms"] = d[7] / d[4] if d[4] else 0.0
    out["disk_aqu_sz"] = d[10] / (interval * 1000)
    out["disk_util_pct"] = min(100.0, d[9] / (interval * 1000) * 100)
    ios = d[0] + d[4]
    out["disk_req_sz_kb"] = (d[2] + d[6]) * SECTOR_BYTES / ios / 1024 if ios else 0.0
    return out


def _parse_time(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def per_second_rows(samples: List[dict]) -> List[Dict[str, Any]]:
    """Return dashboard rows for one run's samples, ordered by elapsed_sec."""
    rows = []
    prev = None
    for s in sorted(samples, key=lambda r: r["elapsed_sec"]):
        info = s.get("valkey_info") or {}
        pinfo = (prev or {}).get("valkey_info") or {}
        t = _parse_time(s["sample_time"])
        interval = t - _parse_time(prev["sample_time"]) if prev else 0.0
        row: Dict[str, Any] = {
            "timestamp": int(t),
            "elapsed_sec": s["elapsed_sec"],
        }
        for col, names in INFO_GAUGES.items():
            v = _info(info, names)
            row[col] = 0 if v is None else int(v) if v.is_integer() else v
        row["total_commands_delta"] = round(
            _rate(
                _info(info, ["total_commands_processed"]),
                _info(pinfo, ["total_commands_processed"]),
                interval,
            )
        )
        row["cpu_user"], row["cpu_sys"] = _cpu_split(
            s.get("process_cpu"), (prev or {}).get("process_cpu")
        )
        row["valkey_cpu_user"] = 100 * _rate(
            _info(info, ["used_cpu_user_main_thread"]),
            _info(pinfo, ["used_cpu_user_main_thread"]),
            interval,
        )
        row["valkey_cpu_sys"] = 100 * _rate(
            _info(info, ["used_cpu_sys_main_thread"]),
            _info(pinfo, ["used_cpu_sys_main_thread"]),
            interval,
        )
        row["valkey_cpu_total"] = row["valkey_cpu_user"] + row["valkey_cpu_sys"]
        # The old collector's column for the storage IO thread; here every
        # server thread besides the main thread, whatever its name.
        row["asio_cpu_pct"] = _worker_threads_cpu(
            s.get("process_cpu"), (prev or {}).get("process_cpu"), interval
        )
        hits = row["dram_value_hits"] or 0
        reads = row["completion_read_ok"] or 0
        row["disk_hit_pct"] = 100.0 * reads / hits if hits else 0.0
        row["mem_hit_pct"] = 100.0 * (hits - reads) / hits if hits else 0.0
        row.update(_disk(s.get("disk"), (prev or {}).get("disk"), interval))
        rows.append(
            {k: (round(v, 3) if isinstance(v, float) else v) for k, v in row.items()}
        )
        prev = s
    return rows


def histogram_percentiles(
    first: Dict[str, int], last: Dict[str, int]
) -> Optional[Dict[str, float]]:
    """Percentiles in usec of the calls between two cumulative histograms.

    Each map is power-of-two bucket upper bound (usec) to the cumulative count
    of calls at or below it; a bucket with no calls is left out of the reply,
    so each reading is carried forward to the other's buckets.
    """
    bounds = sorted({float(b) for b in list(first) + list(last)})
    if not bounds:
        return None

    def cumulative(h):
        out, running = [], 0
        points = {float(b): c for b, c in h.items()}
        for b in bounds:
            running = max(running, points.get(b, running))
            out.append(running)
        return out

    lo, hi = cumulative(first), cumulative(last)
    steps = [h - l for h, l in zip(hi, lo)]
    total = steps[-1]
    if total <= 0 or any(v < 0 for v in steps):
        return None
    result = {}
    for name, q in SERVER_PERCENTILES + [("p100", 1.0)]:
        result[name] = next(b for b, v in zip(bounds, steps) if v >= q * total)
    return result


def server_latency_lines(samples: List[dict]) -> List[str]:
    """INFO latencystats-style lines over a run's measured phase."""
    ordered = sorted(samples, key=lambda r: r["elapsed_sec"])
    with_hist = [s for s in ordered if s.get("latency_histogram")]
    if len(with_hist) < 2:
        return []
    first, last = with_hist[0]["latency_histogram"], with_hist[-1]["latency_histogram"]
    lines = []
    for command in sorted(last):
        if command in INTERNAL_COMMANDS:
            continue
        pct = histogram_percentiles(
            (first.get(command) or {}).get("histogram_usec", {}),
            last[command].get("histogram_usec", {}),
        )
        if not pct:
            continue
        body = ",".join(f"{k}={v:g}" for k, v in pct.items())
        lines.append(f"latency_percentiles_usec_{command}:{body}")
    return lines


def _latency_row(entry: dict) -> Dict[str, Any]:
    """The valkey-benchmark --csv row of one process: test is GET or SET."""
    row = {"test": (entry.get("command") or "").split()[0].upper()}
    for f in CLIENT_LATENCY_FIELDS[1:]:
        v = entry.get(f)
        row[f] = round(v, 3) if isinstance(v, float) else v
    return row


def _write_csv(path: Path, fields: List[str], rows: List[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)


def export(results_dir: Path, out_dir: Path, title: Optional[str] = None) -> dict:
    """Write dashboard files for every scenario run in results_dir."""
    metrics = json.loads((results_dir / "metrics.json").read_text())
    samples_by_run: Dict[Tuple[str, Any], List[dict]] = {}
    ts_path = results_dir / "timeseries.jsonl"
    if ts_path.exists():
        for line in ts_path.open(encoding="utf-8"):
            if line.strip():
                s = json.loads(line)
                key = (f"{s.get('group')}_{s.get('scenario')}", s.get("run", 1) or 1)
                samples_by_run.setdefault(key, []).append(s)

    # Group metric entries by scenario run, keeping test group metadata
    runs: Dict[Tuple[str, Any], Dict[str, Any]] = {}
    for e in metrics:
        if e.get("status") not in (None, "success"):
            continue
        sid = f"{e.get('group')}_{e.get('scenario')}"
        key = (sid, e.get("run", 1) or 1)
        r = runs.setdefault(
            key,
            {
                "group": e.get("group"),
                "group_description": e.get("group_description", ""),
                "scenario": e.get("scenario"),
                "commit": e.get("commit"),
                "entries": [],
            },
        )
        r["entries"].append(e)

    out_dir.mkdir(parents=True, exist_ok=True)
    tabs: Dict[Any, dict] = {}
    # metrics.json order is config order, so datasets keep the order of the config
    for (sid, run), r in runs.items():
        name = sid if run == 1 else f"{sid}-run{run}"
        entries = r["entries"]
        reads = [e for e in entries if e.get("test_phase") == "mixed_read"]
        writes = [e for e in entries if e.get("test_phase") == "mixed_write"]
        if not reads and not writes:
            reads = entries  # a plain read or write scenario
        samples = samples_by_run.get((sid, run), [])

        rows = per_second_rows(samples)
        _write_csv(out_dir / f"{name}.csv", DASHBOARD_COLUMNS, rows)
        if reads:
            _write_csv(
                out_dir / f"{name}-get-latency.csv",
                CLIENT_LATENCY_FIELDS,
                [_latency_row(reads[0])],
            )
        if writes:
            _write_csv(
                out_dir / f"{name}-set-latency.csv",
                CLIENT_LATENCY_FIELDS,
                [_latency_row(writes[0])],
            )
        lines = server_latency_lines(samples)
        (out_dir / f"{name}-server-latency.txt").write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
        )
        tab = tabs.setdefault(
            r["group"],
            {
                "id": f"group{r['group']}",
                "group": r["group"],
                # The group description up to its first colon, e.g. "mixed-rw strings"
                "label": (r["group_description"] or f"Group {r['group']}").split(":")[
                    0
                ],
                "description": r["group_description"],
                "datasets": [],
            },
        )
        tab["datasets"].append(
            {
                "name": name,
                "label": r["scenario"] if run == 1 else f"{r['scenario']} run {run}",
                "scenario": r["scenario"],
                "run": run,
                "commit": r["commit"],
                "seconds": len(rows),
                "rps": round(sum(e.get("rps") or 0 for e in entries), 2),
                "description": (entries[0].get("scenario_description") or ""),
            }
        )
        if not rows:
            logging.warning(
                f"{name}: no per-second samples; enable per_second_sampling"
            )

    manifest = {
        "title": title or f"Benchmarks ({results_dir.name})",
        "source": results_dir.name,
        "columns": DASHBOARD_COLUMNS,
        "tabs": [tabs[g] for g in sorted(tabs, key=lambda g: (g is None, g))],
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=1) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("results_dir", type=Path, help="results/<commit> directory")
    parser.add_argument("out_dir", type=Path, help="dashboard data directory")
    parser.add_argument("--title", help="dashboard title")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    manifest = export(args.results_dir, args.out_dir, args.title)
    n = sum(len(t["datasets"]) for t in manifest["tabs"])
    print(f"wrote {n} datasets in {len(manifest['tabs'])} tabs to {args.out_dir}")


if __name__ == "__main__":
    main()
