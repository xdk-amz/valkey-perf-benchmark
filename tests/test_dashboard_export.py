"""Tests for utils/dashboard_export.py."""

import csv
import json
from pathlib import Path

import pytest

from utils.dashboard_export import (
    DASHBOARD_COLUMNS,
    export,
    histogram_percentiles,
    per_second_rows,
    server_latency_lines,
)


def _sample(
    elapsed,
    *,
    cmds,
    scenario="s",
    hits=0,
    cpu_main=0.0,
    proc=None,
    disk=None,
    hist=None,
    extra=None,
):
    info = {
        "used_memory": "1000",
        "used_memory_rss": "2000",
        "maxmemory": "0",
        "mem_fragmentation_ratio": "2.00",
        "instantaneous_ops_per_sec": str(cmds),
        "total_commands_processed": str(cmds * (elapsed + 1)),
        "keyspace_hits": str(hits),
        "keyspace_misses": "0",
        "blocked_clients": "0",
        "used_cpu_user_main_thread": str(cpu_main),
        "used_cpu_sys_main_thread": "0",
        "db0": "keys=42,expires=0,avg_ttl=0",
    }
    info.update(extra or {})
    s = {
        "group": 1,
        "scenario": scenario,
        "run": 1,
        "sample_time": f"2026-11-05T10:00:{elapsed:02d}+00:00",
        "elapsed_sec": elapsed,
        "valkey_info": info,
    }
    if proc is not None:
        s["process_cpu"] = proc
    if disk is not None:
        s["disk"] = {"device": "nvme0n1", "stat": disk}
    if hist is not None:
        s["latency_histogram"] = hist
    return s


def test_per_second_rows_rates_and_gauges():
    proc0 = {
        "proc_stat": {"cpu": [0] * 8},
        "threads": {"10": {"utime": 0, "stime": 0}, "11": {"utime": 0, "stime": 0}},
    }
    proc1 = {
        "proc_stat": {"cpu": [30, 10, 20, 40, 0, 0, 0, 0]},
        "threads": {
            "10": {"utime": 50, "stime": 0},
            "11": {"utime": 20, "stime": 5},
            "12": {"utime": 5, "stime": 0},
        },
    }
    d0 = [0] * 11
    d1 = [100, 1, 2048, 50, 50, 2, 1024, 100, 3, 500, 800]
    rows = per_second_rows(
        [
            _sample(
                1,
                cmds=1000,
                cpu_main=1.5,
                proc=proc1,
                disk=d1,
                extra={
                    "ext_storage_total_num_items_spilled_to_storage": "7",
                    "dram_value_hits": "200",
                    "completion_read_ok": "50",
                },
            ),
            _sample(0, cmds=1000, cpu_main=1.0, proc=proc0, disk=d0),
        ]
    )
    assert [r["elapsed_sec"] for r in rows] == [0, 1]
    first, second = rows
    assert set(first) == set(DASHBOARD_COLUMNS)
    assert first["total_commands_delta"] == 0
    assert first["valkey_cpu_user"] == 0
    assert second["used_memory"] == 1000 and isinstance(second["used_memory"], int)
    assert second["mem_frag_ratio"] == 2
    assert second["ops_per_sec"] == 1000
    assert second["total_commands_delta"] == 1000
    assert second["valkey_cpu_user"] == 50.0
    assert second["valkey_cpu_total"] == 50.0
    # non-main threads: 25 + 5 ticks over 1 s at 100 ticks/s
    assert second["asio_cpu_pct"] == 30.0
    assert second["cpu_user"] == 40.0 and second["cpu_sys"] == 20.0
    assert second["total_num_items_spilled_to_ext_storage"] == 7
    assert second["disk_hit_pct"] == 25.0 and second["mem_hit_pct"] == 75.0
    assert second["disk_read_iops"] == 100 and second["disk_write_iops"] == 50
    assert second["disk_read_mb"] == 1.0 and second["disk_write_mb"] == 0.5
    assert second["disk_r_await_ms"] == 0.5 and second["disk_w_await_ms"] == 2.0
    assert second["disk_util_pct"] == 50.0 and second["disk_aqu_sz"] == 0.8
    assert second["disk_in_flight"] == 3
    # (2048 + 1024 sectors) * 512 B over 150 IOs
    assert second["disk_req_sz_kb"] == 10.24


