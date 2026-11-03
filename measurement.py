"""Measured runs: server-side throughput over a fixed window, saturation and
validity checks and optional perf counters.

A measured run starts the generator, waits until every client is connected and
commands are flowing, lets the load settle, then reads the server's own
counters at both edges of a fixed window. Throughput is the server's
``total_commands_processed`` delta over that window, and main-thread busy time
is the ``used_active_time_main_thread`` delta, so neither depends on what the
generator reports. A run is marked invalid, with reasons, when the generator
errored, fell short of its connections, ended inside the window, missed its
target rate, the server logged an assertion, or (opt-in) the main thread was
not saturated.

Optionally ``perf stat`` counts cycles, instructions and cache and branch
misses of the server over the window, reported per command.
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
    # Disturbance limits; None turns a check off.
    "max_main_runq_wait_pct": 5.0,
    "max_steal_pct": 2.0,
}


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
    for key in ("max_main_runq_wait_pct", "max_steal_pct"):
        v = value.get(key)
        if v is not None and (
            isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0
        ):
            raise ValueError(f"'measurement.{key}' must be a non-negative number")
    if "require_main_busy_pct" in value:
        v = value["require_main_busy_pct"]
        levels = v.values() if isinstance(v, dict) else [v]
        if (isinstance(v, dict) and not v) or not all(
            not isinstance(x, bool) and isinstance(x, (int, float)) and 0 < x <= 100
            for x in levels
        ):
            raise ValueError(
                "'measurement.require_main_busy_pct' must be a number in (0, 100] "
                "or an object mapping server version prefixes (and 'default') to one"
            )
    if "acl" in value:
        acl = value["acl"]
        if not isinstance(acl, dict) or not acl.get("user") or not acl.get("password"):
            raise ValueError("'measurement.acl' needs 'user' and 'password'")


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


def thread_sched(pid: int, tid: int) -> Dict[str, float]:
    """Run-queue wait (ns) and involuntary context switches of one thread."""
    base = f"/proc/{pid}/task/{tid}"
    wait_ns = float(Path(f"{base}/schedstat").read_text().split()[1])
    nvcsw = 0.0
    for line in Path(f"{base}/status").read_text().splitlines():
        if line.startswith("nonvoluntary_ctxt_switches:"):
            nvcsw = float(line.split()[1])
    return {"wait_ns": wait_ns, "nvcsw": nvcsw}


def cpu_steal(cpus: Optional[set] = None) -> Optional[tuple]:
    """(steal, total) jiffies summed over ``cpus`` (all CPUs when None)."""
    steal = total = 0.0
    found = False
    for line in Path("/proc/stat").read_text().splitlines():
        name, *fields = line.split()
        if not name.startswith("cpu") or name == "cpu":
            continue
        if cpus is not None and int(name[3:]) not in cpus:
            continue
        vals = [float(v) for v in fields[:8]]
        steal += vals[7]
        total += sum(vals)
        found = True
    return (steal, total) if found else None


def io_thread_index(name: str) -> Optional[int]:
    m = re.match(r"io_thd_(\d+)$", name)
    return int(m.group(1)) if m else None


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
    """Reads the counters a measured run needs from one server or every node
    of a cluster. A snapshot sums commands, clients and keys over the nodes
    and keeps main-thread figures per node."""

    def __init__(self, client_factory, local: bool):
        factories = (
            client_factory if isinstance(client_factory, list) else [client_factory]
        )
        self.factories = factories
        self.local = local
        self._clients = [None] * len(factories)

    def clients(self) -> list:
        for i, factory in enumerate(self.factories):
            if self._clients[i] is None:
                self._clients[i] = factory()
        return self._clients

    @property
    def client(self):
        """The first node's client."""
        return self.clients()[0]

    def reset(self):
        for c in self._clients:
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass
        self._clients = [None] * len(self.factories)

    def snapshot(self) -> dict:
        t0 = time.time()
        infos = [c.info("everything") for c in self.clients()]
        t1 = time.time()
        actives = [info.get("used_active_time_main_thread") for info in infos]
        return {
            "t": (t0 + t1) / 2,
            "cmds": sum(int(i.get("total_commands_processed", 0)) for i in infos),
            "active_main_s": None if None in actives else actives,
            # Each node counts the probe's own connection.
            "clients": sum(int(i.get("connected_clients", 0)) - 1 for i in infos),
            "pids": [int(i.get("process_id", 0)) for i in infos],
            "version": infos[0].get("valkey_version") or infos[0].get("redis_version"),
            "keys": sum(
                v.get("keys", 0)
                for info in infos
                for k, v in info.items()
                if k.startswith("db") and isinstance(v, dict)
            ),
        }


