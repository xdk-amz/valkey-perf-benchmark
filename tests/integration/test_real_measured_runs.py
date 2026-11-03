"""Measured runs and validity gates against a real server, both generators."""

import subprocess
import sys

import pytest

from tests.integration.real_valkey import (
    CACHECANNON,
    PROJECT_ROOT,
    free_port,
    make_checkout,
    read_rows,
    requires_cachecannon,
    requires_valkey,
    run_benchmark,
)

pytestmark = [pytest.mark.integration, requires_valkey, requires_cachecannon]

SHAPE = {"keyspacelen": 10000, "data_size": 32, "pipeline": 8, "clients": 8}


def _config(port, scenarios, measurement):
    return {
        "cluster_mode": False,
        "tls_mode": False,
        "port": port,
        "keyspacelen": [10000],
        "measurement": measurement,
        "generators": {"cachecannon": {"path": CACHECANNON, "threads": 2}},
        "test_groups": [
            {"group": i + 1, "scenarios": [sc]} for i, sc in enumerate(scenarios)
        ],
    }


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("measured")
    checkout = make_checkout(tmp_path / "valkey")
    port = free_port()
    measured = [
        {
            "id": "get-vb",
            "type": "read",
            "test": "GET",
            "populate_with": "SET",
            **SHAPE,
        },
        {
            "id": "get-cc",
            "type": "read",
            "generator": "cachecannon",
            "test": "GET",
            **SHAPE,
        },
        {
            "id": "rate-cc",
            "type": "read",
            "generator": "cachecannon",
            "test": "GET",
            "rate": 20000,
            **SHAPE,
            "pipeline": 1,
        },
    ]
    gated = [
        {
            "id": "light-vb",
            "type": "read",
            "test": "GET",
            "populate_with": "SET",
            **SHAPE,
            "clients": 1,
            "pipeline": 1,
        }
    ]
    configs = [
        _config(port, measured, {"window": 3, "settle": 1, "rate_tolerance_pct": 50}),
        _config(port, gated, {"window": 2, "settle": 1, "require_main_busy_pct": 99}),
    ]
    result = run_benchmark(tmp_path, configs, checkout)
    assert result.returncode == 0
    return tmp_path, {r["scenario"]: r for r in read_rows(tmp_path)}


def test_measured_rows_use_server_counters(run):
    _, rows = run
    for sid in ("get-vb", "get-cc"):
        row = rows[sid]
        assert row["valid"] is True, row["invalid_reasons"]
        assert row["benchmark_mode"] == "measured"
        assert row["duration"] == 3
        assert 2.5 < row["window_s"] < 4
        assert row["connected_clients"] == 8
        assert row["main_busy_pct"] > 0
        assert row["main_cpu_pct"] > 0
        assert row["generator_cores"] > 0
        assert row["server_ops_s"] == pytest.approx(row["rps"], rel=0.25)
    assert rows["get-cc"]["generator_latency_scope"] == "window"
    assert rows["get-vb"]["generator"] == "valkey-benchmark"


def test_fixed_rate_is_checked_against_target(run):
    _, rows = run
    row = rows["rate-cc"]
    assert row["valid"] is True, row["invalid_reasons"]
    assert row["server_ops_s"] == pytest.approx(20000, rel=0.5)


def test_saturation_gate_marks_light_load_invalid(run):
    _, rows = run
    row = rows["light-vb"]
    assert row["valid"] is False
    assert "busy < 99%" in row["invalid_reasons"]


def test_compare_drops_invalid_rows(run):
    tmp_path, _ = run
    metrics = tmp_path / "results" / "HEAD" / "metrics.json"
    result = subprocess.run(
        [
            sys.executable,
            "utils/compare_benchmark_results.py",
            "--baseline",
            str(metrics),
            "--new",
            str(metrics),
            "--output",
            str(tmp_path / "cmp.md"),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "excluded 1 invalid run(s)" in result.stderr
