"""Server lifecycle against real Valkey binaries: start, stop, port and paths."""

import pytest

from tests.integration.real_valkey import (
    free_port,
    make_checkout,
    ping,
    requires_valkey,
    start_plain_server,
    stop_server,
)
from valkey_build import ServerBuilder
from valkey_server import ServerLauncher

pytestmark = [pytest.mark.integration, requires_valkey]


@pytest.fixture
def mine(tmp_path):
    return make_checkout(tmp_path / "mine")


@pytest.fixture
def other_server(tmp_path):
    """A server from another checkout, as another user's server would be."""
    port = free_port()
    proc = start_plain_server(make_checkout(tmp_path / "other"), port)
    yield port, proc
    stop_server(proc)


def _launcher(tmp_path, checkout):
    (tmp_path / "logs").mkdir(exist_ok=True)
    return ServerLauncher(results_dir=str(tmp_path / "logs"), valkey_path=str(checkout))


def test_cleanup_leaves_other_checkouts_running(tmp_path, mine, other_server):
    other_port, other_proc = other_server
    port = free_port()
    proc = start_plain_server(mine, port)
    launcher = _launcher(tmp_path, mine)
    try:
        assert launcher._get_valkey_pids() == [str(proc.pid)]
        ServerBuilder(
            commit_id="HEAD", tls_mode=False, valkey_path=str(mine)
        ).terminate_valkey()
        launcher._wait_for_process_shutdown(timeout=10)
        assert proc.wait(timeout=10) is not None
        assert not ping(port)
        assert ping(other_port)
        assert other_proc.poll() is None
    finally:
        stop_server(proc)


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
