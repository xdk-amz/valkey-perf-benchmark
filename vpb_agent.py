"""Host-side agent for benchmark hosts driven over ssh.

``python3 vpb_agent.py '<json>'`` runs one action on the host and prints one
JSON line: the result, or ``{"error": ...}`` with exit status 1. The
orchestrator copies this file and the modules it imports (procstat,
thread_pinning, binfacts, environment_metadata,
utils/cpu_utils) into the host's agent directory,
so it uses the standard library only and runs on Python 3.9.

The measurement window is timed here, not over ssh: ``window`` sleeps until an
absolute start time and reads the server's INFO and /proc counters at both
edges on the host's own clock, so ssh latency never lands inside the window.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import binfacts  # noqa: E402
import procstat  # noqa: E402

FILES = (
    "vpb_agent.py",
    "procstat.py",
    "binfacts.py",
    "environment_metadata.py",
    "thread_pinning.py",
    "utils/__init__.py",
    "utils/cpu_utils.py",
)


def _run(argv, timeout=30):
    return subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=timeout,
    )


def _expand(path):
    return os.path.expanduser(path) if isinstance(path, str) else path


# ---------- processes --------------------------------------------------------


def _supervise(a):
    """Run the child, record its pid and exit status beside its output."""
    out = open(a["out"], "w")
    err = open(a["err"], "w")
    try:
        child = subprocess.Popen(
            a["argv"],
            cwd=_expand(a.get("cwd")) or None,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
        )
    except OSError as exc:
        tmp = a["out"] + ".spawnerr.tmp"
        Path(tmp).write_text(f"{type(exc).__name__}: {exc}")
        os.replace(tmp, a["out"] + ".spawnerr")
        return
    tmp = a["out"] + ".pid.tmp"
    Path(tmp).write_text(str(child.pid))
    os.replace(tmp, a["out"] + ".pid")
    rc = child.wait()
    tmp = a["out"] + ".rc.tmp"
    Path(tmp).write_text(str(rc))
    os.replace(tmp, a["out"] + ".rc")


def act_spawn(a):
    """Start argv detached from the ssh session; returns the child's pid."""
    a["out"], a["err"] = _expand(a["out"]), _expand(a["err"])
    Path(a["out"]).parent.mkdir(parents=True, exist_ok=True)
    for suffix in (".pid", ".rc", ".spawnerr"):
        stale = Path(a["out"] + suffix)
        if stale.exists():
            stale.unlink()
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "_supervise", json.dumps(a)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.time() + 10
    pid_file = Path(a["out"] + ".pid")
    failed = Path(a["out"] + ".spawnerr")
    while not pid_file.exists():
        if failed.exists():
            raise RuntimeError(f"cannot start {a['argv'][0]}: {failed.read_text()}")
        if time.time() > deadline:
            raise RuntimeError(f"no pid for {a['argv'][0]} after 10s")
        time.sleep(0.02)
    return {"pid": int(pid_file.read_text())}


