"""Traffic generators for ``matrix`` scenarios.

Each generator declares the features it can express. A matrix cell lists the
features its template and parameter values need, and the compiler rejects a
cell whose generator lacks one, so a workload is never silently replaced by a
different one. Add a generator by subclassing ``Generator``, declaring its
features and registering it in ``GENERATORS``.

Features:

- ``custom_command``: any command text with key and data placeholders.
- ``random_key``: a random key from the keyspace per request.
- ``cluster_aware``: slot routing for cluster mode enabled (CME).
- ``mixed_ratio``: reads and writes in one stream at a set ratio.
- ``zipfian``: Zipf key choice with a configurable exponent.
- ``weighted_commands``: several commands with per-command weights.
- ``random_ttl``: an expiry drawn from a range on each write.
- ``per_request_random_data``: fresh random data on every request, used for
  "new" elements (memtier ``-R`` regenerates ``__data__`` per request).
- ``random_element``: a random existing element name per request. No
  generator offers it yet.
- ``cluster_multi_key``: multi-key commands (MSET, MGET) in cluster mode;
  memtier's cluster mode refuses them.

This module is standard library only, so it runs on bench hosts with only
Python 3.9.
"""

import csv
import json
import shlex
from typing import Any, Dict, List, Optional

FEATURES = (
    "custom_command",
    "random_key",
    "cluster_aware",
    "mixed_ratio",
    "zipfian",
    "weighted_commands",
    "random_ttl",
    "per_request_random_data",
    "random_element",
    "cluster_multi_key",
)

# Placeholders a rendered command may hold; each generator substitutes its own.
KEY = "{key}"
DATA = "{data}"  # random data of value_size bytes, fresh per request
VALUE = "{value}"  # a value of value_size bytes


def _weights(commands: List[Dict[str, Any]]) -> List[int]:
    """Integer command ratios, in thousandths, at least 1 each."""
    return [max(1, round(1000 * c["weight"])) for c in commands]


class Generator:
    name = ""
    features: frozenset = frozenset()

    def key_name(self, prefix: str, index: int) -> str:
        raise NotImplementedError

    def key_digits(self, keys: int) -> int:
        raise NotImplementedError

    def argv(self, cell: Dict[str, Any], **kw) -> List[str]:
        raise NotImplementedError

    def output_file(self, run_id: str) -> Optional[str]:
        """A file the generator writes its results to, or None for stdout."""
        return None

    def parse(self, stdout: str, output: Optional[str]) -> Dict[str, Any]:
        raise NotImplementedError

    def missing_features(self, needs: List[str]) -> List[str]:
        return sorted(set(needs) - set(self.features))


class ValkeyBenchmark(Generator):
    """valkey-benchmark: one custom command per cell, uniform random keys.

    Keys are ``<prefix><12-digit index>``, which ``<prefix>__rand_int__``
    produces for indices 0 .. keys - 1. Values are a fixed string of
    value_size bytes; there is no per-request random data.
    """

    name = "valkey-benchmark"
    features = frozenset(
        {"custom_command", "random_key", "cluster_aware", "cluster_multi_key"}
    )

    def key_name(self, prefix: str, index: int) -> str:
        return f"{prefix}{index:012d}"

    def key_digits(self, keys: int) -> int:
        return 12

    def render(self, command: str, cell: Dict[str, Any]) -> str:
        if DATA in command:
            raise ValueError(f"{self.name} has no per-request random data")
        value = "v" * cell["value_size"]
        return command.replace(KEY, f"{cell['key_prefix']}__rand_int__").replace(
            VALUE, value
        )

    def argv(
        self,
        cell: Dict[str, Any],
        *,
        binary: str,
        host: str,
        port: int,
        connections: int,
        threads: int,
        duration: int,
        cpus: Optional[str] = None,
        seed: Optional[int] = None,
        output: Optional[str] = None,
    ) -> List[str]:
        commands = cell["commands"]
        if len(commands) != 1:
            raise ValueError(f"{self.name} runs exactly one command per cell")
        argv = ["taskset", "-c", cpus] if cpus else []
        argv += [binary, "-h", host, "-p", str(port)]
        argv += ["-c", str(connections), "--threads", str(threads)]
        argv += ["-P", str(cell["pipeline"]), "-r", str(cell["keys"])]
        argv += ["--duration", str(duration)]
        if cell["cluster_mode"]:
            argv.append("--cluster")
        if seed is not None:
            argv += ["--seed", str(seed)]
        argv += ["--csv", "--"]
        argv += shlex.split(self.render(commands[0]["command"], cell))
        return argv

    def parse(self, stdout: str, output: Optional[str]) -> Dict[str, Any]:
        lines = stdout.splitlines()
        start = next(
            (
                i
                for i, line in enumerate(lines)
                if line.startswith('"test","rps"') or line.startswith("test,rps")
            ),
            None,
        )
        if start is None:
            return {"error": "no CSV in valkey-benchmark output"}
        row = next(csv.DictReader(lines[start:]), None)
        if not row:
            return {"error": "empty CSV in valkey-benchmark output"}

        def num(key):
            try:
                return float(row[key])
            except (KeyError, TypeError, ValueError):
                return None

        return {
            "ops_s": num("rps"),
            "p50_ms": num("p50_latency_ms"),
            "p99_ms": num("p99_latency_ms"),
            "p999_ms": None,
            "max_ms": num("max_latency_ms"),
            "connection_errors": 0,
        }


