"""Unit tests for measured runs: validity, perf parsing and the window protocol."""

import itertools

import pytest

from generators import CachecannonGenerator, GeneratorError, GeneratorRequest
from measurement import (
    DEFAULTS,
    ServerProbe,
    evaluate_validity,
    parse_perf_stat,
    run_measured,
    stamp_validity,
    validate_measurement,
)
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

    def test_stamp_drops_internal_fields(self):
        extra = stamp_validity(
            {"expected_clients": 2, "connected_clients": 1, "generator_ended_early": 0},
            _opts(),
            {},
        )
        assert extra["valid"] is False
        assert extra["invalid_reasons"] == "clients 1 < 2"
        assert "expected_clients" not in extra and "generator_ended_early" not in extra


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
    [{"window": 0}, {"bogus": 1}, {"require_main_busy_pct": 120}, "yes"],
)
def test_validate_measurement_rejects(value):
    with pytest.raises(ValueError):
        validate_measurement(value)


def test_compare_ignores_measured_fields_and_drops_invalid():
    rows = [
        {"command": "GET", "clients": 1, "server_ops_s": 1.0, "run_index": 1},
        {"command": "GET", "clients": 1, "server_ops_s": 2.0, "valid": False},
        {"command": "GET", "clients": 1, "server_ops_s": 3.0, "valid": True},
    ]
    assert [r["server_ops_s"] for r in drop_invalid_rows(rows)] == [1.0, 3.0]
    assert discover_config_keys(rows) == ["clients", "command"]


# ---------- window protocol with a fake server and generator -----------------


class _FakeInfoClient:
    """INFO that counts 1000 commands per call, as a busy server would."""

    def __init__(self, clients=4):
        self.calls = itertools.count()
        self.clients = clients

    def info(self, section):
        n = next(self.calls)
        return {
            "total_commands_processed": 1000 * n,
            "used_active_time_main_thread": 0.5 * n,
            "connected_clients": self.clients + 1,
            "process_id": 0,
        }

    def config_get(self, key):
        return {}

    def close(self):
        pass


class _SleepGenerator(CachecannonGenerator):
    def __init__(self, seconds, samples=""):
        super().__init__("/bin/sh")
        self.script = f"sleep {seconds}; printf '%s' '{samples}'"

    def build_argv(self, req):
        return ["/bin/sh", "-c", self.script]

    def version(self):
        return "fake 1"

    def stop(self, handle, grace_s=3.0):
        if handle.alive():
            handle.proc.kill()
            handle.proc.wait()

    def parse(self, handle, window):
        from generators import GeneratorResult

        return GeneratorResult(
            row={"rps": 1.0, "test": "GET"}, extra={}, argv=handle.argv, stdout=""
        )


def _request(tmp_path):
    return GeneratorRequest(
        scenario={"test": "GET", "clients": 4},
        host="127.0.0.1",
        port=1,
        workdir=tmp_path,
        duration_s=0,
    )


def test_window_reads_server_counters(tmp_path):
    client = _FakeInfoClient()
    probe = ServerProbe(lambda: client, local=False)
    out = run_measured(
        _SleepGenerator(30), _request(tmp_path), probe, _opts(window=1, settle=1)
    )
    extra = out["extra"]
    assert extra["connected_clients"] == 4
    assert extra["server_ops_s"] > 0
    assert extra["main_busy_source"] == "used_active_time_main_thread"
    assert extra["generator_ended_early"] is False
    assert extra["generator_version"] == "fake 1"


def test_generator_that_dies_before_load_raises(tmp_path):
    probe = ServerProbe(lambda: _FakeInfoClient(), local=False)
    with pytest.raises(GeneratorError, match="exited before the load was up"):
        run_measured(
            _SleepGenerator(0), _request(tmp_path), probe, _opts(window=1, settle=1)
        )


def test_load_that_never_connects_times_out(tmp_path):
    probe = ServerProbe(lambda: _FakeInfoClient(clients=1), local=False)
    with pytest.raises(GeneratorError, match="load never came up: clients=1/4"):
        run_measured(
            _SleepGenerator(30),
            _request(tmp_path),
            probe,
            _opts(window=1, settle=1, ready_timeout=2),
        )


def test_cachecannon_needs_a_two_second_window():
    from benchmark import validate_test_groups

    cfg = {
        "measurement": {"window": 1},
        "test_groups": [{"scenarios": [{"generator": "cachecannon", "test": "GET"}]}],
    }
    with pytest.raises(ValueError, match="at least 2"):
        validate_test_groups(cfg)
    cfg["measurement"]["window"] = 2
    validate_test_groups(cfg)


class TestDisturbance:
    def test_runq_wait_over_limit_is_invalid(self):
        row = {"main_runq_wait_pct": 12.0}
        reasons = evaluate_validity(row, _opts(), {})
        assert reasons and "disturbed" in reasons[0]

    def test_limits_can_be_turned_off(self):
        row = {"main_runq_wait_pct": 50.0, "steal_pct": 9.0}
        opts = _opts(max_main_runq_wait_pct=None, max_steal_pct=None)
        assert evaluate_validity(row, opts, {}) == []

    def test_steal(self):
        assert evaluate_validity({"steal_pct": 3.0}, _opts(), {}) == [
            "CPU steal 3.0% > 2.0%"
        ]

    def test_validation(self):
        validate_measurement({"max_main_runq_wait_pct": None, "max_steal_pct": 1})
        with pytest.raises(ValueError):
            validate_measurement({"max_steal_pct": -1})


class TestBusyThresholdPerVersion:
    SETTING = {"default": 98, "255.": 90, "9.0": 97, "9.0.7": 99}

    @pytest.mark.parametrize(
        "version, want",
        [("255.255.255", 90), ("9.0.7", 99), ("9.0.3", 97), ("8.1.1", 98), (None, 98)],
    )
    def test_longest_prefix_wins(self, version, want):
        from measurement import busy_threshold

        assert busy_threshold(self.SETTING, version) == want

    def test_gate_uses_the_row_version(self):
        opts = _opts(require_main_busy_pct=self.SETTING)
        row = {"main_busy_pct": 94.0, "server_version": "255.255.255"}
        assert evaluate_validity(row, opts, {}) == []
        row["server_version"] = "9.0.7"
        assert evaluate_validity(row, opts, {}) == ["main 94.0% busy < 99%"]

    def test_validation(self):
        validate_measurement({"require_main_busy_pct": {"default": 98, "255.": 90}})
        for bad in ({}, {"default": 120}, {"9.0": True}):
            with pytest.raises(ValueError):
                validate_measurement({"require_main_busy_pct": bad})
