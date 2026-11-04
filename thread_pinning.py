"""Pin the server's main thread and each IO thread to its own CPU.

Valkey starts its IO threads with the process, so pinning happens after launch:
each thread's affinity is set by thread id and read back to check it held.
Runs on the server's host: in-process for a local server, through
vpb_agent.py for a remote one, so it uses the standard library only.
"""

import os
import subprocess
from typing import Dict, List, Optional

from procstat import io_thread_index, thread_names
from utils.cpu_utils import parse_core_range


def validate_thread_pinning(value) -> None:
    if not isinstance(value, dict) or "main" not in value:
        raise ValueError("'thread_pinning' must be an object with 'main'")
    unknown = sorted(set(value) - {"main", "io_threads", "other"})
    if unknown:
        raise ValueError(f"'thread_pinning' does not support key(s): {unknown}")
    for key in ("main", "io_threads", "other"):
        if key in value:
            parse_core_range(str(value[key]))


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
        (io_thread_index(n), tid)
        for tid, n in names.items()
        if io_thread_index(n) is not None
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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=5,
        )
        if r.stdout.strip() == "active":
            warnings.append("irqbalance active")
    except Exception:
        pass
    return warnings
