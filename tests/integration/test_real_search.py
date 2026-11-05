"""Measured search scenarios against a real valkey-search module.

Needs, besides VPB_TEST_VALKEY_DIR (Valkey 9.1 or later):
    VPB_TEST_SEARCH_MODULE     valkey-search's libsearch.so
    VPB_TEST_SEARCH_BENCHMARK  a valkey-benchmark with --dataset support
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration.real_valkey import (
    PROJECT_ROOT,
    free_cluster_port,
    free_port,
    make_checkout,
    read_rows,
    requires_valkey,
    stop_server,
    wait_for_ping,
)

SEARCH_MODULE = os.environ.get("VPB_TEST_SEARCH_MODULE")
SEARCH_BENCHMARK = os.environ.get("VPB_TEST_SEARCH_BENCHMARK")

pytestmark = [
    pytest.mark.integration,
    requires_valkey,
    pytest.mark.skipif(
        not (SEARCH_MODULE and SEARCH_BENCHMARK),
        reason="set VPB_TEST_SEARCH_MODULE and VPB_TEST_SEARCH_BENCHMARK",
    ),
]

DATASET = "datasets/search_terms.csv"
DOCS = 1000
MEASUREMENT = {
    "window": 2,
    "settle": 1,
    "max_main_runq_wait_pct": None,
    "max_main_irq_pct": None,
    "max_steal_pct": None,
}
LOAD = {
    "id": "load",
    "type": "write",
    "cluster_execution": "single",
    "setup_commands": ["FT.CREATE idx ON HASH PREFIX 1 doc: SCHEMA title TEXT"],
    "flush_before": True,
    "dataset": DATASET,
    "maxdocs": DOCS,
    "clients": 10,
    "sequential": True,
    "command": 'HSET doc:{tag}:__rand_int__ title "__field:term__"',
}


def _search(execution):
    return {
        "id": "search",
        "type": "read",
        "cluster_execution": execution,
        "dataset": DATASET,
        "clients": 10,
        "command": 'FT.SEARCH idx "__field:term__"',
    }


def _config(port, cluster_ports=None, measured=True):
    cfg = {
        "seed": False,
        "port": port,
        "bind_ip": "127.0.0.1",
        "tls_mode": False,
        "io-threads": 2,
        "cluster_mode": bool(cluster_ports),
        "modules": [{"path": SEARCH_MODULE, "startup_args": ["--use-coordinator"]}],
        "test_groups": [
            {
                "group": 1,
                "scenarios": [
                    LOAD,
                    _search("parallel" if cluster_ports else "single"),
                ],
            }
        ],
    }
    if cluster_ports:
        cfg.update(
            cluster_nodes=len(cluster_ports),
            cluster_ports=cluster_ports,
            cpu_allocation={"cores_per_server": 1, "cores_per_client": 2},
        )
    if measured:
        cfg["measurement"] = MEASUREMENT
    else:
        cfg["test_groups"][0]["scenarios"][1]["duration"] = 3
    return cfg


def _run(tmp_path, cfg, checkout, *extra):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps([cfg], indent=2))
    argv = [
        sys.executable,
        "benchmark.py",
        "--config",
        str(config_path),
        "--valkey-path",
        str(checkout),
        "--valkey-benchmark-path",
        SEARCH_BENCHMARK,
        "--module",
        "search",
        "--module-path",
        SEARCH_MODULE,
        "--results-dir",
        str(tmp_path / "results"),
        *extra,
    ]
    result = subprocess.run(
        argv, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=600
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout[-4000:] + result.stderr[-4000:])
    return result


def _rows(tmp_path):
    rows = []
    for f in (tmp_path / "results").rglob("metrics.json"):
        rows += json.loads(f.read_text())
    return {r["scenario"]: r for r in rows}


@pytest.mark.parametrize("nodes", [1, 2], ids=["standalone", "cluster"])
def test_measured_search_rows(tmp_path, nodes):
    if nodes == 1:
        cfg = _config(free_port())
    else:
        ports = [free_cluster_port() for _ in range(nodes)]
        cfg = _config(ports[0], cluster_ports=ports)
    result = _run(tmp_path, cfg, make_checkout(tmp_path / "valkey"))
    assert result.returncode == 0
    rows = _rows(tmp_path)
    row = rows["search"]
    assert row["status"] == "success", row.get("error")
    # Both fields are written only for more than one node or process.
    assert row.get("server_nodes", 1) == nodes
    assert row.get("generator_processes", 1) == nodes
    assert row["valid"] is True, row["invalid_reasons"]
    assert row["server_ops_s"] > 0
    # The probe counts what the server executed; the generator's own count
    # must agree with it.
    assert row["server_ops_s"] == pytest.approx(row["rps"], rel=0.15)


def test_search_commands_reach_the_index(tmp_path):
    """Against a server the test started itself, the measured FT.SEARCH run
    executes FT.SEARCH on an index holding the loaded documents, with no
    failed calls."""
    import valkey

    checkout = make_checkout(tmp_path / "valkey")
    port = free_port()
    proc = subprocess.Popen(
        [
            str(checkout / "src" / "valkey-server"),
            "--port",
            str(port),
            "--bind",
            "127.0.0.1",
            "--save",
            "",
            "--appendonly",
            "no",
            "--io-threads",
            "2",
            "--loadmodule",
            SEARCH_MODULE,
        ],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_ping(port)
        cfg = _config(port)
        cfg["modules"] = []
        result = _run(tmp_path, cfg, checkout, "--use-running-server")
        assert result.returncode == 0
        row = _rows(tmp_path)["search"]
        assert row["valid"] is True, row["invalid_reasons"]
        client = valkey.Valkey(port=port)
        info = client.ft("idx").info()
        assert int(info["num_docs"]) > 0
        stats = client.info("commandstats")
        search = stats["cmdstat_FT.SEARCH"]
        assert search["calls"] >= row["server_ops_s"] * MEASUREMENT["window"] * 0.5
        assert search.get("failed_calls", 0) == 0
        assert search.get("rejected_calls", 0) == 0
    finally:
        stop_server(proc)
