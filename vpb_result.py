"""Write a run's results as vpb-result run files (dashboards/explorer/SCHEMA.md), beside its metrics.json.

Every runner calls this module: benchmark.py, through ClientRunner, for the basic, test_groups and set_matrix
configs, and matrix_runner.py for matrix configs. A run is one build on one setup with one config: one
fingerprint. Its file is ``<results>/<commit or run name>/vpb-runs/<run id>.json``; a later invocation with the
same fingerprint adds its reps to the same file. metrics.json is unchanged.

Every runner records the same facts, read at run time: the build (repository, ref, commit, commit time, and
from the server's INFO its version and build settings, the binary's sha256 and VPB's make arguments), the
server's settings (CONFIG GET), and for the server's host and the client's host the instance type, CPU model,
microcode, topology, memory, NIC, virtualization, cgroup CPU limit, OS, kernel, boot options and kernel runtime
settings (HOST_SCRIPT, run here or over ssh), plus io-threads, cores, pinning, network path, load generator and
the harness commit. A fact a host cannot report is null. No host identifier is read into a run file.

Standard library only, so matrix_runner.py stays runnable on a bench host's Python 3.9.
"""

import json
import logging
import os
import re
import shlex
import subprocess
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from dashboards.explorer import cells, idscan, schema

RUNS_DIR = "vpb-runs"
HARNESS_REPO = "valkey-io/valkey-perf-benchmark"
BUILD_REPO = "valkey-io/valkey"
ROOT = Path(__file__).resolve().parent
GITHUB_REMOTE = re.compile(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$")
SHA = re.compile(r"^[0-9a-f]{40}$")
INSTANCE_TYPE = re.compile(r"^[a-z][a-z0-9-]*\.[a-z0-9-]+$")
LOOPBACK = ("127.0.0.1", "localhost", "::1")

# Value type of a command, by its name or the first word of its command line.
TYPES = {
    "string": {"SET", "GET", "MSET", "MGET", "INCR", "GETSET", "SETEX", "APPEND"},
    "list": {"LPUSH", "RPUSH", "LPOP", "RPOP", "LRANGE", "LINDEX", "LLEN"},
    "set": {"SADD", "SPOP", "SREM", "SISMEMBER", "SMISMEMBER", "SCARD", "SMEMBERS"},
    "hash": {"HSET", "HGET", "HMSET", "HGETALL", "HDEL", "HINCRBY"},
    "zset": {"ZADD", "ZRANGE", "ZSCORE", "ZPOPMIN", "ZRANK", "ZREM"},
    "stream": {"XADD", "XRANGE", "XREAD", "XLEN"},
}
# Members per key of a matrix template's collection, by the template parameter that sets it.
MATRIX_SIZES = {
    "hash": "fields_per_hash",
    "list": "list_length",
    "set": "set_size",
    "zset": "zset_size",
    "stream": "stream_length",
}


# ---------------------------------------------------------------- host and server facts
HOST_KEYS = schema.HOST_KEYS
PEER = re.compile(r"^[\w.:-]+$")
# CPU implementer and part numbers (MIDR) of the Arm cores lscpu may not name
ARM_PARTS = {
    ("0x41", "0xd08"): "Arm Cortex-A72",
    ("0x41", "0xd0b"): "Arm Cortex-A76",
    ("0x41", "0xd0c"): "Arm Neoverse-N1",
    ("0x41", "0xd40"): "Arm Neoverse-V1",
    ("0x41", "0xd49"): "Arm Neoverse-N2",
    ("0x41", "0xd4f"): "Arm Neoverse-V2",
    ("0x41", "0xd84"): "Arm Neoverse-V3",
    ("0x41", "0xd8e"): "Arm Neoverse-N3",
}
# Boot options that name a device, a file, an address or a host: never recorded
BOOT_DROP = {
    "BOOT_IMAGE",
    "root",
    "resume",
    "initrd",
    "ip",
    "hostname",
    "cryptdevice",
    "systemd.machine_id",
    "rd.luks.uuid",
    "rd.lvm.lv",
    "rd.md.uuid",
    "ifname",
    "nfsroot",
}
ID_LIKE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-|(?:\d{1,3}\.){3}\d{1,3}|(?:[0-9a-f]{2}:){5}|/|UUID|LABEL",
    re.I,
)
# server settings that shape performance; the keys a run sets itself are added to these
SERVER_CONFIG_KEYS = (
    "io-threads",
    "maxmemory",
    "maxmemory-policy",
    "maxclients",
    "appendonly",
    "appendfsync",
    "save",
    "activedefrag",
    "active-expire-effort",
    "hz",
    "dynamic-hz",
    "lazyfree-lazy-eviction",
    "lazyfree-lazy-expire",
    "lazyfree-lazy-server-del",
    "lazyfree-lazy-user-del",
    "lazyfree-lazy-user-flush",
    "cluster-enabled",
    "databases",
    "protected-mode",
    "tcp-nodelay",
    "hash-max-listpack-entries",
    "set-max-intset-entries",
    "set-max-listpack-entries",
    "zset-max-listpack-entries",
    "list-max-listpack-size",
)
# a setting with one of these words in its name holds a secret, a path or an address: never recorded
SECRET_WORDS = {
    "pass",
    "requirepass",
    "masterauth",
    "primaryauth",
    "masteruser",
    "primaryuser",
    "auth",
    "acl",
    "aclfile",
    "key",
    "cert",
    "ca",
    "dir",
    "file",
    "path",
    "bind",
    "announce",
    "ip",
    "logfile",
    "pidfile",
    "unixsocket",
    "dbfilename",
    "appendfilename",
    "appenddirname",
}

