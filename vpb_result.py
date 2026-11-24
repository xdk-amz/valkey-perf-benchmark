"""Write a run's results as vpb-result/2 records (dashboards/explorer/SCHEMA.md), beside its metrics.json.

Every runner calls this module: benchmark.py, through ClientRunner, for the basic, test_groups and set_matrix
configs (one file per results directory, ``<results>/<commit>/vpb-result.json``, the records of each new
invocation merged into it), and matrix_runner.py for matrix configs (``<results>/<run>/vpb-result.json``,
rewritten from the run's rows after every rep). metrics.json is unchanged.

Every runner records the same facts, read at run time: the build (repository, ref, commit, commit time, and
from the server's INFO its version and build settings, the binary's sha256 and VPB's make arguments), the
server's settings (CONFIG GET), and for the server's host and the client's host the instance type, CPU model,
microcode, topology, memory, NIC, virtualization, cgroup CPU limit, OS, kernel, boot options and kernel runtime
settings (HOST_SCRIPT, run here or over ssh), plus io-threads, cores, pinning, network path, load generator,
pipeline, clients and the harness commit. A fact a host cannot report is null, and the source's ``lacks``
names each one with its reason. No host identifier is read into a record.

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

from dashboards.explorer import idscan, records, schema

FILENAME = "vpb-result.json"
HARNESS_NAME = "valkey-perf-benchmark"
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


def parse_host(out: str) -> Tuple[Dict[str, Any], Optional[str], Dict[str, str]]:
    """(host facts, CPUs the measured process may run on, why each null field is null) from HOST_SCRIPT."""
    sec = _sections(out)
    lscpu = _fields(sec.get("lscpu", []))
    cpuinfo = _fields(sec.get("cpuinfo", []))
    why: Dict[str, str] = {}

    imds, product = _first(sec, "imds"), _first(sec, "dmi_product")
    if INSTANCE_TYPE.match(imds):
        itype = imds
    elif "Amazon EC2" in _first(sec, "dmi_vendor") and INSTANCE_TYPE.match(product):
        itype = product  # EC2 puts the instance type in the DMI product name
    else:
        itype = None
        why["instance_type"] = "no instance metadata or EC2 DMI data: not an EC2 host"

    model = lscpu.get("Model name", "")
    if not model or model == "-":
        impl, part = cpuinfo.get("CPU implementer", ""), cpuinfo.get("CPU part", "")
        name = ARM_PARTS.get((impl, part))
        if name:
            v, r = cpuinfo.get("CPU variant", "0x0"), cpuinfo.get("CPU revision", "0")
            model = f"{name} r{int(v, 16)}p{r}"
        elif impl:
            model = f"implementer {impl} part {part}"
    model = model or None
    if not model:
        why["cpu_model"] = "neither lscpu nor /proc/cpuinfo names the CPU"

    micro = cpuinfo.get("microcode") or None
    if not micro and _first(sec, "midr"):
        micro = f"midr {_first(sec, 'midr')} revidr {_first(sec, 'revidr') or 'n/a'}"
    if not micro:
        why["cpu_microcode"] = "the kernel reports no microcode or CPU revision"

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
    else:
        why["cpu_topology"] = "lscpu is not installed"

    mem = _first(sec, "mem").split()
    memory = f"{round(int(mem[0]) / 1048576)} GiB" if mem and mem[0].isdigit() else None
    if not memory:
        why["memory"] = "/proc/meminfo is unreadable"

    nic_f = _fields(sec.get("nic", []), "=")
    dev = nic_f.get("dev", "")
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
    else:
        nic = None
        why["nic"] = "no route to the peer and no default route"

    vm, ctr = _first(sec, "vm"), _first(sec, "container")
    hyp = lscpu.get("Hypervisor vendor") or _first(sec, "hypervisor")
    parts = []
    if vm:
        parts.append("bare metal" if vm == "none" else f"vm {vm}")
    elif hyp:
        parts.append(f"vm {hyp}")
    if ctr and ctr != "none":
        parts.append(f"container {ctr}")
    virt = ", ".join(parts) or None
    if not virt:
        why["virtualization"] = (
            "systemd-detect-virt is not installed and no hypervisor is reported"
        )

    cpumax = _first(sec, "cpumax").split()
    if cpumax and cpumax[0] == "max":
        cg = "none"
    elif len(cpumax) == 2 and cpumax[0].isdigit():
        cg = f"{int(cpumax[0]) / int(cpumax[1]):g} CPUs"
    else:
        cg = None
        why["cgroup_cpu_limit"] = "no cgroup v2 cpu.max for the process"

    cmd = _first(sec, "cmdline")
    boot = boot_options(cmd) if cmd else None
    if boot is None:
        why["kernel_boot_options"] = "/proc/cmdline is unreadable"

    idle = [x.strip() for x in sec.get("idle", []) if x.strip()]
    boost, no_turbo = _first(sec, "boost"), _first(sec, "no_turbo")
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
        "turbo": (
            {"1": "on", "0": "off"}[boost]
            if boost in ("0", "1")
            else (
                {"0": "on", "1": "off"}[no_turbo]
                if no_turbo in ("0", "1")
                else "not available"
            )
        ),
        "idle_states": (
            "not available"
            if not idle
            else (
                "all disabled"
                if all(x == "1" for x in idle)
                else "some disabled" if any(x == "1" for x in idle) else "all enabled"
            )
        ),
    }
    host = {
        "instance_type": itype,
        "arch": _first(sec, "arch") or None,
        "cpu_model": model,
        "cpu_microcode": micro,
        "cpu_topology": topo,
        "memory": memory,
        "nic": nic,
        "virtualization": virt,
        "cgroup_cpu_limit": cg,
        "os": _first(sec, "os") or None,
        "kernel": _first(sec, "kernel") or None,
        "kernel_boot_options": boot,
        "kernel_runtime_settings": runtime,
    }
    for k in ("arch", "os", "kernel"):
        if host[k] is None:
            why[k] = f"the host did not report its {k}"
    return host, _first(sec, "affinity") or None, why


def collect_host(
    run: Run = local_run, pid: Optional[int] = None, peer: Optional[str] = None
) -> Tuple[Dict[str, Any], Optional[str], Dict[str, str]]:
    """Read a host's facts with HOST_SCRIPT. pid: the process whose cgroup and CPUs count (default the
    script's shell); peer: the address the benchmark traffic goes to, used only to pick the interface.
    """
    env = []
    if pid:
        env.append(f"VPB_PID={int(pid)}")
    if peer and PEER.match(peer):
        env.append(f"VPB_PEER={shlex.quote(peer)}")
    cmd = (" ".join(env) + " bash -s") if env else "bash -s"
    rc, out, err = run(cmd, 60, HOST_SCRIPT)
    if rc and "@@" not in out:
        host = dict.fromkeys(HOST_KEYS)
        reason = f"host facts unreadable ({(err or '').strip()[-120:] or f'exit {rc}'})"
        return host, None, {k: reason for k in HOST_KEYS}
    return parse_host(out)


def host_from_info(
    info: Dict[str, Any], reason: str
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """What a server on an unreachable host says about its host in INFO: kernel and CPU architecture."""
    words = str(info.get("os") or "").split()
    host = dict.fromkeys(HOST_KEYS)
    if len(words) >= 3:
        host["kernel"], host["arch"] = words[1], words[-1]
    why = {k: reason for k, v in host.items() if v is None}
    return host, why


def merge_hosts(hosts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """One host record for several generator hosts: a field they agree on, else their values joined."""
    out = {}
    for k in HOST_KEYS:
        vals = []
        for h in hosts:
            if h.get(k) is not None and h[k] not in vals:
                vals.append(h[k])
        out[k] = (
            None
            if not vals
            else vals[0] if len(vals) == 1 else ", ".join(sorted(map(str, vals)))
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
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """The server build: its version and build settings from INFO, the binary's sha256, and the make
    arguments VPB's builder stamped into the tree (valkey_build.py), read on the server's host.
    """
    why = {}
    version = info.get("valkey_version") or info.get("redis_version")
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
        q = shlex.quote(str(exe))
        rc, out, _ = run(f"sha256sum {q}", 60)
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
        else:
            why["build_info.args"] = (
                "the server binary was not built by VPB's builder, so its make arguments are unknown"
            )
    reason = "the server is on another host and no ssh target was given (--server-ssh)"
    if digest is None:
        why["build_info.binary_sha256"] = why.get(
            "build_info.binary_sha256",
            reason if not run else "the binary is unreadable",
        )
    if args is None and "build_info.args" not in why:
        why["build_info.args"] = reason
    if version is None:
        why["build_info.version"] = "INFO server reports no version"
    if settings is None:
        why["build_info.settings"] = "INFO reports no build settings"
    return {
        "version": version,
        "settings": settings,
        "args": args,
        "binary_sha256": digest,
    }, why


def network_path(same_host: bool, loopback: bool, tls: bool = False) -> str:
    path = (
        "loopback"
        if same_host and loopback
        else (
            "same host, non-loopback address"
            if same_host
            else "network, separate hosts"
        )
    )
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


def own_cpus() -> Optional[str]:
    """This process's CPU affinity as a list ('60-63')."""
    try:
        return _ranges(sorted(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return None


# ---------------------------------------------------------------- run facts
def add_arguments(p, ref: bool = True) -> None:
    """The options every runner takes for its vpb-result file."""
    p.add_argument(
        "--data-set",
        default="community",
        type=_data_set,
        help="data set of the vpb-result records: official-ci, community, curated:<owner> "
        "or fork:<owner>/<repo> (default community)",
    )
    if ref:
        p.add_argument(
            "--server-ssh",
            default=None,
            help="ssh target of a server on another host, so its host facts and binary are read there "
            "for the vpb-result records (a server on this host is read here)",
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
            help="branch or tag the commits come from, for the vpb-result records; "
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
        "name": HARNESS_NAME,
        "repo": github_repo(ROOT) or HARNESS_REPO,
        "sha": sha if sha and SHA.match(sha) else None,
    }


def cpu_count(cpus: Optional[str]) -> Optional[int]:
    """'0-8' -> 9, '0,2-3' -> 3."""
    if not cpus:
        return None
    seen = set()
    for part in str(cpus).split(","):
        a, _, b = part.strip().partition("-")
        seen.update(range(int(a), int(b or a) + 1))
    return len(seen)


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


def config_for_rerun(cfg: Dict[str, Any], path: Optional[str]) -> Dict[str, Any]:
    """The config a rerun uses: its path in the harness repo, or the config itself without what the run
    generated from it."""
    if path:
        p = Path(path)
        try:
            rel = (p if p.is_absolute() else Path.cwd() / p).resolve().relative_to(ROOT)
        except ValueError:
            rel = None
        if rel and rel.parts[0] == "configs":
            return {"path": rel.as_posix(), "overrides": {}}
    generated = any(k in cfg for k in ("commands", "set_matrix", "matrix"))
    drop = {"groups_to_run", "scenario_filter", "server_cpu_ranges"}
    if generated:
        drop.add("test_groups")
    inline = {k: v for k, v in cfg.items() if k not in drop}
    return {"inline": json.loads(json.dumps(inline, default=sorted))}


def parameters(scenario: Dict[str, Any]) -> Dict[str, Any]:
    """A scenario's own definition as it ran, without the runner's internal markers."""
    clean = {k: v for k, v in scenario.items() if not str(k).startswith("_")}
    return json.loads(json.dumps(clean, default=sorted))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Every field a vpb-result/2 record can carry from the run; a null one is named in the source's lacks.
FACT_PATHS = (
    [
        "io_threads",
        "hardware.server",
        "hardware.cores",
        "hardware.client",
        "hardware.arch",
    ]
    + ["harness.sha", "build.time"]
    + [f"build_info.{k}" for k in schema.BUILD_INFO_KEYS]
    + ["server_config", "load_generator", "network_path", "pinning", "parameters"]
    + [f"host.server.{k}" for k in HOST_KEYS]
    + [f"host.client.{k}" for k in HOST_KEYS]
)


def _get(r: Dict[str, Any], path: str) -> Any:
    v: Any = r
    for k in path.split("."):
        v = v.get(k) if isinstance(v, dict) else None
    return v


def null_fields(recs: List[Dict[str, Any]]) -> List[str]:
    """The fact paths some record leaves null."""
    return [p for p in FACT_PATHS if any(_get(r, p) is None for r in recs)]


def lacks(recs: List[Dict[str, Any]], why: Dict[str, str]) -> List[str]:
    return [
        f"{p} not recorded: {why.get(p, 'not reported by the run')}"
        for p in null_fields(recs)
    ]


def hardware_of(facts: Dict[str, Any]) -> Dict[str, Any]:
    s, c = facts["server_host"], facts["client_host"]
    return {
        "server": s.get("instance_type"),
        "cores": cpu_count(facts.get("server_cpus")),
        "client": c.get("instance_type"),
        "arch": s.get("arch"),
    }


def record_facts(facts: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """The vpb-result/2 fields of one record."""
    return {
        "build_info": facts["build_info"],
        "server_config": facts["server_config"],
        "load_generator": facts["load_generator"],
        "network_path": facts["network_path"],
        "pinning": pinning(facts.get("server_cpus"), facts.get("client_cpus")),
        "parameters": params,
        "host": {"server": facts["server_host"], "client": facts["client_host"]},
    }


def _prefixed(why: Dict[str, str], prefix: str) -> Dict[str, str]:
    return {f"{prefix}.{k}": v for k, v in why.items()}


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
    no_host_reason: str = "the server is on another host and no ssh target was given (--server-ssh)",
) -> Dict[str, Any]:
    """The server's facts: its host (read with HOST_SCRIPT on the server's host when run reaches it, else
    the kernel and architecture INFO reports), its build and the settings it runs with.
    """
    pid = info.get("process_id")
    why_cpus = {}
    if run:
        host, affinity, why = collect_host(
            run, pid=int(pid) if str(pid).isdigit() else None, peer=peer
        )
    else:
        host, why = host_from_info(info, no_host_reason)
        affinity = None
        why_cpus = {"hardware.cores": no_host_reason}
    if host["instance_type"] is None and instance_type:
        host["instance_type"] = instance_type
        why.pop("instance_type", None)
    binfo, bwhy = build_info(info, run, executable, sha)
    if not config:
        bwhy["server_config"] = "the server answered no CONFIG GET"
    why = _prefixed(why, "host.server")
    if "host.server.instance_type" in why:
        why["hardware.server"] = why["host.server.instance_type"]
    return {
        "host": host,
        "cpus": affinity,
        "build_info": binfo,
        "server_config": server_config(config, set_keys),
        "why": {**why, **why_cpus, **bwhy},
    }


def commit_time(run: Optional[Run], tree: Optional[str], sha: str) -> Optional[str]:
    if not run or not tree:
        return None
    rc, out, _ = run(f"git -C {shlex.quote(tree)} log -1 --format=%cI {sha}", 30)
    if rc:
        return None
    return out.strip() or None


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
    return vals, ok, why


def _cell(cfg, inv) -> Dict[str, Any]:
    """The record fields that define the cell of one invocation of a scenario."""
    sc, rows, group = inv["scenario"], inv["rows"], inv.get("group")
    first = rows[0]
    if "set_matrix" in cfg:
        shape_id, mix, cmd, ptag = sc["id"].split(".")
        sh = cfg["set_matrix"]["shapes"][shape_id]
        return {
            "scenario": sc["id"],
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
            "workload": "",  # 16 B members, or integers for intset: the shape says it
        }
    basic = "commands" in cfg
    if sc.get("type") == "mixed":
        line = " | ".join(dict.fromkeys(r["command"] for r in rows if r.get("command")))
    else:
        line = sc.get("command")
    workload = []
    keys = sc.get("keyspacelen")
    if keys:
        workload.append(f"{int(keys):,} keys")
    if first.get("cluster_mode"):
        workload.append("cluster")
    if first.get("tls"):
        workload.append("TLS")
    workload += [f"{k} {v}" for k, v in sorted((first.get("config_set") or {}).items())]
    if sc.get("dataset"):
        workload.append(f"dataset {Path(sc['dataset']).name}")
    size = first.get("data_size")
    # a basic config's compiled scenarios are all named after their command
    scenario = (
        f"{sc['id']}-d{size}-p{first.get('pipeline')}-c{first.get('clients')}"
        if basic
        else f"{group}.{sc['id']}"
    )
    return {
        "scenario": scenario,
        "command": (
            sc["test"].lower() if "test" in sc else f"{group}.{sc['id']}".lower()
        ),
        "command_line": line,
        "shape": {
            "type": value_type(sc.get("test") or line),
            "encoding": None,
            "size": size,
            "size_unit": "B",
        },
        "mix": "none",
        "pipeline": int(first.get("pipeline") or sc.get("pipeline") or 1),
        "workload": ", ".join(workload),
    }


def benchmark_records(
    cfg: Dict[str, Any], invocations: List[Dict[str, Any]], ctx: Dict[str, Any]
) -> Dict[str, Any]:
    """A vpb-result/2 document for benchmark.py invocations of one build.

    invocations: one per scenario run, ``{"scenario", "group", "rows", "time", "pair"}``, with the metric rows
    it wrote to metrics.json. ctx: the run's facts (``run_context`` makes it).
    """
    h, b, facts = ctx["harness"], ctx["build"], ctx["facts"]
    hw = hardware_of(facts)
    config = ctx["config"]
    steps = [
        f"git clone https://github.com/{h['repo']} vpb && cd vpb && git checkout {h['sha'] or '<harness commit>'}",
        "pip install -r requirements.txt",
        f"git clone https://github.com/{b['repo']} ../valkey && git -C ../valkey checkout {b['sha']}",
    ]
    if "inline" in config:
        steps.append("# save the config above as config.json")
    steps.append(
        f"python benchmark.py --commits {b['sha']} --repository {b['repo']} --valkey-path ../valkey "
        f"--config {config.get('path', 'config.json')} --scenarios <scenario> --runs 1 {ctx['rerun']}".rstrip()
    )
    source = f"vpb:{ctx['data_set']}"
    cells: Dict[str, Dict[str, Any]] = {}
    params: Dict[str, Dict[str, Any]] = {}
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
        clients = sum(int(r.get("clients") or 0) for r in rows)
        for metric, v in vals.items():
            key = json.dumps([cell, clients, metric], sort_keys=True)
            if key not in cells:
                cells[key] = dict(cell, clients=clients, metric=metric, runs=[])
                params[key] = parameters(inv["scenario"])
            cells[key]["runs"].append(
                records.run(
                    v,
                    pair=inv.get("pair"),
                    t=inv.get("time"),
                    ok=ok and v is not None,
                    why=why or "no measurement",
                )
            )
    # a metric no cell of the run reported (no p95 in the generator's output, say) is left out; a cell whose
    # runs all failed keeps its metrics, so it shows as missing
    reported = {
        c["metric"]
        for c in cells.values()
        if any(r["v"] is not None for r in c["runs"])
    } | {"ops"}
    pending = [
        dict(
            data_set=ctx["data_set"],
            source=source,
            build=b,
            family=family(cfg),
            io_threads=ctx["io_threads"],
            hardware=hw,
            harness=h,
            facts=record_facts(facts, params[k]),
            **c,
        )
        for k, c in cells.items()
        if c["metric"] in reported
    ]
    why = dict(facts["why"])
    if ctx["io_threads"] is None:
        why["io_threads"] = "the server answered no CONFIG GET io-threads"
    if h["sha"] is None:
        why["harness.sha"] = "the harness checkout is not a git tree"
    gaps = lacks([{**r, **r["facts"]} for r in pending], why)
    block = {
        "harness": {
            "name": h["name"],
            "repo": f"https://github.com/{h['repo']}",
            "sha": h["sha"],
        },
        "build": {"repo": f"https://github.com/{b['repo']}", "sha": b["sha"]},
        "config": config,
        "steps": steps,
        "setup": ctx["setup"],
        "gaps": gaps,
    }
    rk = records.reproduce_key(block)
    recs = [records.record(reproduce=rk, **r) for r in pending]
    sources = [
        {
            "id": source,
            "name": f"{HARNESS_NAME}, {family(cfg)}",
            "data_set": ctx["data_set"],
            "url": f"https://github.com/{h['repo']}",
            "lacks": gaps,
        }
    ]
    return schema.make_doc(sources, recs, {rk: block}, records.METRICS)


def run_context(
    *,
    data_set: str,
    build: Dict[str, Any],
    io_threads: Optional[int],
    config: Dict[str, Any],
    rerun: str,
    facts: Dict[str, Any],
) -> Dict[str, Any]:
    """Facts of a benchmark.py run. facts: ``server_host``, ``client_host``, ``server_cpus``, ``client_cpus``,
    ``build_info``, ``server_config``, ``load_generator``, ``network_path`` and ``why`` (the reason for each
    null one, by record path)."""
    s, c = facts["server_host"], facts["client_host"]
    setup = "; ".join(
        x
        for x in [
            f"network path {facts['network_path']}",
            f"server {s.get('instance_type') or 'instance type not recorded'}, {s.get('cpu_model')}, "
            f"{s.get('os')}, kernel {s.get('kernel')}",
            f"client {c.get('instance_type') or 'instance type not recorded'}, {c.get('cpu_model')}",
            pinning(facts.get("server_cpus"), facts.get("client_cpus")),
            (
                f"load generator {facts['load_generator']}"
                if facts["load_generator"]
                else ""
            ),
        ]
        if x
    )
    return {
        "data_set": data_set,
        "build": build,
        "harness": harness(),
        "io_threads": io_threads,
        "config": config,
        "rerun": rerun,
        "setup": setup,
        "facts": facts,
    }


# ---------------------------------------------------------------- matrix_runner.py
def _matrix_cell(cell: Dict[str, Any]) -> Dict[str, Any]:
    p = cell["point"]
    line = " | ".join(c["command"] for c in cell["commands"])
    t = value_type(cell["commands"][0]["command"])
    size_key = MATRIX_SIZES.get(t)
    ttl = p.get("ttl_range")
    reads = p.get("read_fraction")
    workload = [
        cell["generator"],
        f"{int(cell['keys']):,} keys, {p.get('access_pattern', 'uniform')}",
    ]
    if reads is not None and 0 < reads < 1:
        workload.append(f"{round(reads * 100)}% reads")
    if cell.get("cluster_mode"):
        workload.append("cluster")
    if p.get("tls"):
        workload.append("TLS")
    workload += [
        f"{k} {v}" for k, v in sorted((cell.get("server_config") or {}).items())
    ]
    return {
        "scenario": cell["id"],
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
        "io_threads": int(cell["io_threads"]),
        "workload": ", ".join(workload),
    }


MATRIX_METRICS = {
    "server_ops": "server_ops_s",
    "ops": "generator_ops_s",
    "p50_ms": "p50_ms",
    "p99_ms": "p99_ms",
    "p999_ms": "p999_ms",
}


def matrix_records(
    cfg: Dict[str, Any],
    cells: List[Dict[str, Any]],
    rows: List[Dict[str, Any]],
    manifest: Dict[str, Any],
    *,
    data_set: str,
    repository: Optional[str],
    config_path: Optional[str],
    server_cpus: Dict[str, Optional[str]],
    client_cpus: Optional[str],
    network: str,
) -> Dict[str, Any]:
    """A vpb-result/2 document for a matrix run: a record per cell, build and metric, a run per rep.

    Reps of one cell made in the same rotation share a pair id. manifest["vpb_hosts"] holds the server's and
    each generator host's facts (preflight), and each rep row its server's build and CONFIG GET (run_rep).
    server_cpus: cell id -> the server's CPUs; network: network_path() of the run.
    """
    h = harness()
    repo = repository or BUILD_REPO
    vh = manifest.get("vpb_hosts") or {}
    server_host = vh.get("server") or dict.fromkeys(HOST_KEYS)
    gens = vh.get("generators") or []
    client_host = merge_hosts(gens) if gens else dict.fromkeys(HOST_KEYS)
    why = dict(vh.get("why") or {})
    config = config_for_rerun(cfg, config_path)
    steps = [
        f"git clone https://github.com/{h['repo']} vpb && cd vpb && git checkout {h['sha'] or '<harness commit>'}",
        f"git clone https://github.com/{repo} ../valkey && git -C ../valkey checkout <sha> && make -C ../valkey -j",
    ]
    if "inline" in config:
        steps.append("# save the config above as config.json")
    steps.append(
        f"python matrix_runner.py --config {config.get('path', 'config.json')} "
        "--build <label>=<sha>:../valkey/src/valkey-server --scenarios <scenario> --results-dir results "
        "--server-host <ssh target> --server-bind <server-ip> --target-ip <server-ip> --generator-hosts <ssh targets>"
    )
    setup = "; ".join(
        x
        for x in [
            f"network path {network}",
            f"server {server_host.get('instance_type') or 'instance type not recorded'}, "
            f"{server_host.get('cpu_model')}, {server_host.get('os')}, kernel {server_host.get('kernel')}"
            + (
                f", layout {manifest['server_layout']}"
                if manifest.get("server_layout")
                else ""
            ),
            f"generators {client_host.get('instance_type') or 'instance type not recorded'}"
            + (
                f" on CPUs {manifest['generator_cpus']}"
                if manifest.get("generator_cpus")
                else ""
            ),
            f"builds alternate per rep with a server restart before each, {manifest.get('reps')} reps",
        ]
        if x
    )
    generator_versions = {
        "memtier": manifest.get("memtier"),
        "valkey-benchmark": manifest.get("valkey_benchmark"),
    }
    by_cell = {c["id"]: c for c in cells}
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for r in rows:
        if r.get("scenario_id") in by_cell and r.get("commit"):
            groups.setdefault((r["scenario_id"], r["build"]), []).append(r)
    pending = []
    for (cid, label), reps in groups.items():
        cell = _matrix_cell(by_cell[cid])
        sha = reps[0]["commit"]
        built = next(
            (r["window_server_build"] for r in reps if r.get("window_server_build")),
            {},
        )
        conf = next(
            (r["window_server_config"] for r in reps if r.get("window_server_config")),
            None,
        )
        why.update(built.get("why") or {})
        if not built:
            why["build.time"] = why["build_info.version"] = (
                "no rep of this build reached the server's INFO"
            )
        if conf is None:
            why["server_config"] = "no rep reached the server's CONFIG GET"
        gen = by_cell[cid]["generator"]
        version = generator_versions.get(gen)
        if not version:
            why["load_generator"] = f"{gen} --version printed nothing"
        facts = {
            "server_host": server_host,
            "client_host": client_host,
            "server_cpus": server_cpus.get(cid),
            "client_cpus": client_cpus,
            "build_info": built.get("info") or dict.fromkeys(schema.BUILD_INFO_KEYS),
            "server_config": conf,
            "load_generator": version,
            "network_path": network,
        }
        build = {"repo": repo, "ref": label, "sha": sha, "time": built.get("time")}
        for metric, field in MATRIX_METRICS.items():
            runs = [
                records.run(
                    r.get(field),
                    pair=f"r{r['run']}",
                    t=r.get("timestamp"),
                    ok=r.get("status") == "ok" and r.get(field) is not None,
                    why=r.get("error") or r.get("status") or "no measurement",
                )
                for r in sorted(reps, key=lambda r: r["run"])
            ]
            pending.append(
                dict(
                    data_set=data_set,
                    build=build,
                    family=family(cfg),
                    hardware=hardware_of(facts),
                    harness=h,
                    metric=metric,
                    runs=runs,
                    facts=record_facts(facts, parameters(by_cell[cid]["point"])),
                    **cell,
                )
            )
    gaps = lacks([{**r, **r["facts"]} for r in pending], why)
    block = {
        "harness": {
            "name": h["name"],
            "repo": f"https://github.com/{h['repo']}",
            "sha": h["sha"],
        },
        "build": {"repo": f"https://github.com/{repo}", "sha": None},
        "config": config,
        "steps": steps,
        "setup": setup,
        "gaps": gaps,
    }
    recs, reproduce = [], {}
    source = f"vpb:{data_set}"
    for r in pending:
        blk = dict(
            block,
            build={"repo": f"https://github.com/{repo}", "sha": r["build"]["sha"]},
        )
        rk = records.reproduce_key(blk)
        reproduce[rk] = blk
        recs.append(records.record(source=source, reproduce=rk, **r))
    sources = [
        {
            "id": source,
            "name": f"{HARNESS_NAME}, {family(cfg)}",
            "data_set": data_set,
            "url": f"https://github.com/{h['repo']}",
            "lacks": gaps,
        }
    ]
    return schema.make_doc(sources, recs, reproduce, records.METRICS)


def server_build(
    info: Dict[str, Any], run: Optional[Run], executable: Optional[str], sha: str
) -> Dict[str, Any]:
    """A matrix rep's record of its server build: build_info, the commit time and why any is null."""
    binfo, why = build_info(info, run, executable, sha)
    tree = os.path.dirname(os.path.dirname(executable)) if executable else None
    t = commit_time(run, tree, sha)
    if t is None:
        why["build.time"] = "git cannot read the commit time in the binary's tree"
    return {"info": binfo, "time": t, "why": why}


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


# ---------------------------------------------------------------- files
def write(results_dir: Path, doc: Dict[str, Any], merge: bool = True) -> Path:
    """Write doc to results_dir/vpb-result.json, merged into the file already there unless merge is False.
    Problems are logged, never raised: the run's metrics.json stands either way."""
    path = Path(results_dir) / FILENAME
    if merge and path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            if schema.is_result_doc(old):
                doc = records.merge_docs([old, doc])
        except ValueError:
            logging.warning(f"{path}: unreadable, rewritten from this run")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    tmp.replace(path)
    for e in schema.check_doc(doc, str(path)):
        logging.warning(f"vpb-result: {e}")
    for hit in idscan.scan(path):
        logging.warning(f"vpb-result: {hit}")
    logging.info(f"Wrote {len(doc['records'])} {doc['schema']} records to {path}")
    return path


def loopback(target: str) -> bool:
    return target in LOOPBACK
