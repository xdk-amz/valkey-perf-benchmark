"""Both generators against a TLS-only server."""

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration.real_valkey import (
    CACHECANNON,
    PROJECT_ROOT,
    VALKEY_DIR,
    VALKEY_TLS_DIR,
    free_port,
    make_checkout,
    read_rows,
    requires_cachecannon,
    requires_valkey,
    requires_valkey_tls,
    run_benchmark,
)

pytestmark = [pytest.mark.integration, requires_valkey_tls, requires_cachecannon]

SHAPE = {"keyspacelen": 5000, "data_size": 32, "pipeline": 8, "clients": 8}


def _config(port, measurement=None):
    cfg = {
        "cluster_mode": False,
        "tls_mode": True,
        "port": port,
        "keyspacelen": [5000],
        "generators": {"cachecannon": {"path": CACHECANNON, "threads": 2}},
        "test_groups": [
            {
                "group": 1,
                "scenarios": [
                    {
                        "id": "tls-vb",
                        "type": "read",
                        "test": "GET",
                        "populate_with": "SET",
                        "duration": 3,
                        **SHAPE,
                    },
                    {
                        "id": "tls-cc",
                        "type": "read",
                        "generator": "cachecannon",
                        "mix": {"get": 80, "set": 20},
                        "duration": 3,
                        **SHAPE,
                    },
                ],
            }
        ],
    }
    if measurement:
        cfg["measurement"] = measurement
        for sc in cfg["test_groups"][0]["scenarios"]:
            del sc["duration"]
    return cfg


@pytest.mark.parametrize("measured", [False, True])
def test_tls_rows_from_both_generators(tmp_path, measured):
    checkout = make_checkout(tmp_path / "valkey", source=VALKEY_TLS_DIR)
    measurement = (
        {
            "window": 2,
            "settle": 1,
            "max_main_runq_wait_pct": None,
            "max_main_irq_pct": None,
            "max_steal_pct": None,
        }
        if measured
        else None
    )
    result = run_benchmark(tmp_path, [_config(free_port(), measurement)], checkout)
    assert result.returncode == 0
    rows = {r["scenario"]: r for r in read_rows(tmp_path)}
    assert set(rows) == {"tls-vb", "tls-cc"}
    for row in rows.values():
        assert row["status"] == "success" and row["tls"] is True
        assert row["rps"] > 0
        if measured:
            assert row["valid"] is True, row["invalid_reasons"]
            assert row["server_ops_s"] > 0
    assert rows["tls-cc"]["generator_errors"] == 0
    toml = next((tmp_path / "results" / "HEAD" / "runs").glob("tls-cc_*/*.toml"))
    assert "tls = true" in toml.read_text()


@requires_valkey
def test_plain_valkey_benchmark_is_refused_before_start(tmp_path):
    """A TLS config given a valkey-benchmark built without TLS exits before
    any server starts, naming the cause."""
    checkout = make_checkout(tmp_path / "valkey", source=VALKEY_TLS_DIR)
    plain = Path(VALKEY_DIR) / "src" / "valkey-benchmark"
    port = free_port()
    cfg = _config(port)
    cfg["test_groups"][0]["scenarios"] = cfg["test_groups"][0]["scenarios"][:1]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps([cfg]))
    result = subprocess.run(
        [
            sys.executable,
            "benchmark.py",
            "--config",
            str(config_path),
            "--valkey-path",
            str(checkout),
            "--valkey-benchmark-path",
            str(plain),
            "--results-dir",
            str(tmp_path / "results"),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 1
    assert "built without TLS" in result.stdout
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    assert read_rows(tmp_path) == []