def test_counter_reset_gives_zero_rate():
    rows = per_second_rows([_sample(0, cmds=1000), _sample(1, cmds=1)])
    assert rows[1]["total_commands_delta"] == 0


def test_histogram_percentiles_carry_buckets_forward():
    first = {"1": 10, "2": 10}
    last = {"1": 60, "4": 109, "8": 110}
    # interval: 50 calls at <=1, 49 more by 4, 1 more by 8
    assert histogram_percentiles(first, last) == {
        "p50": 1.0,
        "p90": 4.0,
        "p99": 4.0,
        "p99.9": 8.0,
        "p100": 8.0,
    }


def test_histogram_with_no_new_calls_is_none():
    assert histogram_percentiles({"1": 5}, {"1": 5}) is None
    assert histogram_percentiles({}, {}) is None


def test_server_latency_lines_skip_internal_commands():
    h0 = {
        "get": {"calls": 0, "histogram_usec": {}},
        "info": {"calls": 1, "histogram_usec": {"64": 1}},
    }
    h1 = {
        "get": {"calls": 4, "histogram_usec": {"2": 3, "16": 4}},
        "info": {"calls": 3, "histogram_usec": {"64": 3}},
    }
    lines = server_latency_lines(
        [_sample(0, cmds=1, hist=h0), _sample(1, cmds=1, hist=h1)]
    )
    assert lines == [
        "latency_percentiles_usec_get:p50=2,p90=16,p99=16,p99.9=16,p100=16"
    ]


def _metric(phase, child, rps, group=1, scenario="zipf", run=None):
    side = "read" if phase == "mixed_read" else "write"
    e = {
        "group": group,
        "scenario": scenario,
        "group_description": "strings: x",
        "commit": "abc",
        "test_phase": phase,
        "test_id": f"{group}_{scenario}_{side}_{child}",
        "command": child.upper(),
        "clients": 4,
        "data_size": 64,
        "status": "success",
        "rps": rps,
        "avg_latency_ms": 0.1234567,
        "min_latency_ms": 0.01,
        "p50_latency_ms": 0.1,
        "p95_latency_ms": 0.11100000000000002,
        "p99_latency_ms": 0.2,
        "max_latency_ms": 1.0,
    }
    if run:
        e["run"] = run
    return e


def test_export_writes_dashboard_files(tmp_path):
    res = tmp_path / "HEAD"
    res.mkdir()
    metrics = [
        _metric("mixed_write", "set", 100.0),
        _metric("mixed_read", "get", 400.0),
        _metric("mixed_read", "get", 300.0, run=2),
        {"status": "failed", "group": 1, "scenario": "bad"},
    ]
    (res / "metrics.json").write_text(json.dumps(metrics))
    with (res / "timeseries.jsonl").open("w") as f:
        for i in range(3):
            f.write(json.dumps(_sample(i, cmds=500, scenario="zipf")) + "\n")

    out = tmp_path / "data"
    manifest = export(res, out, title="T")

    assert manifest["title"] == "T"
    assert manifest["source"] == "HEAD"
    [tab] = manifest["tabs"]
    assert tab["label"] == "strings" and tab["description"] == "strings: x"
    assert [d["name"] for d in tab["datasets"]] == ["1_zipf", "1_zipf-run2"]
    assert tab["datasets"][0]["rps"] == 500.0
    assert tab["datasets"][0]["seconds"] == 3
    assert tab["datasets"][1]["seconds"] == 0  # run 2 had no samples

    rows = list(csv.DictReader((out / "1_zipf.csv").open()))
    assert len(rows) == 3 and list(rows[0]) == DASHBOARD_COLUMNS
    get = list(csv.DictReader((out / "1_zipf-get-latency.csv").open()))
    assert get == [
        {
            "test": "GET",
            "rps": "400.0",
            "avg_latency_ms": "0.123",
            "min_latency_ms": "0.01",
            "p50_latency_ms": "0.1",
            "p95_latency_ms": "0.111",
            "p99_latency_ms": "0.2",
            "max_latency_ms": "1.0",
        }
    ]
    assert (out / "1_zipf-set-latency.csv").exists()
    assert not (out / "1_zipf-run2-set-latency.csv").exists()
    set_ = list(csv.DictReader((out / "1_zipf-set-latency.csv").open()))
    assert set_[0]["test"] == "SET" and set_[0]["rps"] == "100.0"
    assert not (out / "1_zipf-client-latency.csv").exists()
    assert json.loads((out / "manifest.json").read_text()) == manifest
