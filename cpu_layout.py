"""CPU layout v1: which CPU every server thread, NIC IRQ and softirq runs on.

A layout names ``cores`` CPU slots in CPU order, for example
``"main, 3x io, 3x irq, aux"``. ``cores`` is the total CPU count the server
may use; the old "N cores" (io-threads N) is ``worker_cpus``. The server runs
under ``taskset`` of every slot CPU, then each thread is pinned to its slot,
and NIC IRQs (or loopback RPS) go to the ``irq`` slots. Every result row
carries the manifest built here; ``normalize_layout`` derives one for rows
written before layouts existed.
"""

import ipaddress
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from utils.cpu_utils import format_core_list, parse_core_range

SCHEMA = 1
ROLES = ("main", "io", "poller", "irq", "aux", "repl", "proactor", "hk")
# Roles that run commands or client IO; their slot count is the old "N".
WORKER_ROLES = ("main", "io", "proactor")
CONFIG_KEYS = ("cores", "layout", "first_cpu")
LOOPBACK_IF = "lo"
_SLOT = re.compile(r"^(?:(\d+)\s*x\s*)?([a-z]+(?:\s*\+\s*[a-z]+)*)$")


def parse_layout(text: str) -> List[List[str]]:
    """Return one role list per CPU slot, in CPU order."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("layout must be a non-empty string")
    slots: List[List[str]] = []
    for part in text.split(","):
        m = _SLOT.match(part.strip().lower())
        if not m:
            raise ValueError(f"layout slot {part.strip()!r} is not '[Nx] role[+role]'")
        count = int(m.group(1)) if m.group(1) else 1
        roles = [r.strip() for r in m.group(2).split("+")]
        unknown = [r for r in roles if r not in ROLES]
        if unknown:
            raise ValueError(f"layout role(s) {unknown} not in {list(ROLES)}")
        if len(set(roles)) != len(roles):
            raise ValueError(f"layout slot {part.strip()!r} repeats a role")
        if count < 1:
            raise ValueError(f"layout slot {part.strip()!r} has a zero count")
        slots.extend(list(roles) for _ in range(count))
    return slots


def validate_cpu_layout(value, where: str = "cpu_layout") -> None:
    """Check a ``cpu_layout`` object for a Valkey server."""
    if not isinstance(value, dict):
        raise ValueError(f"'{where}' must be an object with 'cores' and 'layout'")
    unknown = sorted(set(value) - set(CONFIG_KEYS))
    if unknown:
        raise ValueError(f"'{where}' does not support key(s): {unknown}")
    cores = value.get("cores")
    if isinstance(cores, bool) or not isinstance(cores, int) or cores <= 0:
        raise ValueError(f"'{where}.cores' must be a positive integer")
    try:
        slots = parse_layout(value.get("layout"))
    except ValueError as e:
        raise ValueError(f"'{where}.layout': {e}") from None
    if len(slots) != cores:
        raise ValueError(
            f"'{where}' has {len(slots)} slots in its layout but cores={cores}"
        )
    mains = sum("main" in s for s in slots)
    if mains != 1:
        raise ValueError(f"'{where}.layout' needs exactly one 'main', found {mains}")
    if any("proactor" in s for s in slots):
        raise ValueError(f"'{where}.layout': 'proactor' is for Dragonfly, not Valkey")
    for role in ("poller", "repl"):
        if sum(role in s for s in slots) > 1:
            raise ValueError(f"'{where}.layout' places '{role}' more than once")
    if "first_cpu" in value:
        fc = value["first_cpu"]
        if isinstance(fc, bool) or not isinstance(fc, int) or fc < 0:
            raise ValueError(f"'{where}.first_cpu' must be a non-negative integer")


def io_threads_for(value: dict) -> int:
    """Valkey io-threads for a layout: main plus one per 'io' token."""
    return 1 + sum("io" in s for s in parse_layout(value["layout"]))


def worker_cpus_for(slots: List[List[str]]) -> int:
    return sum(any(r in s for r in WORKER_ROLES) for s in slots)


def online_cpus() -> List[int]:
    try:
        text = Path("/sys/devices/system/cpu/online").read_text().strip()
        return sorted(parse_core_range(text))
    except (OSError, ValueError):
        return list(range(os.cpu_count() or 1))


def resolve_first_cpu(value: dict, online: Optional[List[int]] = None) -> int:
    """Slot 0's CPU: as configured, else 1 on a larger host (CPU 0 keeps
    housekeeping), else 0."""
    if "first_cpu" in value:
        return value["first_cpu"]
    online = online if online is not None else online_cpus()
    return 1 if len(online) > value["cores"] else 0


def layout_cpus(value: dict, online: Optional[List[int]] = None) -> List[int]:
    first = resolve_first_cpu(value, online)
    return list(range(first, first + value["cores"]))


def describe(slots: List[List[str]]) -> str:
    """Human layout text: runs of equal slots as 'N role', shared CPUs as a/b."""
    parts: List[str] = []
    i = 0
    while i < len(slots):
        j = i
        while j + 1 < len(slots) and slots[j + 1] == slots[i]:
            j += 1
        name = "/".join(slots[i])
        n = j - i + 1
        parts.append(f"{n} {name}" if n > 1 else name)
        i = j + 1
    return " + ".join(parts)


def layout_label(manifest: dict) -> str:
    """Display label: '8 cores (main + 3 io + 3 irq + aux)' or the legacy form."""
    if manifest.get("legacy"):
        n, total = manifest.get("worker_cpus"), manifest.get("cores")
        workers = f"{n} worker cores" if n is not None else "worker cores unknown"
        return f"{workers} ({total if total is not None else 'unknown'} CPUs total)"
    slots = [s["roles"] for s in manifest.get("slots", [])]
    return f"{manifest['cores']} cores ({describe(slots)})"


def plan(value: dict, online: Optional[List[int]] = None) -> dict:
    """The manifest a layout asks for, before anything is applied."""
    validate_cpu_layout(value)
    online = online if online is not None else online_cpus()
    slots = parse_layout(value["layout"])
    cpus = layout_cpus(value, online)
    missing = sorted(set(cpus) - set(online))
    if missing:
        raise ValueError(f"cpu_layout uses CPUs {missing} that are not online")
    manifest = {
        "schema": SCHEMA,
        "cores": value["cores"],
        "layout": value["layout"],
        "worker_cpus": worker_cpus_for(slots),
        "cpus": format_core_list(cpus),
        "slots": [{"cpu": c, "roles": r} for c, r in zip(cpus, slots)],
        "server_config": {"io-threads": io_threads_for(value)},
    }
    manifest["label"] = layout_label(manifest)
    return manifest


def check_disjoint(value: dict, client_ranges: List[str], where: str = "cpu_layout"):
    """Raise when a generator CPU range overlaps the layout's CPUs."""
    cpus = set(layout_cpus(value))
    for r in client_ranges:
        overlap = sorted(cpus & set(parse_core_range(str(r))))
        if overlap:
            raise ValueError(
                f"'{where}' CPUs {format_core_list(sorted(cpus))} overlap the "
                f"generator range {r} on {overlap}"
            )


