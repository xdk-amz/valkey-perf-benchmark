"""Load generator interface.

A generator turns one scenario into a running client process and, once the
process is stopped, into a result row whose keys follow the valkey-benchmark
CSV columns (``rps``, ``*_latency_ms``) so ``MetricsProcessor`` treats every
backend alike. Extra columns a backend can measure (``p999_latency_ms``,
``generator_errors``) travel in ``GeneratorResult.extra``.

Processes are started through an ``Executor`` so a later multi-host runner can
place the generator on another machine without changing the backends.
"""

import hashlib
import logging
import shlex
import signal
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


class GeneratorError(RuntimeError):
    """A generator could not start, or exited without a usable result."""


class Executor:
    """Starts processes on the generator's host. This one is the local host;
    remote.SshExecutor runs them on another host over ssh."""

    remote = False
    host = None

    def path(self, local) -> str:
        """How a process on the generator host names a local file."""
        return str(local)

    def start(
        self, argv: List[str], out_path: Path, err_path: Path, cwd=None, workdir=None
    ):
        """Start argv with stdout and stderr in the given files."""
        with Path(out_path).open("w") as o, Path(err_path).open("w") as e:
            return self.popen(argv, o, e, cwd=cwd)

    def run(self, argv: List[str], cwd=None, timeout=None, capture_output=True):
        return subprocess.run(
            argv, cwd=cwd, timeout=timeout, capture_output=capture_output, text=True
        )

    def identity(self, binary: str) -> Dict[str, object]:
        digest = None
        try:
            digest = hashlib.sha256(Path(binary).read_bytes()).hexdigest()
        except OSError:
            pass
        return {"path": binary, "sha256": digest}

    def popen(self, argv: List[str], stdout, stderr, cwd=None) -> subprocess.Popen:
        logging.info(f"Starting generator: {shlex.join(argv)}")
        return subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            cwd=cwd,
            start_new_session=True,
        )


@dataclass
class GeneratorRequest:
    """Everything a backend needs to drive one scenario run."""

    scenario: dict
    host: str
    port: int
    workdir: Path
    duration_s: int
    tls: bool = False
    # Directory with ca.crt and the client's valkey.crt/valkey.key, as written
    # by Valkey's utils/gen-test-certs.sh.
    tls_dir: Optional[Path] = None
    cluster: bool = False
    cpu_range: Optional[str] = None
    threads: Optional[int] = None
    seed: Optional[int] = None
    auth_user: Optional[str] = None
    auth_password: Optional[str] = None
    cwd: Optional[Path] = None


@dataclass
class RunHandle:
    proc: subprocess.Popen
    argv: List[str]
    stdout_path: Path
    stderr_path: Path
    started_at: float
    label: str = ""

    def alive(self) -> bool:
        return self.proc.poll() is None


@dataclass
class GeneratorResult:
    row: Dict[str, object]
    extra: Dict[str, object]
    argv: List[str]
    stdout: str


class LoadGenerator(ABC):
    """One load generator backend."""

    name: str = ""
    # True when the backend's own report survives being stopped at the edge of
    # the measured window, so the harness need not wait out its full duration.
    stops_at_window_edge: bool = False
    # True when build_argv applies req.cpu_range itself.
    pins_itself: bool = False

    def __init__(self, binary: str, options: Optional[dict] = None, executor=None):
        self.binary = str(binary)
        self.options = dict(options or {})
        self.executor = executor or Executor()

    @abstractmethod
    def unsupported(self, scenario: dict) -> Optional[str]:
        """Return why the scenario cannot run on this backend, or None."""

    @abstractmethod
    def build_argv(self, req: GeneratorRequest) -> List[str]:
        """Return the argv for one run, writing any config files it needs."""

    @abstractmethod
    def parse(
        self, handle: RunHandle, window: Optional[tuple]
    ) -> Optional[GeneratorResult]:
        """Parse a stopped run; window is (start, end) in epoch seconds or None."""

    def expected_connections(self, scenario: dict) -> int:
        return int(scenario.get("clients", 1))

    def start(self, req: GeneratorRequest) -> RunHandle:
        reason = self.unsupported(req.scenario)
        if reason:
            raise GeneratorError(f"{self.name}: {reason}")
        req.workdir.mkdir(parents=True, exist_ok=True)
        argv = self.build_argv(req)
        if req.cpu_range and not self.pins_itself:
            argv = ["taskset", "-c", req.cpu_range, *argv]
        out = req.workdir / f"{self.name}.out"
        err = req.workdir / f"{self.name}.err"
        proc = self.executor.start(argv, out, err, cwd=req.cwd, workdir=req.workdir)
        return RunHandle(
            proc, list(proc.args), out, err, time.time(), self.label(req.scenario)
        )

    def label(self, scenario: dict) -> str:
        """The command label recorded in the row's ``command`` field."""
        return scenario.get("test") or scenario.get("command") or self.name

    def stop(self, handle: RunHandle, grace_s: float = 3.0) -> None:
        """Stop a run early: SIGINT, then SIGTERM, then SIGKILL."""
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            if not handle.alive():
                return
            try:
                handle.proc.send_signal(sig)
            except ProcessLookupError:
                return
            try:
                handle.proc.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                continue

    def wait(self, handle: RunHandle, timeout_s: float) -> None:
        try:
            handle.proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            logging.warning(f"{self.name} overran its duration, stopping it")
            self.stop(handle)

    def version(self) -> str:
        try:
            r = self.executor.run([self.binary, "--version"], timeout=10)
            return (r.stdout or r.stderr).strip().splitlines()[0]
        except Exception:
            return "unknown"

    def identity(self) -> Dict[str, object]:
        """Name, version and binary digest, for the run manifest."""
        return {
            "name": self.name,
            "binary": self.binary,
            "version": self.version(),
            "sha256": self.executor.identity(self.binary).get("sha256"),
        }


def run_for_duration(
    generator: LoadGenerator, req: GeneratorRequest, slack_s: float = 60.0
) -> Optional[GeneratorResult]:
    """Run a scenario for its whole duration and parse the generator's report.

    A generator still running ``slack_s`` past its duration is stopped and its
    partial report parsed; one that exits non-zero by itself raises.
    """
    handle = generator.start(req)
    overran = False
    try:
        try:
            handle.proc.wait(timeout=req.duration_s + slack_s)
        except subprocess.TimeoutExpired:
            logging.warning(f"{generator.name} overran its duration, stopping it")
            overran = True
            generator.stop(handle)
    except BaseException:
        generator.stop(handle)
        raise
    if not overran and handle.proc.returncode != 0:
        tail = handle.stderr_path.read_text(errors="replace").strip().splitlines()
        raise GeneratorError(
            f"{generator.name} exited with {handle.proc.returncode}: "
            f"{tail[-1] if tail else ''}"
        )
    return generator.parse(handle, None)
