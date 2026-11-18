#!/usr/bin/env python3
"""Run compiled ``matrix`` cells: one server host, one or more generator hosts.

    python matrix_runner.py --config configs/matrix/ratatoskr.json \\
        --profile slice-iot4-p1 \\
        --build unstable=<sha>:<path to valkey-server> \\
        --build frontier=<sha>:<path to valkey-server> \\
        --server-host <ssh target> --server-bind <server private ip> \\
        --target-ip <server private ip> --generator-hosts local \\
        --memtier <path> --cli <path to valkey-cli> --results-dir results

``benchmark.py --config <matrix config>`` hands off to this module too.

For every cell and rep the builds run in rotated order (rep k starts with
build k mod n), and the server is restarted before each build's turn. A rep:

1. Stop the server this runner started on the port (pid file), start the
   build with the cell's io-threads and cluster mode, bound to
   ``--server-bind`` and loopback only, then pin its threads if
   ``--server-layout`` is given.
2. Load the cell's preload with ``valkey-cli --pipe``, then apply
   ``server_config`` with CONFIG SET.
3. Start the generator on every generator host, each with its exact share
   of the connections. Read INFO at the window's two edges (timed on this
   host's clock) and each generator host's CPU counters at the same moments.
4. Throughput is server-side: the change in commandstats calls (without this
   runner's INFO) over the change in server_time_usec.

Gates, from the config's ``gates`` block, mark a rep invalid with its reason:
every planned client connected at the window end; no generator host's CPUs
above ``max_generator_busy_pct`` busy; no generator error. Before the first
rep the median PING round trip from every generator host must be at most
``rtt_gate_ms``, or the run refuses to start; a rep whose p50 latency is less
than ``1 / rtt_flag_fraction`` round trips is flagged ``rtt_bound``.

Results: ``<results-dir>/<run>/metrics.json`` (rows for the static dashboard
and ``push_to_postgres.py``), ``summary.json`` (verdicts per cell),
``cells.json`` and ``manifest.json``. A rerun with the same ``--run-name``
skips reps already recorded. A file named STOP in the run directory stops
the run between reps.

Standard library only, so it runs on bench hosts with only Python 3.9.
"""

import argparse
import hashlib
import json
import logging
import os
import shlex
import socket
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import generators

RUNNER_COMMANDS = ("info",)
DEFAULT_GATES = {
    "require_all_clients": True,
    "max_generator_busy_pct": 90,
    "rtt_gate_ms": None,
    "rtt_flag_fraction": 0.5,
    "max_error_reply_pct": 1,
}
DEFAULT_VERDICT = {
    "min_valid_reps": 2,
    "tie_band_pct": 5,
    "require_no_overlap": True,
    "high_variance_cv_pct": 5,
}


# ---------------------------------------------------------------- hosts
class Host:
    """A host reached over ssh, or this host when the target is ``local``."""

    def __init__(self, target: str):
        self.target = target
        self.local = target == "local"

    def run(self, cmd: str, timeout: float = 120, stdin: Optional[str] = None):
        argv = ["bash", "-c", cmd] if self.local else ["ssh", self.target, cmd]
        try:
            p = subprocess.run(
                argv, input=stdin, capture_output=True, text=True, timeout=timeout
            )
            return p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            return 124, "", f"timed out after {timeout}s"

    def check(self, cmd: str, timeout: float = 120, stdin: Optional[str] = None):
        rc, out, err = self.run(cmd, timeout, stdin)
        if rc:
            raise RuntimeError(
                f"{self.target}: {cmd[:120]} failed ({rc}): {err[-400:]}"
            )
        return out


# ---------------------------------------------------------------- RESP client
class Conn:
    """A minimal RESP2 client, enough for INFO, PING, CONFIG and CLUSTER."""

    def __init__(self, host: str, port: int, timeout: float = 10):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.buf = b""

    def close(self):
        self.sock.close()

    def _line(self) -> bytes:
        while b"\r\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("server closed the connection")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\r\n")
        return line

    def _exact(self, n: int) -> bytes:
        while len(self.buf) < n + 2:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("server closed the connection")
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n + 2 :]
        return data

    def _reply(self):
        line = self._line()
        kind, rest = line[:1], line[1:]
        if kind == b"+":
            return rest.decode()
        if kind == b"-":
            raise RuntimeError(rest.decode())
        if kind == b":":
            return int(rest)
        if kind == b"$":
            n = int(rest)
            return None if n < 0 else self._exact(n).decode()
        if kind == b"*":
            n = int(rest)
            return None if n < 0 else [self._reply() for _ in range(n)]
        raise RuntimeError(f"unexpected reply {line[:40]!r}")

    def call(self, *args):
        parts = [f"*{len(args)}\r\n".encode()]
        for a in args:
            b = str(a).encode()
            parts.append(b"$%d\r\n%s\r\n" % (len(b), b))
        self.sock.sendall(b"".join(parts))
        return self._reply()