# ---------- thread pinning ---------------------------------------------------


def _io_index(name: str) -> Optional[int]:
    m = re.match(r"io_thd_(\d+)$", name)
    return int(m.group(1)) if m else None


def thread_targets(
    manifest: dict, pid: int, names: Dict[int, str]
) -> Tuple[Dict[int, set], Dict[str, object]]:
    """Map every server thread to its slot CPUs; returns (tid -> cpus, notes).

    io_thd_i follow the 'io' tokens in order, io_epoll the poller (else the
    last io slot, else main), repl_sender the repl slot (else the poller), and
    every other thread the aux slots (else main's CPU).
    """

    def cpus_of(role: str) -> List[int]:
        return [s["cpu"] for s in manifest["slots"] if role in s["roles"]]

    main = cpus_of("main")[0]
    io = cpus_of("io")
    aux = set(cpus_of("aux")) or {main}
    notes: Dict[str, object] = {"unplaced": []}
    if cpus_of("poller"):
        poller = cpus_of("poller")[0]
    else:
        poller = io[-1] if io else main
        if any(n == "io_epoll" for n in names.values()):
            notes["poller"] = "implicit"
    repl = cpus_of("repl")[0] if cpus_of("repl") else poller

    want: Dict[int, set] = {pid: {main}}
    ios = sorted(
        (_io_index(n), t) for t, n in names.items() if _io_index(n) is not None
    )
    for k, (_, tid) in enumerate(ios):
        if k < len(io):
            want[tid] = {io[k]}
        else:
            notes["unplaced"].append(f"{names[tid]}:{tid} (no io slot)")
    if len(ios) < len(io):
        notes["unplaced"].append(f"{len(io) - len(ios)} io slot(s) without a thread")
    for tid, name in names.items():
        if tid in want or _io_index(name) is not None:
            continue
        if name == "io_epoll":
            want[tid] = {poller}
        elif name == "repl_sender":
            want[tid] = {repl}
        else:
            want[tid] = set(aux)
    return want, notes


