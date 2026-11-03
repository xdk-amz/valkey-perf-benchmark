"""Unit tests for interleaved run order and the run manifest."""

import argparse
import json
from unittest.mock import patch

import benchmark
from run_manifest import build_manifest, manifest_id, write_manifest


def test_manifest_id_ignores_argv_and_frequency():
    m = {"argv": ["a"], "host": {"cpu_freq_mhz_at_setup": 1, "kernel": "k"}, "x": 1}
    n = {"argv": ["b"], "host": {"cpu_freq_mhz_at_setup": 2, "kernel": "k"}, "x": 1}
    assert manifest_id(m) == manifest_id(n)
    assert manifest_id(m) != manifest_id({**m, "x": 2})


def test_manifest_records_binary_and_config(tmp_path):
    server = tmp_path / "src" / "valkey-server"
    server.parent.mkdir()
    server.write_bytes(b"not really a server")
    m = build_manifest(
        commit="abc",
        valkey_dir=tmp_path,
        build_args=["USE_JEMALLOC=no"],
        generators=[{"name": "g", "version": "1"}],
        cfg={"port": 1, "groups_to_run": [1]},
    )
    assert m["server_binary"]["sha256"] and m["server_binary"]["path"] == str(server)
    assert m["valkey"]["build_args"] == ["USE_JEMALLOC=no"]
    assert m["config"] == {"port": 1}
    mid = write_manifest(tmp_path / "r", m)
    doc = json.loads((tmp_path / "r" / "manifests" / f"{mid}.json").read_text())
    assert doc["manifest_id"] == mid and doc["commit_ref"] == "abc"


def test_interleaved_rotates_commit_order():
    calls = []
    args = argparse.Namespace(runs=4, use_running_server=True, valkey_path=None)
    with patch.object(
        benchmark,
        "run_benchmark_matrix",
        side_effect=lambda **kw: calls.append(
            (
                kw["run_state"]["run_offset"],
                kw["run_state"]["order_slot"],
                kw["commit_id"],
            )
        ),
    ):
        benchmark.run_interleaved({"tls_mode": False}, ["A", "B"], args)
    assert [c[2] for c in calls] == ["A", "B", "B", "A", "A", "B", "B", "A"]
    slots = {(c[2], c[1]) for c in calls}
    assert slots == {("A", 0), ("A", 1), ("B", 0), ("B", 1)}


def test_rows_carry_provenance(minimal_client_runner):
    r = minimal_client_runner
    row = {}
    r._stamp_provenance(row)
    assert row == {}
    r.manifest_id, r.order_slot, r.current_run = "m1", 1, 3
    r._stamp_provenance(row)
    assert row == {"manifest_id": "m1", "order_slot": 1, "run_index": 3}
