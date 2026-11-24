"""Benchmark fingerprint: sha256 over the canonical JSON of a run's recorded fingerprint fields.

The fields are in fingerprint-fields.json (vpb-fingerprint-fields/2, fingerprint v4). A field is recorded when its "source" path
resolves to a value in the run's records; the run's value is that value when every record agrees, else the sorted
list of its distinct values. Canonical JSON: keys sorted, no whitespace, UTF-8. web/common.js computes the same
hash in the page.
"""

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

SPEC_PATH = Path(__file__).resolve().parent / "fingerprint-fields.json"
SPEC_SCHEMA = "vpb-fingerprint-fields/2"


def load_spec(path: Path = SPEC_PATH) -> List[Dict[str, Any]]:
    spec = json.loads(path.read_text(encoding="utf-8"))
    if spec.get("schema") != SPEC_SCHEMA:
        raise ValueError(f"{path}: schema is {spec.get('schema')!r}")
    return spec["fields"]


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def get_path(record: Dict[str, Any], path: str) -> Any:
    v: Any = record
    for k in path.split("."):
        v = v.get(k) if isinstance(v, dict) else None
    return v


def fingerprint(records: Iterable[Dict[str, Any]], fields=None) -> Dict[str, Any]:
    records = list(records)
    fields = fields if fields is not None else load_spec()
    recorded: Dict[str, Any] = {}
    for f in fields:
        if not f.get("source"):
            continue
        vals = sorted(
            {
                canonical(v)
                for v in (get_path(r, f["source"]) for r in records)
                if v is not None and v != ""
            }
        )
        if vals:
            recorded[f["field"]] = (
                json.loads(vals[0]) if len(vals) == 1 else [json.loads(v) for v in vals]
            )
    text = canonical(recorded)
    return {
        "hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "recorded": recorded,
        "missing": [f["field"] for f in fields if f["field"] not in recorded],
        "json": text,
    }


def run_key(r: Dict[str, Any]) -> Tuple[str, str, str]:
    """A run, as the pages select it: scenario family, branch and commit."""
    return r["family"], r["build"]["ref"], r["build"]["sha"]


def for_records(records: Iterable[Dict[str, Any]], fields=None) -> List[Dict[str, Any]]:
    """One fingerprint entry per run in the records, in a stable order."""
    runs = defaultdict(list)
    for r in records:
        runs[run_key(r)].append(r)
    out = []
    for (family, ref, sha), recs in sorted(runs.items()):
        out.append(
            {
                "family": family,
                "repo": recs[0]["build"]["repo"],
                "ref": ref,
                "sha": sha,
                "hash": fingerprint(recs, fields)["hash"],
            }
        )
    return out