def thread_names(pid: int) -> Dict[int, str]:
    out = {}
    for tid in os.listdir(f"/proc/{pid}/task"):
        try:
            out[int(tid)] = Path(f"/proc/{pid}/task/{tid}/comm").read_text().strip()
        except OSError:
            pass
    return out


def pin_threads(manifest: dict, pid: int) -> dict:
    """Pin each thread to its slot, verify, and return the 'applied' record."""
    try:
        process_cpus = format_core_list(sorted(os.sched_getaffinity(pid)))
    except OSError:
        process_cpus = None
    names = thread_names(pid)
    want, notes = thread_targets(manifest, pid, names)
    threads = {}
    unplaced = list(notes["unplaced"])
    for tid, cpus in sorted(want.items()):
        label = f"{'main' if tid == pid else names.get(tid, tid)}:{tid}"
        try:
            os.sched_setaffinity(tid, cpus)
            got = os.sched_getaffinity(tid)
        except ProcessLookupError:
            continue  # thread exited
        except OSError as e:
            unplaced.append(f"{label} ({e})")
            continue
        if got != cpus:
            unplaced.append(f"{label} on {format_core_list(sorted(got))}")
            continue
        threads[label] = format_core_list(sorted(cpus))
    applied = {"process_cpus": process_cpus, "threads": threads, "unplaced": unplaced}
    if "poller" in notes:
        applied["poller"] = notes["poller"]
    return applied


# ---------- NIC IRQ and RPS placement ----------------------------------------


class SysIO:
    """Reads and privileged writes of /proc and /sys; swapped out in tests."""

    def __init__(self):
        self._sudo = None

    def read(self, path: str) -> Optional[str]:
        try:
            return Path(path).read_text().strip()
        except OSError:
            return None

    def listdir(self, path: str) -> List[str]:
        try:
            return os.listdir(path)
        except OSError:
            return []

    def privileged(self) -> Optional[str]:
        """None when root or passwordless sudo is available, else why not."""
        if os.geteuid() == 0:
            return None
        if self._sudo is None:
            try:
                r = subprocess.run(
                    ["sudo", "-n", "true"], capture_output=True, timeout=10
                )
                self._sudo = r.returncode == 0
            except (OSError, subprocess.SubprocessError):
                self._sudo = False
        return None if self._sudo else "not root and no passwordless sudo"

    def write(self, path: str, value: str) -> Optional[str]:
        """Write as root; returns an error string or None."""
        try:
            if os.geteuid() == 0:
                Path(path).write_text(value)
                return None
            r = subprocess.run(
                ["sudo", "-n", "tee", path],
                input=value,
                text=True,
                capture_output=True,
                timeout=10,
            )
            return None if r.returncode == 0 else (r.stderr.strip() or "write failed")
        except (OSError, subprocess.SubprocessError) as e:
            return str(e)

    def addresses(self) -> str:
        try:
            r = subprocess.run(
                ["ip", "-o", "addr", "show"], capture_output=True, text=True, timeout=10
            )
            return r.stdout
        except (OSError, subprocess.SubprocessError):
            return ""


def interface_for(target_ip: str, sysio: SysIO) -> Optional[str]:
    """The interface the generator reaches the server through, or None."""
    if str(target_ip).strip().lower() == "localhost":
        return LOOPBACK_IF
    try:
        if ipaddress.ip_address(target_ip).is_loopback:
            return LOOPBACK_IF
    except ValueError:
        return None
    for line in sysio.addresses().splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[2] in ("inet", "inet6"):
            if parts[3].split("/")[0] == target_ip:
                return parts[1].split("@")[0]
    return None


def cpu_mask(cpus: List[int]) -> str:
    """rps_cpus form: 32-bit hex words, most significant first."""
    value = 0
    for c in cpus:
        value |= 1 << c
    words = []
    while True:
        words.append(f"{value & 0xFFFFFFFF:08x}")
        value >>= 32
        if not value:
            break
    return ",".join(reversed(words))


def mask_cpus(mask: str) -> List[int]:
    value = int(mask.replace(",", "") or "0", 16)
    return [i for i in range(value.bit_length()) if value >> i & 1]


