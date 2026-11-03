"""A run whose main thread shares its CPU with another process is invalid."""

import os
import subprocess
import sys

import pytest

from generators import CachecannonGenerator, GeneratorRequest
from measurement import DEFAULTS, ServerProbe, evaluate_validity, run_measured
from tests.integration.real_valkey import (
    CACHECANNON,
    free_port,
    make_checkout,
    requires_cachecannon,
    requires_valkey,
)
from valkey_server import ServerLauncher

pytestmark = [pytest.mark.integration, requires_valkey, requires_cachecannon]

CPUS = sorted(os.sched_getaffinity(0))


@pytest.fixture
def server(tmp_path):
    if len(CPUS) < 8:
        pytest.skip("needs 8 CPUs")
    checkout = make_checkout(tmp_path / "valkey")
    port = free_port()
    main_cpu = CPUS[-6]
    launcher = ServerLauncher(results_dir=str(tmp_path), valkey_path=str(checkout))
    launcher.launch(
        cluster_mode=False,
        tls_mode=False,
        config={"port": port, "thread_pinning": {"main": str(main_cpu)}},
    )
    yield port, main_cpu
    launcher.shutdown(tls_mode=False)


def _measure(tmp_path, port):
    import valkey

    req = GeneratorRequest(
        scenario={"test": "GET", "clients": 8, "pipeline": 8, "keyspacelen": 5000},
        host="127.0.0.1",
        port=port,
        workdir=tmp_path / "gen",
        duration_s=0,
        cpu_range=f"{CPUS[-3]}-{CPUS[-1]}",
        threads=2,
    )
    probe = ServerProbe(lambda: valkey.Valkey(port=port, decode_responses=True), True)
    opts = {**DEFAULTS, "window": 3, "settle": 1}
    out = run_measured(CachecannonGenerator(CACHECANNON), req, probe, opts)
    return out["extra"], evaluate_validity(out["extra"], opts, req.scenario)


def test_quiet_main_cpu_is_valid(tmp_path, server):
    port, _ = server
    extra, reasons = _measure(tmp_path, port)
    assert extra["main_runq_wait_pct"] < 5
    assert extra["steal_scope"] == "main cpus"
    assert not any("disturbed" in r for r in reasons)


def test_cpu_hog_on_main_cpu_is_flagged(tmp_path, server):
    port, main_cpu = server
    hog = subprocess.Popen(
        ["taskset", "-c", str(main_cpu), sys.executable, "-c", "while True: pass"]
    )
    try:
        extra, reasons = _measure(tmp_path, port)
    finally:
        hog.kill()
        hog.wait()
    assert extra["main_runq_wait_pct"] > 20
    assert extra["main_nvcsw_per_s"] > 10
    assert any("disturbed" in r for r in reasons)
