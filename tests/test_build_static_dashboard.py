"""Unit tests for utils/build_static_dashboard.py and utils/metric_rows.py."""

import csv
import json

import pytest

from utils import build_static_dashboard as bsd
from utils.metric_rows import (
    DESCRIPTION_MAX_LENGTH,
    load_metrics,
    metric_columns,
    normalize_metrics,
    parse_timestamp,
)


def _metric(commit="abc123", pipeline=1, rps=100000.0, **extra):
    row = {
        "timestamp": "2024-01-01T00:00:00Z",
        "commit": commit,
        "command": "SET",
        "data_size": 64,
        "pipeline": pipeline,
        "clients": 50,
        "rps": rps,
        "p50_latency_ms": 0.25,
        "p99_latency_ms": 0.5,
        "env_kernel_version": "6.1",
        "config_set": {"io-threads": 1},
    }
    row.update(extra)
    return row


def _write(results, commit, rows):
    d = results / commit
    d.mkdir(parents=True)
    (d / "metrics.json").write_text(json.dumps(rows))
    return d / "metrics.json"


def _as_dicts(dataset):
    names = [c["name"] for c in dataset["columns"]]
    return [dict(zip(names, r)) for r in dataset["rows"]]


@pytest.fixture
def page_dir(tmp_path):
    page = tmp_path / "page"
    (page / "vendor").mkdir(parents=True)
    (page / "vendor" / "lib.js").write_text("var lib = '</script>';")
    (page / "app.js").write_text("console.log(1);")
    (page / "app.css").write_text("body { color: red; }")
    (page / "index.html").write_text(
        '<link rel="stylesheet" href="app.css">\n'
        '<script id="vpb-data" type="application/json"></script>\n'
        '<script src="vendor/lib.js"></script>\n'
        '<script src="app.js"></script>\n'
    )
    return page


class TestMetricRows:
    def test_normalize_skips_invalid_and_truncates(self):
        rows, skipped = normalize_metrics(
            [
                None,
                {"commit": "abc"},
                _metric(group_description="g" * (DESCRIPTION_MAX_LENGTH + 5)),
            ]
        )
        assert skipped == 2
        assert len(rows[0]["group_description"]) == DESCRIPTION_MAX_LENGTH

    def test_normalize_does_not_modify_input(self):
        metric = _metric(scenario_description="s" * (DESCRIPTION_MAX_LENGTH + 1))
        normalize_metrics([metric])
        assert len(metric["scenario_description"]) == DESCRIPTION_MAX_LENGTH + 1

    def test_parse_timestamp(self):
        assert parse_timestamp("2024-01-01T00:00:00Z").tzinfo is not None
        assert parse_timestamp("") is None
        assert parse_timestamp("not a time") is None
        assert parse_timestamp(12) is None

    def test_load_metrics_adds_test_type(self, tmp_path):
        path = _write(tmp_path, "abc", [_metric(), None])
        data = load_metrics(path, "fts")
        assert data[0]["test_type"] == "fts"
        assert data[1] is None

    def test_metric_columns_match_push_to_postgres(self):
        from utils.push_to_postgres import analyze_metrics_schema

        rows, _ = normalize_metrics([_metric(), _metric(status="failed")])
        schema = analyze_metrics_schema(rows)
        expected = [c for c in schema if c not in ("id", "created_at")]
        assert metric_columns(rows) == expected


