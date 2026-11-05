"""Interleaved run order and run manifests against real servers."""

import json
import subprocess
import sys

import pytest

from tests.integration.real_valkey import (
    CACHECANNON,
    PROJECT_ROOT,
    free_port,
    read_rows,
    requires_cachecannon,
    requires_valkey,
    run_benchmark_commits,
)

pytestmark = [pytest.mark.integration, requires_valkey, requires_cachecannon]

REFS = ["base", "cand"]


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("order")
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
            "max_main_irq_pct": None,
            "max_steal_pct": None,
        },
        "generators": {"cachecannon": {"path": CACHECANNON, "threads": 1}},
        "test_groups": [
            {
                "group": 1,
                "scenarios": [
                    {"id": "vb", "type": "read", "test": "GET", **shape},
                    {
                        "id": "cc",
                        "type": "read",
                        "generator": "cachecannon",
                        "test": "GET",
                        **shape,
                    },
                ],
            }
        ],
    }
    result = run_benchmark_commits(
        tmp_path, [cfg], REFS, "--order", "interleaved", "--runs", "4"
    )
    assert result.returncode == 0
    log = result.stdout + result.stderr
    return tmp_path, {ref: read_rows(tmp_path, ref) for ref in REFS}, log


def test_every_commit_fills_every_slot(run):
    _, rows, log = run
    for ref in REFS:
        got = sorted(
            (r["run_index"], r["order_slot"])
            for r in rows[ref]
            if r["scenario"] == "vb"
        )
        assert [i for i, _ in got] == [1, 2, 3, 4]
        assert sorted(s for _, s in got) == [0, 0, 1, 1]
    slot0 = {
        r["run_index"]: r["order_slot"] for r in rows["base"] if r["scenario"] == "vb"
    }
    assert slot0 == {1: 0, 2: 1, 3: 0, 4: 1}
    assert log.index("Interleaved run 1/4: ['base', 'cand']") < log.index(
        "Interleaved run 2/4: ['cand', 'base']"
    )


def test_all_rows_succeed(run):
    _, rows, _ = run
    for ref in REFS:
        assert len(rows[ref]) == 8
        assert all(r["status"] == "success" for r in rows[ref]), rows[ref]


def test_rows_point_at_their_manifest(run):
    tmp_path, rows, _ = run
    for ref in REFS:
        ids = {r["manifest_id"] for r in rows[ref]}
        assert len(ids) == 1
        doc = json.loads(
            (tmp_path / "results" / ref / "manifests" / f"{ids.pop()}.json").read_text()
        )
        assert doc["commit_ref"] == ref
        assert doc["server_binary"]["sha256"]
        assert doc["server_binary"]["text_size"] > 0
        assert doc["valkey"]["commit"] and doc["valkey"]["dirty"] is False
        assert doc["harness"]["commit"]
        assert {g["name"] for g in doc["generators"]} == {
            "valkey-benchmark",
            "cachecannon",
        }
        assert all(g["sha256"] for g in doc["generators"])
        assert doc["config"]["measurement"]["window"] == 2


def test_checkouts_are_removed_at_the_end(run):
    tmp_path, _, _ = run
    assert not any(tmp_path.glob("valkey_*"))


def test_ab_summary_reads_the_run(run):
    tmp_path, _, _ = run
    result = subprocess.run(
        [
            sys.executable,
            "utils/ab_summary.py",
            "--results-dir",
            str(tmp_path / "results"),
            "--json",
            *REFS,
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    entries = json.loads(result.stdout)
    assert len(entries) == 4
    for e in entries:
        assert e["n"] == 4 and e["n_valid"] == 4
        assert e["server_ops_s"] > 0
    assert all("welch_p" in e for e in entries if e["commit"] == "cand")