def parse_info(text: str) -> Dict[str, str]:
    out = {}
    for line in text.splitlines():
        if ":" in line and not line.startswith("#"):
            k, _, v = line.partition(":")
            out[k] = v.strip()
    return out


def command_calls(info: Dict[str, str]) -> Dict[str, int]:
    calls = {}
    for key, value in info.items():
        if key.startswith("cmdstat_"):
            for part in value.split(","):
                name, _, number = part.partition("=")
                if name == "calls":
                    calls[key[len("cmdstat_") :]] = int(number)
    return calls


def window_throughput(first: Dict[str, str], second: Dict[str, str]) -> Dict[str, Any]:
    """Server ops/s between two INFO readings, without this runner's commands."""
    seconds = (int(second["server_time_usec"]) - int(first["server_time_usec"])) / 1e6
    if seconds <= 0:
        raise ValueError("INFO readings are not in time order")
    before, after = command_calls(first), command_calls(second)
    by_command = {
        cmd: round((n - before.get(cmd, 0)) / seconds, 2)
        for cmd, n in sorted(after.items())
        if cmd not in RUNNER_COMMANDS and n > before.get(cmd, 0)
    }
    return {
        "server_ops_s": round(sum(by_command.values()), 2),
        "by_command": by_command,
        "seconds": round(seconds, 3),
        "errors": int(second.get("total_error_replies", 0))
        - int(first.get("total_error_replies", 0)),
    }


# ---------------------------------------------------------------- CPU counters
def cpu_list(spec: Optional[str]) -> Optional[List[int]]:
    """Expand "1-3,8" into [1, 2, 3, 8]; None stays None (all CPUs)."""
    if not spec:
        return None
    out = []
    for part in spec.split(","):
        lo, _, hi = part.partition("-")
        out += list(range(int(lo), int(hi or lo) + 1))
    return out


def cpu_times(proc_stat: str, cpus: Optional[List[int]]) -> Tuple[int, int]:
    """(busy, total) jiffies over ``cpus`` (all CPUs when None) from /proc/stat."""
    busy = total = 0
    for line in proc_stat.splitlines():
        fields = line.split()
        if not fields or not fields[0].startswith("cpu"):
            continue
        name = fields[0]
        if cpus is None and name != "cpu":
            continue
        if cpus is not None and (name == "cpu" or int(name[3:]) not in cpus):
            continue
        values = [int(v) for v in fields[1:9]]
        idle = values[3] + values[4]  # idle + iowait
        busy += sum(values) - idle
        total += sum(values)
    return busy, total


def busy_pct(first: Tuple[int, int], second: Tuple[int, int]) -> Optional[float]:
    d_total = second[1] - first[1]
    if d_total <= 0:
        return None
    return round(100.0 * (second[0] - first[0]) / d_total, 1)


# ---------------------------------------------------------------- gates and verdicts
def gate_reasons(
    gates: Dict[str, Any],
    planned: int,
    connected: Optional[int],
    busy: Dict[str, Optional[float]],
    gen_results: Dict[str, Dict[str, Any]],
    ops: Optional[float] = None,
    error_replies: int = 0,
) -> List[str]:
    """Reasons a rep is invalid (empty when every gate passes)."""
    reasons = []
    limit = gates.get("max_error_reply_pct")
    if limit is not None and error_replies and ops is not None:
        pct = 100.0 * error_replies / max(error_replies + ops, 1)
        if pct > limit:
            reasons.append(
                f"{pct:.1f}% of replies in the window were errors (limit {limit}%)"
            )
    for host, result in gen_results.items():
        if result.get("error"):
            reasons.append(f"generator error on {host}: {result['error']}")
        elif result.get("connection_errors"):
            reasons.append(f"{result['connection_errors']} connection errors on {host}")
    if gates.get("require_all_clients") and (connected is None or connected < planned):
        reasons.append(f"{connected} of {planned} clients connected at the window end")
    limit = gates.get("max_generator_busy_pct")
    if limit is not None:
        for host, pct in busy.items():
            if pct is not None and pct > limit:
                reasons.append(
                    f"client-bound: {host} CPUs {pct:.0f}% busy (limit {limit}%)"
                )
    return reasons


