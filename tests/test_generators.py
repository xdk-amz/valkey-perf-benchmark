"""Unit tests for the load generator backends."""

import json
from pathlib import Path

import pytest

from generators import (
    CachecannonGenerator,
    GeneratorRequest,
    RunHandle,
    ValkeyBenchmarkGenerator,
)
from generators.cachecannon import summarize, window_samples, read_samples


def _req(tmp_path, scenario, **kw):
    return GeneratorRequest(
        scenario=scenario,
        host="127.0.0.1",
        port=16379,
        workdir=tmp_path,
        duration_s=20,
        **kw,
    )


def _sample(t, req_s, p50=100, p99=300, mx=900, err=0.0):
    from datetime import datetime, timezone

    ts = datetime.fromtimestamp(t, timezone.utc).isoformat()
    return json.dumps(
        {
            "type": "sample",
            "ts": ts,
            "req_s": req_s,
            "err_s": err,
            "hit_pct": 100.0,
            "p50_us": p50,
            "p90_us": p50,
            "p99_us": p99,
            "p999_us": p99,
            "p9999_us": p99,
            "max_us": mx,
        }
    )


class TestCachecannonToml:
    def test_get_scenario_maps_fields(self, tmp_path):
        gen = CachecannonGenerator("/bin/cc", {"threads": 4})
        sc = {
            "test": "GET",
            "clients": 32,
            "pipeline": 8,
            "keyspacelen": 5000,
            "data_size": 64,
        }
        toml = gen.toml(_req(tmp_path, sc, cpu_range="4-7"))
        assert "connections = 32" in toml
        assert "pipeline_depth = 8" in toml
        assert "count = 5000" in toml
        assert "length = 64" in toml
        assert "get = 100\nset = 0" in toml
        assert "prefill = true" in toml
        assert "threads = 4" in toml
        assert 'cpu_list = "4-7"' in toml
        assert "rate_limit" not in toml

    def test_mix_rate_and_auth(self, tmp_path):
        gen = CachecannonGenerator("/bin/cc")
        sc = {"mix": {"get": 80, "set": 20}, "rate": 50000, "clients": 4}
        toml = gen.toml(_req(tmp_path, sc, auth_user="bench", auth_password="pw"))
        assert "get = 80\nset = 20" in toml
        assert "rate_limit = 50000" in toml
        assert 'auth_user = "bench"' in toml
        assert 'auth_password = "pw"' in toml
        assert gen.label(sc) == "GET:SET 80:20"

    def test_set_only_skips_prefill(self, tmp_path):
        gen = CachecannonGenerator("/bin/cc")
        assert "prefill = false" in gen.toml(_req(tmp_path, {"test": "SET"}))
        assert not gen.prefills({"test": "SET"})

    @pytest.mark.parametrize(
        "scenario",
        [{"command": "PING"}, {"test": "LPUSH"}, {"type": "mixed", "test": "GET"}],
    )
    def test_unsupported(self, scenario):
        assert CachecannonGenerator("/bin/cc").unsupported(scenario)


class TestCachecannonParse:
    def test_window_keeps_only_whole_seconds_inside(self):
        samples = read_samples("\n".join(_sample(t, 1000) for t in range(100, 110)))
        picked = window_samples(samples, (102.5, 106.5))
        assert [round(s["t"]) for s in picked] == [104, 105, 106]

    def test_summary_median_percentiles_max_of_max(self):
        samples = read_samples(
            "\n".join(
                [
                    _sample(1, 1000, p50=100, p99=200, mx=500),
                    _sample(2, 2000, p50=300, p99=400, mx=900),
                    _sample(3, 3000, p50=200, p99=300, mx=700),
                ]
            )
        )
        row = summarize(samples)
        assert row["rps"] == 2000
        assert row["p50_latency_ms"] == 0.2
        assert row["p99_latency_ms"] == 0.3
        assert row["max_latency_ms"] == 0.9

    def test_parse_counts_window_errors(self, tmp_path):
        out = tmp_path / "cc.out"
        out.write_text(
            "\n".join(
                [
                    _sample(t, 1000, err=2.0 if t == 104 else 0.0)
                    for t in range(100, 110)
                ]
                + ['{"type":"result","timeouts":0,"conns_failed":0}']
            )
        )
        handle = RunHandle(None, ["/bin/cc", "x.toml"], out, out, 0.0, "GET")
        result = CachecannonGenerator("/bin/cc").parse(handle, (102.5, 106.5))
        assert result.row["test"] == "GET"
        assert result.extra["generator_errors"] == 2
        assert result.extra["generator_timeouts"] == 0
        assert result.extra["generator_latency_scope"] == "window"


class TestValkeyBenchmarkBackend:
    def _builder(self, scenario, cpu_range=None, seed_val=None, port=None):
        argv = ["/bin/vb", "-p", str(port), "--duration", str(scenario["duration"])]
        return argv + ["-t", scenario["test"], "--csv"]

    def test_argv_uses_window_duration_and_auth(self, tmp_path):
        gen = ValkeyBenchmarkGenerator("/bin/vb", command_builder=self._builder)
        sc = {"test": "GET", "requests": 100, "warmup_inline": 5}
        argv = gen.build_argv(_req(tmp_path, sc, auth_user="u", auth_password="p"))
        assert argv[:5] == ["/bin/vb", "--user", "u", "-a", "p"]
        assert "--duration" in argv and argv[argv.index("--duration") + 1] == "20"

    def test_rejects_rate_and_mix(self):
        gen = ValkeyBenchmarkGenerator("/bin/vb", command_builder=self._builder)
        assert gen.unsupported({"test": "GET", "rate": 10})
        assert gen.unsupported({"mix": {"get": 1}})

    def test_parse_csv(self, tmp_path):
        out, err = tmp_path / "o", tmp_path / "e"
        out.write_text(
            '"test","rps","avg_latency_ms","min_latency_ms","p50_latency_ms",'
            '"p95_latency_ms","p99_latency_ms","max_latency_ms"\n'
            '"GET","12345.00","0.1","0.01","0.1","0.2","0.3","1.0"\n'
        )
        err.write_text("")
        gen = ValkeyBenchmarkGenerator("/bin/vb", command_builder=self._builder)
        handle = RunHandle(None, ["/bin/vb", "-t", "GET"], out, err, 0.0, "GET")
        result = gen.parse(handle, (0, 1))
        assert result.row["rps"] == "12345.00"
        assert result.extra["generator_errors"] == 0
