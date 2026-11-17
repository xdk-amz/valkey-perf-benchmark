#!/usr/bin/env python3
"""Build a static, server-free dashboard site from benchmark results.

Reads every ``metrics.json`` under a results root, turns the entries into
``benchmark_metrics`` rows with the helpers ``push_to_postgres.py`` uses, adds
dashboard fields, and writes ``data.json`` and ``data.csv`` next to a copy of
``dashboards/static/`` in the output directory::

    python utils/build_static_dashboard.py --results-dir results --out site
    python3 -m http.server 8000 --bind 127.0.0.1 --directory site

``--inline`` also writes the data, script, style and chart library into
``index.html``, so that one file opens from file:// or a sandboxed viewer.

Fields added to each row, beyond the ``benchmark_metrics`` columns:

- every nested object (``config_set`` and any other) as one JSON text column
  plus one ``parent.child`` column per leaf value; lists become JSON text;
- ``config_hash``: a short hash of the row's configuration columns, so rows of
  the same configuration match across commits and branches;
- ``run_index``: the row's ``run`` field when present, else the row's position
  among rows with the same commit and ``config_hash``, in file order (1-based);
- the ``--set-field KEY=VALUE`` pairs, for labels such as the branch.
"""

import argparse
import csv
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Allow ``python utils/build_static_dashboard.py`` to import utils.* modules.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.metric_rows import (
    TIMESTAMP_COLUMNS,
    load_metrics,
    metric_columns,
    normalize_metrics,
    parse_timestamp,
)

DATA_FORMAT = "vpb-dashboard-data"
DATA_VERSION = 1
DEFAULT_PAGE_DIR = REPO_ROOT / "dashboards" / "static"

# Numeric columns that hold a measurement rather than a configuration value.
METRIC_NAME = re.compile(
    r"^(rps|server_ops_s)|(_ms|_us|_bytes|_per_key|_ops_s|_rps)$|^populate_"
)
# Columns that identify a run or describe its result, never its configuration.
NON_CONFIG_COLUMNS = {
    "commit",
    "timestamp",
    "repository",
    "module_commit",
    "module_commit_timestamp",
    "branch",
    "build",
    "label",
    "run",
    "run_index",
    "config_hash",
    "status",
    "error",
    "group_description",
    "scenario_description",
}
NON_CONFIG_PREFIXES = ("env_", "window_", "populate_")


def find_metrics_files(results_root: Path) -> List[Path]:
    """Return every metrics.json under ``results_root``, sorted by path."""
    if results_root.is_file():
        return [results_root]
    return sorted(results_root.rglob("metrics.json"))


def _compact_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _expand(prefix: str, value: Dict[str, Any], out: Dict[str, Any]) -> None:
    for key, item in value.items():
        name = f"{prefix}.{key}"
        if isinstance(item, dict):
            _expand(name, item, out)
        elif isinstance(item, list):
            out[name] = _compact_json(item)
        else:
            out[name] = item


def flatten_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``row`` with only scalar values.

    An object keeps its JSON text under its own name and adds one
    ``parent.child`` column per leaf; a list becomes JSON text. Timestamps are
    rewritten in ISO 8601 when they parse.
    """
    out: Dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, dict):
            out[key] = _compact_json(value)
            _expand(key, value, out)
        elif isinstance(value, list):
            out[key] = _compact_json(value)
        elif key in TIMESTAMP_COLUMNS:
            parsed = parse_timestamp(value)
            out[key] = parsed.isoformat() if parsed else value
        else:
            out[key] = value
    return out


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def column_type(name: str, values: Sequence[Any]) -> str:
    """Return ``time``, ``boolean``, ``number`` or ``string`` for a column."""
    present = [v for v in values if v is not None]
    if name in TIMESTAMP_COLUMNS:
        return "time"
    if present and all(isinstance(v, bool) for v in present):
        return "boolean"
    if present and all(_is_number(v) for v in present):
        return "number"
    return "string"


def is_metric_column(name: str, kind: str) -> bool:
    """Return whether a column holds a measurement."""
    if kind != "number" or name.startswith(("config_set.", "env_")):
        return False
    return bool(METRIC_NAME.search(name))


def is_config_column(name: str, kind: str, labels: Sequence[str]) -> bool:
    """Return whether a column is part of a row's configuration."""
    if name in NON_CONFIG_COLUMNS or name in labels:
        return False
    if name.startswith(NON_CONFIG_PREFIXES) or is_metric_column(name, kind):
        return False
    return True