# One section per source, "@@<name>" then its raw output; parse_host reads it.
HOST_SCRIPT = r"""
export LC_ALL=C
pid=${VPB_PID:-$$}
s() { echo "@@$1"; }
s kernel; uname -r
s arch; uname -m
s os; ( . /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" )
s lscpu; lscpu 2>/dev/null
s cpuinfo; sed '/^$/q' /proc/cpuinfo 2>/dev/null
s midr; cat /sys/devices/system/cpu/cpu0/regs/identification/midr_el1 2>/dev/null
s revidr; cat /sys/devices/system/cpu/cpu0/regs/identification/revidr_el1 2>/dev/null
s mem; sed -n 's/^MemTotal: *//p' /proc/meminfo 2>/dev/null
s vm; systemd-detect-virt --vm 2>/dev/null
s container; systemd-detect-virt --container 2>/dev/null || { [ -f /.dockerenv ] && echo docker; }
s hypervisor; cat /sys/hypervisor/type 2>/dev/null
s dmi_vendor; cat /sys/class/dmi/id/sys_vendor 2>/dev/null
s dmi_product; cat /sys/class/dmi/id/product_name 2>/dev/null
s imds; t=$(curl -s -m 1 -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null) && [ -n "$t" ] && curl -s -m 1 -H "X-aws-ec2-metadata-token: $t" http://169.254.169.254/latest/meta-data/instance-type 2>/dev/null; echo
s cpumax; cg=$(sed -n 's/^0:://p' /proc/$pid/cgroup 2>/dev/null); cat "/sys/fs/cgroup$cg/cpu.max" 2>/dev/null
s affinity; sed -n 's/^Cpus_allowed_list:[[:space:]]*//p' /proc/$pid/status 2>/dev/null
s cmdline; cat /proc/cmdline 2>/dev/null
s thp; cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null
s thp_defrag; cat /sys/kernel/mm/transparent_hugepage/defrag 2>/dev/null
s aslr; cat /proc/sys/kernel/randomize_va_space 2>/dev/null
s numa_balancing; cat /proc/sys/kernel/numa_balancing 2>/dev/null
s governor; cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null
s boost; cat /sys/devices/system/cpu/cpufreq/boost 2>/dev/null
s no_turbo; cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null
s idle; cat /sys/devices/system/cpu/cpu0/cpuidle/state*/disable 2>/dev/null
s nic
dev=
[ -n "$VPB_PEER" ] && dev=$(ip route get "$VPB_PEER" 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -1)
[ -z "$dev" ] && dev=$(ip route show default 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -1)
[ -z "$dev" ] && dev=$(awk 'NR>1 && $2=="00000000" {print $1; exit}' /proc/net/route 2>/dev/null)
echo "dev=$dev"
if [ -n "$dev" ]; then
  d=$(readlink "/sys/class/net/$dev/device/driver" 2>/dev/null); echo "driver=${d##*/}"
  echo "speed=$(cat "/sys/class/net/$dev/speed" 2>/dev/null)"
fi
"""

Run = Callable[..., Tuple[int, str, str]]


