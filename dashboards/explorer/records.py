"""vpb-result records: the metrics table, the record builder, run statistics and the merge of one cell's runs."""

import copy
import hashlib
import json
import statistics
from typing import Any, Dict, List, Optional

from . import schema

SCHEMA = schema.SCHEMA

THROUGHPUT_BAR = [2, 20, "ns per call"]
LATENCY_BAR = [2, 0.01, "ms"]
METRICS: Dict[str, Dict[str, Any]] = {
    "ops": {
        "name": "Throughput, as the client counts it",
        "unit": "ops/s",
        "better": "higher",
        "bar": THROUGHPUT_BAR,
    },
    "server_ops": {
        "name": "Throughput, as the server counts it",
        "unit": "ops/s",
        "better": "higher",
        "bar": THROUGHPUT_BAR,
    },
    "avg_ms": {
        "name": "Average latency",
        "unit": "ms",
        "better": "lower",
        "bar": LATENCY_BAR,
    },
    "p50_ms": {
        "name": "p50 latency",
        "unit": "ms",
        "better": "lower",
        "bar": LATENCY_BAR,
    },
    "p95_ms": {
        "name": "p95 latency",
        "unit": "ms",
        "better": "lower",
        "bar": LATENCY_BAR,
    },
    "p99_ms": {
        "name": "p99 latency",
        "unit": "ms",
        "better": "lower",
        "bar": LATENCY_BAR,
    },
    "p999_ms": {
        "name": "p99.9 latency",
        "unit": "ms",
        "better": "lower",
        "bar": LATENCY_BAR,
    },
    "bytes_per_key": {
        "name": "Memory per key after the preload",
        "unit": "B",
        "better": "lower",
        "bar": [1, 16, "B"],
    },
}


def num(v: Optional[float]) -> Optional[float]:
    """Round to 6 significant digits so the data files stay small."""
    if v is None:
        return None
    return float(f"{float(v):.6g}")


def stats(values: List[float]) -> Dict[str, Any]:
    """Median, n and spread of the valid runs."""
    if not values:
        return {"value": None, "n": 0, "spread": None}
    med = statistics.median(values)
    cv = (
        statistics.pstdev(values) / statistics.mean(values)
        if len(values) > 1 and statistics.mean(values)
        else None
    )
    return {
        "value": num(med),
        "n": len(values),
        "spread": {
            "min": num(min(values)),
            "max": num(max(values)),
            "cv": num(cv) if cv is not None else None,
        },
    }


def run(
    v: Optional[float],
    pair: Optional[str] = None,
    t: Optional[str] = None,
    ok: bool = True,
    why: Optional[str] = None,
) -> Dict[str, Any]:
    r: Dict[str, Any] = {"v": num(v)}
    if pair:
        r["p"] = pair
    if t:
        r["t"] = t
    if not ok:
        r["ok"] = False
        r["why"] = why or "invalid"
    return r


def record(
    *,
    data_set: str,
    source: str,
    build: Dict[str, Any],
    family: str,
    scenario: str,
    command: str,
    command_line: Optional[str],
    shape: Dict[str, Any],
    mix: str,
    pipeline: int,
    clients: int,
    io_threads: Optional[int],
    workload: str,
    hardware: Dict[str, Any],
    harness: Dict[str, Any],
    metric: str,
    runs: List[Dict[str, Any]],
    reproduce: str,
    missing: Optional[str] = None,
    value: Optional[float] = None,
    n: Optional[int] = None,
    facts: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """One result record. ``value``/``n`` override the run statistics when the source keeps only a median.
    ``facts``: the vpb-result/2 fields (host, build and run setup); without them the record is /1.
    """
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric}")
    s = stats([r["v"] for r in runs if r.get("ok", True) and r["v"] is not None])
    if value is not None:
        s["value"] = num(value)
    if n is not None:
        s["n"] = n
    if missing:
        s = {"value": None, "n": 0, "spread": s["spread"]}
    elif s["value"] is None:
        missing = "no valid run"
    return {
        "data_set": data_set,
        "source": source,
        "build": build,
        "family": family,
        "scenario": scenario,
        "command": command,
        "command_line": command_line,
        "shape": shape,
        "mix": mix,
        "pipeline": int(pipeline),
        "clients": int(clients),
        "io_threads": io_threads,
        "workload": workload,
        "hardware": {k: hardware.get(k) for k in ("server", "cores", "client", "arch")},
        "harness": harness,
        "metric": metric,
        **s,
        "runs": runs,
        "missing": missing,
        "reproduce": reproduce,
        **{k: copy.deepcopy(v) for k, v in (facts or {}).items()},
    }


def reproduce_key(block: Dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(block, sort_keys=True).encode()).hexdigest()[:10]


IDENTITY = (
    "data_set",
    "family",
    "harness",
    "command",
    "shape",
    "mix",
    "pipeline",
    "clients",
    "io_threads",
    "workload",
    "metric",
    "build",
)


def cell_key(r: Dict[str, Any]) -> str:
    """The cell a record belongs to (SCHEMA.md, Cell identity)."""
    hw = r["hardware"]
    return json.dumps(
        [r[k] for k in IDENTITY] + [hw["server"], hw["cores"]], sort_keys=True
    )


def merge(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One record per cell. Records of the same cell (one per invocation that ran it) keep the first one's
    fields and join their runs in order; the value, n and spread are recomputed from the joined runs.
    """
    cells: Dict[str, Dict[str, Any]] = {}
    joined = set()
    for r in records:
        k = cell_key(r)
        if k not in cells:
            cells[k] = copy.deepcopy(r)
            continue
        m = cells[k]
        m["runs"] = m["runs"] + copy.deepcopy(r["runs"])
        clients = {
            c for x in (m, r) for c in (x["hardware"]["client"] or "").split(", ") if c
        }
        m["hardware"]["client"] = ", ".join(sorted(clients)) or None
        m["missing"] = m["missing"] or r["missing"]
        joined.add(k)
    for k in joined:
        m = cells[k]
        s = stats(
            [x["v"] for x in m["runs"] if x.get("ok", True) and x["v"] is not None]
        )
        if s["value"] is None:
            s = {"value": None, "n": 0, "spread": None}
            m["missing"] = m["missing"] or "no valid run"
        else:
            m["missing"] = None
        m.update(s)
    return list(cells.values())


def merge_docs(docs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """vpb-result/1 documents as one: their tables joined, the records merged per cell, fingerprints recomputed."""
    sources: Dict[str, Dict[str, Any]] = {}
    metrics: Dict[str, Any] = {}
    reproduce: Dict[str, Any] = {}
    recs: List[Dict[str, Any]] = []
    for d in docs:
        for s in d["sources"]:
            sources.setdefault(s["id"], s)
        metrics.update(d["metrics"])
        reproduce.update(d["reproduce"])
        recs += d["records"]
    return schema.make_doc(list(sources.values()), merge(recs), reproduce, metrics)
