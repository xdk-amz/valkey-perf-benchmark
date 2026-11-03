"""Unit tests for utils/ab_summary.py."""

import json

from utils.ab_summary import main, summarize


def _write(tmp_path, commit, rows):
    d = tmp_path / commit
    d.mkdir()
    (d / "metrics.json").write_text(json.dumps(rows))


def _row(ops, valid=True, reason="", **kw):
    return {
        "scenario": "get",
        "command": "GET",
        "clients": 8,
        "server_ops_s": ops,
        "main_busy_pct": 99.0,
        "p50_latency_ms": 0.1,
        "p99_latency_ms": 0.3,
        "valid": valid,
        "invalid_reasons": reason,
        **kw,
    }


def test_delta_ci_and_invalid_runs(tmp_path):
    _write(tmp_path, "base", [_row(100.0), _row(102.0), _row(98.0)])
    _write(
        tmp_path,
        "cand",
        [_row(110.0), _row(112.0), _row(108.0), _row(50.0, False, "clients 1 < 8")],
    )
    base, cand = summarize(tmp_path, ["base", "cand"])
    assert base["n_valid"] == 3 and "delta_pct" not in base
    assert cand["n"] == 4 and cand["n_valid"] == 3
    assert cand["server_ops_s"] == 110.0
    assert round(cand["delta_pct"], 1) == 10.0
    assert cand["welch_p"] < 0.01
    assert cand["invalid_reasons"] == ["clients 1 < 8"]


def test_rows_without_server_counters_are_ignored(tmp_path, capsys):
    _write(tmp_path, "base", [{"command": "GET", "rps": 1.0}])
    assert main(["--results-dir", str(tmp_path), "base"]) == 1
    assert "no measured rows" in capsys.readouterr().err


def test_scenarios_differing_in_generator_stay_apart(tmp_path):
    _write(
        tmp_path,
        "base",
        [_row(1.0, generator="cachecannon"), _row(2.0, generator="valkey-benchmark")],
    )
    entries = summarize(tmp_path, ["base"])
    assert len(entries) == 2