# ---------- ACL user -----------------------------------------------------------


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


def _server_asserts(clients) -> Optional[int]:
    found = None
    for client in clients:
        try:
            log = client.config_get("logfile").get("logfile")
            if log and Path(log).exists():
                text = Path(log).read_text(errors="replace")
                found = (found or 0) + len(ASSERT_PATTERN.findall(text))
        except Exception:
            pass
    return found


def _stderr_tail(path: Path) -> str:
    try:
        lines = [l for l in path.read_text(errors="replace").splitlines() if l.strip()]
        return lines[-1] if lines else ""
    except OSError:
        return ""


def busy_threshold(setting, version: Optional[str]) -> Optional[float]:
    """The saturation gate's threshold for a server version.

    ``setting`` is a number, or an object whose keys are version prefixes
    ("9.0", "255." for unstable) plus an optional "default"; the longest
    prefix of ``version`` wins.
    """
    if not isinstance(setting, dict):
        return setting
    matches = [
        k for k in setting if k != "default" and version and version.startswith(k)
    ]
    if matches:
        return setting[max(matches, key=len)]
    return setting.get("default")


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
    wait_cap = opts.get("max_main_runq_wait_pct")
    wait = row.get("main_runq_wait_pct")
    if wait_cap is not None and wait is not None and wait > wait_cap:
        reasons.append(
            f"main thread waited for a CPU {wait:.1f}% of the window "
            f"(> {wait_cap}%): disturbed"
        )
    steal_cap = opts.get("max_steal_pct")
    steal = row.get("steal_pct")
    if steal_cap is not None and steal is not None and steal > steal_cap:
        reasons.append(f"CPU steal {steal:.1f}% > {steal_cap}%")
    need = busy_threshold(opts.get("require_main_busy_pct"), row.get("server_version"))
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
    admin_client_factory=None,
) -> Optional[dict]:
    """Run one generator under the window protocol; returns row and extra.

    See ``run_measured_parts``, which this wraps for a single generator.
    """
    out = run_measured_parts(
        [(generator, req)],
        probe,
        opts,
        expect_keys=expect_keys,
        admin_client_factory=admin_client_factory,
    )
    part = out["parts"][0]
    if part is None:
        return None
    return {"row": part["row"], "extra": {**out["extra"], **part["extra"]}}


def run_measured_parts(
    parts: List[tuple],
    probe: ServerProbe,
    opts: dict,
    *,
    expect_keys: int = 0,
    admin_client_factory=None,
) -> dict:
    """Run several generators against the server together under one window.

    ``parts`` is a list of (generator, request): the writers and readers of a
    mixed scenario, or one generator per cluster node. Returns the fields
    shared by every part (server counters over the window) under ``extra`` and,
    per part, its parsed row and its own fields under ``parts`` (None for a
    part whose output could not be parsed).

    With ``opts["acl"]`` the bench user is created through
    ``admin_client_factory`` (one default-user client factory, or a list with
    one per node) before the run, and the default user is re-enabled
    afterwards through the probe, which authenticates as the bench user.
    """
    window, settle = opts["window"], opts["settle"]
    for generator, req in parts:
        if generator.stops_at_window_edge:
            req.duration_s = opts["ready_timeout"] + settle + window + 30
        else:
            req.duration_s = 2 + settle + window + 2
    acl = opts.get("acl")
    if acl:
        factories = (
            admin_client_factory
            if isinstance(admin_client_factory, list)
            else [admin_client_factory]
        )
        for factory in factories:
            admin = factory()
            try:
                apply_acl(admin, acl)
            finally:
                admin.close()
    probe.reset()
    try:
        return _run_window(parts, probe, opts, expect_keys, window, settle)
    finally:
        try:
            if acl:
                for client in probe.clients():
                    restore_default_user(client)
        finally:
            probe.reset()