def rtt_bound(gates: Dict[str, Any], rtt_ms: Optional[float], p50_ms) -> bool:
    fraction = gates.get("rtt_flag_fraction")
    if not fraction or rtt_ms is None or not p50_ms:
        return False
    return rtt_ms > fraction * p50_ms


def _cv(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    return statistics.pstdev(values) / statistics.mean(values) * 100


def verdict(
    baseline: str,
    candidate: str,
    base_ops: List[float],
    cand_ops: List[float],
    rules: Dict[str, Any],
    base_reasons: Optional[List[str]] = None,
    cand_reasons: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Compare the valid reps of two builds for one cell.

    Higher when the medians differ by more than ``tie_band_pct`` and (with
    ``require_no_overlap``) the rep ranges do not overlap; otherwise a tie.
    Fewer than ``min_valid_reps`` valid reps on either side is MISSING, with
    the reasons; when only one side lost connections it is flagged.
    """
    need = rules.get("min_valid_reps", 2)
    if len(base_ops) < need or len(cand_ops) < need:
        out = {"verdict": "MISSING", "ratio": None, "high_variance": False, "flags": []}
        why = []
        for label, ops, rs in (
            (baseline, base_ops, base_reasons or []),
            (candidate, cand_ops, cand_reasons or []),
        ):
            if len(ops) < need:
                why.append(
                    f"{label}: {len(ops)} valid reps of {need} needed ({'; '.join(sorted(set(rs)))[:300]})"
                )
        out["reason"] = " / ".join(why)
        lost = {
            label: any("clients connected" in r for r in rs or [])
            for label, rs in ((baseline, base_reasons), (candidate, cand_reasons))
        }
        if len(base_ops) >= need and lost[candidate]:
            out["flags"].append(f"{candidate} drops connections")
        if len(cand_ops) >= need and lost[baseline]:
            out["flags"].append(f"{baseline} drops connections")
        return out
    mb, mc = statistics.median(base_ops), statistics.median(cand_ops)
    ratio = mc / mb if mb else None
    overlap = not (min(cand_ops) > max(base_ops) or max(cand_ops) < min(base_ops))
    band = rules.get("tie_band_pct", 5) / 100
    differs = ratio is not None and abs(ratio - 1) > band
    if differs and not (rules.get("require_no_overlap", True) and overlap):
        result = f"{candidate} higher" if ratio > 1 else f"{baseline} higher"
    else:
        result = "tie"
    limit = rules.get("high_variance_cv_pct", 5)
    return {
        "verdict": result,
        "ratio": round(ratio, 4) if ratio else None,
        "high_variance": _cv(base_ops) > limit or _cv(cand_ops) > limit,
        "flags": [],
    }


# ---------------------------------------------------------------- preload
def _resp(*args) -> bytes:
    out = [b"*%d\r\n" % len(args)]
    for a in args:
        b = a if isinstance(a, bytes) else str(a).encode()
        out.append(b"$%d\r\n%s\r\n" % (len(b), b))
    return b"".join(out)


def write_preload(spec: Dict[str, Any], gen: generators.Generator, path: Path) -> int:
    """Write the preload as RESP commands for ``valkey-cli --pipe``; return the count."""
    kind, n, size = spec["type"], spec["keys"], spec["value_size"]
    value = b"v" * size
    elements = spec.get("elements", 0)
    prefix = spec.get("element_prefix", "e")
    count = 0
    with open(path, "wb") as f:
        for i in range(n):
            key = gen.key_name(spec["key_prefix"], i)
            if kind == "string":
                f.write(_resp("SET", key, value))
            elif kind == "hash":
                args = []
                for e in range(elements):
                    args += [f"{prefix}{e}", value]
                f.write(_resp("HSET", key, *args))
            elif kind == "list":
                f.write(_resp("RPUSH", key, *([value] * elements)))
            elif kind == "set":
                f.write(_resp("SADD", key, *[f"{prefix}{e}" for e in range(elements)]))
            elif kind == "zset":
                args = []
                for e in range(elements):
                    args += [e, f"{prefix}{e}"]
                f.write(_resp("ZADD", key, *args))
            elif kind == "stream":
                fields = []
                for e in range(spec.get("fields_per_entry", 1)):
                    fields += [f"f{e}", value]
                for _ in range(elements):
                    f.write(_resp("XADD", key, "*", *fields))
                    count += 1
                continue
            else:
                raise ValueError(f"unknown preload type {kind!r}")
            count += 1
    return count


# ---------------------------------------------------------------- the runner
class Runner:
    def __init__(self, cfg: Dict[str, Any], args: argparse.Namespace):
        self.cfg, self.args = cfg, args
        self.port = int(cfg.get("port", 6379))
        self.gates = dict(DEFAULT_GATES, **(cfg.get("gates") or {}))
        self.rules = dict(DEFAULT_VERDICT, **(cfg.get("verdict") or {}))
        self.run_settings = cfg.get("matrix_run") or {}
        self.reps = args.reps or self.run_settings.get("reps", 3)
        self.builds = [parse_build(b) for b in args.build]
        if not self.builds:
            raise SystemExit("give at least one --build label=sha:path")
        self.server = Host(args.server_host)
        self.gen_hosts = [Host(t) for t in args.generator_hosts.split(",")]
        self.bind = args.server_bind or ("127.0.0.1" if self.server.local else None)
        if not self.bind:
            raise SystemExit(
                "--server-bind (the server's private IP) is required for a remote server"
            )
        self.target = args.target_ip
        self.layout = parse_layout(args.server_layout)
        self.gen_cpus = cpu_list(args.generator_cpus)
        self.work = args.work_dir
        name = args.run_name or self.run_settings.get("profile", "matrix")
        self.out = (Path(args.results_dir) / name).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.local_work = self.out / "work"
        self.local_work.mkdir(exist_ok=True)
        self.rows_path = self.out / "metrics.json"
        self.rows = (
            json.loads(self.rows_path.read_text()) if self.rows_path.exists() else []
        )
        self.rtt_ms: Optional[float] = None
        self.manifest: Dict[str, Any] = {}

    # ---- helpers
    def wdir(self, host: Host) -> str:
        return str(self.local_work) if host.local else self.work

    def cells(self) -> List[Dict[str, Any]]:
        groups = self.cfg.get("groups_to_run")
        ids = self.cfg.get("scenario_filter")
        out = []
        for g in self.cfg["test_groups"]:
            if groups and g["group"] not in groups:
                continue
            out += [c for c in g["scenarios"] if not ids or c["id"] in ids]
        for c in out:
            if c["generator_hosts"] != len(self.gen_hosts):
                raise SystemExit(
                    f"cell {c['id']} wants {c['generator_hosts']} generator hosts, "
                    f"{len(self.gen_hosts)} given"
                )
        return out

    # ---- server control
    def stop_server(self):
        pid = f"{self.wdir(self.server)}/server-{self.port}.pid"
        self.server.run(
            f"if [ -f {pid} ]; then p=$(cat {pid}); kill $p 2>/dev/null; "
            f"for i in $(seq 50); do kill -0 $p 2>/dev/null || break; sleep 0.2; done; "
            f"kill -9 $p 2>/dev/null; rm -f {pid}; fi",
            timeout=30,
        )

    def start_server(self, binary: str, cell: Dict[str, Any]):
        w = self.wdir(self.server)
        bind = " ".join(dict.fromkeys([self.bind, "127.0.0.1"]))
        args = [
            binary, "--port", str(self.port), "--bind", bind,
            "--daemonize", "yes", "--pidfile", f"{w}/server-{self.port}.pid",
            "--logfile", f"{w}/server-{self.port}.log", "--dir", w,
            "--save", "", "--appendonly", "no",
            "--io-threads", str(cell["io_threads"]),
        ]  # fmt: skip
        if cell["cluster_mode"]:
            # announce the address clients use, or CLUSTER SLOTS gives them none
            args += ["--cluster-enabled", "yes", "--cluster-config-file", f"nodes-{self.port}.conf",
                     "--cluster-announce-ip", self.target]  # fmt: skip
        prefix = f"taskset -c {self.cpus_for(cell)} " if self.layout else ""
        self.server.check(
            f"mkdir -p {w} && rm -f {w}/nodes-{self.port}.conf && "
            + prefix
            + " ".join(shlex.quote(a) for a in args),
            timeout=30,
        )
        deadline = time.time() + 30
        while True:
            try:
                c = Conn(self.target, self.port)
                c.call("PING")
                break
            except (OSError, RuntimeError):
                if time.time() > deadline:
                    raise RuntimeError("server did not answer PING within 30 s")
                time.sleep(0.2)
        try:
            if cell["cluster_mode"]:
                c.call("CLUSTER", "ADDSLOTSRANGE", 0, 16383)
                for _ in range(100):
                    if "cluster_state:ok" in c.call("CLUSTER", "INFO"):
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("cluster state did not become ok")
        finally:
            c.close()
        return self.pin_server(cell)

    def cpus_for(self, cell) -> str:
        """Every CPU the layout gives a server with this cell's io-threads."""
        L, n = self.layout, cell["io_threads"] - 1
        io = [f"{L['io']}-{L['io'] + n - 1}"] if n > 0 else []
        return ",".join([str(L["main"])] + io + [L["other"]])

    def pin_server(self, cell) -> Dict[str, Any]:
        """Pin main and each IO thread to its own CPU; report the threads found."""
        if not self.layout:
            return {}
        pid = f"{self.wdir(self.server)}/server-{self.port}.pid"
        main, io0, other = self.layout["main"], self.layout["io"], self.layout["other"]
        script = (
            f"p=$(cat {pid}); n=0; for t in /proc/$p/task/*; do id=${{t##*/}}; c=$(cat $t/comm); "
            f'case "$c" in io_thd_*) i=${{c#io_thd_}}; cpu=$(({io0} + i - 1)); n=$((n+1));; '
            f'*) if [ "$id" = "$p" ]; then cpu={main}; else cpu={other}; fi;; esac; '
            f"taskset -pc $cpu $id >/dev/null; done; echo $n"
        )
        found = int(self.server.check(script, timeout=30).strip() or 0)
        if found != cell["io_threads"] - 1:
            raise RuntimeError(
                f"found {found} IO threads, expected {cell['io_threads'] - 1}"
            )
        return {
            "main": main,
            "io_first": io0,
            "other": other,
            "io_threads_found": found,
        }

    # ---- preload
    def preload(self, cell: Dict[str, Any]):
        gen = generators.get(cell["generator"])
        spec = dict(cell["preload"], generator=gen.name)
        digest = hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[
            :12
        ]
        path = self.local_work / f"preload-{digest}.resp"
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            write_preload(cell["preload"], gen, tmp)
            tmp.rename(path)
        with open(path, "rb") as f:
            p = subprocess.run(
                [self.args.cli, "-h", self.target, "-p", str(self.port), "--pipe"],
                stdin=f,
                capture_output=True,
                timeout=900,
            )
        out = p.stdout.decode(errors="replace")
        if p.returncode or "errors: 0" not in out:
            raise RuntimeError(
                f"preload failed: {out[-300:]} {p.stderr.decode(errors='replace')[-300:]}"
            )
        c = Conn(self.target, self.port)
        try:
            for k, v in (cell.get("server_config") or {}).items():
                c.call("CONFIG", "SET", k, v)
            keys = parse_info(c.call("INFO", "keyspace"))
        finally:
            c.close()
        return keys

    # ---- measurements
    def info(self) -> Dict[str, str]:
        c = Conn(self.target, self.port)
        try:
            return parse_info(
                c.call("INFO", "server", "clients", "stats", "commandstats")
            )
        finally:
            c.close()

    def proc_stat(self, host: Host) -> Tuple[int, int]:
        rc, out, _ = host.run("cat /proc/stat", timeout=10)
        return cpu_times(out, self.gen_cpus) if rc == 0 else (0, 0)

    def measure_rtt(self) -> Dict[str, float]:
        code = (
            "import socket,time,statistics,sys\n"
            f"s=socket.create_connection(({self.target!r},{self.port}),timeout=5)\n"
            "s.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)\nr=[]\n"
            "for _ in range(200):\n"
            "  a=time.perf_counter(); s.sendall(b'*1\\r\\n$4\\r\\nPING\\r\\n'); s.recv(64)\n"
            "  r.append((time.perf_counter()-a)*1000)\n"
            "print(round(statistics.median(r),4))\n"
        )
        out = {}
        for h in self.gen_hosts:
            out[h.target] = float(h.check("python3 -", timeout=60, stdin=code).strip())
        return out

    def host_facts(self, host: Host) -> Dict[str, str]:
        script = (
            "t=$(curl -s -m 2 -X PUT http://169.254.169.254/latest/api/token "
            "-H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null); "
            'it=$(curl -s -m 2 -H "X-aws-ec2-metadata-token: $t" '
            "http://169.254.169.254/latest/meta-data/instance-type 2>/dev/null); "
            'echo "${it:-unknown}"; uname -r; nproc'
        )
        rc, out, _ = host.run(script, timeout=20)
        lines = (out.splitlines() + ["unknown", "unknown", "0"])[:3]
        return {
            "instance_type": lines[0] or "unknown",
            "kernel": lines[1],
            "cpus": lines[2],
        }

    # ---- one rep
    def run_rep(self, cell, label, sha, binary, rep, slot) -> Dict[str, Any]:
        gen = generators.get(cell["generator"])
        row: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "commit": sha,
            "build": label,
            "run": rep + 1,
            "test_name": self.cfg.get("test_name"),
            "group": cell["group"],
            "group_description": cell["description"],
            "scenario_id": cell["id"],
            "template": cell["template"],
            "generator": cell["generator"],
            "point": cell["point"],
            "window_order_slot": slot,
            "window_rtt_ms": self.rtt_ms,
        }
        self.stop_server()
        try:
            row["window_pinning"] = self.start_server(binary, cell)
            row["window_keyspace"] = self.preload(cell)
            per_host = cell["connections"] // len(self.gen_hosts)
            threads = cell["client_threads"]
            results: Dict[str, Dict[str, Any]] = {}

            def drive(i: int, host: Host):
                run_id = f"{cell['id']}-{label}-r{rep}-g{i}"
                outfile = gen.output_file(run_id)
                outpath = f"{self.wdir(host)}/{outfile}" if outfile else None
                binary_gen = (
                    self.args.memtier
                    if gen.name == "memtier"
                    else self.args.valkey_benchmark
                )
                argv = gen.argv(
                    cell, binary=binary_gen, host=self.target, port=self.port,
                    connections=per_host, threads=threads, duration=cell["duration"],
                    cpus=self.args.generator_cpus, output=outpath,
                )  # fmt: skip
                # timeout: the generator ends by itself even if this runner dies
                cmd = (
                    f"ulimit -n 65536 2>/dev/null; timeout {cell['duration'] + 60} "
                    + " ".join(shlex.quote(a) for a in argv)
                )
                rc, out, err = host.run(cmd, timeout=cell["duration"] + 120)
                output = None
                if outpath:
                    _, output, _ = host.run(
                        f"cat {outpath}; rm -f {outpath}", timeout=30
                    )
                res = gen.parse(out, output)
                if rc or res.get("error"):
                    tail = (err.strip() or out.strip())[-300:]
                    res["error"] = f"{res.get('error') or 'failed'} (exit {rc}: {tail})"
                results[host.target] = res

            threads_ = [
                threading.Thread(target=drive, args=(i, h))
                for i, h in enumerate(self.gen_hosts)
            ]
            t0 = time.time()
            for t in threads_:
                t.start()
            w0, w1 = cell["window"]
            time.sleep(max(0.0, t0 + w0 - time.time()))
            info0, cpu0 = self.info(), {
                h.target: self.proc_stat(h) for h in self.gen_hosts
            }
            time.sleep(max(0.0, t0 + w1 - time.time()))
            info1, cpu1 = self.info(), {
                h.target: self.proc_stat(h) for h in self.gen_hosts
            }
            for t in threads_:
                t.join()
            through = window_throughput(info0, info1)
            connected = int(info1.get("connected_clients", 0)) - 1  # minus this reading
            busy = {h: busy_pct(cpu0[h], cpu1[h]) for h in cpu0}
            reasons = gate_reasons(
                self.gates, cell["connections"], connected, busy, results,
                through["server_ops_s"] * through["seconds"], through["errors"],
            )  # fmt: skip
            ok = [r for r in results.values() if not r.get("error")]
            p50s = [r["p50_ms"] for r in ok if r.get("p50_ms") is not None]

            def worst(k):
                vals = [r[k] for r in ok if r.get(k) is not None]
                return max(vals) if vals else None

            row.update(
                {
                    "server_ops_s": through["server_ops_s"],
                    "generator_ops_s": round(sum(r.get("ops_s") or 0 for r in ok), 2),
                    "p50_ms": round(statistics.mean(p50s), 4) if p50s else None,
                    "p99_ms": worst("p99_ms"),
                    "p999_ms": worst("p999_ms"),
                    "max_ms": worst("max_ms"),
                    "window_ops_by_command": through["by_command"],
                    "window_seconds": through["seconds"],
                    "window_error_replies": through["errors"],
                    "window_connected_clients": connected,
                    "window_planned_connections": cell["connections"],
                    "window_client_busy_pct": max(
                        (b for b in busy.values() if b is not None), default=None
                    ),
                    # a generator failure is a harness error, rerun on resume
                    "status": (
                        "error"
                        if any(r.get("error") for r in results.values())
                        else ("invalid" if reasons else "ok")
                    ),
                    "error": "; ".join(reasons) if reasons else None,
                }
            )
            if rtt_bound(self.gates, self.rtt_ms, row["p50_ms"]):
                row["window_flags"] = ["rtt_bound"]
        except Exception as e:  # noqa: BLE001 - a failed rep is recorded, not fatal
            row.update(
                {
                    "status": "error",
                    "error": f"{type(e).__name__}: {e}"[:1000],
                    "server_ops_s": None,
                }
            )
        finally:
            self.stop_server()
        return row

    # ---- the run
    def save(self):
        tmp = self.rows_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.rows, indent=1) + "\n")
        tmp.rename(self.rows_path)

    def done(self, cell_id, label, rep) -> bool:
        return any(
            r["scenario_id"] == cell_id and r["build"] == label and r["run"] == rep + 1
            and r.get("status") in ("ok", "invalid")
            for r in self.rows
        )  # fmt: skip

    def log(self, msg: str):
        logging.info(msg)
        with open(self.out / "runner.log", "a") as f:
            f.write(time.strftime("%H:%M:%S ") + msg + "\n")

    def preflight(self, cells):
        shapes = self.run_settings.get("instance_shapes") or {}
        facts = {"server": self.host_facts(self.server)}
        facts.update(
            {f"generator:{h.target}": self.host_facts(h) for h in self.gen_hosts}
        )
        for role, fact in facts.items():
            want = shapes.get(role.split(":")[0])
            if want and fact["instance_type"] != want:
                raise SystemExit(
                    f"{role} is {fact['instance_type']}, the profile expects {want}"
                )
        sums = {}
        for label, sha, binary in self.builds:
            sums[label] = self.server.check(f"sha256sum {shlex.quote(binary)}").split()[
                0
            ]
        _, mt, _ = self.gen_hosts[0].run(
            f"{shlex.quote(self.args.memtier)} --version 2>&1 | head -1"
        )
        self.start_server(self.builds[0][2], cells[0])
        try:
            rtts = self.measure_rtt()
        finally:
            self.stop_server()
        self.rtt_ms = max(rtts.values())
        self.manifest = {
            "started": datetime.now(timezone.utc).isoformat(),
            "profile": self.run_settings.get("profile"),
            "reps": self.reps,
            "builds": [
                {"label": l, "sha": s, "binary_sha256": sums[l]}
                for l, s, _ in self.builds
            ],
            "hosts": facts,
            "rtt_ms": rtts,
            "memtier": mt.strip(),
            "server_layout": self.args.server_layout,
            "generator_cpus": self.args.generator_cpus,
            "gates": self.gates,
            "verdict_rules": self.rules,
            "cells": len(cells),
        }
        (self.out / "manifest.json").write_text(
            json.dumps(self.manifest, indent=1) + "\n"
        )
        gate = self.gates.get("rtt_gate_ms")
        if gate is not None and self.rtt_ms > gate:
            raise SystemExit(
                f"median PING round trip {self.rtt_ms} ms exceeds rtt_gate_ms {gate}"
            )
        self.log(
            f"preflight ok: {facts['server']['instance_type']} server, rtt {rtts}, builds {sums}"
        )

    def run(self) -> Dict[str, Any]:
        cells = self.cells()
        (self.out / "cells.json").write_text(json.dumps(cells, indent=1) + "\n")
        self.preflight(cells)
        total = len(cells) * self.reps * len(self.builds)
        self.log(
            f"{len(cells)} cells x {self.reps} reps x {len(self.builds)} builds = {total} reps"
        )
        n = 0
        for cell in cells:
            for rep in range(self.reps):
                k = rep % len(self.builds)
                order = self.builds[k:] + self.builds[:k]
                for slot, (label, sha, binary) in enumerate(order):
                    n += 1
                    if self.done(cell["id"], label, rep):
                        continue
                    if (self.out / "STOP").exists():
                        self.log("STOP file found, stopping")
                        return self.summarize(cells)
                    row = self.run_rep(cell, label, sha, binary, rep, slot)
                    self.rows = [
                        r for r in self.rows
                        if not (r["scenario_id"] == cell["id"] and r["build"] == label and r["run"] == rep + 1)
                    ] + [row]  # fmt: skip
                    self.save()
                    ops = row.get("server_ops_s")
                    self.log(
                        f"[{n}/{total}] {cell['id']} {label} r{rep + 1}: "
                        f"{(ops or 0) / 1e3:.1f}k ops/s p99 {row.get('p99_ms')} ms "
                        f"busy {row.get('window_client_busy_pct')}% {row['status']} {row.get('error') or ''}"
                    )
        summary = self.summarize(cells)
        self.log("ALL_DONE")
        return summary

    def summarize(self, cells) -> Dict[str, Any]:
        labels = [b[0] for b in self.builds]
        out = {"builds": labels, "cells": []}
        for cell in cells:
            entry = {
                "id": cell["id"],
                "template": cell["template"],
                "point": cell["point"],
                "builds": {},
            }
            reasons = {}
            for label in labels:
                rows = [
                    r
                    for r in self.rows
                    if r["scenario_id"] == cell["id"] and r["build"] == label
                ]
                valid = [r["server_ops_s"] for r in rows if r.get("status") == "ok"]
                reasons[label] = [
                    r.get("error") or "" for r in rows if r.get("status") != "ok"
                ]
                entry["builds"][label] = {
                    "reps": len(rows),
                    "valid": len(valid),
                    "median_ops_s": statistics.median(valid) if valid else None,
                    "cv_pct": round(_cv(valid), 2) if valid else None,
                    "range": [min(valid), max(valid)] if valid else None,
                }
            if len(labels) >= 2:
                b, c = labels[0], labels[1]
                ops = lambda l: [r["server_ops_s"] for r in self.rows  # noqa: E731
                                 if r["scenario_id"] == cell["id"] and r["build"] == l and r.get("status") == "ok"]  # fmt: skip
                entry["compare"] = verdict(
                    b, c, ops(b), ops(c), self.rules, reasons[b], reasons[c]
                )
            out["cells"].append(entry)
        (self.out / "summary.json").write_text(json.dumps(out, indent=1) + "\n")
        return out


