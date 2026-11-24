"""vpb_result.py: every scenario config in configs/ emits vpb-result/1 records that pass the explorer's check.

The rows are built with the runners' own code (MetricsProcessor for benchmark.py, the matrix row fields for
matrix_runner.py), one invocation per compiled scenario, so each config's scenario shapes go through the writer.
"""

import json
from pathlib import Path

import pytest

import benchmark
import config_loader
import vpb_result
from dashboards.explorer import fingerprint, idscan, schema
from process_metrics import MetricsProcessor

ROOT = Path(__file__).resolve().parents[1]
SHA = "c" * 40
HARNESS = {"name": "valkey-perf-benchmark", "repo": "o/vpb", "sha": "d" * 40}
CONFIGS = sorted(p.name for p in (ROOT / "configs").glob("*.json"))
BENCH = {
    "rps": 1000.0,
    "avg_latency_ms": 0.5,
    "p50_latency_ms": 0.4,
    "p95_latency_ms": 0.9,
    "p99_latency_ms": 1.2,
}


@pytest.fixture(autouse=True)
def facts(monkeypatch):
    monkeypatch.setattr(vpb_result, "harness", lambda: HARNESS)
    monkeypatch.setattr(vpb_result, "instance_type", lambda: "r7g.2xlarge")


def rows_for(proc, scenario):
    """The metric rows one invocation of scenario writes to metrics.json."""
    subs = (
        scenario.get("writes", []) + scenario.get("reads", [])
        if scenario.get("type") == "mixed"
        else [scenario]
    )
    rows = []
    for sub in subs:
        row = proc.create_metrics(
            BENCH,
            sub.get("command") or sub.get("test"),
            sub.get("data_size", scenario.get("data_size", 100)),
            sub.get("pipeline", scenario.get("pipeline", 1)),
            sub.get("clients", scenario.get("clients", 1)),
            duration=scenario.get("duration"),
        )
        row["status"] = "success"
        if scenario.get("window"):
            row.update(server_ops_s=900.0, window_complete=True, window_error_replies=0)
        rows.append(row)
    return rows


def emit(path, cfg, server_local=True):
    proc = MetricsProcessor(SHA, False, False, "2026-11-19T10:00:00Z", None, None, "aarch64",
                            "valkey-io/valkey")  # fmt: skip
    invs = [
        {
            "scenario": s,
            "group": g["group"],
            "rows": rows_for(proc, s),
            "time": "2026-11-19T10:01:00Z",
        }
        for g in cfg["test_groups"]
        for s in g["scenarios"]
    ]
    ctx = vpb_result.run_context(
        data_set="official-ci",
        build={
            "repo": "valkey-io/valkey",
            "ref": "unstable",
            "sha": SHA,
            "time": "2026-11-19T10:00:00Z",
        },
        server_local=server_local,
        server_cpus="0-3" if server_local else None,
        io_threads=4,
        arch="aarch64",
        config=vpb_result.config_for_rerun(cfg, str(ROOT / "configs" / path)),
        rerun="",
        env={"kernel_version": "6.1", "benchmark_tool_version": "valkey-benchmark 9.0"},
        server_type=None if server_local else "c8g.4xlarge",
    )
    return vpb_result.benchmark_records(cfg, invs, ctx), invs


@pytest.mark.parametrize("name", CONFIGS)
def test_every_scenario_config_emits_records_that_pass_check(name, tmp_path):
    for cfg in benchmark.load_configs(str(ROOT / "configs" / name)):
        doc, invs = emit(name, cfg)
        assert schema.check_doc(doc, name) == []
        f = vpb_result.write(tmp_path, doc, merge=False)
        assert list(idscan.scan(f)) == []
        scenarios = {r["scenario"] for r in doc["records"]}
        assert len(scenarios) == len(invs), "one cell per compiled scenario"
        r = doc["records"][0]
        assert r["family"] == vpb_result.family(cfg)
        assert r["hardware"] == {
            "server": "r7g.2xlarge",
            "cores": 4,
            "client": "r7g.2xlarge",
            "arch": "aarch64",
        }
        assert (r["io_threads"], r["harness"]["sha"], r["build"]["ref"]) == (
            4,
            "d" * 40,
            "unstable",
        )
        block = doc["reproduce"][r["reproduce"]]
        assert block["config"] == {"path": f"configs/{name}", "overrides": {}}
        recorded = fingerprint.fingerprint(doc["records"])["recorded"]
        assert {
            "repository",
            "commit",
            "scenario_family",
            "scenario",
            "measurement",
            "instance_type",
        } <= set(recorded)


def test_set_scenarios_keep_shape_mix_and_server_throughput():
    cfg = benchmark.load_configs(str(ROOT / "configs" / "set-commands-smoke.json"))[0]
    doc, _ = emit("set-commands-smoke.json", cfg, server_local=False)
    r = next(
        x
        for x in doc["records"]
        if x["scenario"] == "intset512.exp_half.sismember.P1"
        and x["metric"] == "server_ops"
    )
    assert (r["family"], r["command"], r["mix"], r["pipeline"], r["value"]) == (
        "set commands (smoke)",
        "sismember",
        "exp_half",
        1,
        900.0,
    )
    assert r["shape"] == {
        "type": "set",
        "encoding": "intset",
        "size": 512,
        "size_unit": "members",
    }
    # the server is on another host: its type is the option's, its cores are unknown
    assert r["hardware"] == {
        "server": "c8g.4xlarge",
        "cores": None,
        "client": "r7g.2xlarge",
        "arch": "aarch64",
    }


