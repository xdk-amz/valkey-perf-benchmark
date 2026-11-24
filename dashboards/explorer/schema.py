"""vpb-result/1 data files: the checks every file must pass (SCHEMA.md)."""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import fingerprint

SCHEMA = "vpb-result/1"
RECORD_FIELDS = {
    "data_set",
    "source",
    "build",
    "family",
    "scenario",
    "command",
    "command_line",
    "shape",
    "mix",
    "pipeline",
    "clients",
    "io_threads",
    "workload",
    "hardware",
    "harness",
    "metric",
    "value",
    "n",
    "spread",
    "runs",
    "missing",
    "reproduce",
}
NESTED = {
    "build": {"repo", "ref", "sha", "time"},
    "shape": {"type", "encoding", "size", "size_unit"},
    "hardware": {"server", "cores", "client", "arch"},
    "harness": {"name", "repo", "sha"},
}
DOC_KEYS = {"schema", "sources", "metrics", "reproduce", "records", "fingerprints"}
DATA_SET = re.compile(
    r"^(?:official-ci|community|curated:[\w.-]+|fork:[\w.-]+/[\w.-]+)$"
)
SHA = re.compile(r"^[0-9a-f]{40}$")


def data_set_dir(data_set: str) -> str:
    """Directory of a data set in a results repo: curated:o -> curated/o, fork:o/r -> fork/o/r."""
    return data_set.replace(":", "/", 1)


def check_doc(
    doc: Dict[str, Any], name: str, require_fingerprints: bool = True
) -> List[str]:
    errors = []
    if doc.get("schema") != SCHEMA:
        return [f"{name}: schema is {doc.get('schema')!r}"]
    extra = set(doc) - DOC_KEYS
    if extra:
        errors.append(f"{name}: unknown keys {sorted(extra)}")
    sources = {s.get("id") for s in doc.get("sources", [])}
    records = doc.get("records", [])
    if not records:
        errors.append(f"{name}: no records")
    for i, r in enumerate(records):
        where = f"{name} record {i}"
        if not isinstance(r, dict) or set(r) != RECORD_FIELDS:
            got = set(r) if isinstance(r, dict) else set()
            errors.append(f"{where}: fields {sorted(got ^ RECORD_FIELDS)}")
            continue
        for k, want in NESTED.items():
            if not isinstance(r[k], dict) or set(r[k]) != want:
                errors.append(f"{where}: {k} fields")
        if errors and errors[-1].startswith(where):
            continue
        if not SHA.match(str(r["build"]["sha"])):
            errors.append(f"{where}: build.sha is not 40 hex")
        if not DATA_SET.match(str(r["data_set"])):
            errors.append(f"{where}: data_set {r['data_set']!r}")
        if (r["value"] is None) != bool(r["missing"]):
            errors.append(f"{where}: value and missing disagree")
        if r["metric"] not in doc.get("metrics", {}):
            errors.append(f"{where}: metric {r['metric']} not in the metrics table")
        if r["reproduce"] not in doc.get("reproduce", {}):
            errors.append(f"{where}: reproduce {r['reproduce']} not in the table")
        if r["source"] not in sources:
            errors.append(f"{where}: source {r['source']} not listed")
    if errors:
        return errors
    if "fingerprints" in doc or require_fingerprints:
        want = fingerprint.for_records(records)
        if doc.get("fingerprints") != want:
            errors.append(
                f"{name}: fingerprints do not match the records (recompute with fingerprint.for_records)"
            )
    return errors


def check_location(doc: Dict[str, Any], path: Path, data_root: Path) -> List[str]:
    """In a results repo every record's data set must match the file's directory under data/."""
    try:
        rel = path.resolve().parent.relative_to(data_root.resolve()).as_posix()
    except ValueError:
        return [f"{path}: not under {data_root}"]
    sets = {r.get("data_set") for r in doc.get("records", [])}
    return [
        f"{path}: data_set {s} belongs in {data_set_dir(s)}/, not {rel}/"
        for s in sorted(sets, key=str)
        if data_set_dir(str(s)) != rel
    ]


def make_doc(sources, records, reproduce, metrics) -> Dict[str, Any]:
    used = {r["metric"] for r in records}
    return {
        "schema": SCHEMA,
        "sources": sources,
        "metrics": {k: v for k, v in metrics.items() if k in used},
        "reproduce": reproduce,
        "records": records,
        "fingerprints": fingerprint.for_records(records),
    }


def is_result_doc(doc: Optional[Any]) -> bool:
    return isinstance(doc, dict) and doc.get("schema") == SCHEMA
