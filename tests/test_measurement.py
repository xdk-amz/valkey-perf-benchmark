"""Unit tests for measured-run validity, perf parsing, manifests and run order."""

import argparse
from unittest.mock import patch

import pytest

import benchmark
from measurement import (
    DEFAULTS,
    evaluate_validity,
    finalize_row,
    parse_perf_stat,
    validate_measurement,
    validate_thread_pinning,
)
from run_manifest import manifest_id
from utils.compare_benchmark_results import discover_config_keys, drop_invalid_rows


def _opts(**kw):
    return {**DEFAULTS, **kw}


class TestValidity:
    def test_clean_run_is_valid(self):
        row = {"connected_clients": 10, "expected_clients": 10, "main_busy_pct": 50}
        assert evaluate_validity(row, _opts(), {}) == []

    def test_saturation_gate_is_opt_in(self):
        row = {"main_busy_pct": 50.0}
        assert evaluate_validity(row, _opts(), {}) == []
        reasons = evaluate_validity(row, _opts(require_main_busy_pct=98), {})
        assert reasons == ["main 50.0% busy < 98%"]

    def test_rate_tolerance(self):
        sc = {"rate": 100000}
        assert evaluate_validity({"server_ops_s": 97000}, _opts(), sc) == []
        assert evaluate_validity({"server_ops_s": 90000}, _opts(), sc)

    def test_fixed_rate_skips_saturation_gate(self):
        sc = {"rate": 1000}
        row = {"server_ops_s": 1000, "main_busy_pct": 5}
        assert evaluate_validity(row, _opts(require_main_busy_pct=98), sc) == []

    def test_errors_clients_asserts_early_exit(self):
        row = {
            "generator_errors": 3,
            "connected_clients": 5,
            "expected_clients": 10,
            "server_asserts": 1,
            "generator_ended_early": True,
        }
        assert len(evaluate_validity(row, _opts(), {})) == 4

    def test_finalize_stamps_and_drops_internal_fields(self):
        row = finalize_row(
            {"rps": 1.0},
            {
                "expected_clients": 2,
                "connected_clients": 1,
                "generator_ended_early": False,
            },
            _opts(),
            {},
        )
        assert row["valid"] is False
        assert "expected_clients" not in row and "generator_ended_early" not in row


def test_parse_perf_stat_per_command():
    text = """
     1,000,000      cycles:u
     2,000,000      instructions:u
        10,000      L1-icache-load-misses:u
        20,000      L1-dcache-load-misses:u
         5,000      branch-misses:u
"""
    p = parse_perf_stat(text, 1000)
    assert p["cyc_per_cmd"] == 1000
    assert p["ipc"] == 2.0
    assert p["l1i_miss_per_cmd"] == 10


@pytest.mark.parametrize(
    "value",
    [
        {"window": 0},
        {"bogus": 1},
        {"require_main_busy_pct": 120},
        {"acl": {"user": "x"}},
    ],
)
def test_validate_measurement_rejects(value):
    with pytest.raises(ValueError):
        validate_measurement(value)


def test_validate_thread_pinning():
    validate_thread_pinning({"main": "1", "io_threads": "2-4"})
    with pytest.raises(ValueError):
        validate_thread_pinning({"io_threads": "2-4"})


class TestGeneratorScenarioValidation:
    def _cfg(self, scenario, **top):
        return {"test_groups": [{"scenarios": [scenario]}], **top}

    def test_rate_needs_capable_generator(self):
        with pytest.raises(ValueError, match="valkey-benchmark does not"):
            benchmark.validate_test_groups(self._cfg({"test": "GET", "rate": 10}))

    def test_cachecannon_mix_ok(self):
        benchmark.validate_test_groups(
            self._cfg({"mix": {"get": 80, "set": 20}, "generator": "cachecannon"})
        )

    def test_cachecannon_rejects_populate(self):
        with pytest.raises(ValueError, match="populate_with"):
            benchmark.validate_test_groups(
                self._cfg(
                    {"test": "GET", "populate_with": "SET"}, generator="cachecannon"
                )
            )


def test_manifest_id_ignores_argv_and_frequency():
    m = {"argv": ["a"], "host": {"cpu_freq_mhz_at_setup": 1, "kernel": "k"}, "x": 1}
    n = {"argv": ["b"], "host": {"cpu_freq_mhz_at_setup": 2, "kernel": "k"}, "x": 1}
    assert manifest_id(m) == manifest_id(n)
    assert manifest_id(m) != manifest_id({**m, "x": 2})


def test_interleaved_rotates_commit_order():
    calls = []
    args = argparse.Namespace(runs=4, use_running_server=True, valkey_path=None)
    with patch.object(
        benchmark,
        "run_benchmark_matrix",
        side_effect=lambda **kw: calls.append(
            (
                kw["run_state"]["run_offset"],
                kw["run_state"]["order_slot"],
                kw["commit_id"],
            )
        ),
    ):
        benchmark.run_interleaved({"tls_mode": False}, ["A", "B"], args)
    assert [c[2] for c in calls] == ["A", "B", "B", "A", "A", "B", "B", "A"]
    slots = {(c[2], c[1]) for c in calls}
    assert slots == {("A", 0), ("A", 1), ("B", 0), ("B", 1)}


def test_compare_ignores_measured_fields_and_drops_invalid():
    rows = [
        {
            "command": "GET",
            "clients": 1,
            "server_ops_s": 1.0,
            "run_index": 1,
            "valid": True,
        },
        {
            "command": "GET",
            "clients": 1,
            "server_ops_s": 2.0,
            "run_index": 2,
            "valid": False,
        },
    ]
    kept = drop_invalid_rows(rows)
    assert len(kept) == 1
    assert discover_config_keys(rows) == ["clients", "command"]
