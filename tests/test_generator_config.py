"""Config validation and ClientRunner wiring for non-default generators."""

from pathlib import Path
from unittest.mock import patch

import pytest

from benchmark import _with_cli_generator_paths, validate_config, validate_test_groups
from generators import GeneratorResult
from process_metrics import MetricsProcessor
from valkey_benchmark import ClientRunner


def _cfg(scenario, **top):
    return {"test_groups": [{"group": 1, "scenarios": [scenario]}], **top}


CC = {"generator": "cachecannon", "test": "GET", "duration": 10, "clients": 4}


class TestValidation:
    def test_cachecannon_scenario_passes(self):
        validate_test_groups(_cfg(CC))

    def test_top_level_generator(self):
        sc = {k: v for k, v in CC.items() if k != "generator"}
        validate_test_groups(_cfg(sc, generator="cachecannon"))

    def test_mix_counts_as_the_command(self):
        sc = {**CC, "mix": {"get": 80, "set": 20}}
        del sc["test"]
        validate_test_groups(_cfg(sc))

    @pytest.mark.parametrize(
        "change, match",
        [
            ({"generator": "nope"}, "'generator' must be one of"),
            ({"mix": {"get": 1, "del": 1}}, "'mix' must map"),
            ({"mix": {"get": 0}}, "'mix' must map"),
            ({"rate": 0}, "rate"),
            ({"warmup": 5}, "does not support 'warmup'"),
            ({"requests": 1000}, "does not support 'requests'"),
            ({"duration": None}, "needs a 'duration'"),
        ],
    )
    def test_rejects(self, change, match):
        sc = {**CC, **change}
        if sc.get("mix"):
            sc.pop("test")
        with pytest.raises(ValueError, match=match):
            validate_test_groups(_cfg(sc))

    @pytest.mark.parametrize("key", ["rate", "mix"])
    def test_valkey_benchmark_rejects_cachecannon_only_keys(self, key):
        sc = {"test": "GET", "duration": 5, key: 10 if key == "rate" else {"get": 1}}
        if key == "mix":
            sc.pop("test")
        with pytest.raises(ValueError, match="valkey-benchmark does not support"):
            validate_test_groups(_cfg(sc))

    def test_generators_block_must_name_known_backends(self, minimal_valid_config):
        minimal_valid_config["generators"] = {"memtier": {}}
        with pytest.raises(ValueError, match="'generators' must map"):
            validate_config(minimal_valid_config)


def test_cli_path_overrides_config(tmp_path):
    class Args:
        cachecannon_path = tmp_path / "cc"

    cfg = {"generators": {"cachecannon": {"path": "/old", "threads": 2}}}
    out = _with_cli_generator_paths(cfg, Args())
    assert out["generators"]["cachecannon"] == {
        "path": str(tmp_path / "cc"),
        "threads": 2,
    }
    assert cfg["generators"]["cachecannon"]["path"] == "/old"


def test_runner_row_carries_generator_fields(tmp_path):
    cfg = {"generators": {"cachecannon": {"path": "/bin/cc"}}, "port": 16379}
    runner = ClientRunner(
        commit_id="abc",
        config=cfg,
        cluster_mode=False,
        tls_mode=False,
        target_ip="127.0.0.1",
        results_dir=tmp_path,
        valkey_path=str(tmp_path),
        valkey_benchmark_path="src/valkey-benchmark",
    )
    seen = {}

    def fake_run(gen, req):
        seen["req"] = req
        return GeneratorResult(
            row={"rps": 1000.0, "p50_latency_ms": 0.2, "test": "GET"},
            extra={"generator_errors": 0, "p999_latency_ms": 1.5},
            argv=["/bin/cc", "x.toml"],
            stdout="",
        )

    with (
        patch("valkey_benchmark.run_for_duration", side_effect=fake_run),
        patch("generators.base.LoadGenerator.version", return_value="cc 1.0"),
    ):
        proc, row = runner._execute_benchmark_run(CC, seed_val=None)
    assert proc is None
    assert seen["req"].port == 16379 and seen["req"].duration_s == 10
    metrics = runner._build_scenario_metrics(
        CC,
        None,
        row,
        1,
        None,
        0,
        None,
        MetricsProcessor("abc", False, False, "2026-01-01T00:00:00Z"),
    )
    assert metrics["rps"] == 1000.0
    assert metrics["avg_latency_ms"] is None
    assert metrics["generator"] == "cachecannon"
    assert metrics["generator_version"] == "cc 1.0"
    assert metrics["p999_latency_ms"] == 1.5
    assert "_generator_extra" not in metrics
