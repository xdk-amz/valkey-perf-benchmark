"""Benchmark hosts reached over ssh: the server host and the generator host.

The orchestrator (benchmark.py) runs on one machine and drives each host over
ssh, as offbench does. Commands and vpb_agent.py actions go over one shared
ssh connection per host (ControlMaster). The orchestrator's own Valkey clients
(readiness checks, setup, flush, the probe used before the window) reach the
server through ssh port forwards, so the hosts need no open ports beyond ssh;
the generators connect to the server's private address directly.

``SshExecutor`` starts load generators on a generator host. Local files a
generator needs (its config file, TLS certificates) are copied to the same
path under ``<root>/stage`` on the host, and their paths in argv are
rewritten to match. Output is written on the host and copied back when the
process exits.
"""

import json
import logging
import os
import shlex
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from generators.base import Executor

HARNESS_DIR = Path(__file__).resolve().parent
AGENT_FILES = (
    "vpb_agent.py",
    "procstat.py",
    "binfacts.py",
    "environment_metadata.py",
    "thread_pinning.py",
    "utils/__init__.py",
    "utils/cpu_utils.py",
)


class RemoteError(RuntimeError):
    """A command or agent action on a remote host failed."""


# ---------- control-plane addresses ------------------------------------------

_forwarder: Optional[Callable[[str, int], tuple]] = None


def set_forwarder(fn: Optional[Callable[[str, int], tuple]]) -> None:
    """Route the orchestrator's own server connections (None: connect directly)."""
    global _forwarder
    _forwarder = fn


def connect_addr(host: str, port: int) -> tuple:
    """Where the orchestrator connects to reach ``host:port``."""
    return _forwarder(host, port) if _forwarder else (host, port)


def _free_local_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------- hosts --------------------------------------------------------------