class TestFlatten:
    def test_nested_objects_expand_and_keep_json(self):
        flat = bsd.flatten_row(
            {
                "config_set": {"io-threads": 4, "save": ""},
                "server_ops_s_by_command": {"set": 10.0, "get": {"x": 1}},
                "window_sec": [3, 13],
                "timestamp": "2024-01-01T00:00:00Z",
            }
        )
        assert json.loads(flat["config_set"]) == {"io-threads": 4, "save": ""}
        assert flat["config_set.io-threads"] == 4
        assert flat["server_ops_s_by_command.set"] == 10.0
        assert flat["server_ops_s_by_command.get.x"] == 1
        assert flat["window_sec"] == "[3,13]"
        assert flat["timestamp"] == "2024-01-01T00:00:00+00:00"

    def test_column_type(self):
        assert bsd.column_type("timestamp", ["2024-01-01"]) == "time"
        assert bsd.column_type("tls", [True, None, False]) == "boolean"
        assert bsd.column_type("pipeline", [1, 2.5]) == "number"
        assert bsd.column_type("command", ["SET", 1]) == "string"
        assert bsd.column_type("empty", [None]) == "string"

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("rps", True),
            ("p999_latency_ms", True),
            ("server_ops_s", True),
            ("server_ops_s_by_command.set", True),
            ("populate_used_memory_bytes", True),
            ("pipeline", False),
            ("data_size", False),
            ("config_set.lua-time-limit_ms", False),
            ("env_cpu_freq_mhz_at_setup", False),
        ],
    )
    def test_is_metric_column(self, name, expected):
        assert bsd.is_metric_column(name, "number") is expected

    def test_parse_set_fields(self):
        assert bsd.parse_set_fields(["branch=unstable", "k=a=b"]) == {
            "branch": "unstable",
            "k": "a=b",
        }
        with pytest.raises(ValueError):
            bsd.parse_set_fields(["nokey"])


class TestBuildDataset:
    def test_columns_and_extras(self, tmp_path):
        files = [
            _write(
                tmp_path,
                "aaa",
                [
                    _metric("aaa", pipeline=1, server_ops_s=1.0),
                    _metric("aaa", pipeline=8),
                    _metric("aaa", pipeline=1, rps=99.0),
                ],
            ),
            _write(tmp_path, "bbb", [_metric("bbb", pipeline=1)]),
        ]
        ds = bsd.build_dataset(files, set_fields={"branch": "unstable"}, title="T")
        assert ds["format"] == bsd.DATA_FORMAT
        assert ds["title"] == "T"
        assert ds["files"] == 2
        cols = {c["name"]: c for c in ds["columns"]}
        # benchmark_metrics columns come first, in push_to_postgres order.
        names = [c["name"] for c in ds["columns"]]
        table = metric_columns(
            [r for f in files for r in normalize_metrics(load_metrics(f, "core"))[0]]
        )
        assert names[: len(table)] == table
        assert cols["test_type"]["config"] is True
        assert cols["rps"]["role"] == "metric"
        assert cols["p99_latency_ms"]["role"] == "metric"
        assert cols["server_ops_s"]["role"] == "metric"
        assert cols["pipeline"]["role"] == "dimension"
        assert cols["config_set"]["role"] == "object"
        assert cols["config_set.io-threads"]["config"] is True
        assert cols["branch"]["config"] is False
        assert cols["env_kernel_version"]["config"] is False

        rows = _as_dicts(ds)
        assert all(r["branch"] == "unstable" for r in rows)
        assert all(r["test_type"] == "core" for r in rows)
        hashes = {(r["commit"], r["pipeline"]): r["config_hash"] for r in rows}
        # Same config across commits, and across runs, shares one hash.
        assert hashes[("aaa", 1)] == hashes[("bbb", 1)]
        assert hashes[("aaa", 1)] != hashes[("aaa", 8)]
        assert [r["run_index"] for r in rows] == [1, 1, 2, 1]

    def test_run_field_wins_over_derived_index(self, tmp_path):
        files = [_write(tmp_path, "aaa", [_metric(run=3), _metric(run=4)])]
        rows = _as_dicts(bsd.build_dataset(files))
        assert [r["run_index"] for r in rows] == [3, 4]

    def test_measurement_fields_stay_out_of_hash(self, tmp_path):
        first = _metric(
            server_ops_s_by_command={"set": 1.0},
            populate_keys=999,
            window_complete=True,
            status="success",
        )
        second = _metric(
            server_ops_s_by_command={"set": 2.0},
            populate_keys=1001,
            window_complete=False,
            status="failed",
        )
        files = [_write(tmp_path, "aaa", [first, second])]
        rows = _as_dicts(bsd.build_dataset(files))
        assert rows[0]["config_hash"] == rows[1]["config_hash"]

    def test_existing_field_is_not_overwritten(self, tmp_path):
        files = [_write(tmp_path, "aaa", [_metric(branch="feature")])]
        rows = _as_dicts(bsd.build_dataset(files, set_fields={"branch": "unstable"}))
        assert rows[0]["branch"] == "feature"

    def test_skips_invalid_entries(self, tmp_path):
        files = [_write(tmp_path, "aaa", [_metric(), {"commit": "x"}])]
        ds = bsd.build_dataset(files)
        assert ds["skipped"] == 1
        assert len(ds["rows"]) == 1

    def test_find_metrics_files_recurses(self, tmp_path):
        _write(tmp_path / "artifact" / "results", "aaa", [_metric()])
        _write(tmp_path, "bbb", [_metric()])
        found = bsd.find_metrics_files(tmp_path)
        assert [p.parent.name for p in found] == ["aaa", "bbb"]


