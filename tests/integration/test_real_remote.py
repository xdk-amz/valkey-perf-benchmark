"""Server and generators on two other hosts, driven over ssh.

Needs, besides the binaries of real_valkey.py, two hosts reachable over ssh
that can run those binaries and Python 3:

    VPB_TEST_SSH_SERVER     user@host for the server host
    VPB_TEST_SSH_SERVER_IP  the server host's address as the generator host
                            reaches it
    VPB_TEST_SSH_GENERATOR  user@host for the generator host
    VPB_TEST_SSH_KEY        identity file for both

The binary paths in VPB_TEST_* must exist on both hosts (a shared mount).
The server host gets the build already in place, so no compiler is needed.
"""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from remote import SshHost, build_dir
from tests.integration.real_valkey import (
    CACHECANNON,
    PROJECT_ROOT,
    VALKEY_DIR,
    VALKEY_TLS_DIR,
    make_checkout,
    requires_cachecannon,
    requires_valkey,
    requires_valkey_tls,
)

SERVER = os.environ.get("VPB_TEST_SSH_SERVER")
SERVER_IP = os.environ.get("VPB_TEST_SSH_SERVER_IP")
GENERATOR = os.environ.get("VPB_TEST_SSH_GENERATOR")
KEY = os.environ.get("VPB_TEST_SSH_KEY")

pytestmark = [
    pytest.mark.integration,
    requires_valkey,
    requires_cachecannon,
    pytest.mark.skipif(
        not (SERVER and SERVER_IP and GENERATOR),
        reason="set VPB_TEST_SSH_SERVER, VPB_TEST_SSH_SERVER_IP and VPB_TEST_SSH_GENERATOR",
    ),
]

PORT = 16379
# The interrupt check is off: these hosts share the test machine's interrupts.
MEASUREMENT = {
    "window": 3,
    "settle": 1,
    "max_main_runq_wait_pct": 5.0,
    "max_main_irq_pct": None,
}


@pytest.fixture
def root():
    return f"~/vpb-test-{uuid.uuid4().hex[:8]}"


def _host(target, root, private_ip=None):
    return SshHost(target, private_ip=private_ip, key=KEY, root=root)


def _seed_build(checkout: Path, root: str, tls: bool) -> str:
    """Put the checkout's binaries where remote_build would leave them."""
    host = _host(SERVER, root, SERVER_IP)
    sha = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    dst = build_dir(host, sha, tls=tls)
    host.put_tree(checkout, dst)
    return dst


def _config(tls=False, scenarios=None, **extra):
    cfg = {
        "seed": False,
        "port": PORT,
        "tls_mode": tls,
        "cluster_mode": False,
        "io-threads": 2,
        "generators": {"cachecannon": {"threads": 2, "warmup": 1}},
        "thread_pinning": {"main": "2", "io_threads": "3", "other": "4"},
        "measurement": dict(MEASUREMENT),
        "test_groups": [
            {
                "group": 1,
                "scenarios": scenarios
                or [
                    {
                        "id": "get_vb",
                        "type": "read",
                        "test": "GET",
                        "clients": 10,
                        "pipeline": 4,
                        "keyspacelen": 1000,
                        "data_size": 16,
                    },
                    {
                        "id": "get_cc",
                        "type": "read",
                        "test": "GET",
                        "generator": "cachecannon",
                        "clients": 10,
                        "pipeline": 4,
                        "keyspacelen": 1000,
                        "data_size": 16,
                    },
                ],
            }
        ],
    }
    cfg.update(extra)
    return cfg


def _run(tmp_path, cfg, checkout, root, benchmark):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps([cfg], indent=2))
    argv = [
        sys.executable,
        "benchmark.py",
        "--config",
        str(config_path),
        "--valkey-path",
        str(checkout),
        "--valkey-benchmark-path",
        benchmark,
        "--cachecannon-path",
        CACHECANNON,
        "--results-dir",
        str(tmp_path / "results"),
        "--server-host",
        SERVER,
        "--target-ip",
        SERVER_IP,
        "--generator-host",
        GENERATOR,
        "--ssh-key",
        KEY,
        "--remote-root",
        root,
    ]
    r = subprocess.run(
        argv, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=900
    )
    if r.returncode != 0:
        sys.stderr.write(r.stdout[-6000:] + r.stderr[-4000:])
    return r


def _rows(tmp_path):
    rows = []
    for f in (tmp_path / "results").rglob("metrics.json"):
        rows += json.loads(f.read_text())
    return {r["scenario"]: r for r in rows}