def config_hash(row: Dict[str, Any], config_columns: Sequence[str]) -> str:
    """Hash the row's non-null configuration values to 12 hex characters."""
    config = {c: row[c] for c in config_columns if row.get(c) is not None}
    return hashlib.sha1(_compact_json(config).encode()).hexdigest()[:12]


def parse_set_fields(pairs: Sequence[str]) -> Dict[str, str]:
    """Parse ``KEY=VALUE`` strings into a dict."""
    fields = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--set-field expects KEY=VALUE, got {pair!r}")
        fields[key.strip()] = value
    return fields


def build_dataset(
    metrics_files: Sequence[Path],
    test_type: str = "core",
    set_fields: Optional[Dict[str, str]] = None,
    title: str = "",
) -> Dict[str, Any]:
    """Read ``metrics_files`` and return the dashboard data document."""
    set_fields = set_fields or {}
    rows: List[Dict[str, Any]] = []
    skipped = 0
    for path in metrics_files:
        print(f"Reading {path}")
        normalized, file_skipped = normalize_metrics(load_metrics(path, test_type))
        skipped += file_skipped
        rows.extend(normalized)

    table_columns = metric_columns(rows)
    flat_rows = []
    for row in rows:
        flat = flatten_row(row)
        for key, value in set_fields.items():
            flat.setdefault(key, value)
        flat_rows.append(flat)

    all_names = set().union(*flat_rows) if flat_rows else set()
    names = table_columns + sorted(all_names - set(table_columns))
    kinds = {n: column_type(n, [r.get(n) for r in flat_rows]) for n in names}
    # An object's JSON text is covered by its parent.child columns.
    objects = {k for row in rows for k, v in row.items() if isinstance(v, dict)}
    labels = list(set_fields)
    config_columns = [
        n for n in names if n not in objects and is_config_column(n, kinds[n], labels)
    ]

    seen: Dict[Tuple[Any, str], int] = {}
    for flat in flat_rows:
        digest = config_hash(flat, config_columns)
        flat["config_hash"] = digest
        run = flat.get("run")
        if _is_number(run):
            flat["run_index"] = run
        else:
            key = (flat.get("commit"), digest)
            seen[key] = seen.get(key, 0) + 1
            flat["run_index"] = seen[key]
    for extra, kind in (("config_hash", "string"), ("run_index", "number")):
        if extra not in kinds:
            names.append(extra)
            kinds[extra] = kind

    columns = [
        {
            "name": n,
            "type": kinds[n],
            "role": (
                "object"
                if n in objects
                else "metric" if is_metric_column(n, kinds[n]) else "dimension"
            ),
            "config": n in config_columns,
        }
        for n in names
    ]
    return {
        "format": DATA_FORMAT,
        "version": DATA_VERSION,
        "title": title,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": len(metrics_files),
        "skipped": skipped,
        "columns": columns,
        "rows": [[flat.get(n) for n in names] for flat in flat_rows],
    }


def write_csv(dataset: Dict[str, Any], path: Path) -> None:
    """Write the dataset rows as CSV with a header line."""

    def cell(value: Any) -> Any:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return value

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([c["name"] for c in dataset["columns"]])
        for row in dataset["rows"]:
            writer.writerow([cell(v) for v in row])


def _script_safe(text: str) -> str:
    """Keep inlined text from closing its <script> element early."""
    return re.sub(r"</(script)", r"<\\/\1", text, flags=re.IGNORECASE)


