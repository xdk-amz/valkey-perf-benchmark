"""Cells of a vpb-result run: the metric definitions, the per-metric summary of a cell's reps, and the merge of
two files of the same run."""

import copy
import json
import re
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional

METRICS_PATH = Path(__file__).resolve().parent / "metrics.json"
METRICS_SCHEMA = "vpb-metrics"
REP_KEYS = {"rep", "pair", "t", "ok", "why"}
# an error that names the server's address says <server-ip>, as the reproduce line does
IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
CELL_KEYS = (
    "id",
    "family",
    "command",
    "command_line",
    "shape",
    "mix",
    "pipeline",
    "clients",
    "metrics",
    "runs",
    "missing",
)


def load_metrics(path: Path = METRICS_PATH) -> Dict[str, Dict[str, Any]]:
    """Metric definitions (name, unit, better, bar) from a metrics.json."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("schema") != METRICS_SCHEMA:
        raise ValueError(f"{path}: not a {METRICS_SCHEMA} file")
    return doc["metrics"]


METRICS = load_metrics()


def num(v: Optional[float]) -> Optional[float]:
    """Round to 6 significant digits so the files stay small."""
    if v is None:
        return None
    return float(f"{float(v):.6g}")


def summary(values: List[float]) -> Dict[str, Any]:
    """median, min, max and cv (coefficient of variation) of a metric's valid rep values."""
    med = statistics.median(values)
    mean = statistics.mean(values)
    cv = statistics.pstdev(values) / mean if len(values) > 1 and mean else None
    return {
        "median": num(med),
        "min": num(min(values)),
        "max": num(max(values)),
        "cv": num(cv) if cv is not None else None,
    }


def rep_metrics(rep: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in rep.items() if k not in REP_KEYS}


def summarize(cell: Dict[str, Any]) -> Dict[str, Any]:
    """Fill a cell's metrics and missing from its reps: every metric a valid rep measured; missing names why
    no rep is valid."""
    valid = [r for r in cell["runs"] if r.get("ok")]
    names = []
    for r in valid:
        for k, v in rep_metrics(r).items():
            if v is not None and k not in names:
                names.append(k)
    cell["metrics"] = {
        m: summary([r[m] for r in valid if r.get(m) is not None]) for m in names
    }
    if cell["metrics"]:
        cell["missing"] = None
    else:
        why = next((r.get("why") for r in cell["runs"] if r.get("why")), None)
        cell["missing"] = why or "no valid rep"
    return cell


def rep(
    n: int,
    values: Dict[str, Optional[float]],
    *,
    pair: Optional[str] = None,
    t: Optional[str] = None,
    ok: bool = True,
    why: Optional[str] = None,
) -> Dict[str, Any]:
    """One rep of a cell: its number, pair id, time, validity and the value of every metric it measured."""
    r: Dict[str, Any] = {"rep": int(n), "pair": pair, "t": t, "ok": bool(ok)}
    if not ok:
        r["why"] = IPV4.sub("<server-ip>", why or "invalid")
    r.update({k: num(v) for k, v in values.items()})
    return r


def merge_cells(
    old: List[Dict[str, Any]], new: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """The cells of two writes of one run: a cell in both keeps the first one's fields, its reps joined with the
    new ones numbered after the old."""
    out = {c["id"]: copy.deepcopy(c) for c in old}
    for c in new:
        if c["id"] not in out:
            out[c["id"]] = copy.deepcopy(c)
            continue
        m = out[c["id"]]
        top = max((r["rep"] for r in m["runs"]), default=0)
        m["runs"] += [dict(r, rep=top + r["rep"]) for r in copy.deepcopy(c["runs"])]
        summarize(m)
    return list(out.values())