class TestWriteSite:
    def test_writes_data_files_and_copies_page(self, tmp_path, page_dir):
        files = [_write(tmp_path / "r", "aaa", [_metric(tls=False)])]
        ds = bsd.build_dataset(files)
        out = tmp_path / "site"
        index = bsd.write_site(ds, out, page_dir)
        assert index.read_text() == (page_dir / "index.html").read_text()
        assert (out / "vendor" / "lib.js").exists()
        assert json.loads((out / "data.json").read_text())["rows"] == ds["rows"]
        with open(out / "data.csv", newline="") as f:
            header, row = list(csv.reader(f))
        assert header == [c["name"] for c in ds["columns"]]
        assert row[header.index("tls")] == "false"

    def test_inline_embeds_everything(self, tmp_path, page_dir):
        files = [_write(tmp_path / "r", "aaa", [_metric(command="</script><b>")])]
        ds = bsd.build_dataset(files, title="Inline")
        html = bsd.write_site(ds, tmp_path / "site", page_dir, inline=True).read_text()
        assert 'src="' not in html and 'href="' not in html
        assert "body { color: red; }" in html
        assert "console.log(1);" in html
        assert "var lib = '<\\/script>';" in html
        # Only the four elements we wrote close a script or style.
        assert html.count("</script>") == 3
        start = html.index('type="application/json">') + len('type="application/json">')
        payload = json.loads(html[start : html.index("</script>", start)])
        assert payload["title"] == "Inline"
        assert "</script><b>" in json.dumps(payload["rows"])

    def test_inline_refuses_files_outside_page(self, tmp_path, page_dir):
        (page_dir / "index.html").write_text(
            '<script id="vpb-data" type="application/json"></script>'
            '<script src="../secret.js"></script>'
        )
        (tmp_path / "secret.js").write_text("x")
        with pytest.raises(ValueError, match="outside the page"):
            bsd.inline_page((page_dir / "index.html").read_text(), page_dir, {})

    def test_main_end_to_end(self, tmp_path):
        results = tmp_path / "results"
        _write(results, "aaa", [_metric()])
        out = tmp_path / "site"
        rc = bsd.main(
            [
                "--results-dir",
                str(results),
                "--out",
                str(out),
                "--inline",
                "--set-field",
                "branch=unstable",
                "--view",
                "?view=compare",
            ]
        )
        assert rc == 0
        html = (out / "index.html").read_text()
        assert "vpb-dashboard-data" in html
        assert (out / "vendor" / "echarts" / "echarts.common.min.js").exists()
        assert json.loads((out / "data.json").read_text())["view"] == "view=compare"

    def test_main_missing_results_dir(self, tmp_path):
        assert bsd.main(["--results-dir", str(tmp_path / "nope")]) == 1
