#!/usr/bin/env python3
"""Write a matrix config with its run profile inlined and JSON overrides applied.

    python scripts/resolve_matrix.py --config configs/matrix/perf-matrix.json \\
        --profile slice-iot4-p1 \\
        --overrides '{"matrix": {"profile": {"values": {"io_threads": 8}}}}' \\
        --out results/resolved-config.json

Overrides are merged into every config entry: objects merge key by key, other
values replace, and null deletes a key. ``matrix.profile`` in the overrides
may name another profile, or be an object merged into the chosen profile.
The written file is what the runner runs, so it can be kept next to the
results it produced. The cell count is printed after compiling a copy, so a
bad pairing or value fails here, before any build.
"""

import argparse
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config_loader  # noqa: E402
import matrix  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True)
    parser.add_argument("--profile", default=None, help="profile name or path")
    parser.add_argument("--overrides", default="{}", help="JSON object")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    overrides = json.loads(args.overrides or "{}")
    if not isinstance(overrides, dict):
        raise SystemExit("--overrides must be a JSON object")
    with open(args.config) as f:
        configs = json.load(f)

    resolved, cells = [], 0
    for cfg in configs:
        out = config_loader.prepare(
            copy.deepcopy(cfg), args.config, args.profile, overrides
        )
        cells += sum(len(g["scenarios"]) for g in out["test_groups"])
        # keep the source form (profile inlined), not the compiled cells
        for key in ("test_groups", "matrix_run"):
            out.pop(key)
        resolved.append(out)
    for cfg in resolved:
        matrix.compile_config(copy.deepcopy(cfg))  # the written file must compile

    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(resolved, indent=2) + "\n")
    print(f"{path}: {cells} cells")


if __name__ == "__main__":
    main()