def local_run(cmd: str, timeout: float = 60, stdin: Optional[str] = None):
    try:
        p = subprocess.run(
            ["bash", "-c", cmd],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 124, "", str(e)


def ssh_run(target: str) -> Run:
    def run(cmd: str, timeout: float = 60, stdin: Optional[str] = None):
        try:
            p = subprocess.run(
                ["ssh", target, cmd],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return p.returncode, p.stdout, p.stderr
        except (OSError, subprocess.TimeoutExpired) as e:
            return 124, "", str(e)

    return run


def _sections(out: str) -> Dict[str, List[str]]:
    sec: Dict[str, List[str]] = {}
    cur = None
    for line in out.splitlines():
        if line.startswith("@@"):
            cur = line[2:].strip()
            sec[cur] = []
        elif cur is not None:
            sec[cur].append(line.rstrip())
    return sec


def _first(sec, name) -> str:
    return next((x.strip() for x in sec.get(name, []) if x.strip()), "")


def _fields(lines: List[str], sep: str = ":") -> Dict[str, str]:
    out = {}
    for line in lines:
        k, s, v = line.partition(sep)
        if s and k.strip() not in out:
            out[k.strip()] = v.strip()
    return out


def boot_options(cmdline: str) -> Optional[str]:
    """The kernel command line without anything that names a device, file, address or host."""
    keep = []
    for tok in cmdline.split():
        key = tok.split("=", 1)[0]
        if key in BOOT_DROP or ID_LIKE.search(tok):
            continue
        keep.append(tok)
    return " ".join(keep) if cmdline.strip() else None


def _bracketed(text: str) -> str:
    m = re.search(r"\[(\w+)\]", text)
    return m.group(1) if m else (text or "not available")


def parse_host(out: str) -> Tuple[Dict[str, Any], Optional[str]]:
    """(host facts, CPUs the measured process may run on) from HOST_SCRIPT's output. A fact the host does not
    report is null."""
    sec = _sections(out)
    lscpu = _fields(sec.get("lscpu", []))
    cpuinfo = _fields(sec.get("cpuinfo", []))

    imds, product = _first(sec, "imds"), _first(sec, "dmi_product")
    if INSTANCE_TYPE.match(imds):
        itype = imds
    elif "Amazon EC2" in _first(sec, "dmi_vendor") and INSTANCE_TYPE.match(product):
        itype = product  # EC2 puts the instance type in the DMI product name
    else:
        itype = None

    model = lscpu.get("Model name", "")
    if not model or model == "-":
        impl, part = cpuinfo.get("CPU implementer", ""), cpuinfo.get("CPU part", "")
        name = ARM_PARTS.get((impl, part))
        if name:
            v, r = cpuinfo.get("CPU variant", "0x0"), cpuinfo.get("CPU revision", "0")
            model = f"{name} r{int(v, 16)}p{r}"
        elif impl:
            model = f"implementer {impl} part {part}"

    micro = cpuinfo.get("microcode") or None
    if not micro and _first(sec, "midr"):
        micro = f"midr {_first(sec, 'midr')} revidr {_first(sec, 'revidr') or 'n/a'}"

    cpus = lscpu.get("CPU(s)")
    sockets = lscpu.get("Socket(s)")
    if not sockets or sockets == "-":
        sockets = lscpu.get("Cluster(s)", "")
    cores = lscpu.get("Core(s) per socket") or lscpu.get("Core(s) per cluster")
    topo = None
    if cpus:
        topo = (
            f"{cpus} CPUs, {sockets or '?'} sockets x {cores or '?'} cores x "
            f"{lscpu.get('Thread(s) per core', '?')} threads, {lscpu.get('NUMA node(s)', '?')} NUMA nodes"
        )
        if lscpu.get("L3 cache"):
            topo += f", L3 {lscpu['L3 cache'].split(' (')[0]}"

    mem = _first(sec, "mem").split()
    memory = f"{round(int(mem[0]) / 1048576)} GiB" if mem and mem[0].isdigit() else None

    nic_f = _fields(sec.get("nic", []), "=")
    dev = nic_f.get("dev", "")
    nic = None
    if dev == "lo":
        nic = "loopback"
    elif dev:
        speed = nic_f.get("speed", "")
        speed = (
            f"{speed} Mb/s"
            if speed.isdigit() and int(speed) > 0
            else "speed not reported"
        )
        nic = f"{nic_f.get('driver') or 'virtual interface'}, {speed}"

    vm, ctr = _first(sec, "vm"), _first(sec, "container")
    hyp = lscpu.get("Hypervisor vendor") or _first(sec, "hypervisor")
    parts = []
    if vm:
        parts.append("bare metal" if vm == "none" else f"vm {vm}")
    elif hyp:
        parts.append(f"vm {hyp}")
    if ctr and ctr != "none":
        parts.append(f"container {ctr}")

    cpumax = _first(sec, "cpumax").split()
    cg = None
    if cpumax and cpumax[0] == "max":
        cg = "none"
    elif len(cpumax) == 2 and cpumax[0].isdigit():
        cg = f"{int(cpumax[0]) / int(cpumax[1]):g} CPUs"

    cmd = _first(sec, "cmdline")
    idle = [x.strip() for x in sec.get("idle", []) if x.strip()]
    boost, no_turbo = _first(sec, "boost"), _first(sec, "no_turbo")
    if boost in ("0", "1"):
        turbo = {"1": "on", "0": "off"}[boost]
    elif no_turbo in ("0", "1"):
        turbo = {"0": "on", "1": "off"}[no_turbo]
    else:
        turbo = "not available"
    if not idle:
        idle_states = "not available"
    elif all(x == "1" for x in idle):
        idle_states = "all disabled"
    else:
        idle_states = "some disabled" if any(x == "1" for x in idle) else "all enabled"
    runtime = {
        "thp": _bracketed(_first(sec, "thp")),
        "thp_defrag": _bracketed(_first(sec, "thp_defrag")),
        "aslr": {"0": "off", "1": "partial", "2": "full"}.get(
            _first(sec, "aslr"), "not available"
        ),
        "numa_balancing": {"0": "off", "1": "on", "2": "on"}.get(
            _first(sec, "numa_balancing"), "not available"
        ),
        "cpufreq_governor": _first(sec, "governor") or "not available",
        "turbo": turbo,
        "idle_states": idle_states,
    }
    host = {
        "instance_type": itype,
        "arch": _first(sec, "arch") or None,
        "cpu_model": model or None,
        "cpu_microcode": micro,
        "cpu_topology": topo,
        "memory": memory,
        "nic": nic,
        "virtualization": ", ".join(parts) or None,
        "cgroup_cpu_limit": cg,
        "os": _first(sec, "os") or None,
        "kernel": _first(sec, "kernel") or None,
        "kernel_boot_options": boot_options(cmd) if cmd else None,
        "kernel_runtime_settings": runtime,
    }
    return host, _first(sec, "affinity") or None


def collect_host(
    run: Run = local_run, pid: Optional[int] = None, peer: Optional[str] = None
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Read a host's facts with HOST_SCRIPT. pid: the process whose cgroup and CPUs count (default the
    script's shell); peer: the address the benchmark traffic goes to, used only to pick the interface.
    """
    env = []
    if pid:
        env.append(f"VPB_PID={int(pid)}")
    if peer and PEER.match(peer):
        env.append(f"VPB_PEER={shlex.quote(peer)}")
    cmd = (" ".join(env) + " bash -s") if env else "bash -s"
    rc, out, _ = run(cmd, 60, HOST_SCRIPT)
    if rc and "@@" not in out:
        logging.warning(f"host facts unreadable (exit {rc})")
        return dict.fromkeys(HOST_KEYS), None
    return parse_host(out)


def host_from_info(info: Dict[str, Any]) -> Dict[str, Any]:
    """What a server on an unreachable host says about its host in INFO: kernel and CPU architecture."""
    words = str(info.get("os") or "").split()
    host = dict.fromkeys(HOST_KEYS)
    if len(words) >= 3:
        host["kernel"], host["arch"] = words[1], words[-1]
    return host


def merge_hosts(hosts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """One host record for several hosts of one role: a field they agree on, else their values joined."""
    out = {}
    for k in HOST_KEYS:
        vals = []
        for h in hosts:
            if h.get(k) is not None and h[k] not in vals:
                vals.append(h[k])
        if not vals:
            out[k] = None
        elif len(vals) == 1:
            out[k] = vals[0]
        else:
            out[k] = ", ".join(
                sorted(json.dumps(v) if isinstance(v, dict) else str(v) for v in vals)
            )
    return out


# ---------------------------------------------------------------- the server
def server_config(
    config: Dict[str, Any], set_keys: List[str] = ()
) -> Optional[Dict[str, Any]]:
    """CONFIG GET values of the performance settings and of every setting the run set, without secrets,
    paths or addresses."""
    if not config:
        return None
    want = set(SERVER_CONFIG_KEYS) | {str(k) for k in set_keys}
    return {
        k: str(config[k])
        for k in sorted(want)
        if k in config and not (set(k.split("-")) & SECRET_WORDS)
    }


def build_info(
    info: Dict[str, Any], run: Optional[Run], executable: Optional[str], sha: str
) -> Dict[str, Any]:
    """The server build: its version and build settings from INFO, the binary's sha256, and the make
    arguments VPB's builder stamped into the tree (valkey_build.py), read on the server's host.
    """
    settings = {
        k: str(info[src])
        for k, src in (
            ("malloc", "mem_allocator"),
            ("bits", "arch_bits"),
            ("compiler", "gcc_version"),
            ("multiplexing", "multiplexing_api"),
            ("monotonic_clock", "monotonic_clock"),
            ("git_dirty", "valkey_git_dirty"),
        )
        if info.get(src) not in (None, "")
    } or None
    digest, args = None, None
    exe = executable or info.get("executable")
    if run and exe:
        rc, out, _ = run(f"sha256sum {shlex.quote(str(exe))}", 60)
        if rc == 0 and re.match(r"^[0-9a-f]{64}\s", out):
            digest = out.split()[0]
        tree = shlex.quote(os.path.dirname(os.path.dirname(str(exe))))
        rc, out, _ = run(
            f"p=$(git -C {tree} rev-parse --git-path vpb-build-stamp 2>/dev/null) && "
            f'cd {tree} && cat "$p" 2>/dev/null',
            30,
        )
        m = re.match(r"^([0-9a-f]{40}) tls=(\w+) args=(.*)$", out.strip())
        if m and m.group(1) == sha:
            args = [f"BUILD_TLS={m.group(2)}"] + m.group(3).split()
    return {
        "version": info.get("valkey_version") or info.get("redis_version"),
        "settings": settings,
        "args": args,
        "binary_sha256": digest,
    }


def network_path(same_host: bool, loopback: bool, tls: bool = False) -> str:
    if same_host and loopback:
        path = "loopback"
    elif same_host:
        path = "same host, non-loopback address"
    else:
        path = "network, separate hosts"
    return path + (", TLS" if tls else "")


def pinning(server_cpus: Optional[str], client_cpus: Optional[str]) -> str:
    return (
        f"server CPUs {cpu_list(server_cpus) or 'not recorded'}, "
        f"client CPUs {cpu_list(client_cpus) or 'not recorded'}"
    )


def _ranges(cpus: List[int]) -> str:
    ranges, start = [], None
    for i, c in enumerate(cpus):
        if start is None:
            start = c
        if i + 1 == len(cpus) or cpus[i + 1] != c + 1:
            ranges.append(f"{start}-{c}" if c != start else str(c))
            start = None
    return ",".join(ranges)


def cpu_list(cpus: Optional[str]) -> Optional[str]:
    """A CPU list in its shortest form: '60,61-61,61' -> '60-61'; anything unparsable as it is."""
    if not cpus:
        return None
    seen = set()
    try:
        for part in str(cpus).replace(" ", "").split(","):
            a, _, b = part.partition("-")
            seen.update(range(int(a), int(b or a) + 1))
    except ValueError:
        return str(cpus)
    return _ranges(sorted(seen))


def cpu_count(cpus: Optional[str]) -> Optional[int]:
    """'0-8' -> 9, '0,2-3' -> 3."""
    listed = cpu_list(cpus)
    if not listed or not re.match(r"^[\d,-]+$", listed):
        return None
    return sum(
        int(b or a) - int(a) + 1
        for a, _, b in (p.partition("-") for p in listed.split(","))
    )


def own_cpus() -> Optional[str]:
    """This process's CPU affinity as a list ('60-63')."""
    try:
        return _ranges(sorted(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return None


def config_get(call: Callable[..., Any], set_keys: List[str] = ()) -> Dict[str, Any]:
    """CONFIG GET of the wanted settings only, so no secret is ever read; one key at a time for a server
    that takes a single pattern."""
    want = sorted(set(SERVER_CONFIG_KEYS) | {str(k) for k in set_keys})
    want = [k for k in want if not (set(k.split("-")) & SECRET_WORDS)]

    def pairs(got):
        return dict(zip(got[::2], got[1::2])) if isinstance(got, list) else got or {}

    out: Dict[str, Any] = {}
    try:
        out.update(pairs(call(*want)))
    except Exception:  # noqa: BLE001 - an older server takes one pattern per CONFIG GET
        for k in want:
            try:
                out.update(pairs(call(k)))
            except Exception:  # noqa: BLE001 - a setting this server does not have
                pass
    return out


def commit_time(run: Optional[Run], tree: Optional[str], sha: str) -> Optional[str]:
    if not run or not tree:
        return None
    rc, out, _ = run(f"git -C {shlex.quote(tree)} log -1 --format=%cI {sha}", 30)
    if rc:
        return None
    return out.strip() or None


def server_side(
    info: Dict[str, Any],
    config: Dict[str, Any],
    *,
    run: Optional[Run],
    sha: str,
    set_keys: List[str] = (),
    executable: Optional[str] = None,
    peer: Optional[str] = None,
    instance_type: Optional[str] = None,
) -> Dict[str, Any]:
    """The server's facts: its host (read with HOST_SCRIPT on the server's host when run reaches it, else the
    kernel and architecture INFO reports), its build and the settings it runs with."""
    pid = info.get("process_id")
    if run:
        host, cpus = collect_host(
            run, pid=int(pid) if str(pid).isdigit() else None, peer=peer
        )
    else:
        host, cpus = host_from_info(info), None
    if host["instance_type"] is None and instance_type:
        host["instance_type"] = instance_type
    return {
        "host": host,
        "cpus": cpus,
        "build_info": build_info(info, run, executable, sha),
        "server_config": server_config(config, set_keys),
    }


# ---------------------------------------------------------------- run facts
def add_arguments(p, ref: bool = True) -> None:
    """The options every runner takes for its run files."""
    p.add_argument(
        "--data-set",
        default="community",
        type=_data_set,
        help="data set of the run files: official-ci, community, curated:<owner> "
        "or fork:<owner>/<repo> (default community)",
    )
    if ref:
        p.add_argument(
            "--server-ssh",
            default=None,
            help="ssh target of a server on another host, so its host facts and binary are read there "
            "for the run file (a server on this host is read here)",
        )
        p.add_argument(
            "--server-instance-type",
            default=None,
            type=_instance_type,
            help="instance type of a server on another host when it cannot be read there",
        )
        p.add_argument(
            "--ref",
            default=None,
            help="branch or tag the commits come from, for the run file; "
            "defaults to a --commits value that is not a SHA, else the checkout's branch",
        )


def _data_set(value: str) -> str:
    if not schema.DATA_SET.match(value):
        raise ValueError(f"not a data set: {value!r}")
    return value


def _instance_type(value: str) -> str:
    if not INSTANCE_TYPE.match(value):
        raise ValueError(f"not an instance type: {value!r}")
    return value


def _git(path: Path, *args: str) -> Optional[str]:
    try:
        p = subprocess.run(
            ["git", "-C", str(path), *args], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode:
        return None
    return p.stdout.strip() or None


def github_repo(path: Path) -> Optional[str]:
    """owner/name of a checkout's GitHub origin, or None."""
    m = GITHUB_REMOTE.search(_git(path, "remote", "get-url", "origin") or "")
    return m.group(1) if m else None


@lru_cache(maxsize=1)
def harness() -> Dict[str, Any]:
    """This checkout of VPB: its GitHub repository and commit."""
    sha = _git(ROOT, "rev-parse", "HEAD")
    return {
        "repo": github_repo(ROOT) or HARNESS_REPO,
        "sha": sha if sha and SHA.match(sha) else None,
    }


def build_ref(commit: str, ref: Optional[str], checkout: Optional[Path]) -> str:
    """The branch a commit was taken from: --ref, else the commit argument when it names a branch or tag,
    else the checkout's branch."""
    if ref:
        return ref
    if commit and commit != "HEAD" and not SHA.match(commit):
        return commit
    branch = _git(checkout, "rev-parse", "--abbrev-ref", "HEAD") if checkout else None
    return branch if branch and branch != "HEAD" else "unknown"


def family(cfg: Dict[str, Any]) -> str:
    """Scenario family: the config's test_name, lowercased; the basic commands format is the continuous
    benchmark's."""
    name = cfg.get("test_name")
    return name.strip().lower() if name else "continuous"


def value_type(command: Optional[str]) -> str:
    word = (command or "").split(" ", 1)[0].upper()
    return next((t for t, names in TYPES.items() if word in names), "string")


def harness_config(
    cfg: Dict[str, Any], path: Optional[str]
) -> Tuple[Optional[str], Any]:
    """(config path in the harness repo, or None; the config itself when the path does not say it all)."""
    if path:
        p = Path(path)
        try:
            rel = (p if p.is_absolute() else Path.cwd() / p).resolve().relative_to(ROOT)
        except ValueError:
            rel = None
        if rel and rel.parts[0] == "configs":
            return rel.as_posix(), None
    generated = any(k in cfg for k in ("commands", "set_matrix", "matrix"))
    drop = {"groups_to_run", "scenario_filter", "server_cpu_ranges"}
    if generated:
        drop.add("test_groups")
    inline = {k: v for k, v in cfg.items() if k not in drop}
    return None, json.loads(json.dumps(inline, default=sorted))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", str(text).lower()).strip("-.") or "x"


def fingerprint_of(facts: Dict[str, Any]) -> Dict[str, Any]:
    """The run-level fingerprint object (its hash is set by schema.finish)."""
    b = facts["build_info"]
    return {
        "hash": None,
        "build": {k: b.get(k) for k in schema.BUILD_FACT_KEYS},
        "server": {k: facts["server_host"].get(k) for k in HOST_KEYS},
        "client": {k: facts["client_host"].get(k) for k in HOST_KEYS},
        "server_config": facts["server_config"],
        "load_generator": facts["load_generator"],
        "network_path": facts["network_path"],
        "pinning": pinning(facts.get("server_cpus"), facts.get("client_cpus")),
        "io_threads": facts.get("io_threads"),
        "cores": cpu_count(facts.get("server_cpus")),
    }


def run_doc(
    *,
    run_id: Optional[str],
    data_set: str,
    build: Dict[str, Any],
    harness_: Dict[str, Any],
    config: Tuple[Optional[str], Any],
    facts: Dict[str, Any],
    cells_: List[Dict[str, Any]],
    reproduce: str,
    started: Optional[str],
    finished: Optional[str],
    id_parts: List[str] = (),
) -> Dict[str, Any]:
    """A vpb-result run file. run_id None: '<started date>-<id parts>-<fingerprint hash prefix>'."""
    run = {
        "id": run_id,
        "data_set": data_set,
        "started": started,
        "finished": finished,
        "build": {
            "repo": build["repo"],
            "ref": build["ref"],
            "sha": build["sha"],
            "time": build.get("time"),
            "version": facts["build_info"].get("version"),
        },
        "harness": {
            "repo": harness_["repo"],
            "sha": harness_["sha"],
            "config": config[0],
            "config_inline": config[1],
        },
        "fingerprint": fingerprint_of(facts),
        "reproduce": reproduce,
    }
    doc = schema.finish({"schema": schema.SCHEMA, "run": run, "cells": cells_})
    if run_id is None:
        date = (started or now())[:10]
        h = doc["run"]["fingerprint"]["hash"][:8]
        doc["run"]["id"] = "-".join([date, *map(slug, id_parts), h])[:128]
    return doc


# ---------------------------------------------------------------- benchmark.py
def _worst(rows, field) -> Optional[float]:
    vals = [float(r[field]) for r in rows if r.get(field)]
    return max(vals) if vals else None


def measure(
    rows: List[Dict[str, Any]], windowed: bool = False
) -> Tuple[Dict[str, Any], bool, Optional[str]]:
    """Metric values and validity of one scenario in one invocation. The rows of the parallel clients of a
    mixed scenario belong together: client ops/s are summed, latency is the worst row, and the server-side
    rate, read over the window, is the first row's."""
    first = rows[0]
    why = next((r.get("error") for r in rows if r.get("error")), None)
    if not all(r.get("status", "success") == "success" for r in rows):
        ok, why = False, why or "failed"
    elif "window_complete" in first and not first["window_complete"]:
        ok, why = False, "measurement window incomplete"
    elif first.get("window_error_replies"):
        ok, why = False, f"{first['window_error_replies']} error replies in the window"
    elif "window_complete" in first and not first.get("server_ops_s"):
        ok, why = False, "no server-side rate"
    else:
        ok = True
    vals = {
        "ops": sum(float(r.get("rps") or 0) for r in rows) or None,
        "avg_ms": _worst(rows, "avg_latency_ms"),
        "p50_ms": _worst(rows, "p50_latency_ms"),
        "p95_ms": _worst(rows, "p95_latency_ms"),
        "p99_ms": _worst(rows, "p99_latency_ms"),
    }
    if windowed or first.get("server_ops_s") is not None:
        vals["server_ops"] = first.get("server_ops_s")
    # populate_bytes_per_key includes the server's fixed memory, so it means something only over many keys
    if (first.get("populate_keys") or 0) >= 1000:
        vals["bytes_per_key"] = first.get("populate_bytes_per_key")
    return {k: v for k, v in vals.items() if v is not None}, ok, why


def _cell(cfg, inv) -> Dict[str, Any]:
    """The fields that define the cell of one invocation of a scenario."""
    sc, rows, group = inv["scenario"], inv["rows"], inv.get("group")
    first = rows[0]
    fam = family(cfg)
    if "set_matrix" in cfg:
        shape_id, mix, cmd, ptag = sc["id"].split(".")
        sh = cfg["set_matrix"]["shapes"][shape_id]
        return {
            "id": sc["id"],
            "family": fam,
            "command": cmd,
            "command_line": " | ".join(r["command"] for r in rows),
            "shape": {
                "type": "set",
                "encoding": sh.get("encoding"),
                "size": sh["members"],
                "size_unit": "members",
            },
            "mix": mix,
            "pipeline": int(ptag[1:]),
        }
    basic = "commands" in cfg
    if sc.get("type") == "mixed":
        line = " | ".join(dict.fromkeys(r["command"] for r in rows if r.get("command")))
    else:
        line = sc.get("command")
    size = first.get("data_size")
    pipe = int(first.get("pipeline") or sc.get("pipeline") or 1)
    if basic:
        # a basic config's compiled scenarios are all named after their command
        cid = f"{sc['id']}-d{size}-p{pipe}-c{first.get('clients')}"
        if len(cfg.get("keyspacelen") or []) > 1:
            cid += f"-k{sc.get('keyspacelen')}"
    else:
        cid = f"{group}.{sc['id']}"
    return {
        "id": cid,
        "family": fam,
        "command": sc["test"].lower() if "test" in sc else cid.lower(),
        "command_line": line,
        "shape": {
            "type": value_type(sc.get("test") or line),
            "encoding": None,
            "size": size,
            "size_unit": "B",
        },
        "mix": "none",
        "pipeline": pipe,
    }


def benchmark_cells(
    cfg: Dict[str, Any], invocations: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """The cells of benchmark.py invocations of one run.

    invocations: one per scenario run, ``{"scenario", "group", "rows", "time", "pair", "rep"}``, with the metric
    rows it wrote to metrics.json. Reps of a cell are numbered in order unless an invocation gives its own.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for inv in invocations:
        if not inv["rows"]:
            continue
        cell = _cell(cfg, inv)
        # a scenario with a window measures server-side throughput, so a failed run of it still counts there
        windowed = inv["scenario"].get("window", cfg.get("window")) is not None
        vals, ok, why = measure(inv["rows"], windowed)
        # the parallel clients of a mixed scenario add up; other rows of one invocation share theirs
        rows = (
            inv["rows"] if inv["scenario"].get("type") == "mixed" else inv["rows"][:1]
        )
        cell["clients"] = sum(int(r.get("clients") or 0) for r in rows)
        c = out.setdefault(cell["id"], dict(cell, metrics={}, runs=[], missing=None))
        n = inv.get("rep") or len(c["runs"]) + 1
        c["runs"].append(
            cells.rep(n, vals, pair=inv.get("pair"), t=inv.get("time"), ok=ok, why=why)
        )
    return list(out.values())


def benchmark_reproduce(h, b, config, rerun: str) -> str:
    cfg = config[0] or "config.json"
    saved = "" if config[0] else " (harness.config_inline saved as config.json)"
    return (
        f"git clone https://github.com/{h['repo']} vpb && cd vpb && git checkout {h['sha'] or '<harness commit>'}"
        f" && pip install -r requirements.txt && git clone https://github.com/{b['repo']} ../valkey"
        f" && git -C ../valkey checkout {b['sha']} && python benchmark.py --commits {b['sha']}"
        f" --repository {b['repo']} --valkey-path ../valkey --config {cfg} --runs 1 {rerun}".rstrip()
        + saved
    )


def benchmark_run(
    cfg: Dict[str, Any],
    invocations: List[Dict[str, Any]],
    *,
    data_set: str,
    build: Dict[str, Any],
    config: Tuple[Optional[str], Any],
    facts: Dict[str, Any],
    rerun: str = "",
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """The run file of benchmark.py invocations of one build on one setup (one fingerprint). facts:
    ``server_host``, ``client_host``, ``server_cpus``, ``client_cpus``, ``build_info``, ``server_config``,
    ``load_generator``, ``network_path``, ``io_threads``."""
    h = harness()
    times = [i["time"] for i in invocations if i.get("time")]
    return run_doc(
        run_id=run_id,
        data_set=data_set,
        build=build,
        harness_=h,
        config=config,
        facts=facts,
        cells_=benchmark_cells(cfg, invocations),
        reproduce=benchmark_reproduce(h, build, config, rerun),
        started=min(times) if times else None,
        finished=now(),
        id_parts=[family(cfg), build["ref"], build["sha"][:9]],
    )


# ---------------------------------------------------------------- matrix_runner.py
def _matrix_cell(cell: Dict[str, Any], fam: str) -> Dict[str, Any]:
    p = cell["point"]
    line = " | ".join(c["command"] for c in cell["commands"])
    t = value_type(cell["commands"][0]["command"])
    size_key = MATRIX_SIZES.get(t)
    ttl = p.get("ttl_range")
    return {
        "id": cell["id"],
        "family": fam,
        "command": cell["template"],
        "command_line": line,
        "shape": {
            "type": t,
            "encoding": None,
            "size": p.get(size_key) if size_key else cell.get("value_size"),
            "size_unit": "members" if size_key else "B",
        },
        "mix": f"key ttl {ttl[0]}-{ttl[1]} s" if ttl else "none",
        "pipeline": int(cell["pipeline"]),
        "clients": int(cell["connections"]),
    }


MATRIX_METRICS = {
    "server_ops": "server_ops_s",
    "ops": "generator_ops_s",
    "p50_ms": "p50_ms",
    "p99_ms": "p99_ms",
    "p999_ms": "p999_ms",
}


def matrix_runs(
    cfg: Dict[str, Any],
    cells_: List[Dict[str, Any]],
    rows: List[Dict[str, Any]],
    manifest: Dict[str, Any],
    *,
    data_set: str,
    repository: Optional[str],
    config_path: Optional[str],
    server_cpus: Dict[str, Optional[str]],
    client_cpus: Optional[str],
    network: str,
) -> List[Dict[str, Any]]:
    """The run files of a matrix run: one per build and fingerprint. A cell's io-threads, server mode and
    settings, generator and TLS are run-level facts, so cells that differ in them are in different runs.

    manifest["vpb_hosts"] holds the server's and each generator host's facts (preflight), and each rep row its
    server's build and CONFIG GET (run_rep). Reps of one cell made in the same rotation share a pair id.
    server_cpus: cell id -> the server's CPUs; network: network_path() of the run.
    """
    h = harness()
    repo = repository or BUILD_REPO
    fam = family(cfg)
    vh = manifest.get("vpb_hosts") or {}
    server_host = vh.get("server") or dict.fromkeys(HOST_KEYS)
    gens = vh.get("generators") or []
    client_host = merge_hosts(gens) if gens else dict.fromkeys(HOST_KEYS)
    config = harness_config(cfg, config_path)
    versions = {
        "memtier": manifest.get("memtier") or None,
        "valkey-benchmark": manifest.get("valkey_benchmark") or None,
    }
    by_cell = {c["id"]: c for c in cells_}
    reps_of: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for r in rows:
        if r.get("scenario_id") in by_cell and r.get("commit"):
            reps_of.setdefault((r["build"], r["scenario_id"]), []).append(r)
    runs: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for (label, cid), reps in reps_of.items():
        spec = by_cell[cid]
        sha = reps[0]["commit"]
        built = next(
            (r["window_server_build"] for r in reps if r.get("window_server_build")), {}
        )
        conf = next(
            (r["window_server_config"] for r in reps if r.get("window_server_config")),
            None,
        )
        tls = bool(spec["point"].get("tls"))
        facts = {
            "server_host": server_host,
            "client_host": client_host,
            "server_cpus": server_cpus.get(cid),
            "client_cpus": client_cpus,
            "build_info": built.get("info")
            or dict.fromkeys(("version", *schema.BUILD_FACT_KEYS)),
            "server_config": conf,
            "load_generator": versions.get(spec["generator"]),
            "network_path": network + (", TLS" if tls and "TLS" not in network else ""),
            "io_threads": int(spec["io_threads"]),
        }
        cell = dict(_matrix_cell(spec, fam), metrics={}, missing=None)
        cell["runs"] = [
            cells.rep(
                r["run"],
                {
                    m: r.get(f)
                    for m, f in MATRIX_METRICS.items()
                    if r.get(f) is not None
                },
                pair=f"r{r['run']}",
                t=r.get("timestamp"),
                ok=r.get("status") == "ok",
                why=r.get("error") or r.get("status") or "invalid",
            )
            for r in sorted(reps, key=lambda r: r["run"])
        ]
        key = (label, json.dumps(fingerprint_of(facts), sort_keys=True))
        g = runs.setdefault(
            key,
            {
                "label": label,
                "sha": sha,
                "time": built.get("time"),
                "facts": facts,
                "cells": [],
                "t": [],
            },
        )
        g["cells"].append(cell)
        g["t"] += [r.get("timestamp") for r in reps if r.get("timestamp")]
    out = []
    for g in runs.values():
        cfgfile = config[0] or "config.json"
        reproduce = (
            f"git clone https://github.com/{h['repo']} vpb && cd vpb && git checkout {h['sha'] or '<harness commit>'}"
            f" && git clone https://github.com/{repo} ../valkey && git -C ../valkey checkout {g['sha']}"
            f" && make -C ../valkey -j && python matrix_runner.py --config {cfgfile}"
            f" --build {g['label']}={g['sha']}:../valkey/src/valkey-server"
            f" --scenarios {','.join(c['id'] for c in g['cells'])} --results-dir results"
            " --server-host <ssh target> --server-bind <server-ip> --target-ip <server-ip>"
            " --generator-hosts <ssh targets>"
        )
        out.append(
            run_doc(
                run_id=None,
                data_set=data_set,
                build={
                    "repo": repo,
                    "ref": g["label"],
                    "sha": g["sha"],
                    "time": g["time"],
                },
                harness_=h,
                config=config,
                facts=g["facts"],
                cells_=g["cells"],
                reproduce=reproduce,
                started=min(g["t"]) if g["t"] else None,
                finished=max(g["t"]) if g["t"] else None,
                id_parts=[fam, g["label"], g["sha"][:9]],
            )
        )
    return out


def server_build(
    info: Dict[str, Any], run: Optional[Run], executable: Optional[str], sha: str
) -> Dict[str, Any]:
    """A matrix rep's record of its server build: build_info and the commit time."""
    tree = os.path.dirname(os.path.dirname(executable)) if executable else None
    return {
        "info": build_info(info, run, executable, sha),
        "time": commit_time(run, tree, sha),
    }


# ---------------------------------------------------------------- files
def _check(path: Path, doc: Dict[str, Any]) -> None:
    for e in schema.check_doc(doc, str(path)):
        logging.warning(f"vpb-result: {e}")
    for hit in idscan.scan(path):
        logging.warning(f"vpb-result: {hit}")


def _dump(path: Path, doc: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def write_run(results_dir: Path, doc: Dict[str, Any], merge: bool = True) -> Path:
    """Write a run file to results_dir/vpb-runs/<run id>.json. With merge, a file there with the same
    fingerprint is the same run: the new reps join its cells. Problems are logged, never raised: the run's
    metrics.json stands either way."""
    folder = Path(results_dir) / RUNS_DIR
    if merge and folder.is_dir():
        for f in sorted(folder.glob("*.json")):
            try:
                old = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if not schema.is_run_doc(old):
                continue
            if old["run"]["fingerprint"]["hash"] != doc["run"]["fingerprint"]["hash"]:
                continue
            run = dict(doc["run"], id=old["run"]["id"])
            times = [t for t in (old["run"]["started"], doc["run"]["started"]) if t]
            run["started"] = min(times) if times else None
            merged = {
                "schema": schema.SCHEMA,
                "run": run,
                "cells": cells.merge_cells(old["cells"], doc["cells"]),
            }
            doc = schema.finish(merged)
            break
    path = folder / f"{doc['run']['id']}.json"
    _dump(path, doc)
    _check(path, doc)
    logging.info(f"Wrote run {doc['run']['id']} ({len(doc['cells'])} cells) to {path}")
    return path


def loopback(target: str) -> bool:
    return target in LOOPBACK
