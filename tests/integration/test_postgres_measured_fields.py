"""Measured-run fields pushed to a real PostgreSQL instance (see
test_postgres_push_metrics.py for the setup). Each run writes a fresh table
in the test database."""

import time

import pytest

from .conftest import requires_postgres
from utils.push_to_postgres import push_to_postgres


def _row(**kw):
    return {
        "timestamp": "2026-11-03T00:00:00Z",
        "commit": "abc123",
        "command": "GET",
        "data_size": 64,
        "pipeline": 16,
        "clients": 50,
        "cluster_mode": False,
        "tls": False,
        "io_threads": 3,
        "architecture": "x86_64",
        "rps": 1000.0,
        "avg_latency_ms": None,
        "server_ops_s": 1000,
        "main_busy_pct": 99,
        "generator_errors": 0,
        "p999_latency_ms": 1,
        "valid": True,
        "invalid_reasons": "",
        "generator_argv": "/bin/cc x.toml",
        "thread_pinning": "main=2",
        **kw,
    }


def _types(conn, table):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = %s",
            (table,),
        )
        return dict(cur.fetchall())


@requires_postgres
def test_later_rows_fit_columns_typed_from_the_first(pg_conn):
    table = f"test_measured_{time.time_ns()}"
    rows = [
        _row(),
        _row(
            server_ops_s=1234.5,
            main_busy_pct=97.25,
            p999_latency_ms=1.75,
            avg_latency_ms=0.5,
            valid=False,
            invalid_reasons="main thread waited for a CPU 12.3% of the window "
            "(> 5.0%): disturbed; generator errors: 4; clients 1 < 8 " * 3,
            generator_argv="taskset -c 8-15 /opt/valkey/src/valkey-benchmark " * 8,
            thread_pinning="main=2,io_thd_1=3,io_thd_2=4,bio_close_file=1," * 3,
        ),
    ]
    assert push_to_postgres(rows, pg_conn, table) == 2
    types = _types(pg_conn, table)
    for col in ("server_ops_s", "main_busy_pct", "p999_latency_ms", "avg_latency_ms"):
        assert types[col] in ("numeric", "double precision"), (col, types[col])
    assert types["valid"] == "boolean"
    assert types["invalid_reasons"] == "text"
    with pg_conn.cursor() as cur:
        cur.execute(f"SELECT server_ops_s, main_busy_pct FROM {table} WHERE NOT valid")
        ops, busy = cur.fetchone()
    assert float(ops) == 1234.5 and float(busy) == 97.25
