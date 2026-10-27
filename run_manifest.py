"""Per-run manifest: what exactly ran, so a result can be traced and repeated.

The manifest records the server binary by digest with its text-segment size
and compiler, the Valkey and harness commits, every generator by name, version
and digest, the effective config, the CPU layout as applied and the host's
tuning state. Its id is a digest of that content (the layout without its
per-start readback), so runs on an identical setup share one id; each
metrics row carries it as ``manifest_id``.
"""

import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from environment_metadata import collect_environment_metadata

HARNESS_DIR = Path(__file__).resolve().parent


def _cmd(argv: List[str], cwd=None) -> Optional[str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=30, cwd=cwd)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def _sha256(path: Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _text_size(path: Path) -> Optional[int]:
    out = _cmd(["size", "-A", str(path)])
    for line in (out or "").splitlines():
        parts = line.split()
        if parts and parts[0] == ".text":
            return int(parts[1])
    return None


def _compiler(path: Path) -> Optional[str]:
    out = _cmd(["readelf", "-p", ".comment", str(path)])
    for line in (out or "").splitlines():
        if "]" in line:
            return line.split("]", 1)[1].strip()
    return None


def git_state(path: Path) -> Dict[str, object]:
    sha = _cmd(["git", "rev-parse", "HEAD"], cwd=path)
    dirty = _cmd(["git", "status", "--porcelain", "--untracked-files=no"], cwd=path)
    return {"commit": sha, "dirty": bool(dirty) if dirty is not None else None}


def binary_facts(path: Path) -> Dict[str, object]:
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "text_size": _text_size(path),
        "compiler": _compiler(path),
    }


def host_facts() -> Dict[str, object]:
    cpu = None
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith(("model name", "cpu part")):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return {
        "cpu_model": cpu,
        "cpus": os.cpu_count(),
        "machine": platform.machine(),
        "kernel": platform.release(),
        **collect_environment_metadata(benchmark_path=None),
    }


def build_manifest(
    *,
    commit: str,
    valkey_dir: Path,
    build_args: List[str],
    generators: List[Dict[str, object]],
    cfg: dict,
    cpu_layout: Optional[dict] = None,
) -> dict:
    server = (valkey_dir / "src" / "valkey-server").resolve()
    effective = {
        k: v for k, v in cfg.items() if k not in ("groups_to_run", "scenario_filter")
    }
    return {
        "commit_ref": commit,
        "valkey": {**git_state(valkey_dir), "build_args": list(build_args)},
        "server_binary": binary_facts(server),
        "generators": generators,
        "harness": git_state(HARNESS_DIR),
        "config": json.loads(json.dumps(effective, default=sorted)),
        "cpu_layout": cpu_layout,
        "argv": sys.argv,
        "host": host_facts(),
    }


def manifest_id(manifest: dict) -> str:
    stable = {k: v for k, v in manifest.items() if k != "argv"}
    stable["host"] = {
        k: v for k, v in manifest["host"].items() if k != "cpu_freq_mhz_at_setup"
    }
    # Thread ids and kernel readbacks change every start; the plan does not.
    if stable.get("cpu_layout"):
        stable["cpu_layout"] = {
            k: v for k, v in stable["cpu_layout"].items() if k != "applied"
        }
    blob = json.dumps(stable, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def write_manifest(results_dir: Path, manifest: dict) -> str:
    """Write ``manifests/<id>.json`` under results_dir and return the id."""
    mid = manifest_id(manifest)
    out = results_dir / "manifests" / f"{mid}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        doc = {
            "manifest_id": mid,
            "created_at": datetime.now(timezone.utc).isoformat(),
            **manifest,
        }
        out.write_text(json.dumps(doc, indent=2, sort_keys=True, default=str))
    return mid
