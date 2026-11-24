"""python -m dashboards.explorer: build the static site from vpb-result run files, or check them.

build INPUT... --out DIR [--inline] [--metrics FILE]   site from run files, or directories holding them (a VPB
                                                       results directory: each run writes vpb-runs/<id>.json)
check PATH... [--data-root DIR] [--metrics FILE]      the format, the fingerprint and the identifier scan;
                                                       --data-root also checks each file's place in a results
                                                       repo, data/<data set>/<run id>.json
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from . import cells, idscan, schema, site


def _read(f: Path) -> Any:
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def collect(inputs: List[Path]) -> List[Tuple[str, Dict[str, Any]]]:
    """(path, run file) for every vpb-result file in the inputs; other files in a directory are skipped."""
    docs = []
    for p in inputs:
        if p.is_dir():
            for f in sorted(p.rglob("*.json")):
                doc = _read(f)
                if schema.is_run_doc(doc):
                    docs.append((str(f), doc))
        else:
            doc = _read(p)
            if not schema.is_run_doc(doc):
                raise SystemExit(f"{p}: not a vpb-result file")
            docs.append((str(p), doc))
    if not docs:
        raise SystemExit(f"no vpb-result files in {', '.join(map(str, inputs))}")
    ids = [d["run"].get("id") for _, d in docs]
    dup = sorted({i for i in ids if ids.count(i) > 1}, key=str)
    if dup:
        raise SystemExit(f"two files hold the same run id: {', '.join(map(str, dup))}")
    return docs


def cmd_build(a) -> int:
    metrics = cells.load_metrics(a.metrics)
    docs = collect(a.inputs)
    errors = [e for n, d in docs for e in schema.check_doc(d, n, metrics)]
    if errors:
        print("\n".join(errors[:50]), file=sys.stderr)
        return 1
    written = site.write_site([d for _, d in docs], a.out, metrics, inline=a.inline)
    hits = site.scan(written)
    for h in hits[:50]:
        print(h, file=sys.stderr)
    n = sum(len(d["cells"]) for _, d in docs)
    print(
        f"{a.out}: {len(written)} files from {len(docs)} runs, {n} cells, {len(hits)} identifier hits"
    )
    return 1 if hits else 0


def cmd_check(a) -> int:
    metrics = cells.load_metrics(a.metrics)
    files = [
        f for p in a.paths for f in (sorted(p.rglob("*.json")) if p.is_dir() else [p])
    ]
    errors, hits, n = [], [], 0
    ids: Dict[str, str] = {}
    for f in files:
        doc = _read(f)
        if doc is None:
            errors.append(f"{f}: not JSON")
            continue
        n += 1
        errors += schema.check_doc(doc, str(f), metrics)
        if a.data_root:
            errors += schema.check_location(doc, f, a.data_root)
        rid = (doc.get("run") or {}).get("id") if isinstance(doc, dict) else None
        if rid in ids:
            errors.append(f"{f}: run id {rid} is also {ids[rid]}")
        ids[rid] = str(f)
        hits += list(idscan.scan(f))
    for line in errors[:50] + hits[:50]:
        print(line)
    print(f"check: {n} files, {len(errors)} errors, {len(hits)} identifier hits")
    return 1 if errors or hits or not n else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m dashboards.explorer",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    metrics_help = "metric definitions (default: the explorer's own metrics.json)"
    b = sub.add_parser("build", help="build the static site")
    b.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="run files, or directories searched for them",
    )
    b.add_argument("--out", required=True, type=Path)
    b.add_argument(
        "--inline",
        action="store_true",
        help="self-contained pages with the data inlined",
    )
    b.add_argument(
        "--metrics", type=Path, default=cells.METRICS_PATH, help=metrics_help
    )
    c = sub.add_parser("check", help="validate vpb-result run files")
    c.add_argument("paths", nargs="+", type=Path)
    c.add_argument(
        "--data-root",
        type=Path,
        help="results repo data directory: each run sits at data/<data set>/<run id>.json",
    )
    c.add_argument(
        "--metrics", type=Path, default=cells.METRICS_PATH, help=metrics_help
    )
    a = ap.parse_args(argv)
    return {"build": cmd_build, "check": cmd_check}[a.cmd](a)
