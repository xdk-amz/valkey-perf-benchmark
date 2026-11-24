"""Write a run's results as vpb-result/1 records (dashboards/explorer/SCHEMA.md), beside its metrics.json.

Every runner calls this module: benchmark.py, through ClientRunner, for the basic, test_groups and set_matrix
configs (one file per results directory, ``<results>/<commit>/vpb-result.json``, the records of each new
invocation merged into it), and matrix_runner.py for matrix configs (``<results>/<run>/vpb-result.json``,
rewritten from the run's rows after every rep). metrics.json is unchanged.

The records carry what the run knows: the build's repository, branch and commit, the harness commit, the
server and client instance types (EC2 instance metadata), the server's cores, io-threads and CPU architecture,
pipeline and clients. What it does not know is null, listed in the source's ``lacks`` and the rerun ``gaps``.

Standard library only, so matrix_runner.py stays runnable on a bench host's Python 3.9.
"""

import json
import logging
import re
import subprocess
import urllib.request
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


# ---------------------------------------------------------------- run facts
def add_arguments(p, ref: bool = True) -> None:
    """The options every runner takes for its vpb-result/1 file."""
    p.add_argument(
        "--data-set",
        default="community",
        type=_data_set,
        help="data set of the vpb-result/1 records: official-ci, community, curated:<owner> "
        "or fork:<owner>/<repo> (default community)",
    )
    if ref:
        p.add_argument(
            "--server-instance-type",
            default=None,
            type=_instance_type,
            help="instance type of a server on another host, for the vpb-result/1 records "
            "(a server on this host is read from the instance metadata)",
        )
        p.add_argument(
            "--ref",
            default=None,
            help="branch or tag the commits come from, for the vpb-result/1 records; "
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


@lru_cache(maxsize=1)
def instance_type() -> Optional[str]:
    """This host's EC2 instance type from the instance metadata (IMDSv2), or None off EC2."""
    base = "http://169.254.169.254/latest"
    try:
        req = urllib.request.Request(
            f"{base}/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        )
        token = urllib.request.urlopen(req, timeout=1).read().decode()
        req = urllib.request.Request(
            f"{base}/meta-data/instance-type",
            headers={"X-aws-ec2-metadata-token": token},
        )
        value = urllib.request.urlopen(req, timeout=1).read().decode().strip()
    except Exception:  # noqa: BLE001 - off EC2, or metadata disabled
        return None
    return value if INSTANCE_TYPE.match(value) else None


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


def _lacks(hardware: Dict[str, Any], io_threads, harness_sha, build_time) -> List[str]:
    names = {
        "server instance type": hardware.get("server"),
        "server cores": hardware.get("cores"),
        "client instance type": hardware.get("client"),
        "CPU architecture": hardware.get("arch"),
        "io-threads": io_threads,
        "harness commit": harness_sha,
        "commit time": build_time,
    }
    return [f"{k} not recorded" for k, v in names.items() if v is None]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    """A vpb-result/1 document for benchmark.py invocations of one build.

    invocations: one per scenario run, ``{"scenario", "group", "rows", "time", "pair"}``, with the metric rows
    it wrote to metrics.json. ctx: the run's facts, ``{"data_set", "build", "harness", "hardware",
    "io_threads", "config", "rerun", "setup"}`` (``run_context`` makes it).
    """
    h, b, hw = ctx["harness"], ctx["build"], ctx["hardware"]
    gaps = _lacks(hw, ctx["io_threads"], h["sha"], b["time"])
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
    source = f"vpb:{ctx['data_set']}"
    cells: Dict[str, Dict[str, Any]] = {}
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
    cells = {k: c for k, c in cells.items() if c["metric"] in reported}
    recs = [
        records.record(
            data_set=ctx["data_set"],
            source=source,
            build=b,
            family=family(cfg),
            io_threads=ctx["io_threads"],
            hardware=hw,
            harness=h,
            reproduce=rk,
            **c,
        )
        for c in cells.values()
    ]
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
    server_local: bool,
    server_cpus: Optional[str],
    io_threads: Optional[int],
    arch: Optional[str],
    config: Dict[str, Any],
    rerun: str,
    env: Dict[str, Any],
    server_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Facts of a benchmark.py run. server_local: the server runs on this host (VPB launched it, or the
    target is loopback), so this host's instance type and the server CPU range describe it; else the server's
    instance type is server_type (--server-instance-type), when given."""
    here = instance_type()
    hw = {
        "server": here if server_local else server_type,
        "cores": cpu_count(server_cpus) if server_local else None,
        "client": here,
        "arch": arch,
    }
    setup = "; ".join(
        x
        for x in [
            ("server on the client host" if server_local else "server on its own host")
            + f", {hw['server'] or 'instance type not recorded'}"
            + (f", CPUs {server_cpus}" if server_local and server_cpus else ""),
            f"client {here or 'instance type not recorded'}"
            + (
                f", CPUs {env['client_cpu_range']}"
                if env.get("client_cpu_range")
                else ""
            ),
            (
                f"load generator {env['benchmark_tool_version']}"
                if env.get("benchmark_tool_version")
                else ""
            ),
            f"CPU {env['cpu_model']}" if env.get("cpu_model") else "",
            f"kernel {env['kernel_version']}" if env.get("kernel_version") else "",
            f"{env['os']}" if env.get("os") else "",
        ]
        if x
    )
    return {
        "data_set": data_set,
        "build": build,
        "harness": harness(),
        "hardware": hw,
        "io_threads": io_threads,
        "config": config,
        "rerun": rerun,
        "setup": setup,
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
    server_cores: Dict[str, Optional[int]],
) -> Dict[str, Any]:
    """A vpb-result/1 document for a matrix run: a record per cell, build and metric, a run per rep.

    Reps of one cell made in the same rotation share a pair id. server_cores: cell id -> server CPUs the
    layout gave it, or None when the server was not pinned.
    """
    h = harness()
    repo = repository or BUILD_REPO
    hosts = manifest.get("hosts") or {}
    typ = lambda f: (
        None if f.get("instance_type") in (None, "unknown") else f["instance_type"]
    )  # noqa: E731
    server = typ(hosts.get("server") or {})
    gens = sorted(
        {typ(f) for k, f in hosts.items() if k.startswith("generator") and typ(f)}
    )
    kernel = (hosts.get("server") or {}).get("kernel")
    arch = (hosts.get("server") or {}).get("arch")
    arch = None if arch in (None, "unknown") else arch
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
            f"server {server or 'type not recorded'}"
            + (
                f", layout {manifest['server_layout']}"
                if manifest.get("server_layout")
                else ""
            ),
            f"generators {', '.join(gens) or 'type not recorded'}"
            + (
                f" on CPUs {manifest['generator_cpus']}"
                if manifest.get("generator_cpus")
                else ""
            ),
            f"memtier {manifest['memtier']}" if manifest.get("memtier") else "",
            f"builds alternate per rep with a server restart before each, {manifest.get('reps')} reps",
            f"kernel {kernel}" if kernel else "",
        ]
        if x
    )
    by_cell = {c["id"]: c for c in cells}
    hw = {"server": server, "cores": 0, "client": ", ".join(gens) or None, "arch": arch}
    # cores are per cell (the layout); the commit time is not read
    gaps = _lacks(hw, 0, h["sha"], None)
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
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for r in rows:
        if r.get("scenario_id") in by_cell and r.get("commit"):
            groups.setdefault((r["scenario_id"], r["build"]), []).append(r)
    recs, reproduce = [], {}
    source = f"vpb:{data_set}"
    for (cid, label), reps in groups.items():
        cell = _matrix_cell(by_cell[cid])
        sha = reps[0]["commit"]
        blk = dict(block, build={"repo": f"https://github.com/{repo}", "sha": sha})
        rk = records.reproduce_key(blk)
        reproduce[rk] = blk
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
            recs.append(
                records.record(
                    data_set=data_set,
                    source=source,
                    build={"repo": repo, "ref": label, "sha": sha, "time": None},
                    family=family(cfg),
                    hardware={
                        "server": server,
                        "cores": server_cores.get(cid),
                        "client": ", ".join(gens) or None,
                        "arch": arch,
                    },
                    harness=h,
                    metric=metric,
                    runs=runs,
                    reproduce=rk,
                    **cell,
                )
            )
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
    logging.info(f"Wrote {len(doc['records'])} vpb-result/1 records to {path}")
    return path


def loopback(target: str) -> bool:
    return target in LOOPBACK
