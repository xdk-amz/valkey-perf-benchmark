"""cachecannon backend (https://github.com/cachecannon/cachecannon).

cachecannon prints one JSON sample per second, so latency can be taken from
exactly the seconds inside the measured window and the process can be stopped
at the window edge. It also offers fixed-rate load (``rate``) and weighted
GET/SET mixes (``mix``), which valkey-benchmark does not.

RESP AUTH needs a cachecannon build with ``target.auth_user`` and
``target.auth_password``; stock builds reject those keys.
"""

import json
import re
import statistics
from datetime import datetime
from typing import Dict, List, Optional

from .base import GeneratorRequest, GeneratorResult, LoadGenerator, RunHandle

TESTS = {"GET": {"get": 100, "set": 0}, "SET": {"get": 0, "set": 100}}
PERCENTILES = {
    "p50_us": "p50_latency_ms",
    "p90_us": "p90_latency_ms",
    "p99_us": "p99_latency_ms",
    "p999_us": "p999_latency_ms",
    "p9999_us": "p9999_latency_ms",
}
KEY_LENGTH = 16


def _weights(scenario: dict) -> Optional[Dict[str, int]]:
    if scenario.get("mix"):
        mix = scenario["mix"]
        return {"get": int(mix.get("get", 0)), "set": int(mix.get("set", 0))}
    return TESTS.get(str(scenario.get("test", "")).upper())


def _toml_str(value: str) -> str:
    return json.dumps(value)


def _sample_time(ts: str) -> float:
    ts = ts.replace("Z", "+00:00")
    m = re.match(r"(.*\.\d{1,6})\d*([+-]\d\d:\d\d)$", ts)
    return datetime.fromisoformat(m.group(1) + m.group(2) if m else ts).timestamp()


def read_samples(text: str) -> List[dict]:
    out = []
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") == "sample":
            d["t"] = _sample_time(d["ts"])
            out.append(d)
    return out


def read_result(text: str) -> Optional[dict]:
    for line in reversed(text.splitlines()):
        if line.startswith("{") and '"result"' in line:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") == "result":
                return d
    return None


def window_samples(samples: List[dict], window: Optional[tuple]) -> List[dict]:
    """Samples whose whole second lies in the window; all but the first otherwise."""
    if window is None:
        return samples[1:] or samples
    w_a, w_b = window
    return [s for s in samples if w_a + 1.0 <= s["t"] <= w_b + 0.05]


def summarize(samples: List[dict]) -> Dict[str, object]:
    """Median of each percentile across seconds, max of maxima, mean rate."""
    row: Dict[str, object] = {
        ms: statistics.median(s[us] for s in samples) / 1000.0
        for us, ms in PERCENTILES.items()
    }
    row["max_latency_ms"] = max(s["max_us"] for s in samples) / 1000.0
    row["rps"] = statistics.fmean(s["req_s"] for s in samples)
    return row


class CachecannonGenerator(LoadGenerator):
    name = "cachecannon"
    stops_at_window_edge = True

    def unsupported(self, scenario: dict) -> Optional[str]:
        if "command" in scenario:
            return "arbitrary 'command' scenarios are valkey-benchmark only"
        if _weights(scenario) is None:
            return (
                f"test {scenario.get('test')!r} is not GET or SET and no 'mix' is set"
            )
        if scenario.get("type") == "mixed":
            return "'mixed' scenarios run separate valkey-benchmark processes"
        return None

    def toml(self, req: GeneratorRequest) -> str:
        sc = req.scenario
        w = _weights(sc)
        threads = req.threads or self.options.get("threads")
        prefill = self.options.get("prefill", w["get"] > 0)
        lines = [
            "[general]",
            f'duration = "{req.duration_s}s"',
            f'warmup = "{self.options.get("warmup", 1)}s"',
        ]
        if threads:
            lines.append(f"threads = {int(threads)}")
        if req.cpu_range:
            lines.append(f"cpu_list = {_toml_str(req.cpu_range)}")
        lines += [
            "[target]",
            f"endpoints = [{_toml_str(f'{req.host}:{req.port}')}]",
            'protocol = "resp"',
        ]
        if req.cluster:
            lines.append("cluster = true")
        if req.auth_password:
            if req.auth_user:
                lines.append(f"auth_user = {_toml_str(req.auth_user)}")
            lines.append(f"auth_password = {_toml_str(req.auth_password)}")
        lines += [
            "[connection]",
            f"connections = {int(sc.get('clients', 1))}",
            f"pipeline_depth = {int(sc.get('pipeline', 1))}",
            f'request_timeout = "{self.options.get("request_timeout", "1s")}"',
            "[workload]",
            f"prefill = {'true' if prefill else 'false'}",
            'prefill_settle = "0s"',
        ]
        if sc.get("rate"):
            lines.append(f"rate_limit = {int(sc['rate'])}")
        lines += [
            "[workload.keyspace]",
            f"length = {KEY_LENGTH}",
            f"count = {int(sc.get('keyspacelen', 1000000))}",
            'distribution = "uniform"',
            "[workload.commands]",
            f"get = {w['get']}",
            f"set = {w['set']}",
            "[workload.values]",
            f"length = {int(sc.get('data_size', 100))}",
            "[timestamps]",
            "enabled = true",
            'mode = "userspace"',
            "[admin]",
            'format = "json"',
            'color = "never"',
        ]
        return "\n".join(lines) + "\n"

    def build_argv(self, req: GeneratorRequest) -> List[str]:
        if req.tls:
            raise ValueError("cachecannon backend: TLS is not wired up yet")
        path = req.workdir / "cachecannon.toml"
        path.write_text(self.toml(req))
        return [self.binary, str(path)]

    def prefills(self, scenario: dict) -> bool:
        return self.options.get("prefill", (_weights(scenario) or {}).get("get", 0) > 0)

    def parse(
        self, handle: RunHandle, window: Optional[tuple]
    ) -> Optional[GeneratorResult]:
        stdout = handle.stdout_path.read_text(errors="replace")
        picked = window_samples(read_samples(stdout), window)
        if not picked:
            return None
        row = summarize(picked)
        row["test"] = handle.label
        result = read_result(stdout)
        extra = {
            "generator_errors": round(sum(s.get("err_s", 0.0) for s in picked)),
            "generator_latency_scope": "window" if window else "run",
            "generator_samples": len(picked),
            "p90_latency_ms": row.pop("p90_latency_ms"),
            "p999_latency_ms": row.pop("p999_latency_ms"),
            "p9999_latency_ms": row.pop("p9999_latency_ms"),
        }
        if result:
            extra["generator_timeouts"] = result.get("timeouts")
            extra["generator_conns_failed"] = result.get("conns_failed")
        return GeneratorResult(row=row, extra=extra, argv=handle.argv, stdout="")

    def label(self, scenario: dict) -> str:
        w = _weights(scenario) or {}
        if scenario.get("mix"):
            return f"GET:SET {w.get('get', 0)}:{w.get('set', 0)}"
        return str(scenario.get("test", "")).upper()
