"""Tests for ServerLauncher shutdown logic.

Unit tests for helpers + a real-process test that validates SIGKILL escalation
by spawning a process that ignores SIGTERM (simulating a stuck valkey-server).
"""

import os
import shutil
import signal
import subprocess
import time
from unittest.mock import MagicMock, patch

import pytest

from valkey_server import ServerLauncher


@pytest.fixture
def launcher():
    """Create a minimal ServerLauncher instance."""
    sl = ServerLauncher(
        results_dir="/tmp/test_results",
        valkey_path="/tmp/valkey",
    )
    sl.config = None
    return sl


# ---------------------------------------------------------------------------
# Unit tests for simple helpers (no time mocking needed)
# ---------------------------------------------------------------------------


class TestValkeyProcessesRunning:
    """Test _valkey_processes_running helper."""

    @patch("valkey_server.subprocess.run")
    def test_returns_true_when_process_found(self, mock_run, launcher):
        mock_run.return_value = MagicMock(returncode=0, stdout="12345\n")
        assert launcher._valkey_processes_running() is True

    @patch("valkey_server.subprocess.run")
    def test_returns_false_when_no_process(self, mock_run, launcher):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert launcher._valkey_processes_running() is False

    @patch("valkey_server.subprocess.run")
    def test_returns_false_on_exception(self, mock_run, launcher):
        mock_run.side_effect = Exception("pgrep not found")
        assert launcher._valkey_processes_running() is False


class TestGetValkeyPids:
    """Test _get_valkey_pids helper."""

    @patch("valkey_server.subprocess.run")
    def test_returns_pids(self, mock_run, launcher):
        mock_run.return_value = MagicMock(returncode=0, stdout="12345\n67890\n")
        assert launcher._get_valkey_pids() == ["12345", "67890"]

    @patch("valkey_server.subprocess.run")
    def test_returns_empty_when_no_process(self, mock_run, launcher):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert launcher._get_valkey_pids() == []


class TestWaitForPortAvailable:
    """Test _wait_for_port_available helper."""

    @patch("valkey_server.subprocess.run")
    def test_returns_immediately_when_port_free(self, mock_run, launcher):
        """Port is free (no output lines) — should return quickly."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="State  Recv-Q Send-Q Local Address:Port\n"
        )
        start = time.time()
        launcher._wait_for_port_available(port=6379, timeout=10)
        elapsed = time.time() - start
        assert (
            elapsed < 2.0
        ), f"Should return immediately when port free, took {elapsed:.1f}s"

    @patch("valkey_server.subprocess.run")
    def test_waits_until_port_free(self, mock_run, launcher):
        """Port is busy first, then becomes free after 2 calls."""
        busy_result = MagicMock(
            returncode=0,
            stdout="State  Recv-Q Send-Q Local Address:Port\nLISTEN 0      511    *:6379\n",
        )
        free_result = MagicMock(
            returncode=0, stdout="State  Recv-Q Send-Q Local Address:Port\n"
        )
        mock_run.side_effect = [busy_result, busy_result, free_result]

        start = time.time()
        launcher._wait_for_port_available(port=6379, timeout=10)
        elapsed = time.time() - start
        # Should take ~2s (2 sleep(1) iterations before free)
        assert 1.5 < elapsed < 4.0, f"Expected ~2s wait, got {elapsed:.1f}s"

    @patch("valkey_server.subprocess.run")
    def test_proceeds_after_timeout(self, mock_run, launcher):
        """Port never frees — should warn and proceed after timeout."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="State  Recv-Q Send-Q Local Address:Port\nLISTEN 0      511    *:6379\n",
        )
        start = time.time()
        # Use short timeout so test doesn't take long
        launcher._wait_for_port_available(port=6379, timeout=3)
        elapsed = time.time() - start
        assert 2.5 < elapsed < 5.0, f"Should wait ~3s then proceed, took {elapsed:.1f}s"

    @patch("valkey_server.subprocess.run")
    def test_handles_ss_failure(self, mock_run, launcher):
        """If ss command fails, should still proceed after timeout."""
        mock_run.side_effect = Exception("ss not found")
        start = time.time()
        launcher._wait_for_port_available(port=6379, timeout=3)
        elapsed = time.time() - start
        assert 2.5 < elapsed < 5.0, f"Should wait ~3s then proceed, took {elapsed:.1f}s"


# ---------------------------------------------------------------------------
# Real-process test: validates shutdown + SIGKILL escalation
# ---------------------------------------------------------------------------


def _spawn_stub(checkout):
    """Start a SIGTERM-ignoring process whose argv[0] is
    <checkout>/src/valkey-server (a copy of bash), like a stuck server."""
    server = checkout / "src" / "valkey-server"
    server.parent.mkdir(parents=True)
    shutil.copy(shutil.which("bash"), server)
    proc = subprocess.Popen(
        [str(server.resolve()), "-c", 'trap "" TERM; while :; do sleep 0.1; done'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.3)
    return proc


def _reap(proc):
    try:
        proc.kill()
        proc.wait(timeout=2)
    except Exception:
        pass


@pytest.mark.slow
class TestShutdownWithRealProcess:
    """Spawn real processes that ignore SIGTERM and check SIGKILL escalation.

    Process matching is scoped to the launcher's own checkout, so these run
    safely on hosts where other Valkey servers are running.
    """

    @pytest.fixture
    def checkout_launcher(self, tmp_path):
        sl = ServerLauncher(
            results_dir=str(tmp_path / "results"),
            valkey_path=str(tmp_path / "mine"),
        )
        sl.config = None
        return sl

    def test_sigkill_escalation_kills_stubborn_process(
        self, checkout_launcher, tmp_path
    ):
        proc = _spawn_stub(tmp_path / "mine")
        try:
            assert proc.poll() is None
            assert str(proc.pid) in checkout_launcher._get_valkey_pids()
            checkout_launcher._wait_for_process_shutdown(timeout=2)
            assert proc.wait(timeout=10) == -signal.SIGKILL
        finally:
            _reap(proc)

    def test_server_from_another_checkout_is_left_running(
        self, checkout_launcher, tmp_path
    ):
        mine = _spawn_stub(tmp_path / "mine")
        other = _spawn_stub(tmp_path / "other")
        try:
            assert str(other.pid) not in checkout_launcher._get_valkey_pids()
            checkout_launcher._wait_for_process_shutdown(timeout=2)
            assert mine.wait(timeout=10) == -signal.SIGKILL
            assert other.poll() is None
        finally:
            _reap(mine)
            _reap(other)

    def test_graceful_shutdown_no_sigkill_needed(self, checkout_launcher):
        """When no process is running, _wait_for_process_shutdown returns quickly."""
        start = time.time()
        checkout_launcher._wait_for_process_shutdown(timeout=30)
        elapsed = time.time() - start
        assert elapsed < 2.0, f"Should return fast when no process, took {elapsed:.1f}s"
