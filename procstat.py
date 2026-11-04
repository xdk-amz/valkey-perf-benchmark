"""Process and CPU counters read from /proc on the host that runs the server.

Standard library only, and Python 3.9 compatible, because the same functions
run on a remote benchmark host through vpb_agent.py.
"""

import os
import re
from pathlib import Path
from typing import Dict, Optional


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


def main_cpus(pids) -> Optional[set]:
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


def ticks(pids, gen_pids, cpus) -> dict:
    """CPU ticks and scheduler counters of every server and generator.

    ``main`` and ``sched`` are keyed by server pid; ``io`` sums every IO
    thread; ``gen`` sums the generator processes (None if one is gone);
    ``steal`` is (steal, total) jiffies over ``cpus``.
    """
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
    t["steal"] = cpu_steal(cpus)
    return t
