"""Measured runs: server-side throughput over a fixed window, saturation and
validity checks, per-thread CPU pinning and optional perf counters.

A measured run starts the generator, waits until every client is connected and
commands are flowing, lets the load settle, then reads the server's own
counters at both edges of a fixed window. Throughput is the server's
``total_commands_processed`` delta over that window, and main-thread busy time
is the ``used_active_time_main_thread`` delta, so neither depends on what the
generator reports. A run is marked invalid, with reasons, when the generator
errored, fell short of its connections, ended inside the window, missed its
target rate, the server logged an assertion, or (opt-in) the main thread was
not saturated.
"""

import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from generators import GeneratorError, GeneratorRequest, LoadGenerator
from utils.cpu_utils import parse_core_range

CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PERF_EVENTS = (
    "cycles:u,instructions:u,L1-icache-load-misses:u,"
    "L1-dcache-load-misses:u,branch-misses:u"
)
ASSERT_PATTERN = re.compile(r"crashed by|ASSERTION|=== ASSERTION", re.I)

DEFAULTS = {
    "window": 6,
    "settle": 3,
    "ready_timeout": 60,
    "rate_tolerance_pct": 5.0,
    "perf_counters": False,
}

from utils.measured_fields import MEASURED_FIELDS  # noqa: E402


def measurement_options(cfg: dict) -> Optional[dict]:
    """Merged ``measurement`` options, or None when the config has none."""
    m = cfg.get("measurement")
    if not m:
        return None
    return {**DEFAULTS, **(m if isinstance(m, dict) else {})}


