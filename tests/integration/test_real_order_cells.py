"""Interleaving commits within each cell against real servers."""

import pytest

from tests.integration.real_valkey import (
    CACHECANNON,
    free_port,
    read_rows,
    requires_cachecannon,
    requires_valkey,
    run_benchmark_commits,
)

pytestmark = [pytest.mark.integration, requires_valkey, requires_cachecannon]

REFS = ["base", "cand"]


def test_cells_finish_on_every_commit_before_the_next_cell(tmp_path):
    shape = {"keyspacelen": 5000, "data_size": 16, "pipeline": 4, "clients": 4}
    cfg = {
        "cluster_mode": False,
        "tls_mode": False,
        "port": free_port(),
        "keyspacelen": [5000],
        "measurement": {
            "window": 2,
            "settle": 1,
            "max_main_runq_wait_pct": None,
            "max_steal_pct": None,
        },
        "generators": {"cachecannon": {"path": CACHECANNON, "threads": 1}},
        "test_groups": [
            {
                "group": 1,
                "scenarios": [{"id": "vb", "type": "read", "test": "GET", **shape}],
            },
            {
                "group": 2,
                "scenarios": [
                    {
                        "id": "cc",
                        "type": "read",
                        "generator": "cachecannon",
                        "test": "GET",
                        **shape,
                    }
                ],
            },
        ],
    }
    result = run_benchmark_commits(
        tmp_path, [cfg], REFS, "--order", "interleaved-cell", "--runs", "2"
    )
    assert result.returncode == 0
    rows = [r for ref in REFS for r in read_rows(tmp_path, ref)]
    assert len(rows) == 8 and all(r["status"] == "success" for r in rows)
    log = result.stdout + result.stderr
    cells = [l.split("=== Cell ", 1)[1] for l in log.splitlines() if "=== Cell " in l]
    assert [c.split(":")[0] for c in cells] == [
        "1/2 (1, 'vb'), run 1/2",
        "1/2 (1, 'vb'), run 2/2",
        "2/2 (2, 'cc'), run 1/2",
        "2/2 (2, 'cc'), run 2/2",
    ]
    assert cells[0].endswith("['base', 'cand'] ===")
    assert cells[1].endswith("['cand', 'base'] ===")
    for ref in REFS:
        for sid in ("vb", "cc"):
            got = sorted(
                (r["run_index"], r["order_slot"])
                for r in read_rows(tmp_path, ref)
                if r["scenario"] == sid
            )
            assert [i for i, _ in got] == [1, 2] and sorted(s for _, s in got) == [0, 1]
    ids = {r["manifest_id"] for r in read_rows(tmp_path, "base")}
    assert len(ids) == 1
    assert not any(tmp_path.glob("valkey_*"))
