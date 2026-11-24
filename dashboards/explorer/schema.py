"""vpb-result files: one file per run, and the checks every file must pass (SCHEMA.md)."""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import cells, fingerprint

SCHEMA = "vpb-result"
DOC_KEYS = {"schema", "run", "cells"}
RUN_KEYS = {
    "id",
    "data_set",
    "started",
    "finished",
    "build",
    "harness",
    "fingerprint",
    "reproduce",
}
BUILD_KEYS = {"repo", "ref", "sha", "time", "version"}
HARNESS_KEYS = {"repo", "sha", "config", "config_inline"}
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
BUILD_FACT_KEYS = ("settings", "args", "binary_sha256")
FINGERPRINT_KEYS = {
    "hash",
    "build",
    "server",
    "client",
    "server_config",
    "load_generator",
    "network_path",
    "pinning",
    "io_threads",
    "cores",
}
SHAPE_KEYS = {"type", "encoding", "size", "size_unit"}
SUMMARY_KEYS = {"median", "min", "max", "cv"}
DATA_SET = re.compile(
    r"^(?:official-ci|community|curated:[\w.-]+|fork:[\w.-]+/[\w.-]+)$"
)
RUN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
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


def _keys(obj: Any, want, where: str) -> List[str]:
    if not isinstance(obj, dict) or set(obj) != set(want):
        got = set(obj) if isinstance(obj, dict) else set()
        return [f"{where}: fields {sorted(got ^ set(want))}"]
    return []


def _check_run(run: Dict[str, Any], name: str) -> List[str]:
    errors = _keys(run, RUN_KEYS, f"{name} run")
    if errors:
        return errors
    if not RUN_ID.match(str(run["id"])):
        errors.append(f"{name}: run.id {run['id']!r} is not a lowercase id")
    if not DATA_SET.match(str(run["data_set"])):
        errors.append(f"{name}: run.data_set {run['data_set']!r}")
    errors += _keys(run["build"], BUILD_KEYS, f"{name} run.build")
    errors += _keys(run["harness"], HARNESS_KEYS, f"{name} run.harness")
    fp = run["fingerprint"]
    errors += _keys(fp, FINGERPRINT_KEYS, f"{name} run.fingerprint")
    if errors:
        return errors
    errors += _keys(fp["build"], BUILD_FACT_KEYS, f"{name} run.fingerprint.build")
    for side in ("server", "client"):
        errors += _keys(fp[side], HOST_KEYS, f"{name} run.fingerprint.{side}")
    if not SHA.match(str(run["build"]["sha"])):
        errors.append(f"{name}: run.build.sha is not 40 hex")
    for k in ("server_config",):
        if fp[k] is not None and not isinstance(fp[k], dict):
            errors.append(f"{name}: run.fingerprint.{k} is not an object")
    for k in ("io_threads", "cores"):
        if fp[k] is not None and (
            not isinstance(fp[k], int) or isinstance(fp[k], bool)
        ):
            errors.append(f"{name}: run.fingerprint.{k} is not an integer")
    if run["reproduce"] is not None and not isinstance(run["reproduce"], str):
        errors.append(f"{name}: run.reproduce is not one command")
    if not errors and fp["hash"] != fingerprint.compute(run):
        errors.append(f"{name}: run.fingerprint.hash does not match the run's fields")
    return errors


def _check_cell(c: Dict[str, Any], where: str, metrics: Dict[str, Any]) -> List[str]:
    errors = _keys(c, cells.CELL_KEYS, where)
    if errors:
        return errors
    errors += _keys(c["shape"], SHAPE_KEYS, f"{where} shape")
    for k in ("pipeline", "clients"):
        if not isinstance(c[k], int) or isinstance(c[k], bool):
            errors.append(f"{where}: {k} is not an integer")
    if not isinstance(c["runs"], list) or not c["runs"]:
        return errors + [f"{where}: no runs"]
    reps = set()
    for i, r in enumerate(c["runs"]):
        rw = f"{where} run {i}"
        if not isinstance(r, dict) or not {"rep", "pair", "t", "ok"} <= set(r):
            errors.append(f"{rw}: needs rep, pair, t and ok")
            continue
        if r["rep"] in reps:
            errors.append(f"{rw}: rep {r['rep']} repeated")
        reps.add(r["rep"])
        if (not r["ok"]) != ("why" in r):
            errors.append(f"{rw}: why is set exactly when ok is false")
        for m, v in cells.rep_metrics(r).items():
            if m not in metrics:
                errors.append(f"{rw}: metric {m} is not defined in metrics.json")
            elif v is not None and not isinstance(v, (int, float)):
                errors.append(f"{rw}: {m} is not a number")
    if errors:
        return errors
    want = cells.summarize(
        {"runs": c["runs"], "metrics": None, "missing": None}
    )  # recomputed from the reps
    if c["metrics"] != want["metrics"]:
        errors.append(f"{where}: metrics do not summarize the reps")
    if (c["missing"] is None) != bool(c["metrics"]):
        errors.append(f"{where}: missing is set exactly when no metric was measured")
    for m, s in c["metrics"].items():
        errors += _keys(s, SUMMARY_KEYS, f"{where} metrics.{m}")
    return errors


def check_doc(
    doc: Dict[str, Any], name: str, metrics: Optional[Dict[str, Any]] = None
) -> List[str]:
    metrics = metrics if metrics is not None else cells.METRICS
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        got = doc.get("schema") if isinstance(doc, dict) else type(doc).__name__
        return [f"{name}: schema is {got!r}, not {SCHEMA!r}"]
    errors = _keys(doc, DOC_KEYS, name)
    if errors:
        return errors
    # the rerun config is the harness's own config file (bind_ip and the like are settings, not facts)
    scanned = {
        "run": {k: v for k, v in doc["run"].items() if k != "harness"},
        "cells": doc["cells"],
    }
    for path in host_id_keys(scanned):
        errors.append(f"{name}: {path} names a host identifier")
    errors += _check_run(doc["run"], name)
    if not isinstance(doc["cells"], list) or not doc["cells"]:
        return errors + [f"{name}: no cells"]
    ids = set()
    for i, c in enumerate(doc["cells"]):
        where = f"{name} cell {i}"
        errors += _check_cell(c, where, metrics)
        cid = c.get("id") if isinstance(c, dict) else None
        if cid in ids:
            errors.append(f"{where}: id {cid!r} repeated")
        ids.add(cid)
    return errors


def check_location(doc: Dict[str, Any], path: Path, data_root: Path) -> List[str]:
    """In a results repo a run's file is data/<data set directory>/<run id>.json."""
    run = doc.get("run") or {}
    try:
        rel = path.resolve().relative_to(data_root.resolve()).as_posix()
    except ValueError:
        return [f"{path}: not under {data_root}"]
    want = f"{data_set_dir(str(run.get('data_set')))}/{run.get('id')}.json"
    return (
        []
        if rel == want
        else [f"{path}: run {run.get('id')} belongs at {want}, not {rel}"]
    )


def finish(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize every cell and set the fingerprint hash: the last step before a run file is written."""
    for c in doc["cells"]:
        cells.summarize(c)
    doc["run"]["fingerprint"]["hash"] = fingerprint.compute(doc["run"])
    return doc


def is_run_doc(doc: Optional[Any]) -> bool:
    return isinstance(doc, dict) and doc.get("schema") == SCHEMA