def _local_pids(probe, pids) -> list:
    if not probe.local:
        return []
    return [p for p in pids if p and Path(f"/proc/{p}").exists()]


def _ticks(pids, gen_pids, main_cpus) -> dict:
    """CPU ticks and scheduler counters of every server and generator."""
    t = {"main": {}, "sched": {}, "io": 0.0, "gen": 0.0}
    for pid in pids:
        names = thread_names(pid)
        t["main"][pid] = thread_ticks(pid, pid)
        t["io"] += sum(
            thread_ticks(pid, tid)
            for tid, n in names.items()
            if io_thread_index(n) is not None
        )
        try:
            t["sched"][pid] = thread_sched(pid, pid)
        except (OSError, IndexError, ValueError):
            pass
    for gpid in gen_pids:
        try:
            t["gen"] += thread_ticks(gpid)
        except OSError:
            t["gen"] = None
            break
    t["steal"] = cpu_steal(main_cpus)
    return t


def _main_cpus(pids) -> Optional[set]:
    """CPUs the main threads are confined to, or None when not confined."""
    cpus = set()
    for pid in pids:
        try:
            affinity = os.sched_getaffinity(pid)
        except OSError:
            return None
        if len(affinity) >= (os.cpu_count() or 0):
            return None
        cpus |= affinity
    return cpus or None