def parse_build(text: str) -> Tuple[str, str, str]:
    label, _, rest = text.partition("=")
    sha, _, path = rest.partition(":")
    if not (label and sha and path):
        raise SystemExit(f"--build {text!r}: expected label=sha:path")
    return label, sha, path


def parse_layout(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """ "main=1;io=2;other=67-79": main on CPU 1, IO thread i on CPU 2+i-1,
    every other server thread on 67-79."""
    if not text:
        return None
    parts = dict(p.split("=", 1) for p in text.split(";") if p)
    return {"main": int(parts["main"]), "io": int(parts["io"]), "other": parts["other"]}


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--build",
        action="append",
        default=[],
        help="label=sha:path to valkey-server on the server host; the first is the baseline",
    )
    p.add_argument(
        "--server-host", default="local", help="ssh target of the server host, or local"
    )
    p.add_argument(
        "--server-bind",
        default=None,
        help="address the server binds besides loopback (its private IP)",
    )
    p.add_argument(
        "--generator-hosts",
        default="local",
        help="comma-separated ssh targets, or local",
    )
    p.add_argument(
        "--server-layout",
        default=None,
        help="main=<cpu>;io=<first io cpu>;other=<cpu list>",
    )
    p.add_argument(
        "--generator-cpus",
        default=None,
        help="CPU list for the generator on each generator host",
    )
    p.add_argument("--memtier", default="memtier_benchmark")
    p.add_argument("--valkey-benchmark", default="valkey-benchmark")
    p.add_argument(
        "--cli", default="valkey-cli", help="valkey-cli on this host, for the preload"
    )
    p.add_argument(
        "--work-dir",
        default="/tmp/vpb-matrix",
        help="scratch directory on remote hosts",
    )
    p.add_argument(
        "--run-name",
        default=None,
        help="results subdirectory; defaults to the profile name",
    )
    p.add_argument("--reps", type=int, default=None, help="override the profile's reps")


def run_configs(configs: List[Dict[str, Any]], args: argparse.Namespace) -> None:
    for cfg in configs:
        if "matrix_run" in cfg:
            Runner(cfg, args).run()


def main() -> None:
    import config_loader

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", required=True)
    p.add_argument(
        "--profile", default=None, help="profile name or path, overriding the config's"
    )
    p.add_argument("--overrides", default="{}", help="JSON merged into the config")
    p.add_argument("--target-ip", default="127.0.0.1")
    p.add_argument("--results-dir", default="results")
    p.add_argument("--scenarios", default=None, help="comma-separated cell ids")
    add_arguments(p)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    configs = config_loader.load(
        args.config, profile=args.profile, overrides=json.loads(args.overrides)
    )
    if args.scenarios:
        for cfg in configs:
            cfg["scenario_filter"] = {s.strip() for s in args.scenarios.split(",")}
    run_configs(configs, args)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