class SshHost:
    """One benchmark host reached over ssh."""

    def __init__(
        self,
        target: str,
        *,
        private_ip: Optional[str] = None,
        key: Optional[str] = None,
        known_hosts: Optional[str] = None,
        root: str = "~/vpb",
        control_dir: Optional[str] = None,
        name: Optional[str] = None,
    ):
        self.target = target
        self.private_ip = private_ip
        self.key = key
        self.known_hosts = known_hosts
        self.root_spec = root
        self.name = name or target
        # A unix socket path is limited to about 104 bytes, and ssh appends a
        # temporary suffix while creating it, so the directory must be short.
        control_dir = (
            control_dir
            or os.environ.get("VPB_SSH_CONTROL_DIR")
            or f"/tmp/vpb-ssh-{os.getuid()}"
        )
        os.makedirs(control_dir, mode=0o700, exist_ok=True)
        self.control_path = os.path.join(control_dir, "%C")
        self._root: Optional[str] = None
        self._forwards: Dict[int, int] = {}
        self._agent_ready = False

    # ---- ssh ----
    def ssh_options(self) -> List[str]:
        opts = [
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=" + ("yes" if self.known_hosts else "accept-new"),
            "-o",
            "ControlMaster=auto",
            "-o",
            f"ControlPath={self.control_path}",
            "-o",
            "ControlPersist=600",
            "-o",
            "ServerAliveInterval=15",
        ]
        if self.known_hosts:
            opts += ["-o", f"UserKnownHostsFile={self.known_hosts}"]
        if self.key:
            opts += ["-i", self.key]
        return opts

    def argv(self, script: str) -> List[str]:
        return [
            "ssh",
            *self.ssh_options(),
            self.target,
            "bash -lc " + shlex.quote(script),
        ]

    def script(
        self,
        script: str,
        *,
        timeout: Optional[float] = 120,
        check: bool = True,
        input: Optional[bytes] = None,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        r = subprocess.run(
            self.argv(script),
            input=input,
            capture_output=True,
            text=text and input is None,
            timeout=timeout,
        )
        if check and r.returncode != 0:
            err = (
                r.stderr
                if isinstance(r.stderr, str)
                else r.stderr.decode(errors="replace")
            )
            raise RemoteError(
                f"{self.name}: `{script[:200]}` exited {r.returncode}: {err.strip()[-500:]}"
            )
        return r

    def run(
        self,
        argv: Iterable[str],
        cwd: Optional[str] = None,
        timeout: Optional[float] = 120,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        prefix = f"cd {shlex.quote(cwd)} && " if cwd else ""
        return self.script(
            prefix + shlex.join(list(argv)), timeout=timeout, check=check
        )

    @property
    def root(self) -> str:
        """The host's working directory, as an absolute path."""
        if self._root is None:
            spec = self.root_spec
            if spec.startswith("~"):
                home = self.script("echo $HOME").stdout.strip()
                spec = home + spec[1:]
            self.script(f"mkdir -p {shlex.quote(spec)}")
            self._root = spec.rstrip("/")
        return self._root

    # ---- files ----
    def put_tree(self, local_dir: Path, remote_dir: str) -> None:
        tar = subprocess.run(
            ["tar", "-C", str(local_dir), "-cf", "-", "."],
            capture_output=True,
            check=True,
        )
        q = shlex.quote(remote_dir)
        self.script(f"mkdir -p {q} && tar -xf - -C {q}", input=tar.stdout, timeout=600)

    def put_file(self, local: Path, remote: str) -> None:
        q = shlex.quote(remote)
        self.script(
            f"mkdir -p $(dirname {q}) && cat > {q}.tmp && mv {q}.tmp {q}",
            input=Path(local).read_bytes(),
            timeout=600,
        )

    def get_file(self, remote: str, local: Path) -> bool:
        r = self.script(
            f"test -f {shlex.quote(remote)} && cat {shlex.quote(remote)}",
            check=False,
            input=b"",
            timeout=600,
        )
        if r.returncode != 0:
            return False
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        Path(local).write_bytes(r.stdout)
        return True

    def get_tree(self, remote_dir: str, local_dir: Path) -> None:
        r = self.script(
            f"tar -C {shlex.quote(remote_dir)} -cf - .", input=b"", timeout=600
        )
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["tar", "-C", str(local_dir), "-xf", "-"], input=r.stdout, check=True
        )

    # ---- agent ----
    def ensure_agent(self) -> None:
        """Copy vpb_agent.py and its modules to the host unless already current."""
        if self._agent_ready:
            return
        from binfacts import sha256

        want = {f: sha256(HARNESS_DIR / f) for f in AGENT_FILES}
        have = {}
        try:
            have = self.agent("hello", _check=False).get("files", {})
        except RemoteError:
            pass
        if have != want:
            with tempfile.TemporaryDirectory() as tmp:
                for f in AGENT_FILES:
                    dst = Path(tmp) / f
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes((HARNESS_DIR / f).read_bytes())
                self.put_tree(Path(tmp), f"{self.root}/agent")
            got = self.agent("hello", _check=False).get("files", {})
            if got != want:
                raise RemoteError(f"{self.name}: agent files did not arrive intact")
        self._agent_ready = True

    def _agent_argv(self, payload: dict) -> List[str]:
        return self.argv(
            f"python3 {shlex.quote(self.root + '/agent/vpb_agent.py')} "
            + shlex.quote(json.dumps(payload))
        )

    def _agent_reply(self, action: str, r: subprocess.CompletedProcess) -> dict:
        lines = [l for l in r.stdout.splitlines() if l.startswith("{")]
        if not lines:
            raise RemoteError(
                f"{self.name} {action}: no reply (exit {r.returncode}) {r.stderr.strip()[-300:]}"
            )
        out = json.loads(lines[-1])
        if "error" in out:
            raise RemoteError(f"{self.name} {action}: {out['error']}")
        return out

    def agent(
        self, action: str, timeout: float = 120, _check: bool = True, **payload
    ) -> dict:
        if _check:
            self.ensure_agent()
        r = subprocess.run(
            self._agent_argv({"action": action, **payload}),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return self._agent_reply(action, r)

    def agent_start(self, action: str, **payload) -> Callable[..., dict]:
        """Start an agent action without waiting; returns ``wait(timeout)``."""
        self.ensure_agent()
        p = subprocess.Popen(
            self._agent_argv({"action": action, **payload}),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def wait(timeout: float = 120) -> dict:
            out, err = p.communicate(timeout=timeout)
            return self._agent_reply(
                action, subprocess.CompletedProcess(p.args, p.returncode, out, err)
            )

        return wait

    # ---- control-plane port forwards ----
    def forward(self, port: int) -> int:
        """Local port forwarded to ``private_ip:port`` on this host."""
        if port not in self._forwards:
            self.script("true")  # the forward rides on the shared connection
            check = subprocess.run(
                ["ssh", *self.ssh_options(), "-O", "check", self.target],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if check.returncode != 0:
                raise RemoteError(
                    f"{self.name}: no shared ssh connection ({check.stderr.strip()}); "
                    f"is the control path {self.control_path!r} too long?"
                )
            local = _free_local_port()
            target = self.private_ip or "127.0.0.1"
            r = subprocess.run(
                [
                    "ssh",
                    *self.ssh_options(),
                    "-O",
                    "forward",
                    "-L",
                    f"127.0.0.1:{local}:{target}:{port}",
                    self.target,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if r.returncode != 0:
                raise RemoteError(f"{self.name}: forward {port}: {r.stderr.strip()}")
            self._forwards[port] = local
        return self._forwards[port]

    def forwarder(self, *addresses: str) -> Callable[[str, int], tuple]:
        """A ``connect_addr`` route sending ``addresses`` (every address when
        none are named) through forwards to this host."""
        names = {a for a in addresses if a}

        def route(host: str, port: int) -> tuple:
            if not names or host in names:
                return ("127.0.0.1", self.forward(port))
            return (host, port)

        return route

    def close(self) -> None:
        subprocess.run(
            ["ssh", *self.ssh_options(), "-O", "exit", self.target],
            capture_output=True,
            timeout=30,
        )
        self._forwards.clear()


# ---------- processes on a remote host ---------------------------------------


class RemoteProcess:
    """A process started by vpb_agent.py, with the subprocess.Popen calls
    LoadGenerator uses: poll, wait, send_signal, pid, returncode, args."""

    def __init__(self, host: SshHost, pid: int, argv, remote_out, remote_err, out, err):
        self.host, self.pid, self.args = host, pid, argv
        self.remote_out, self.remote_err = remote_out, remote_err
        self.out, self.err = Path(out), Path(err)
        self.returncode: Optional[int] = None

    def poll(self) -> Optional[int]:
        if self.returncode is not None:
            return self.returncode
        r = self.host.agent("poll", pid=self.pid, out=self.remote_out)
        if r["alive"]:
            return None
        self.returncode = r["rc"] if r["rc"] is not None else -1
        self.host.get_file(self.remote_out, self.out)
        self.host.get_file(self.remote_err, self.err)
        return self.returncode

    def wait(self, timeout: Optional[float] = None) -> int:
        deadline = None if timeout is None else time.time() + timeout
        while self.poll() is None:
            if deadline is not None and time.time() > deadline:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(0.5)
        return self.returncode

    def send_signal(self, sig: int) -> None:
        if self.returncode is None:
            self.host.agent("signal", pid=self.pid, sig=int(sig))


class SshExecutor(Executor):
    """Runs load generators on a generator host."""

    remote = True

    def __init__(self, host: SshHost, local_roots: Iterable[Path] = ()):
        self.host = host
        self.local_roots = [Path(r).resolve() for r in local_roots]
        self._staged_dirs: set = set()

    def add_root(self, path: Path) -> None:
        p = Path(path).resolve()
        if p not in self.local_roots:
            self.local_roots.append(p)

    def _mapped(self, local: Path) -> str:
        return f"{self.host.root}/stage{Path(local).resolve()}"

    def _under_roots(self, p: Path) -> bool:
        p = p.resolve()
        return any(p == r or r in p.parents for r in self.local_roots)

    def path(self, local) -> str:
        """The host-side path of a local file or directory, copied there."""
        p = Path(local).resolve()
        remote = self._mapped(p)
        if p.is_dir():
            if p not in self._staged_dirs:
                self.host.put_tree(p, remote)
                self._staged_dirs.add(p)
        elif p.exists():
            self.host.put_file(p, remote)
        return remote

    def map_argv(self, argv: List[str]) -> List[str]:
        out = []
        for token in argv:
            if (
                os.path.isabs(token)
                and os.path.exists(token)
                and self._under_roots(Path(token))
            ):
                out.append(self.path(token))
            else:
                out.append(token)
        return out

    def _cwd(self, cwd) -> str:
        return self.host.root

    def start(self, argv, out_path, err_path, cwd=None, workdir=None):
        if workdir is not None:
            self.host.put_tree(Path(workdir), self._mapped(workdir))
        argv = self.map_argv(list(argv))
        rout, rerr = self._mapped(out_path), self._mapped(err_path)
        logging.info(f"Starting generator on {self.host.name}: {shlex.join(argv)}")
        pid = self.host.agent(
            "spawn", argv=argv, cwd=self._cwd(cwd), out=rout, err=rerr
        )["pid"]
        return RemoteProcess(self.host, pid, argv, rout, rerr, out_path, err_path)

    def run(self, argv, cwd=None, timeout=None, capture_output=True):
        argv = self.map_argv(list(argv))
        return self.host.run(argv, cwd=self._cwd(cwd), timeout=timeout, check=False)

    def identity(self, binary: str) -> dict:
        try:
            return self.host.agent("binfacts", path=binary)
        except RemoteError:
            return {"path": binary, "sha256": None}


# ---------- builds on a remote host ------------------------------------------


def build_dir(
    host: SshHost, sha: str, *, tls: bool, build_args: Iterable[str] = ()
) -> str:
    """Where ``remote_build`` keeps a commit's build on ``host``."""
    args = list(build_args) + (["BUILD_TLS=yes"] if tls else [])
    tag = "".join(ch for ch in "-".join(args) if ch.isalnum() or ch == "-")
    return f"{host.root}/builds/{sha}" + (f"-{tag}" if tag else "")


def remote_build(
    host: SshHost,
    checkout: Path,
    *,
    tls: bool,
    build_args: Iterable[str] = (),
    targets: Iterable[str] = (),
) -> str:
    """Build the commit checked out at ``checkout`` on ``host``; returns the
    host-side source directory.

    The tree is copied with ``git archive`` (no clone on the host) into
    ``<root>/builds/<sha>[-<args>]`` and built there with plain make. A tree
    already built is reused. With ``tls`` the test certificates are generated.
    """
    sha = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    args = list(build_args) + (["BUILD_TLS=yes"] if tls else [])
    dst = build_dir(host, sha, tls=tls, build_args=build_args)
    q = shlex.quote(dst)
    expect = "src/" + (list(targets)[0] if targets else "valkey-server")
    done = host.script(f"test -x {q}/{expect}", check=False).returncode == 0
    if not done:
        logging.info(f"Building {sha[:10]} on {host.name} in {dst}")
        archive = subprocess.run(
            ["git", "-C", str(checkout), "archive", sha],
            capture_output=True,
            check=True,
        )
        # A fresh directory per attempt; an interrupted build is left behind
        # rather than half-reused.
        part = shlex.quote(f"{dst}.part-{os.getpid()}-{time.time_ns()}")
        host.script(
            f"mkdir -p {part} && tar -xf - -C {part}", input=archive.stdout, timeout=600
        )
        make = [
            "make",
            "-j$(nproc)",
            *map(shlex.quote, args),
            *map(shlex.quote, targets),
        ]
        host.script(
            f"cd {part} && {' '.join(make)} > build.log 2>&1 "
            f"|| {{ tail -30 build.log >&2; exit 1; }}",
            timeout=3600,
        )
        host.script(f"test -x {part}/{expect} && mv -T {part} {q}")
    if tls:
        host.script(
            f"cd {q} && test -f tests/tls/ca.crt || ./utils/gen-test-certs.sh > /dev/null",
            timeout=120,
        )
    return dst


# ---------- one run's remote hosts -------------------------------------------


class RemoteSetup:
    """The server host and, optionally, the generator host of a run.

    ``prepare`` builds a commit on the server host (once per commit and build
    configuration) and copies its TLS certificates to this machine.
    """

    def __init__(self, server: SshHost, generator: Optional[SshHost] = None):
        self.server = server
        self.generator = generator
        self.executor = SshExecutor(generator) if generator else None
        self._builds: Dict[tuple, dict] = {}
        self._facts: Dict[str, dict] = {}

    def facts(self) -> Dict[str, dict]:
        """Host facts of the server host and generator host, read once."""
        if not self._facts:
            for role, host in (
                ("server_host", self.server),
                ("generator_host", self.generator),
            ):
                if host is not None:
                    self._facts[role] = {**host.agent("envmeta"), **host.agent("facts")}
        return self._facts

    def prepare(self, checkout: Path, cfg: dict, results_root: Path) -> dict:
        sha = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        tls = bool(cfg.get("tls_mode"))
        build_args = tuple(cfg.get("build_args", []))
        key = (sha, tls, build_args)
        if key not in self._builds:
            server_dir = remote_build(
                self.server, checkout, tls=tls, build_args=build_args
            )
            tls_dir = None
            if tls:
                tls_dir = Path(results_root) / "tls" / sha[:12]
                self.server.get_tree(f"{server_dir}/tests/tls", tls_dir)
                if self.executor is not None:
                    self.executor.add_root(tls_dir)
            self._builds[key] = {
                "server_dir": server_dir,
                "tls_dir": tls_dir,
                "server_binary": self.server.agent(
                    "binfacts", path=f"{server_dir}/src/valkey-server"
                ),
                "architecture": self.facts()["server_host"]["machine"],
            }
        return self._builds[key]

    def close(self) -> None:
        set_forwarder(None)
        self.server.close()
        if self.generator is not None:
            self.generator.close()