def _run_window(parts, probe, opts, expect_keys, window, settle):
    handles = []
    try:
        for generator, req in parts:
            handles.append(generator.start(req))
        expected_clients = sum(g.expected_connections(req.scenario) for g, req in parts)
        deadline = time.time() + opts["ready_timeout"]
        last_cmds, snap = None, None
        while True:
            time.sleep(1)
            for (generator, _), handle in zip(parts, handles):
                if not handle.alive():
                    raise GeneratorError(
                        f"{generator.name} exited before the load was up: "
                        f"{_stderr_tail(handle.stderr_path)}"
                    )
            snap = probe.snapshot()
            flowing = last_cmds is not None and snap["cmds"] > last_cmds + 100
            last_cmds = snap["cmds"]
            if (
                snap["clients"] >= expected_clients
                and snap["keys"] >= expect_keys
                and flowing
            ):
                break
            if time.time() > deadline:
                raise GeneratorError(
                    f"load never came up: clients={snap['clients']}/{expected_clients} "
                    f"keys={snap['keys']}/{expect_keys}"
                )
        time.sleep(settle)

        pids = _local_pids(probe, snap["pids"])
        gen_pids = [h.proc.pid for h in handles]
        main_cpus = _main_cpus(pids) if pids else None

        def ticks():
            return _ticks(pids, gen_pids, main_cpus) if pids else None

        a, ta = probe.snapshot(), ticks()
        perf_text, perf_status = "", "off"
        if opts.get("perf_counters"):
            if not pids:
                perf_status = "unavailable: server not local"
            elif len(pids) > 1:
                perf_status = "unavailable: more than one server"
            elif not shutil.which("perf"):
                perf_status = "unavailable: perf not installed"
            else:
                r = subprocess.run(
                    ["perf", "stat", "-e", PERF_EVENTS, "-t", str(pids[0]), "--"]
                    + ["sleep", str(window)],
                    capture_output=True,
                    text=True,
                )
                perf_text = r.stderr
                perf_status = "ok" if r.returncode == 0 else "failed"
        if not perf_text:
            time.sleep(window)
        b, tb = probe.snapshot(), ticks()
        ended_early = [not h.alive() for h in handles]

        if any(g.stops_at_window_edge for g, _ in parts):
            time.sleep(1.2)  # let the last in-window sample flush
        for (generator, req), handle in zip(parts, handles):
            if generator.stops_at_window_edge:
                generator.stop(handle)
            else:
                generator.wait(handle, timeout_s=req.duration_s + 30)
        asserts = _server_asserts(probe.clients())
    except BaseException:
        for (generator, _), handle in zip(parts, handles):
            generator.stop(handle)
        raise

    span = b["t"] - a["t"]
    cmds = b["cmds"] - a["cmds"]
    shared = {
        "server_ops_s": cmds / span,
        "window_s": round(span, 3),
        "connected_clients": b["clients"],
        "expected_clients": expected_clients,
        "server_asserts": asserts,
        "server_version": b.get("version"),
        "perf_status": perf_status,
    }
    if len(snap["pids"]) > 1:
        shared["server_nodes"] = len(snap["pids"])
    # For a cluster, main-thread figures are the least busy node's (every
    # node must be saturated) and the worst disturbance of any node.
    if a["active_main_s"] is not None and b["active_main_s"] is not None:
        busy = [
            (y - x) / span * 100 for x, y in zip(a["active_main_s"], b["active_main_s"])
        ]
        shared["main_busy_pct"] = min(busy)
        shared["main_busy_source"] = "used_active_time_main_thread"
        if len(busy) > 1:
            shared["main_busy_pct_max"] = max(busy)
    if ta and tb:
        cpu = [(tb["main"][p] - ta["main"][p]) / CLK_TCK / span * 100 for p in pids]
        shared["main_cpu_pct"] = min(cpu)
        shared["io_threads_cores"] = (tb["io"] - ta["io"]) / CLK_TCK / span
        if ta["gen"] is not None and tb["gen"] is not None:
            shared["generator_cores"] = (tb["gen"] - ta["gen"]) / CLK_TCK / span
        if "main_busy_pct" not in shared:
            shared["main_busy_pct"] = shared["main_cpu_pct"]
            shared["main_busy_source"] = "main thread cpu"
        sched = [p for p in pids if p in ta["sched"] and p in tb["sched"]]
        if sched:
            shared["main_runq_wait_pct"] = max(
                (tb["sched"][p]["wait_ns"] - ta["sched"][p]["wait_ns"])
                / 1e9
                / span
                * 100
                for p in sched
            )
            shared["main_nvcsw_per_s"] = max(
                (tb["sched"][p]["nvcsw"] - ta["sched"][p]["nvcsw"]) / span
                for p in sched
            )
        if ta["steal"] and tb["steal"] and tb["steal"][1] > ta["steal"][1]:
            shared["steal_pct"] = (
                (tb["steal"][0] - ta["steal"][0])
                / (tb["steal"][1] - ta["steal"][1])
                * 100
            )
            shared["steal_scope"] = "main cpus" if main_cpus else "host"
    if perf_text:
        shared.update(parse_perf_stat(perf_text, cmds))

    out_parts = []
    for (generator, req), handle, early in zip(parts, handles, ended_early):
        result = generator.parse(handle, (a["t"], b["t"]))
        if result is None:
            logging.error(f"{generator.name} produced no parsable output")
            out_parts.append(None)
            continue
        out_parts.append(
            {
                "row": result.row,
                "extra": {
                    "generator": generator.name,
                    "generator_version": generator.version(),
                    "generator_argv": " ".join(handle.argv),
                    "generator_ended_early": early,
                    **result.extra,
                },
            }
        )
    return {"extra": shared, "parts": out_parts}


def stamp_validity(extra: dict, opts: dict, scenario: dict) -> dict:
    """Mark measured fields valid or invalid and drop the inputs to that check."""
    reasons = evaluate_validity(extra, opts, scenario)
    extra.pop("expected_clients", None)
    extra.pop("generator_ended_early", None)
    extra["valid"] = not reasons
    extra["invalid_reasons"] = "; ".join(reasons)
    return extra
