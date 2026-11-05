"""Measured runs on a multi-node cluster and of mixed scenarios."""

import pytest

from tests.integration.real_valkey import (
    CACHECANNON,
    free_cluster_port,
    free_port,
    make_checkout,
    read_rows,
    requires_cachecannon,
    requires_valkey,
    run_benchmark,
)

pytestmark = [pytest.mark.integration, requires_valkey, requires_cachecannon]

SHAPE = {"keyspacelen": 5000, "data_size": 32, "pipeline": 4, "clients": 6}
MEASUREMENT = {
    "window": 2,
    "settle": 1,
    "max_main_runq_wait_pct": None,
    "max_main_irq_pct": None,
    "max_steal_pct": None,
}


def _base(port, **top):
    return {
        "tls_mode": False,
        "port": port,
        "keyspacelen": [5000],
        "measurement": MEASUREMENT,
        "generators": {"cachecannon": {"path": CACHECANNON, "threads": 2}},
        **top,
    }


def test_cluster_counters_cover_every_node(tmp_path):
    ports = [free_cluster_port() for _ in range(3)]
    cfg = _base(
        ports[0],
        cluster_mode=True,
        cluster_nodes=3,
        cluster_ports=ports,
        cpu_allocation={"cores_per_server": 1, "cores_per_client": 2},
        test_groups=[
            {
                "group": 1,
                "scenarios": [
                    {"id": "cl-vb", "type": "read", "test": "GET", **SHAPE},
                    # One process per node, as search scenarios run; a
                    # command any node serves stands in for FT.SEARCH.
                    {
                        "id": "cl-vb-par",
                        "type": "read",
                        "command": "PING",
                        "cluster_execution": "parallel",
                        **SHAPE,
                    },
                    {
                        "id": "cl-cc",
                        "type": "read",
                        "generator": "cachecannon",
                        "test": "GET",
                        **SHAPE,
                    },
                ],
            }
        ],
    )
    result = run_benchmark(tmp_path, [cfg], make_checkout(tmp_path / "valkey"))
    assert result.returncode == 0
    rows = {r["scenario"]: r for r in read_rows(tmp_path)}
    assert set(rows) == {"cl-vb", "cl-vb-par", "cl-cc"}, rows
    for sid, row in rows.items():
        assert row["status"] == "success", (sid, row.get("error"))
        assert row["server_nodes"] == 3
        assert row["server_ops_s"] > 0
        assert row["main_busy_pct"] <= row["main_busy_pct_max"]
        assert row["valid"] is True, (sid, row["invalid_reasons"])
    assert rows["cl-vb-par"]["generator_processes"] == 3
    assert rows["cl-vb-par"]["server_ops_s"] == pytest.approx(
        rows["cl-vb-par"]["rps"], rel=0.3
    )


def test_mixed_writers_and_readers_share_one_window(tmp_path):
    cfg = _base(
        free_port(),
        cluster_mode=False,
        test_groups=[
            {
                "group": 1,
                "scenarios": [
                    {
                        "id": "mx",
                        "type": "mixed",
                        "writes": [{"id": "w", "test": "SET", **SHAPE, "clients": 2}],
                        "reads": [{"id": "r", "test": "GET", **SHAPE}],
                    },
                    {
                        "id": "mx-cc",
                        "type": "mixed",
                        "generator": "cachecannon",
                        "writes": [{"id": "w", "test": "SET", **SHAPE, "clients": 2}],
                        "reads": [{"id": "r", "test": "GET", **SHAPE}],
                    },
                ],
            }
        ],
    )
    result = run_benchmark(tmp_path, [cfg], make_checkout(tmp_path / "valkey"))
    assert result.returncode == 0
    rows = read_rows(tmp_path)
    by = {(r["scenario"], r["test_phase"]): r for r in rows}
    assert set(by) == {
        ("mx", "mixed_write"),
        ("mx", "mixed_read"),
        ("mx-cc", "mixed_write"),
        ("mx-cc", "mixed_read"),
    }, [(r.get("scenario"), r.get("test_phase"), r.get("error")) for r in rows]
    for sid in ("mx", "mx-cc"):
        w, r = by[(sid, "mixed_write")], by[(sid, "mixed_read")]
        assert w["status"] == r["status"] == "success"
        assert w["command"] == "SET" and r["command"] == "GET"
        assert w["server_ops_s"] == r["server_ops_s"]
        assert w["connected_clients"] == 8
        assert w["valid"] is True and r["valid"] is True, (w, r)
        assert w["server_ops_s"] == pytest.approx(w["rps"] + r["rps"], rel=0.3)
