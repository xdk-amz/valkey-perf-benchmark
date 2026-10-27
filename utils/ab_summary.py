#!/usr/bin/env python3
"""Summarize measured runs of several commits side by side.

Reads ``<results>/<commit>/metrics.json`` for each commit, keeps measured rows
(those with ``server_ops_s``), groups them by scenario, and prints for every
scenario and commit: valid runs out of total, mean server-side ops/s with its
95% confidence interval, main-thread busy %, client-side p50/p99, and the
change against the first commit with a Welch t-test p-value. Invalid runs are
counted but never averaged; a commit with fewer than two valid runs gets no
statistics.

    python utils/ab_summary.py --results-dir results unstable 9.0.7
"""

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from scipy import stats

SCENARIO_KEYS = (
    "group",
    "scenario",
    "command",
    "generator",
    "io_threads",
    "clients",
    "pipeline",
    "data_size",
    "acl_user",
    "cluster_mode",
    "tls",
)


def load_rows(results_dir: Path, commit: str) -> List[dict]:
    path = results_dir / commit / "metrics.json"
    rows = json.loads(path.read_text()) if path.exists() else []
    return [r for r in rows if r.get("server_ops_s") is not None]


def scenario_key(row: dict) -> Tuple:
    return tuple((k, row.get(k)) for k in SCENARIO_KEYS if row.get(k) is not None)


def ci95(values: List[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    t = stats.t.ppf(0.975, len(values) - 1)
    return t * statistics.stdev(values) / math.sqrt(len(values))


def welch_p(a: List[float], b: List[float]) -> Optional[float]:
    if len(a) < 2 or len(b) < 2:
        return None
    if statistics.pstdev(a) == 0 and statistics.pstdev(b) == 0:
        return None
    return float(stats.ttest_ind(a, b, equal_var=False).pvalue)


def _mean(rows: List[dict], key: str) -> Optional[float]:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return statistics.fmean(vals) if vals else None


def summarize(results_dir: Path, commits: List[str]) -> List[dict]:
    by_scenario: Dict[Tuple, Dict[str, List[dict]]] = {}
    for commit in commits:
        for row in load_rows(results_dir, commit):
            by_scenario.setdefault(scenario_key(row), {}).setdefault(commit, []).append(
                row
            )

    out = []
    for key, per_commit in sorted(by_scenario.items(), key=lambda kv: str(kv[0])):
        base_vals = None
        for commit in commits:
            rows = per_commit.get(commit, [])
            valid = [r for r in rows if r.get("valid", True)]
            vals = [r["server_ops_s"] for r in valid]
            entry = {
                "scenario": dict(key),
                "commit": commit,
                "n": len(rows),
                "n_valid": len(valid),
                "invalid_reasons": sorted(
                    {r.get("invalid_reasons") for r in rows if not r.get("valid", True)}
                ),
            }
            if len(vals) >= 2:
                mean = statistics.fmean(vals)
                entry.update(
                    {
                        "server_ops_s": mean,
                        "ci95_pct": ci95(vals) / mean * 100 if mean else None,
                        "main_busy_pct": _mean(valid, "main_busy_pct"),
                        "p50_latency_ms": _mean(valid, "p50_latency_ms"),
                        "p99_latency_ms": _mean(valid, "p99_latency_ms"),
                    }
                )
                if base_vals is None:
                    base_vals = vals
                else:
                    base = statistics.fmean(base_vals)
                    entry["delta_pct"] = (mean - base) / base * 100 if base else None
                    entry["welch_p"] = welch_p(base_vals, vals)
            out.append(entry)
    return out


def _fmt(v, spec):
    return format(v, spec) if v is not None else "-"


def print_table(entries: List[dict]) -> None:
    last = None
    for e in entries:
        sc = e["scenario"]
        label = " ".join(f"{k}={v}" for k, v in sc.items() if k not in ("group",))
        if label != last:
            print(f"\n## {label}")
            print(
                f"{'commit':<14} {'valid':>6} {'ops/s':>12} {'ci95':>7} {'main%':>6} "
                f"{'p50ms':>7} {'p99ms':>7} {'delta':>7} {'p':>7}"
            )
            last = label
        print(
            f"{e['commit']:<14} {e['n_valid']:>3}/{e['n']:<2} "
            f"{_fmt(e.get('server_ops_s'), ',.0f'):>12} "
            f"{_fmt(e.get('ci95_pct'), '.1f'):>6}% "
            f"{_fmt(e.get('main_busy_pct'), '.0f'):>6} "
            f"{_fmt(e.get('p50_latency_ms'), '.3f'):>7} "
            f"{_fmt(e.get('p99_latency_ms'), '.3f'):>7} "
            f"{_fmt(e.get('delta_pct'), '+.1f'):>6}% "
            f"{_fmt(e.get('welch_p'), '.3f'):>7}"
        )
        for reason in e["invalid_reasons"]:
            print(f"{'':<14}   invalid: {reason}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("commits", nargs="+", help="commits; the first is the baseline")
    ap.add_argument("--results-dir", type=Path, default=Path("results"))
    ap.add_argument("--json", action="store_true", help="print JSON instead of tables")
    args = ap.parse_args(argv)
    entries = summarize(args.results_dir, args.commits)
    if not entries:
        print("no measured rows found", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(entries, indent=2))
    else:
        print_table(entries)
    return 0


if __name__ == "__main__":
    sys.exit(main())
