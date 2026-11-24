"""vpb-result data files: the checks every file must pass (SCHEMA.md).

vpb-result/2 is what VPB writes; vpb-result/1 files (no host, build or run-setup facts) stay readable and are
upgraded with those fields null wherever they are read together with /2 files.
"""

import copy
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import fingerprint

SCHEMA = "vpb-result/2"
SCHEMA_V1 = "vpb-result/1"
SCHEMAS = (SCHEMA_V1, SCHEMA)
RECORD_FIELDS_V1 = {
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
V2_FIELDS = {
    "build_info",
    "server_config",
    "load_generator",
    "network_path",
    "pinning",
    "parameters",
    "host",
}
RECORD_FIELDS = RECORD_FIELDS_V1 | V2_FIELDS
HOST_KEYS = (
    "instance_type",
    "arch",
    "cpu_model",
    "cpu_microcode",
    "cpu_topology",
    "memory",
    "nic",
    "virtualization",
    "cgroup_cpu_limit",
    "os",
    "kernel",
    "kernel_boot_options",
    "kernel_runtime_settings",
)
BUILD_INFO_KEYS = ("version", "settings", "args", "binary_sha256")
NESTED = {
    "build": {"repo", "ref", "sha", "time"},
    "shape": {"type", "encoding", "size", "size_unit"},
    "hardware": {"server", "cores", "client", "arch"},
    "harness": {"name", "repo", "sha"},
}
NESTED_V2 = {"build_info": set(BUILD_INFO_KEYS), "host": {"server", "client"}}
DOC_KEYS = {"schema", "sources", "metrics", "reproduce", "records", "fingerprints"}
DATA_SET = re.compile(
    r"^(?:official-ci|community|curated:[\w.-]+|fork:[\w.-]+/[\w.-]+)$"
)
SHA = re.compile(r"^[0-9a-f]{40}$")
# keys that would hold a host identifier: a file carrying one is rejected, whatever its value
HOST_ID_KEY = re.compile(
    r"(?:^|[_.-])(?:host_?name|fqdn|ip|ipv4|ipv6|ip_?addr(?:ess)?|private_ip|public_ip|mac|mac_?address|"
    r"instance_?id|availability_?zone|az|zone_?id|account|account_?id|serial|machine_?id|uuid)$",
    re.I,
)


def data_set_dir(data_set: str) -> str:
    """Directory of a data set in a results repo: curated:o -> curated/o, fork:o/r -> fork/o/r."""
    return data_set.replace(":", "/", 1)


def host_id_keys(value: Any, where: str = "") -> List[str]:
    """Every key, at any depth, that names a host identifier."""
    out = []
    if isinstance(value, dict):
        for k, v in value.items():
            here = f"{where}.{k}" if where else str(k)
            if HOST_ID_KEY.search(str(k)):
                out.append(here)
            out += host_id_keys(v, here)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out += host_id_keys(v, f"{where}[{i}]")
    return out


def _check_v2(r: Dict[str, Any], where: str) -> List[str]:
    errors = []
    for k, want in NESTED_V2.items():
        if not isinstance(r[k], dict) or set(r[k]) != want:
            errors.append(f"{where}: {k} fields")
    if not errors:
        for side in ("server", "client"):
            h = r["host"][side]
            if not isinstance(h, dict) or set(h) != set(HOST_KEYS):
                errors.append(f"{where}: host.{side} fields")
    for k in ("server_config", "parameters"):
        if r[k] is not None and not isinstance(r[k], dict):
            errors.append(f"{where}: {k} is not an object")
    for k in ("load_generator", "network_path", "pinning"):
        if r[k] is not None and not isinstance(r[k], str):
            errors.append(f"{where}: {k} is not a string")
    return errors


def check_doc(
    doc: Dict[str, Any], name: str, require_fingerprints: bool = True
) -> List[str]:
    errors = []
    if doc.get("schema") not in SCHEMAS:
        return [f"{name}: schema is {doc.get('schema')!r}"]
    v2 = doc["schema"] == SCHEMA
    fields = RECORD_FIELDS if v2 else RECORD_FIELDS_V1
    extra = set(doc) - DOC_KEYS
    if extra:
        errors.append(f"{name}: unknown keys {sorted(extra)}")
    # reproduce configs are the harness's own config files (bind_ip and the like are settings, not facts)
    scanned = {k: v for k, v in doc.items() if k != "reproduce"}
    scanned["reproduce"] = {
        k: {kk: vv for kk, vv in (b or {}).items() if kk != "config"}
        for k, b in (doc.get("reproduce") or {}).items()
    }
    for path in host_id_keys(scanned):
        errors.append(f"{name}: {path} names a host identifier")
    sources = {s.get("id") for s in doc.get("sources", [])}
    records = doc.get("records", [])
    if not records:
        errors.append(f"{name}: no records")
    for i, r in enumerate(records):
        where = f"{name} record {i}"
        if not isinstance(r, dict) or set(r) != fields:
            got = set(r) if isinstance(r, dict) else set()
            errors.append(f"{where}: fields {sorted(got ^ fields)}")
            continue
        for k, want in NESTED.items():
            if not isinstance(r[k], dict) or set(r[k]) != want:
                errors.append(f"{where}: {k} fields")
        if v2:
            errors += _check_v2(r, where)
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


def upgrade(record: Dict[str, Any]) -> Dict[str, Any]:
    """A vpb-result/1 record as /2: the facts it never had are null. Its fingerprint does not change, since
    unrecorded fields are not hashed."""
    if "host" in record:
        return record
    r = copy.deepcopy(record)
    r.update(
        build_info=dict.fromkeys(BUILD_INFO_KEYS),
        server_config=None,
        load_generator=None,
        network_path=None,
        pinning=None,
        parameters=None,
        host={"server": dict.fromkeys(HOST_KEYS), "client": dict.fromkeys(HOST_KEYS)},
    )
    return r


def make_doc(sources, records, reproduce, metrics) -> Dict[str, Any]:
    """A data file: /2 when any record is /2 (the /1 ones upgraded), else /1."""
    v2 = any("host" in r for r in records)
    if v2:
        records = [upgrade(r) for r in records]
    used = {r["metric"] for r in records}
    return {
        "schema": SCHEMA if v2 else SCHEMA_V1,
        "sources": sources,
        "metrics": {k: v for k, v in metrics.items() if k in used},
        "reproduce": reproduce,
        "records": records,
        "fingerprints": fingerprint.for_records(records),
    }


def is_result_doc(doc: Optional[Any]) -> bool:
    return isinstance(doc, dict) and doc.get("schema") in SCHEMAS
