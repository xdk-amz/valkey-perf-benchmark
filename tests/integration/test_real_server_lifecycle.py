"""Server lifecycle against real Valkey binaries: start, stop, port and paths."""

import pytest

from tests.integration.real_valkey import (
    free_port,
    make_checkout,
    ping,
    read_rows,
    requires_valkey,
    run_benchmark,
)
from valkey_server import ServerLauncher

pytestmark = [pytest.mark.integration, requires_valkey]


@pytest.fixture
def mine(tmp_path):
    return make_checkout(tmp_path / "mine")


def _launcher(tmp_path, checkout):
    (tmp_path / "logs").mkdir(exist_ok=True)
    return ServerLauncher(results_dir=str(tmp_path / "logs"), valkey_path=str(checkout))


def test_launch_and_shutdown(tmp_path, mine, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    launcher = ServerLauncher(results_dir="logs", valkey_path=str(mine))
    launcher.launch(cluster_mode=False, tls_mode=False)
    try:
        assert ping(6379)
    finally:
        launcher.shutdown(tls_mode=False)
    assert not ping(6379)
    assert not launcher._valkey_processes_running()


def test_server_starts_on_configured_port_and_stops(tmp_path, mine, monkeypatch):
    monkeypatch.chdir(tmp_path)
    port = free_port()
    launcher = ServerLauncher(results_dir="logs", valkey_path=str(mine))
    (tmp_path / "logs").mkdir()
    launcher.launch(cluster_mode=False, tls_mode=False, config={"port": port})
    try:
        assert ping(port)
        assert launcher._get_valkey_pids()
    finally:
        launcher.shutdown(tls_mode=False)
    assert not ping(port)
    assert not launcher._get_valkey_pids()


def test_full_run_with_absolute_results_dir_and_port(tmp_path, mine):
    port = free_port()
    cfg = {
        "requests": [20000],
        "keyspacelen": [1000],
        "data_sizes": [16],
        "pipelines": [1],
        "clients": [4],
        "commands": ["SET", "GET"],
        "cluster_mode": False,
        "tls_mode": False,
        "warmup": 0,
        "port": port,
    }
    result = run_benchmark(tmp_path, [cfg], mine)
    assert result.returncode == 0
    rows = read_rows(tmp_path)
    assert {r["command"] for r in rows} == {"SET", "GET"}
    assert all(r["rps"] > 0 for r in rows)
    logs = list((tmp_path / "results" / "HEAD").glob("valkey_log_*.log"))
    assert logs and "Ready to accept connections" in logs[0].read_text()
