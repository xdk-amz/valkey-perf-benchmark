"""Benchmark Fingerprint of a run: sha256 over the canonical JSON of the run's fingerprint fields.

fingerprint-fields.json (vpb-fingerprint-fields/3, fingerprint v5) names every field and its path in the run
object of a vpb-result file. The hash input is {field: value} over every field in the spec, null included.
Canonical JSON: keys sorted, no whitespace, UTF-8.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

SPEC_PATH = Path(__file__).resolve().parent / "fingerprint-fields.json"
SPEC_SCHEMA = "vpb-fingerprint-fields/3"


def load_spec(path: Path = SPEC_PATH) -> List[Dict[str, Any]]:
    spec = json.loads(path.read_text(encoding="utf-8"))
    if spec.get("schema") != SPEC_SCHEMA:
        raise ValueError(f"{path}: schema is {spec.get('schema')!r}")
    return spec["fields"]


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def get_path(obj: Dict[str, Any], path: str) -> Any:
    v: Any = obj
    for k in path.split("."):
        v = v.get(k) if isinstance(v, dict) else None
    return v


def fields(run: Dict[str, Any], spec=None) -> Dict[str, Any]:
    """{field: value} of a run, every field of the spec."""
    spec = spec if spec is not None else load_spec()
    return {f["field"]: get_path(run, f["path"]) for f in spec}


def compute(run: Dict[str, Any], spec=None) -> str:
    """The fingerprint hash of a run (its run object)."""
    return hashlib.sha256(canonical(fields(run, spec)).encode("utf-8")).hexdigest()