def nic_irqs(ifname: str, sysio: SysIO) -> List[Tuple[int, str]]:
    irqs = sorted(
        int(n)
        for n in sysio.listdir(f"/sys/class/net/{ifname}/device/msi_irqs")
        if n.isdigit()
    )
    names = {}
    for line in (sysio.read("/proc/interrupts") or "").splitlines():
        head = line.split(":", 1)
        if head[0].strip().isdigit():
            names[int(head[0])] = line.split()[-1]
    return [(irq, names.get(irq, ifname)) for irq in irqs]


def place_irqs(manifest: dict, target_ip: str, sysio: Optional[SysIO] = None) -> dict:
    """Send NIC IRQs (or loopback RPS) to the irq slots; returns the record.

    The record has ``irqs`` and ``rps`` as the kernel reports them after the
    change, ``irq_placement`` saying what happened, and ``problems`` that
    make the row invalid.
    """
    sysio = sysio or SysIO()
    irq_cpus = [s["cpu"] for s in manifest["slots"] if "irq" in s["roles"]]
    layout_set = {s["cpu"] for s in manifest["slots"]}
    out = {"irqs": {}, "rps": {}, "problems": []}
    ifname = interface_for(target_ip, sysio)
    out["interface"] = ifname
    if ifname is None:
        out["irq_placement"] = f"not applied: no local interface for {target_ip}"
        if irq_cpus:
            out["problems"].append(out["irq_placement"])
        return out

    if ifname == LOOPBACK_IF:
        path = f"/sys/class/net/{LOOPBACK_IF}/queues/rx-0/rps_cpus"
        if irq_cpus:
            why = sysio.privileged() or sysio.write(path, cpu_mask(irq_cpus))
            out["irq_placement"] = f"not applied: {why}" if why else "applied"
        else:
            out["irq_placement"] = "none requested"
        cur = sysio.read(path)
        if cur is not None:
            out["rps"][f"{LOOPBACK_IF}/rx-0"] = (
                format_core_list(mask_cpus(cur)) if mask_cpus(cur) else ""
            )
        return out

    irqs = nic_irqs(ifname, sysio)
    queues = [
        q
        for q in sysio.listdir(f"/sys/class/net/{ifname}/queues")
        if q.startswith("rx-")
    ]
    out["nic_queues"] = len(queues)
    if not irqs:
        out["irq_placement"] = f"not applied: no IRQs found for {ifname}"
        out["problems"].append(out["irq_placement"])
        return out
    if irq_cpus:
        why = sysio.privileged()
        if why is None:
            for k, (irq, _) in enumerate(irqs):
                cpu = irq_cpus[k % len(irq_cpus)]
                err = sysio.write(f"/proc/irq/{irq}/smp_affinity_list", str(cpu))
                if err:
                    why = f"irq {irq}: {err}"
                    break
        out["irq_placement"] = f"not applied: {why}" if why else "applied"
        if why:
            out["problems"].append(f"IRQ placement {out['irq_placement']}")
    else:
        out["irq_placement"] = "none requested"
    for irq, name in irqs:
        eff = sysio.read(f"/proc/irq/{irq}/effective_affinity_list")
        if not eff:
            eff = sysio.read(f"/proc/irq/{irq}/smp_affinity_list")
        out["irqs"][f"{irq}:{name}"] = eff
    if out["problems"]:
        return out
    want = set(irq_cpus) if irq_cpus else layout_set
    stray = sorted(
        k
        for k, v in out["irqs"].items()
        if not v or not set(parse_core_range(v)) <= want
    )
    if stray:
        where = "irq slots" if irq_cpus else "the layout CPUs"
        out["problems"].append(f"NIC IRQs outside {where}: {', '.join(stray)}")
    return out


def host_facts(nic_queues: Optional[int] = None) -> dict:
    product = SysIO().read("/sys/devices/virtual/dmi/id/product_name")
    return {
        "instance_type": product or None,
        "online_cpus": len(online_cpus()),
        "nic_queues": nic_queues,
    }


def apply_layout(manifest: dict, pid: int, target_ip: str, sysio=None) -> dict:
    """Pin threads and place IRQs for a started server; returns the manifest
    with ``applied`` and ``host`` filled in."""
    # IO threads start with the server; give them a moment to be named.
    time.sleep(0.3)
    applied = pin_threads(manifest, pid)
    irq = place_irqs(manifest, target_ip, sysio)
    applied["irqs"] = irq["irqs"]
    applied["rps"] = irq["rps"]
    applied["irq_placement"] = irq["irq_placement"]
    applied["interface"] = irq.get("interface")
    problems = list(irq["problems"])
    if applied["unplaced"]:
        problems.append(f"threads not pinned: {', '.join(applied['unplaced'])}")
    applied["problems"] = problems
    done = dict(manifest)
    done["applied"] = applied
    done["host"] = host_facts(irq.get("nic_queues"))
    for p in problems:
        logging.warning(f"cpu_layout: {p}")
    return done