class Memtier(Generator):
    """memtier_benchmark.

    Keys are ``<prefix><n>`` for n = 1 .. keys, unpadded (memtier's key
    minimum must be at least 1), so key index i is named ``<prefix><i + 1>``.
    A string cell without hot keys uses ``--ratio`` and ``--key-pattern``
    (so ``--expiry-range`` applies); every other cell uses ``--command``
    entries with ``--command-ratio``.
    """

    name = "memtier"
    features = frozenset(set(FEATURES) - {"random_element", "cluster_multi_key"})

    def key_name(self, prefix: str, index: int) -> str:
        return f"{prefix}{index + 1}"

    def key_digits(self, keys: int) -> int:
        return len(str(keys))

    def render(self, command: str, cell: Dict[str, Any]) -> str:
        return (
            command.replace(KEY, "__key__")
            .replace(DATA, "__data__")
            .replace(VALUE, "__data__")
        )

    @staticmethod
    def _pattern(cell: Dict[str, Any]) -> str:
        return "Z" if cell["access"]["pattern"] == "zipfian" else "R"

    def argv(
        self,
        cell: Dict[str, Any],
        *,
        binary: str,
        host: str,
        port: int,
        connections: int,
        threads: int,
        duration: int,
        cpus: Optional[str] = None,
        seed: Optional[int] = None,
        output: Optional[str] = None,
    ) -> List[str]:
        if connections % threads:
            raise ValueError(
                f"{connections} connections do not split over {threads} threads"
            )
        argv = ["taskset", "-c", cpus] if cpus else []
        argv += [binary, "-s", host, "-p", str(port), "--protocol", "redis"]
        argv += ["-t", str(threads), "-c", str(connections // threads)]
        argv += ["--pipeline", str(cell["pipeline"]), "--test-time", str(duration)]
        argv += ["--key-prefix", cell["key_prefix"], "--key-minimum", "1"]
        argv += ["--key-maximum", str(cell["keys"]), "-d", str(cell["value_size"])]
        argv += ["-R", "--distinct-client-seed", "--hide-histogram"]
        argv += ["--print-percentiles", "50,99,99.9,100"]
        if cell["access"]["pattern"] == "zipfian":
            argv += ["--key-zipf-exp", str(cell["access"]["alpha"])]
        if cell["cluster_mode"]:
            argv.append("--cluster-mode")
        if cell["kind"] == "string" and cell["access"]["pattern"] != "hotkey":
            read = cell["read_fraction"]
            # memtier's ratio is SET:GET
            sets, gets = round(1000 * (1 - read)), round(1000 * read)
            argv += ["--ratio", f"{sets}:{gets}"]
            p = self._pattern(cell)
            argv += ["--key-pattern", f"{p}:{p}"]
            if cell.get("ttl_range"):
                low, high = cell["ttl_range"]
                argv += ["--expiry-range", f"{low}-{high}"]
        else:
            for command, ratio in zip(cell["commands"], _weights(cell["commands"])):
                argv += ["--command", self.render(command["command"], cell)]
                argv += ["--command-ratio", str(ratio)]
                argv += ["--command-key-pattern", self._pattern(cell)]
                if command.get("read"):
                    argv.append("--command-is-read")
        if output:
            argv += ["--json-out-file", output]
        return argv

    def output_file(self, run_id: str) -> Optional[str]:
        return f"{run_id}.json"

    def parse(self, stdout: str, output: Optional[str]) -> Dict[str, Any]:
        if not output:
            return {"error": "memtier wrote no JSON output"}
        try:
            totals = json.loads(output)["ALL STATS"]["Totals"]
        except (ValueError, KeyError, TypeError) as e:
            return {"error": f"memtier JSON unreadable: {e}"}
        pct = totals.get("Percentile Latencies") or {}

        def p(key):
            value = pct.get(key)
            return float(value) if value is not None else None

        return {
            "ops_s": float(totals.get("Ops/sec", 0.0)),
            "p50_ms": p("p50.00"),
            "p99_ms": p("p99.00"),
            "p999_ms": p("p99.90"),
            "max_ms": p("p100.00"),
            "connection_errors": int(totals.get("Connection Errors", 0) or 0),
        }


GENERATORS = {g.name: g for g in (ValkeyBenchmark(), Memtier())}


def get(name: str) -> Generator:
    try:
        return GENERATORS[name]
    except KeyError:
        raise ValueError(
            f"unknown generator {name!r}; known: {', '.join(sorted(GENERATORS))}"
        ) from None
