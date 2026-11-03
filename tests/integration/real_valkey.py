"""Helpers for integration tests that start real Valkey servers.

These tests need built binaries, found through environment variables:

    VPB_TEST_VALKEY_DIR      a built Valkey checkout (src/valkey-server,
                             src/valkey-benchmark, src/valkey-cli)
    VPB_TEST_VALKEY_TLS_DIR  the same, built with BUILD_TLS=yes and with
                             certificates from utils/gen-test-certs.sh
    VPB_TEST_CACHECANNON     a cachecannon binary

Tests that need a binary skip when its variable is unset. Every server runs
from a private copy of the binaries on a free port, so the tests never touch
servers started by anything else on the host.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

VALKEY_DIR = os.environ.get("VPB_TEST_VALKEY_DIR")
VALKEY_TLS_DIR = os.environ.get("VPB_TEST_VALKEY_TLS_DIR")
CACHECANNON = os.environ.get("VPB_TEST_CACHECANNON")

requires_valkey = pytest.mark.skipif(
    not VALKEY_DIR, reason="set VPB_TEST_VALKEY_DIR to a built Valkey checkout"
)
requires_valkey_tls = pytest.mark.skipif(
    not VALKEY_TLS_DIR,
    reason="set VPB_TEST_VALKEY_TLS_DIR to a Valkey checkout built with TLS",
)
requires_cachecannon = pytest.mark.skipif(
    not CACHECANNON, reason="set VPB_TEST_CACHECANNON to a cachecannon binary"
)

BINARIES = ("valkey-server", "valkey-benchmark", "valkey-cli")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_checkout(dest: Path, source: Optional[str] = None) -> Path:
    """Copy built binaries into a small git checkout at ``dest``.

    The copy gives every test its own server path, so the framework's
    process matching only ever reaches servers the test started.
    """
    source = Path(source or VALKEY_DIR)
    (dest / "src").mkdir(parents=True)
    for name in BINARIES:
        if (source / "src" / name).exists():
            shutil.copy2(source / "src" / name, dest / "src" / name)
    if (source / "tests" / "tls").is_dir():
        shutil.copytree(source / "tests" / "tls", dest / "tests" / "tls")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
    (dest / "README").write_text("binaries copied for an integration test\n")
    subprocess.run([*git, "add", "README"], cwd=dest, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], cwd=dest, check=True)
    return dest


def start_plain_server(checkout: Path, port: int) -> subprocess.Popen:
    """Start a server from ``checkout`` outside the framework."""
    proc = subprocess.Popen(
        [
            str((checkout / "src" / "valkey-server").resolve()),
            "--port",
            str(port),
            "--save",
            "",
            "--appendonly",
            "no",
        ],
        cwd=checkout,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    wait_for_ping(port)
    return proc


def ping(port: int, tls_dir: Optional[Path] = None) -> bool:
    import valkey

    kwargs = {}
    if tls_dir:
        kwargs = dict(
            ssl=True,
            ssl_certfile=str(tls_dir / "valkey.crt"),
            ssl_keyfile=str(tls_dir / "valkey.key"),
            ssl_ca_certs=str(tls_dir / "ca.crt"),
        )
    try:
        return bool(valkey.Valkey(port=port, socket_connect_timeout=1, **kwargs).ping())
    except Exception:
        return False


def wait_for_ping(port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ping(port):
            return
        time.sleep(0.1)
    raise RuntimeError(f"server on {port} did not answer PING")


def stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def run_benchmark(
    tmp_path: Path,
    configs: List[dict],
    checkout: Path,
    *extra: str,
    timeout: int = 600,
) -> subprocess.CompletedProcess:
    """Run benchmark.py on ``configs`` against ``checkout`` (no build)."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(configs, indent=2))
    argv = [
        sys.executable,
        "benchmark.py",
        "--config",
        str(config_path),
        "--valkey-path",
        str(checkout),
        "--valkey-benchmark-path",
        str(checkout / "src" / "valkey-benchmark"),
        "--results-dir",
        str(tmp_path / "results"),
        *extra,
    ]
    result = subprocess.run(
        argv, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout[-4000:] + result.stderr[-4000:])
    return result


def read_rows(tmp_path: Path, commit: str = "HEAD") -> List[dict]:
    path = tmp_path / "results" / commit / "metrics.json"
    return json.loads(path.read_text()) if path.exists() else []