# ---------- result rows ------------------------------------------------------

ROW_FIELDS = ("cores", "worker_cpus", "cpu_layout_label")


def row_identity(manifest: dict) -> dict:
    """Flat fields every row of a layout run carries, success or failure."""
    return {
        "cores": manifest["cores"],
        "worker_cpus": manifest["worker_cpus"],
        "cpu_layout_label": manifest["label"],
        "io_threads": manifest["server_config"]["io-threads"],
    }


def stamp_row(row: dict, manifest: dict) -> dict:
    """Record the layout on a row and mark it invalid for layout problems."""
    row.update(row_identity(manifest))
    row["cpu_layout"] = manifest
    problems = (manifest.get("applied") or {}).get("problems") or []
    if problems:
        reasons = [r for r in (row.get("invalid_reasons") or "").split("; ") if r]
        row["valid"] = False
        row["invalid_reasons"] = "; ".join(reasons + problems)
    return row


def _legacy_pinning_cpus(text: str) -> List[int]:
    """CPUs named in an old 'main=2,io_thd_1=3,bio=1,5' thread_pinning field."""
    cpus: List[int] = []
    for token in str(text).split(","):
        value = token.split("=", 1)[1] if "=" in token else token
        try:
            cpus.extend(parse_core_range(value.strip()))
        except ValueError:
            continue
    return sorted(set(cpus))


def legacy_layout(row: dict, run_manifest: Optional[dict] = None) -> dict:
    """Derived manifest for a row written before cpu_layout existed.

    worker_cpus is the old N (io-threads). cores is the CPU count the old
    harness confined the server to: its thread_pinning CPUs, else the
    server's taskset range from the run manifest; null with a reason when
    neither was recorded.
    """
    n = row.get("io_threads")
    try:
        workers = int(n) if n is not None and not isinstance(n, bool) else None
    except (TypeError, ValueError):
        workers = None
    out = {"schema": SCHEMA, "legacy": True, "worker_cpus": workers, "cores": None}
    cfg = (run_manifest or {}).get("config") or {}
    cpus: List[int] = []
    if row.get("thread_pinning"):
        cpus = _legacy_pinning_cpus(row["thread_pinning"])
        out["cores_source"] = "thread_pinning"
    elif cfg.get("server_cpu_range"):
        try:
            cpus = sorted(set(parse_core_range(str(cfg["server_cpu_range"]))))
            out["cores_source"] = "server_cpu_range"
        except ValueError:
            pass
    if cpus:
        out["cores"] = len(cpus)
        out["cpus"] = format_core_list(cpus)
    else:
        out.pop("cores_source", None)
        out["cores_reason"] = "server CPU range not recorded"
    out["label"] = layout_label(out)
    return out


def normalize_layout(row: dict, run_manifest: Optional[dict] = None) -> dict:
    """The v1 layout manifest of any row, new or old. Never mutates the row."""
    m = row.get("cpu_layout")
    if isinstance(m, dict) and m.get("schema") == SCHEMA and not m.get("legacy"):
        out = dict(m)
        out.setdefault("label", layout_label(out))
        out["legacy"] = False
        return out
    return legacy_layout(row, run_manifest)


def annotate_rows(rows, manifests_dir: Optional[Path] = None):
    """Add the flat layout fields to rows that lack them, for readers.

    Run manifests under ``manifests_dir`` (``<id>.json``) supply the old
    taskset range for legacy rows.
    """
    import json

    cache: Dict[str, Optional[dict]] = {}

    def run_manifest(mid):
        if not mid or manifests_dir is None:
            return None
        if mid not in cache:
            try:
                cache[mid] = json.loads(
                    (Path(manifests_dir) / f"{mid}.json").read_text()
                )
            except (OSError, ValueError):
                cache[mid] = None
        return cache[mid]

    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or "cpu_layout_label" in row:
            continue
        m = normalize_layout(row, run_manifest(row.get("manifest_id")))
        row["cpu_layout_label"] = m["label"]
        if m.get("legacy"):
            row["layout_legacy"] = True
            for key in ("worker_cpus", "cores"):
                if m[key] is not None:
                    row.setdefault(key, m[key])
    return rows
