"""A TLS config fails before any build when valkey-benchmark lacks TLS."""

import sys

import pytest

import benchmark
from benchmark import benchmark_supports_tls, check_benchmark_tls

HELP_TLS = (
    "Usage: valkey-benchmark [OPTIONS]\n --tls  Establish a secure TLS connection.\n"
)
HELP_PLAIN = "Usage: valkey-benchmark [OPTIONS]\n -h <hostname>  Server hostname\n"


def _fake_benchmark(tmp_path, help_text):
    path = tmp_path / "valkey-benchmark"
    path.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdout.write({help_text!r})\n"
    )
    path.chmod(0o755)
    return path


def _cfg(tls, generator=None, mixed=False):
    sc = {"id": "s", "command": "GET"}
    if mixed:
        sc = {"id": "m", "type": "mixed", "writes": [{"command": "SET"}], "reads": []}
    if generator:
        sc["generator"] = generator
    return {"tls_mode": tls, "test_groups": [{"group": 1, "scenarios": [sc]}]}


def test_detects_tls_from_help(tmp_path):
    assert benchmark_supports_tls(_fake_benchmark(tmp_path, HELP_TLS))
    plain = tmp_path / "plain"
    plain.mkdir()
    assert not benchmark_supports_tls(_fake_benchmark(plain, HELP_PLAIN))


def test_tls_config_with_plain_benchmark_is_refused(tmp_path):
    path = _fake_benchmark(tmp_path, HELP_PLAIN)
    with pytest.raises(ValueError, match="built without TLS"):
        check_benchmark_tls([_cfg(False), _cfg(True)], path)


def test_mixed_scenario_counts_as_valkey_benchmark_use(tmp_path):
    path = _fake_benchmark(tmp_path, HELP_PLAIN)
    with pytest.raises(ValueError):
        check_benchmark_tls([_cfg(True, mixed=True)], path)


@pytest.mark.parametrize(
    "cfgs",
    [
        [_cfg(False)],
        [_cfg(True, generator="cachecannon")],
    ],
    ids=["no-tls", "tls-cachecannon-only"],
)
def test_allowed_without_tls_build(tmp_path, cfgs):
    check_benchmark_tls(cfgs, _fake_benchmark(tmp_path, HELP_PLAIN))


def test_binary_that_cannot_run_is_reported_as_such(tmp_path):
    path = _fake_benchmark(tmp_path, "error while loading shared libraries\n")
    with pytest.raises(ValueError, match="cannot run valkey-benchmark"):
        check_benchmark_tls([_cfg(True)], path)


def test_tls_build_passes(tmp_path):
    check_benchmark_tls([_cfg(True)], _fake_benchmark(tmp_path, HELP_TLS))


def test_main_exits_before_building(tmp_path, monkeypatch, capsys):
    cfg_file = tmp_path / "c.json"
    cfg_file.write_text(
        '[{"tls_mode": true, "cluster_mode": false, "warmup": 0, '
        '"io-threads": [1], "requests": [10], "keyspacelen": [10], '
        '"data_sizes": [16], "pipelines": [1], "clients": [1], '
        '"commands": ["GET"]}]'
    )
    path = _fake_benchmark(tmp_path, HELP_PLAIN)
    built = []
    monkeypatch.setattr(benchmark, "ServerBuilder", lambda *a, **k: built.append(a))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark.py",
            "--config",
            str(cfg_file),
            "--commits",
            "unstable",
            "--valkey-benchmark-path",
            str(path),
            "--results-dir",
            str(tmp_path / "results"),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        benchmark.main()
    assert exc.value.code == 1
    assert "built without TLS" in capsys.readouterr().out
    assert built == []