def _alive(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def act_poll(a):
    rc_file = Path(_expand(a["out"]) + ".rc")
    rc = int(rc_file.read_text()) if rc_file.exists() else None
    return {"alive": rc is None and _alive(a["pid"]), "rc": rc}


def act_signal(a):
    try:
        os.kill(int(a["pid"]), int(a["sig"]))
        return {"sent": True}
    except ProcessLookupError:
        return {"sent": False}


def act_pids(a):
    return {"pids": procstat.pids_of_binary(a["binary"])}


def act_kill(a):
    pids = procstat.pids_of_binary(a["binary"])
    for pid in pids:
        try:
            os.kill(pid, int(a.get("sig", signal.SIGTERM)))
        except ProcessLookupError:
            pass
    return {"pids": pids}


# ---------- host facts -------------------------------------------------------


def act_hello(a):
    here = Path(os.path.dirname(os.path.abspath(__file__)))
    return {
        "python": sys.version.split()[0],
        "files": {f: binfacts.sha256(here / f) for f in FILES},
        "home": str(Path.home()),
    }


def _irqbalance_active():
    try:
        return _run(["systemctl", "is-active", "irqbalance"]).stdout.strip() == "active"
    except Exception:
        return None


def act_facts(a):
    cpu = None
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith(("model name", "cpu part")):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    product = None
    try:
        product = Path("/sys/devices/virtual/dmi/id/product_name").read_text().strip()
    except OSError:
        pass
    return {
        "cpu_model": cpu,
        "cpus": os.cpu_count(),
        "machine": os.uname().machine,
        "kernel": os.uname().release,
        "product_name": product,
        "irqbalance_active": _irqbalance_active(),
    }


def act_envmeta(a):
    """The environment fields upstream records on every metrics row."""
    from environment_metadata import collect_environment_metadata

    return collect_environment_metadata(
        benchmark_path=a.get("benchmark_path"),
        server_cpu_range=a.get("server_cpu_range"),
        client_cpu_range=a.get("client_cpu_range"),
    )


def act_binfacts(a):
    return binfacts.binary_facts(Path(_expand(a["path"])))


def act_read(a):
    path = Path(_expand(a["path"]))
    if not path.exists():
        return {"text": None}
    data = path.read_bytes()
    limit = a.get("max_bytes")
    if limit:
        data = data[-int(limit) :]
    return {"text": data.decode(errors="replace")}


# ---------- server threads ---------------------------------------------------


def act_pin(a):
    from thread_pinning import pin_server_threads

    return {"layout": pin_server_threads(int(a["pid"]), a["spec"], a.get("io_threads"))}


# ---------- measurement window -----------------------------------------------


def _info(node):
    r = _run([*node["cli"], "info", "everything"], timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"INFO on port {node.get('port')}: {r.stderr.strip()}")
    return r.stdout


def _snapshot(a, cpus):
    snap = {"infos": []}
    t0 = time.time()
    for node in a.get("nodes", []):
        snap["infos"].append(_info(node))
    t1 = time.time()
    snap["t"] = (t0 + t1) / 2
    pids = a.get("pids", [])
    gen = a.get("gen_pids", [])
    if pids or gen:
        snap["ticks"] = procstat.ticks(
            [p for p in pids if Path(f"/proc/{p}").exists()], gen, cpus
        )
        snap["tt"] = time.time()
    return snap


def act_window(a):
    """Read INFO and /proc at ``at`` and ``at + window`` on this host's clock."""
    at, window = float(a["at"]), float(a["window"])
    pids = [p for p in a.get("pids", []) if Path(f"/proc/{p}").exists()]
    cpus = procstat.main_cpus(pids) if pids else None
    delay = at - time.time()
    if delay < 0:
        raise RuntimeError(f"window start already passed by {-delay:.3f}s")
    time.sleep(delay)
    perf = None
    perf_status = None
    if a.get("perf"):
        if len(pids) != 1:
            perf_status = "unavailable: more than one server"
        elif not shutil.which("perf"):
            perf_status = "unavailable: perf not installed"
    if a.get("perf") and perf_status is None:
        perf = subprocess.Popen(
            ["perf", "stat", "-e", a["perf"], "-t", str(pids[0]), "--"]
            + ["sleep", str(window)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
    snap_a = _snapshot(a, cpus)
    time.sleep(max(0.0, at + window - time.time()))
    snap_b = _snapshot(a, cpus)
    out = {
        "a": snap_a,
        "b": snap_b,
        "main_cpus": sorted(cpus) if cpus else None,
        "local_pids": pids,
    }
    if perf is not None:
        _, err = perf.communicate(timeout=window + 30)
        out["perf_text"] = err
        out["perf_rc"] = perf.returncode
    if perf_status:
        out["perf_status"] = perf_status
    return out


ACTIONS = {
    "hello": act_hello,
    "facts": act_facts,
    "binfacts": act_binfacts,
    "envmeta": act_envmeta,
    "read": act_read,
    "spawn": act_spawn,
    "poll": act_poll,
    "signal": act_signal,
    "pids": act_pids,
    "kill": act_kill,
    "pin": act_pin,
    "window": act_window,
}


def main(argv):
    if len(argv) == 3 and argv[1] == "_supervise":
        _supervise(json.loads(argv[2]))
        return 0
    try:
        a = json.loads(argv[1])
        print(json.dumps(ACTIONS[a["action"]](a)))
        return 0
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