def test_mixed_scenario_sums_clients_and_keeps_both_commands():
    cfg = benchmark.load_configs(str(ROOT / "configs" / "tiering.json"))[0]
    doc, _ = emit("tiering.json", cfg)
    r = next(
        x
        for x in doc["records"]
        if x["scenario"] == "1.zipf80" and x["metric"] == "ops"
    )
    assert (r["clients"], r["command_line"], r["value"]) == (200, "SET | GET", 2000.0)


def test_failed_invocation_is_a_missing_cell():
    cfg = benchmark.load_configs(str(ROOT / "configs" / "sampler-smoke.json"))[0]
    proc = MetricsProcessor(SHA, False, False, "t", None, None, None, None)
    s = cfg["test_groups"][0]["scenarios"][0]
    bad = proc.build_base_metadata(s["test"], 64, 1, 4, duration=15)
    bad.update(status="failed", error="No results")
    good = rows_for(proc, s)
    ctx = vpb_result.run_context(data_set="community", build={"repo": "o/v", "ref": "x", "sha": SHA, "time": None},
                                 server_local=True, server_cpus=None, io_threads=None, arch=None,
                                 config={"inline": {}}, rerun="", env={})  # fmt: skip
    one = vpb_result.benchmark_records(
        cfg, [{"scenario": s, "group": 1, "rows": [bad]}], ctx
    )
    r = one["records"][0]
    assert (r["metric"], r["value"], r["missing"], r["runs"][0]["why"]) == (
        "ops",
        None,
        "no valid run",
        "No results",
    )
    both = vpb_result.benchmark_records(
        cfg,
        [
            {"scenario": s, "group": 1, "rows": [bad]},
            {"scenario": s, "group": 1, "rows": good},
        ],
        ctx,
    )
    r = next(x for x in both["records"] if x["metric"] == "ops")
    assert (r["value"], r["n"], len(r["runs"])) == (1000.0, 1, 2)
    assert "server instance type not recorded" not in one["sources"][0]["lacks"]
    assert "io-threads not recorded" in one["sources"][0]["lacks"]


def test_write_merges_each_invocation_into_the_results_file(tmp_path):
    cfg = benchmark.load_configs(str(ROOT / "configs" / "sampler-smoke.json"))[0]
    doc, _ = emit("sampler-smoke.json", cfg)
    vpb_result.write(tmp_path, doc)
    vpb_result.write(tmp_path, doc)
    merged = json.loads((tmp_path / vpb_result.FILENAME).read_text())
    assert schema.check_doc(merged, "merged") == []
    assert len(merged["records"]) == len(doc["records"])
    assert all(r["n"] == 2 for r in merged["records"])


def test_matrix_run_emits_a_record_per_cell_build_and_metric():
    cfg = config_loader.load(
        str(ROOT / "configs" / "matrix" / "perf-matrix.json"), profile="slice-iot4-p1"
    )[0]
    cells = [c for g in cfg["test_groups"] for c in g["scenarios"]]
    rows = [
        {"scenario_id": c["id"], "build": label, "commit": sha, "run": rep, "status": "ok",
         "timestamp": "2026-11-19T10:00:00+00:00", "server_ops_s": 1000.0 + rep, "generator_ops_s": 990.0,
         "p50_ms": 0.3, "p99_ms": 0.9, "p999_ms": None}
        for c in cells for label, sha in (("unstable", "a" * 40), ("candidate", "b" * 40)) for rep in (1, 2, 3)
    ]  # fmt: skip
    manifest = {
        "hosts": {
            "server": {
                "instance_type": "c8g.metal-24xl",
                "kernel": "6.1",
                "arch": "aarch64",
            },
            "generator:g1": {"instance_type": "c8g.metal-24xl", "arch": "aarch64"},
        },
        "reps": 3,
    }
    doc = vpb_result.matrix_records(cfg, cells, rows, manifest, data_set="community", repository=None,
                                    config_path=str(ROOT / "configs" / "matrix" / "perf-matrix.json"),
                                    server_cores={c["id"]: 6 for c in cells})  # fmt: skip
    assert schema.check_doc(doc, "matrix") == []
    assert len(doc["records"]) == len(cells) * 2 * len(vpb_result.MATRIX_METRICS)
    r = next(
        x
        for x in doc["records"]
        if x["scenario"] == "hget"
        and x["metric"] == "server_ops"
        and x["build"]["ref"] == "candidate"
    )
    assert (r["family"], r["command"], r["io_threads"], r["clients"], r["value"]) == (
        "perf matrix",
        "hget",
        4,
        512,
        1002.0,
    )
    assert r["shape"] == {
        "type": "hash",
        "encoding": None,
        "size": 10,
        "size_unit": "members",
    }
    assert r["hardware"] == {
        "server": "c8g.metal-24xl",
        "cores": 6,
        "client": "c8g.metal-24xl",
        "arch": "aarch64",
    }
    assert [x["p"] for x in r["runs"]] == ["r1", "r2", "r3"]
    p999 = next(
        x
        for x in doc["records"]
        if x["scenario"] == "hget" and x["metric"] == "p999_ms"
    )
    assert p999["value"] is None and p999["missing"] == "no valid run"


def test_cpu_count_and_build_ref():
    assert (
        vpb_result.cpu_count("0-8"),
        vpb_result.cpu_count("0,2-3"),
        vpb_result.cpu_count("1,1-2"),
    ) == (9, 3, 2)
    assert vpb_result.build_ref("a" * 40, None, None) == "unknown"
    assert vpb_result.build_ref("v8.1.0", None, None) == "v8.1.0"
    assert vpb_result.build_ref("a" * 40, "unstable", None) == "unstable"
