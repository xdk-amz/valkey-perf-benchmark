"""Thread pinning and the ACL user against a real server, both generators."""

import os

import pytest

from tests.integration.real_valkey import (
    CACHECANNON,
    free_port,
    make_checkout,
    read_rows,
    requires_cachecannon,
    requires_valkey,
    run_benchmark,
)
from measurement import io_thread_index, thread_names
from valkey_server import ServerLauncher

pytestmark = [pytest.mark.integration, requires_valkey]

CPUS = sorted(os.sched_getaffinity(0))
needs_cpus = pytest.mark.skipif(len(CPUS) < 8, reason="needs 8 CPUs")


def _pinning():
    main, io1, io2, other = CPUS[-8:-4]
    return {"main": str(main), "io_threads": f"{io1},{io2}", "other": str(other)}


@needs_cpus
def test_main_and_io_threads_get_their_own_cpu(tmp_path):
    checkout = make_checkout(tmp_path / "valkey")
    port = free_port()
    spec = _pinning()
    launcher = ServerLauncher(results_dir=str(tmp_path), valkey_path=str(checkout))
    launcher.launch(
        cluster_mode=False,
        tls_mode=False,
        io_threads=3,
        config={"port": port, "thread_pinning": spec},
    )
    try:
        pid = int(launcher._get_valkey_pids()[0])
        names = thread_names(pid)
        affinity = {tid: os.sched_getaffinity(tid) for tid in names}
        ios = sorted(
            (io_thread_index(n), tid)
            for tid, n in names.items()
            if io_thread_index(n) is not None
        )
        assert affinity[pid] == {int(spec["main"])}
        assert [affinity[tid] for _, tid in ios] == [
            {int(c)} for c in spec["io_threads"].split(",")
        ]
        others = [
            a
            for tid, a in affinity.items()
            if tid != pid and tid not in dict(ios).values()
        ]
        assert others and all(a == {int(spec["other"])} for a in others)
        assert launcher.thread_layout["main"] == spec["main"]
    finally:
        launcher.shutdown(tls_mode=False)


@needs_cpus
@requires_cachecannon
def test_measured_acl_runs_with_both_generators(tmp_path):
    checkout = make_checkout(tmp_path / "valkey")
    shape = {"keyspacelen": 10000, "data_size": 32, "pipeline": 8, "clients": 8}
    cfg = {
        "cluster_mode": False,
        "tls_mode": False,
        "port": free_port(),
        "keyspacelen": [10000],
        "io-threads": 3,
        "server_cpu_range": f"{CPUS[-8]}-{CPUS[-5]}",
        "client_cpu_range": f"{CPUS[-4]}-{CPUS[-1]}",
        "thread_pinning": _pinning(),
        "measurement": {
            "window": 2,
            "settle": 1,
            "max_main_runq_wait_pct": None,
            "max_steal_pct": None,
            "acl": {"user": "bench", "password": "bench-pw"},
        },
        "generators": {"cachecannon": {"path": CACHECANNON, "threads": 2}},
        "test_groups": [
            {
                "group": 1,
                "scenarios": [
                    {
                        "id": "acl-vb",
                        "type": "read",
                        "test": "GET",
                        "populate_with": "SET",
                        **shape,
                    }
                ],
            },
            {
                "group": 2,
                "scenarios": [
                    {
                        "id": "acl-cc",
                        "type": "read",
                        "generator": "cachecannon",
                        "test": "GET",
                        **shape,
                    }
                ],
            },
        ],
    }
    result = run_benchmark(tmp_path, [cfg], checkout)
    assert result.returncode == 0
    rows = {r["scenario"]: r for r in read_rows(tmp_path)}
    assert set(rows) == {"acl-vb", "acl-cc"}
    for row in rows.values():
        assert row["valid"] is True, row["invalid_reasons"]
        assert row["acl_user"] == "bench"
        assert row["connected_clients"] == 8
        assert f"main={_pinning()['main']}" in row["thread_pinning"]
