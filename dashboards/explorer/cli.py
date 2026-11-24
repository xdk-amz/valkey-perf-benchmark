"""python -m dashboards.explorer: build the static site from vpb-result/1 files, or check them.

build INPUT... --out DIR [--inline]   site from vpb-result/1 files, or directories holding them (a VPB
                                      results directory: each run writes vpb-result.json beside metrics.json)
check PATH... [--data-root DIR]       schema, fingerprints and the identifier scan; --data-root also checks
                                      that each file sits in its data set's directory (results repo layout)
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from . import idscan, records, schema, site

DATA_FILE = "results.json"


def _read(f: Path) -> Any:
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def collect(inputs: List[Path]) -> List[Tuple[str, Dict[str, Any]]]:
    """(path, document) for every vpb-result/1 file in the inputs; other files in a directory are skipped."""
    docs = []
    for p in inputs:
        if p.is_dir():
            for f in sorted(p.rglob("*.json")):
                doc = _read(f)
                if schema.is_result_doc(doc):
                    docs.append((str(f), doc))
        else:
            doc = _read(p)
            if not schema.is_result_doc(doc):
                raise SystemExit(f"{p}: not a vpb-result/1 file")
            docs.append((str(p), doc))
    if not docs:
        raise SystemExit(f"no vpb-result/1 files in {', '.join(map(str, inputs))}")
    return docs


def cmd_build(a) -> int:
    docs = collect(a.inputs)
    errors = [e for n, d in docs for e in schema.check_doc(d, n)]
    if errors:
        print("\n".join(errors[:50]), file=sys.stderr)
        return 1
    doc = records.merge_docs([d for _, d in docs])
    written = site.write_site([(DATA_FILE, doc)], a.out, inline=a.inline)
    hits = site.scan(written)
    for h in hits[:50]:
        print(h, file=sys.stderr)
    print(
        f"{a.out}: {len(written)} files from {len(docs)} data files, {len(doc['records'])} records, "
        f"{len(hits)} identifier hits"
    )
    return 1 if hits else 0


def cmd_check(a) -> int:
    files = [
        f for p in a.paths for f in (sorted(p.rglob("*.json")) if p.is_dir() else [p])
    ]
    errors, hits, n = [], [], 0
    for f in files:
        doc = _read(f)
        if doc is None:
            errors.append(f"{f}: not JSON")
            continue
        n += 1
        errors += schema.check_doc(doc, str(f))
        if a.data_root:
            errors += schema.check_location(doc, f, a.data_root)
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
    b = sub.add_parser("build", help="build the static site")
    b.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="vpb-result/1 files, or directories searched for them",
    )
    b.add_argument("--out", required=True, type=Path)
    b.add_argument(
        "--inline",
        action="store_true",
        help="self-contained pages with the data inlined, no data files",
    )
    c = sub.add_parser("check", help="validate vpb-result/1 files")
    c.add_argument("paths", nargs="+", type=Path)
    c.add_argument(
        "--data-root",
        type=Path,
        help="results repo data directory: each file must sit in its data set's directory",
    )
    a = ap.parse_args(argv)
    return {"build": cmd_build, "check": cmd_check}[a.cmd](a)