def inline_page(html: str, page_dir: Path, dataset: Dict[str, Any]) -> str:
    """Return ``html`` with local scripts, styles and the data written inline."""

    def local_file(ref: str) -> Path:
        path = (page_dir / ref).resolve()
        if page_dir.resolve() not in path.parents:
            raise ValueError(f"Refusing to inline {ref!r} from outside the page")
        return path

    def script(match: "re.Match[str]") -> str:
        code = local_file(match.group(1)).read_text(encoding="utf-8")
        return f"<script>{_script_safe(code)}</script>"

    def style(match: "re.Match[str]") -> str:
        css = local_file(match.group(1)).read_text(encoding="utf-8")
        css = re.sub(r"</(style)", r"<\\/\1", css, flags=re.IGNORECASE)
        return f"<style>{css}</style>"

    def data(_match: "re.Match[str]") -> str:
        payload = json.dumps(dataset, separators=(",", ":"), ensure_ascii=False)
        # "<" only occurs inside JSON strings, where \u003c is the same text.
        payload = payload.replace("<", "\\u003c")
        return f'<script id="vpb-data" type="application/json">{payload}</script>'

    html = re.sub(r'<script src="([^":]+)"></script>', script, html)
    html = re.sub(r'<link rel="stylesheet" href="([^":]+)">', style, html)
    html, count = re.subn(
        r'<script id="vpb-data" type="application/json"></script>', data, html
    )
    if count != 1:
        raise ValueError("index.html has no empty vpb-data script element")
    return html


def write_site(
    dataset: Dict[str, Any],
    out_dir: Path,
    page_dir: Path = DEFAULT_PAGE_DIR,
    inline: bool = False,
) -> Path:
    """Copy the page into ``out_dir`` and write the data next to it."""
    shutil.copytree(page_dir, out_dir, dirs_exist_ok=True)
    with open(out_dir / "data.json", "w", encoding="utf-8") as f:
        json.dump(dataset, f, separators=(",", ":"), ensure_ascii=False)
    write_csv(dataset, out_dir / "data.csv")
    index = out_dir / "index.html"
    if inline:
        html = (page_dir / "index.html").read_text(encoding="utf-8")
        index.write_text(inline_page(html, page_dir, dataset), encoding="utf-8")
    return index


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--results-dir",
        required=True,
        help="Results root; every metrics.json below it is read",
    )
    parser.add_argument("--out", default="site", help="Output directory")
    parser.add_argument(
        "--inline",
        action="store_true",
        help="Write data, script, style and library into index.html",
    )
    parser.add_argument("--title", default="", help="Title shown on the page")
    parser.add_argument(
        "--view",
        default="",
        metavar="QUERY",
        help="View the page opens with when its link has no query string, "
        "e.g. 'view=compare&cc=commit'",
    )
    parser.add_argument(
        "--test-type",
        default="core",
        help="test_type column value, as push_to_postgres.py --test-type",
    )
    parser.add_argument(
        "--set-field",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Add a column to every row that lacks it, e.g. branch=unstable",
    )
    parser.add_argument(
        "--page-dir",
        default=str(DEFAULT_PAGE_DIR),
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    try:
        set_fields = parse_set_fields(args.set_field)
    except ValueError as e:
        parser.error(str(e))

    results_root = Path(args.results_dir)
    if not results_root.exists():
        print(f"Error: results directory not found: {results_root}", file=sys.stderr)
        return 1
    files = find_metrics_files(results_root)
    if not files:
        print(f"Warning: no metrics.json under {results_root}", file=sys.stderr)

    dataset = build_dataset(files, args.test_type, set_fields, args.title)
    dataset["view"] = args.view.lstrip("?")
    index = write_site(dataset, Path(args.out), Path(args.page_dir), args.inline)
    print(
        f"Wrote {len(dataset['rows'])} rows from {len(files)} file(s) "
        f"({dataset['skipped']} skipped) to {index.parent}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
