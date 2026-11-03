"""valkey-benchmark backend.

Argv construction stays in ``ClientRunner._build_benchmark_command``, which
this backend receives as ``command_builder``, so a measured run passes the same
flags as the framework's ordinary runs. Measured runs use this backend to run
valkey-benchmark under the measurement window; it only reports at exit, so the
harness lets it run to the end of its duration.
"""

import csv
import copy
import re
from typing import Callable, List, Optional

from .base import GeneratorRequest, GeneratorResult, LoadGenerator, RunHandle

CSV_HEADERS = ('"test","rps"', "test,rps")


class ValkeyBenchmarkGenerator(LoadGenerator):
    name = "valkey-benchmark"
    pins_itself = True

    def __init__(self, binary, options=None, executor=None, command_builder=None):
        super().__init__(binary, options, executor)
        if command_builder is None:
            raise ValueError("valkey-benchmark backend needs a command_builder")
        self.command_builder: Callable[..., List[str]] = command_builder

    def unsupported(self, scenario: dict) -> Optional[str]:
        if scenario.get("rate"):
            return "fixed-rate scenarios ('rate') need a generator with a rate limit"
        if scenario.get("mix"):
            return "'mix' scenarios need a generator with weighted commands"
        if scenario.get("type") == "mixed":
            return "measured runs of 'mixed' scenarios are not supported"
        return None

    def build_argv(self, req: GeneratorRequest) -> List[str]:
        sc = copy.deepcopy(req.scenario)
        sc["duration"] = req.duration_s
        sc.pop("requests", None)
        sc.pop("warmup_inline", None)
        argv = self.command_builder(
            scenario=sc, cpu_range=req.cpu_range, seed_val=req.seed, port=req.port
        )
        if req.auth_password:
            at = argv.index(self.binary) + 1
            auth = ["-a", req.auth_password]
            if req.auth_user:
                auth = ["--user", req.auth_user, *auth]
            argv[at:at] = auth
        return argv

    def parse(
        self, handle: RunHandle, window: Optional[tuple]
    ) -> Optional[GeneratorResult]:
        stdout = handle.stdout_path.read_text(errors="replace")
        stderr = handle.stderr_path.read_text(errors="replace")
        lines = stdout.splitlines()
        start = next(
            (i for i, l in enumerate(lines) if l.startswith(CSV_HEADERS)), None
        )
        if start is None:
            return None
        test = self._test_name(handle.argv)
        row = None
        for r in csv.DictReader(lines[start:]):
            if test is None or r.get("test", "").startswith(test):
                row = dict(r)
                break
        if row is None:
            return None
        errors = len(re.findall(r"^Error", stderr, re.M))
        return GeneratorResult(
            row=row,
            extra={"generator_errors": errors, "generator_latency_scope": "run"},
            argv=handle.argv,
            stdout=stdout,
        )

    @staticmethod
    def _test_name(argv: List[str]) -> Optional[str]:
        if "-t" in argv:
            return argv[argv.index("-t") + 1]
        return None