def validate_measurement(value) -> None:
    if isinstance(value, bool):
        return
    if not isinstance(value, dict):
        raise ValueError("'measurement' must be a boolean or an object")
    known = set(DEFAULTS) | {"require_main_busy_pct", "acl"}
    unknown = sorted(set(value) - known)
    if unknown:
        raise ValueError(f"'measurement' does not support key(s): {unknown}")
    for key in ("window", "settle", "ready_timeout"):
        if key in value and (not isinstance(value[key], int) or value[key] <= 0):
            raise ValueError(f"'measurement.{key}' must be a positive integer")
    if "require_main_busy_pct" in value:
        v = value["require_main_busy_pct"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v <= 100:
            raise ValueError("'measurement.require_main_busy_pct' must be in (0, 100]")
    if "acl" in value:
        acl = value["acl"]
        if not isinstance(acl, dict) or not acl.get("user") or not acl.get("password"):
            raise ValueError("'measurement.acl' needs 'user' and 'password'")


def validate_thread_pinning(value) -> None:
    if not isinstance(value, dict) or "main" not in value:
        raise ValueError("'thread_pinning' must be an object with 'main'")
    unknown = sorted(set(value) - {"main", "io_threads", "other"})
    if unknown:
        raise ValueError(f"'thread_pinning' does not support key(s): {unknown}")
    for key in ("main", "io_threads", "other"):
        if key in value:
            parse_core_range(str(value[key]))


# ---------- /proc helpers (local server only) --------------------------------


def thread_names(pid: int) -> Dict[int, str]:
    out = {}
    for tid in os.listdir(f"/proc/{pid}/task"):
        try:
            out[int(tid)] = Path(f"/proc/{pid}/task/{tid}/comm").read_text().strip()
        except OSError:
            pass
    return out


def thread_ticks(pid: int, tid: Optional[int] = None) -> float:
    path = f"/proc/{pid}/task/{tid or pid}/stat" if tid else f"/proc/{pid}/stat"
    fields = Path(path).read_text().rsplit(")", 1)[1].split()
    return float(fields[11]) + float(fields[12])


def _io_thread_index(name: str) -> Optional[int]:
    m = re.match(r"io_thd_(\d+)$", name)
    return int(m.group(1)) if m else None


def pin_server_threads(pid: int, spec: dict, io_threads: Optional[int]) -> Dict:
    """Pin main and each IO thread to its own CPU; returns thread -> cpus.

    ``io_threads`` lists CPUs handed out one per IO thread in thread order;
    with fewer CPUs than threads the threads share the whole list. ``other``
    (bio, jemalloc) defaults to every listed CPU outside main and IO.
    """
    main = sorted(parse_core_range(str(spec["main"])))
    io = (
        sorted(parse_core_range(str(spec["io_threads"])))
        if "io_threads" in spec
        else []
    )
    names = thread_names(pid)
    ios = sorted(
        (_io_thread_index(n), tid)
        for tid, n in names.items()
        if _io_thread_index(n) is not None
    )
    expected = (io_threads or 1) - 1
    if len(ios) != expected:
        raise RuntimeError(f"pinning: expected {expected} io threads, found {len(ios)}")
    if ios and not io:
        raise RuntimeError("pinning: server has io threads but no 'io_threads' cpus")
    want = {pid: set(main)}
    for k, (idx, tid) in enumerate(ios):
        want[tid] = {io[k]} if len(io) >= len(ios) else set(io)
    if "other" in spec:
        other = set(parse_core_range(str(spec["other"])))
        for tid, n in names.items():
            if tid not in want:
                want[tid] = other
    for tid, cpus in want.items():
        os.sched_setaffinity(tid, cpus)
    layout = {}
    for tid, cpus in want.items():
        got = os.sched_getaffinity(tid)
        if got != cpus:
            raise RuntimeError(
                f"pinning: thread {tid} on {sorted(got)}, wanted {sorted(cpus)}"
            )
        layout[names.get(tid, str(tid)) if tid != pid else "main"] = ",".join(
            map(str, sorted(cpus))
        )
    return layout


def pinning_warnings() -> List[str]:
    """Host conditions that let interrupts land on pinned cores."""
    warnings = []
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "irqbalance"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.stdout.strip() == "active":
            warnings.append("irqbalance active")
    except Exception:
        pass
    return warnings


def parse_perf_stat(text: str, cmds: float) -> Dict[str, Optional[float]]:
    def ev(name: str) -> Optional[float]:
        m = re.search(rf"^\s*([\d,]+)\s+{re.escape(name)}", text, re.M)
        return float(m.group(1).replace(",", "")) if m else None

    cyc, ins = ev("cycles:u"), ev("instructions:u")
    l1i, l1d = ev("L1-icache-load-misses:u"), ev("L1-dcache-load-misses:u")
    br = ev("branch-misses:u")

    def per(v):
        return v / cmds if v is not None and cmds else None

    return {
        "cyc_per_cmd": per(cyc),
        "ins_per_cmd": per(ins),
        "ipc": ins / cyc if ins and cyc else None,
        "l1i_miss_per_cmd": per(l1i),
        "l1d_miss_per_cmd": per(l1d),
        "br_miss_per_cmd": per(br),
    }


# ---------- server probe -----------------------------------------------------


class ServerProbe:
    """Reads the counters a measured run needs from one server."""

    def __init__(self, client_factory: Callable, local: bool):
        self.client_factory = client_factory
        self.local = local
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = self.client_factory()
        return self._client

    def reset(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None

    def info(self) -> dict:
        return self.client.info("everything")

    def snapshot(self) -> dict:
        t0 = time.time()
        info = self.info()
        t1 = time.time()
        return {
            "t": (t0 + t1) / 2,
            "cmds": int(info.get("total_commands_processed", 0)),
            "active_main_s": info.get("used_active_time_main_thread"),
            "clients": int(info.get("connected_clients", 0)),
            "pid": int(info.get("process_id", 0)),
            "keys": sum(
                v.get("keys", 0)
                for k, v in info.items()
                if k.startswith("db") and isinstance(v, dict)
            ),
        }


def apply_acl(client, acl: dict) -> None:
    """Create the bench user with a per-key pattern walk, then disable default.

    ``~*`` is short-circuited by the server, so the default patterns force a
    real key-pattern check on every command.
    """
    keys = acl.get("keys", ["~x:*", "~?*"])
    client.execute_command(
        "ACL", "SETUSER", acl["user"], "on", f">{acl['password']}", *keys, "&*", "+@all"
    )
    client.execute_command("ACL", "SETUSER", "default", "off")


def restore_default_user(client) -> None:
    client.execute_command("ACL", "SETUSER", "default", "on")


# ---------- measured run -----------------------------------------------------


def _server_asserts(client) -> Optional[int]:
    try:
        log = client.config_get("logfile").get("logfile")
        if not log or not Path(log).exists():
            return None
        return len(ASSERT_PATTERN.findall(Path(log).read_text(errors="replace")))
    except Exception:
        return None


def _stderr_tail(path: Path) -> str:
    try:
        lines = [l for l in path.read_text(errors="replace").splitlines() if l.strip()]
        return lines[-1] if lines else ""
    except OSError:
        return ""


def evaluate_validity(row: dict, opts: dict, scenario: dict) -> List[str]:
    reasons = []
    if row.get("generator_ended_early"):
        reasons.append("generator ended inside the window")
    if row.get("generator_errors"):
        reasons.append(f"generator errors: {row['generator_errors']}")
    if row.get("generator_timeouts"):
        reasons.append(f"generator timeouts: {row['generator_timeouts']}")
    expected = row.get("expected_clients")
    if expected and (row.get("connected_clients") or 0) < expected:
        reasons.append(f"clients {row.get('connected_clients')} < {expected}")
    if row.get("server_asserts"):
        reasons.append(f"server asserts: {row['server_asserts']}")
    if scenario.get("rate"):
        rate, got = float(scenario["rate"]), float(row.get("server_ops_s") or 0)
        if abs(got - rate) / rate * 100.0 > opts["rate_tolerance_pct"]:
            reasons.append(f"served {got:.0f}/s against a {rate:.0f}/s target")
    need = opts.get("require_main_busy_pct")
    if need and not scenario.get("rate"):
        busy = row.get("main_busy_pct")
        if busy is None:
            reasons.append("main busy % unknown")
        elif busy < need:
            reasons.append(f"main {busy:.1f}% busy < {need}%")
    return reasons


def run_measured(
    generator: LoadGenerator,
    req: GeneratorRequest,
    probe: ServerProbe,
    opts: dict,
    *,
    expect_keys: int = 0,
    acl: Optional[dict] = None,
    admin_client_factory: Optional[Callable] = None,
) -> Optional[dict]:
    """Run one scenario under the window protocol; returns row and extra fields."""
    window, settle = opts["window"], opts["settle"]
    expected_clients = generator.expected_connections(req.scenario)
    if generator.stops_at_window_edge:
        req.duration_s = opts["ready_timeout"] + settle + window + 30
    else:
        req.duration_s = 2 + settle + window + 2

    admin = admin_client_factory() if (acl and admin_client_factory) else None
    if acl:
        apply_acl(admin, acl)
        probe.reset()
    try:
        return _run_window(
            generator, req, probe, opts, expected_clients, expect_keys, window, settle
        )
    finally:
        if acl:
            try:
                restore_default_user(probe.client)
            finally:
                probe.reset()
                admin.close()


def _run_window(
    generator, req, probe, opts, expected_clients, expect_keys, window, settle
):
    handle = generator.start(req)
    try:
        deadline = time.time() + opts["ready_timeout"]
        last_cmds, snap = None, None
        while True:
            time.sleep(1)
            if not handle.alive():
                raise GeneratorError(
                    f"{generator.name} exited before the load was up: "
                    f"{_stderr_tail(handle.stderr_path)}"
                )
            snap = probe.snapshot()
            flowing = last_cmds is not None and snap["cmds"] > last_cmds + 100
            last_cmds = snap["cmds"]
            # The probe's own connection counts in connected_clients.
            if (
                snap["clients"] - 1 >= expected_clients
                and snap["keys"] >= expect_keys
                and flowing
            ):
                break
            if time.time() > deadline:
                raise GeneratorError(
                    f"load never came up: clients={snap['clients'] - 1}/{expected_clients} "
                    f"keys={snap['keys']}/{expect_keys}"
                )
        time.sleep(settle)

        pid = snap["pid"]
        local = probe.local and pid and Path(f"/proc/{pid}").exists()
        gen_pid = handle.proc.pid

        def ticks():
            if not local:
                return None
            names = thread_names(pid)
            t = {"main": thread_ticks(pid, pid)}
            t["io"] = sum(
                thread_ticks(pid, tid)
                for tid, n in names.items()
                if _io_thread_index(n) is not None
            )
            try:
                t["gen"] = thread_ticks(gen_pid)
            except OSError:
                t["gen"] = None
            return t

        a, ta = probe.snapshot(), ticks()
        perf_text, perf_status = "", "off"
        if opts.get("perf_counters"):
            if not local:
                perf_status = "unavailable: server not local"
            elif not shutil.which("perf"):
                perf_status = "unavailable: perf not installed"
            else:
                r = subprocess.run(
                    [
                        "perf",
                        "stat",
                        "-e",
                        PERF_EVENTS,
                        "-t",
                        str(pid),
                        "--",
                        "sleep",
                        str(window),
                    ],
                    capture_output=True,
                    text=True,
                )
                perf_text, perf_status = r.stderr, (
                    "ok" if r.returncode == 0 else "failed"
                )
        if not perf_text:
            time.sleep(window)
        b, tb = probe.snapshot(), ticks()
        ended_early = not handle.alive()

        if generator.stops_at_window_edge:
            time.sleep(1.2)  # let the last in-window sample flush
            generator.stop(handle)
        else:
            generator.wait(handle, timeout_s=req.duration_s + 30)
        asserts = _server_asserts(probe.client)
    except BaseException:
        generator.stop(handle)
        raise

    result = generator.parse(handle, (a["t"], b["t"]))
    if result is None:
        logging.error(f"{generator.name} produced no parsable output")
        return None

    span = b["t"] - a["t"]
    cmds = b["cmds"] - a["cmds"]
    extra = {
        "generator": generator.name,
        "generator_version": generator.version(),
        "generator_argv": " ".join(handle.argv),
        "server_ops_s": cmds / span,
        "window_s": round(span, 3),
        "connected_clients": b["clients"] - 1,
        "expected_clients": expected_clients,
        "server_asserts": asserts,
        "generator_ended_early": ended_early,
        "perf_status": perf_status,
        **result.extra,
    }
    if a["active_main_s"] is not None and b["active_main_s"] is not None:
        extra["main_busy_pct"] = (b["active_main_s"] - a["active_main_s"]) / span * 100
        extra["main_busy_source"] = "used_active_time_main_thread"
    if ta and tb:
        extra["main_cpu_pct"] = (tb["main"] - ta["main"]) / CLK_TCK / span * 100
        extra["io_threads_cores"] = (tb["io"] - ta["io"]) / CLK_TCK / span
        if ta["gen"] is not None and tb["gen"] is not None:
            extra["generator_cores"] = (tb["gen"] - ta["gen"]) / CLK_TCK / span
        if "main_busy_pct" not in extra:
            extra["main_busy_pct"] = extra["main_cpu_pct"]
            extra["main_busy_source"] = "main thread cpu"
    if perf_text:
        extra.update(parse_perf_stat(perf_text, cmds))
    return {"row": result.row, "extra": extra, "stdout": result.stdout}


def finalize_row(metrics: dict, extra: dict, opts: dict, scenario: dict) -> dict:
    """Merge measured fields into a metrics row and stamp validity."""
    metrics.update(extra)
    reasons = evaluate_validity(metrics, opts, scenario)
    metrics.pop("expected_clients", None)
    metrics.pop("generator_ended_early", None)
    metrics["valid"] = not reasons
    metrics["invalid_reasons"] = "; ".join(reasons)
    return metrics
