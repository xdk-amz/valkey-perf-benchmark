"""Both load generators end to end against a real server."""

import pytest

from tests.integration.real_valkey import (
    CACHECANNON,
    free_port,
    make_checkout,
    read_rows,
    requires_cachecannon,
    requires_valkey,
    run_benchmark,
)

pytestmark = [pytest.mark.integration, requires_valkey, requires_cachecannon]

SHAPE = {"keyspacelen": 10000, "data_size": 32, "pipeline": 4, "clients": 8}


def _config(port, scenarios, **top):
    return {
        "cluster_mode": False,
        "tls_mode": False,
        "port": port,
        "keyspacelen": [10000],
        "generators": {"cachecannon": {"path": CACHECANNON, "threads": 2}},
        "test_groups": [
            {"group": i + 1, "scenarios": [sc]} for i, sc in enumerate(scenarios)
        ],
        **top,
    }


@pytest.fixture
def checkout(tmp_path):
    return make_checkout(tmp_path / "valkey")


def test_valkey_benchmark_and_cachecannon_rows(tmp_path, checkout):
    scenarios = [
        {
            "id": "get-vb",
            "type": "read",
            "test": "GET",
            "populate_with": "SET",
            "duration": 3,
            **SHAPE,
        },
        {
            "id": "get-cc",
            "type": "read",
            "generator": "cachecannon",
            "test": "GET",
            "duration": 3,
            **SHAPE,
        },
        {
            "id": "mix-cc",
            "type": "read",
            "generator": "cachecannon",
            "mix": {"get": 80, "set": 20},
            "duration": 3,
            **SHAPE,
        },
    ]
    result = run_benchmark(tmp_path, [_config(free_port(), scenarios)], checkout)
    assert result.returncode == 0
    rows = {r["scenario"]: r for r in read_rows(tmp_path)}
    assert set(rows) == {"get-vb", "get-cc", "mix-cc"}
    assert all(r["status"] == "success" and r["rps"] > 0 for r in rows.values())

    vb, cc, mix = rows["get-vb"], rows["get-cc"], rows["mix-cc"]
    assert "generator" not in vb
    assert vb["avg_latency_ms"] is not None
    for row in (cc, mix):
        assert row["generator"] == "cachecannon"
        assert row["generator_version"].startswith("cachecannon")
        assert row["generator_argv"].endswith("cachecannon.toml")
        assert row["generator_errors"] == 0
        assert row["p999_latency_ms"] >= row["p99_latency_ms"] > 0
        assert row["avg_latency_ms"] is None
    assert cc["command"] == "GET"
    assert mix["command"] == "GET:SET 80:20"
    toml = next((tmp_path / "results" / "HEAD" / "runs").glob("mix-cc_*/*.toml"))
    assert "get = 80\nset = 20" in toml.read_text()
