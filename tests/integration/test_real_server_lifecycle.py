"""Server lifecycle against real Valkey binaries: start, stop, port and paths."""

import pytest

from tests.integration.real_valkey import (
    free_port,
    make_checkout,
    ping,
    requires_valkey,
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
