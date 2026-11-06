"""Tests for configs/mixed-rw.json and configs/mixed-rw-smoke.json."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from benchmark import load_configs, validate_config, validate_cpu_allocation
from utils.cpu_utils import calculate_client_cpu_ranges
from valkey_benchmark import ClientRunner

FILES = ["configs/mixed-rw.json", "configs/mixed-rw-smoke.json"]
FULL = load_configs(FILES[0])[0]
SMOKE = load_configs(FILES[1])[0]


def _runner(config):
    runner = ClientRunner(
        commit_id="abc123",
        config=config,
        cluster_mode=False,
        tls_mode=False,
        target_ip="127.0.0.1",
        results_dir=Path("/tmp/test_results"),
        valkey_path="/tmp/valkey",
        valkey_benchmark_path="src/valkey-benchmark",
    )
    runner.client_cpu_ranges = calculate_client_cpu_ranges(config)
    return runner


def _scenarios(config):
    return [s for g in config["test_groups"] for s in g["scenarios"]]


@pytest.mark.parametrize("config", [FULL, SMOKE], ids=["full", "smoke"])
def test_config_validates_and_keeps_everything_in_memory(config):
    validate_config(config)
    validate_cpu_allocation(config)
    assert "build_args" not in config
    servers = config["custom-server-configs"]
    assert servers["maxmemory"] == "0"
    assert servers["maxmemory-policy"] == "noeviction"
    assert not any(k.startswith("ext-storage") for k in servers)


@pytest.mark.parametrize("path", FILES)
def test_no_storage_engine_is_named(path):
    text = Path(path).read_text().lower()
    assert "ext-storage" not in text
    assert "flash" not in text


def test_only_zipf80_and_unif80():
    for cfg in (FULL, SMOKE):
        assert [s["id"] for s in _scenarios(cfg)] == ["zipf80", "unif80"]


@pytest.mark.parametrize("scenario", _scenarios(FULL), ids=lambda s: s["id"])
def test_full_size_shape(scenario):
    assert scenario["keyspacelen"] == 4000000
    assert scenario["data_size"] == 512
    assert scenario["duration"] == 60
    runner = _runner(FULL)
    writes, reads = runner._normalize_mixed_configs(scenario)
    assert [c["clients"] for c in reads] == [160]
    assert [c["clients"] for c in writes] == [40]
    zipfian = scenario["id"] == "zipf80"
    for child in writes + reads:
        argv = runner._build_benchmark_command(child)
        assert argv[argv.index("-r") + 1] == "4000000"
        assert argv[argv.index("-d") + 1] == "512"
        assert argv[argv.index("--keysize") + 1] == "100"
        assert ("--zipfian" in argv) == zipfian
        if zipfian:
            assert argv[argv.index("--zipfian") + 1] == "1.0"


@pytest.mark.parametrize("scenario", _scenarios(FULL), ids=lambda s: s["id"])
def test_populate_loads_every_key_once_with_100_byte_keys(scenario):
    runner = _runner(FULL)
    captured = []
    with (
        patch.object(
            runner,
            "_run",
            side_effect=lambda command=None, **k: captured.append(command),
        ),
        patch.object(runner, "_count_loaded_keys", return_value=4000000),
    ):
        runner._populate_scenario_keyspace(scenario, 7)
    [argv] = captured
    assert argv[argv.index("-t") + 1] == "SET"
    assert argv[argv.index("-n") + 1] == "4000000"
    assert argv[argv.index("--keysize") + 1] == "100"
    assert "--sequential" in argv
    assert "--zipfian" not in argv