def test_measured_rows_read_on_the_remote_hosts(tmp_path, root):
    checkout = make_checkout(tmp_path / "valkey")
    server_dir = _seed_build(checkout, root, tls=False)
    r = _run(tmp_path, _config(), checkout, root, f"{VALKEY_DIR}/src/valkey-benchmark")
    assert r.returncode == 0
    rows = _rows(tmp_path)
    assert set(rows) == {"get_vb", "get_cc"}
    for name, row in rows.items():
        assert row["valid"] is True, (name, row["invalid_reasons"])
        assert row["server_ops_s"] > 0
        assert row["server_ops_s"] == pytest.approx(row["rps"], rel=0.15)
        # Read from /proc on the server host: this machine runs no server.
        assert row["main_cpu_pct"] > 0
        assert row["main_runq_wait_pct"] is not None
        assert row["steal_pct"] is not None
        assert row["steal_scope"] == "main cpus"
        assert row["io_threads_cores"] is not None
        # Read from /proc on the generator host.
        assert row["generator_cores"] > 0
        assert row["thread_pinning"].startswith("main=2")
    # The generator ran on the generator host, from the remote root.
    assert rows["get_cc"]["generator_argv"].startswith(CACHECANNON)
    assert "/stage/" in rows["get_cc"]["generator_argv"]

    manifest = next((tmp_path / "results").rglob("manifests/*.json"))
    doc = json.loads(manifest.read_text())
    assert doc["server_binary"]["path"] == f"{server_dir}/src/valkey-server"
    assert doc["server_binary"]["sha256"]
    assert doc["server_host"]["cpus"] and doc["generator_host"]["kernel"]
    assert list((tmp_path / "results").rglob("server_logs/*.log"))

    server = _host(SERVER, root, SERVER_IP)
    assert server.agent("pids", binary=f"{server_dir}/src/valkey-server")["pids"] == []


def test_disturbed_server_host_is_flagged(tmp_path, root):
    """A CPU hog on the server host, on main's CPU, makes the run invalid."""
    checkout = make_checkout(tmp_path / "valkey")
    _seed_build(checkout, root, tls=False)
    server = _host(SERVER, root, SERVER_IP)
    hog = server.agent(
        "spawn",
        argv=["taskset", "-c", "2", "python3", "-c", "while True: pass"],
        out=f"{server.root}/hog.out",
        err=f"{server.root}/hog.err",
    )["pid"]
    try:
        cfg = _config()
        cfg["test_groups"][0]["scenarios"] = cfg["test_groups"][0]["scenarios"][1:]
        r = _run(tmp_path, cfg, checkout, root, f"{VALKEY_DIR}/src/valkey-benchmark")
    finally:
        server.agent("signal", pid=hog, sig=9)
    assert r.returncode == 0
    row = _rows(tmp_path)["get_cc"]
    assert row["valid"] is False
    assert "disturbed" in row["invalid_reasons"]
    assert row["main_runq_wait_pct"] > 5


@requires_valkey_tls
def test_tls_and_acl_on_remote_hosts(tmp_path, root):
    checkout = make_checkout(tmp_path / "valkey", source=VALKEY_TLS_DIR)
    _seed_build(checkout, root, tls=True)
    cfg = _config(tls=True)
    cfg["measurement"]["acl"] = {"user": "bench", "password": "benchpw"}
    r = _run(tmp_path, cfg, checkout, root, f"{VALKEY_TLS_DIR}/src/valkey-benchmark")
    assert r.returncode == 0
    rows = _rows(tmp_path)
    for name, row in rows.items():
        assert row["valid"] is True, (name, row["invalid_reasons"])
        assert row["acl_user"] == "bench"
        assert row["main_cpu_pct"] > 0


def test_cluster_on_remote_hosts(tmp_path, root):
    checkout = make_checkout(tmp_path / "valkey")
    _seed_build(checkout, root, tls=False)
    ports = [PORT, PORT + 1]
    cfg = _config(
        cluster_mode=True,
        cluster_nodes=2,
        cluster_ports=ports,
        # Main and its IO thread on CPUs of their own.
        cpu_allocation={"cores_per_server": 2, "cores_per_client": 2},
    )
    del cfg["thread_pinning"]
    r = _run(tmp_path, cfg, checkout, root, f"{VALKEY_DIR}/src/valkey-benchmark")
    assert r.returncode == 0
    rows = _rows(tmp_path)
    for name, row in rows.items():
        assert row["valid"] is True, (name, row["invalid_reasons"])
        assert row["server_nodes"] == 2
        assert row["main_cpu_pct"] > 0
