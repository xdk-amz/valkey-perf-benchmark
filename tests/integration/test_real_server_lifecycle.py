"""Server lifecycle against real Valkey binaries: start, stop, port and paths."""

import pytest

from tests.integration.real_valkey import (
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
