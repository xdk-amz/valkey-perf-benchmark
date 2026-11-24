"""Tests for dashboards/explorer: the vpb-result/1 checks, the fingerprint, the record merge, the CLI and the two
templates' own code (run in node through tests/explorer_harness.js).

Fixtures in tests/fixtures/explorer are vpb-result/1 files VPB's own writer (vpb_result.py) made from published
runs' metrics.json rows:
pr4959-sets-throughput.json.gz is the server-side throughput of the valkey PR #4959 set scenarios (head 2605393f2
on set-member-ttl, merge-base 8c82072a6 on unstable) with one shared reproduce block; ci-6c11c7fb2.json.gz is one
valkey-io CI run of the continuous benchmark.
"""

import gzip
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from dashboards.explorer import cli, fingerprint, idscan, records, schema

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures" / "explorer"
SETS = FIX / "pr4959-sets-throughput.json.gz"
CI = FIX / "ci-6c11c7fb2.json.gz"
SHA = "a" * 40
PR, BASE = "2605393f2", "8c82072a6"


def load(p):
    return json.loads(gzip.open(p, "rt", encoding="utf-8").read())


# ---------- schema ----------
def test_fixtures_pass_the_checks():
    for p in (SETS, CI):
        assert schema.check_doc(load(p), p.name) == []


def test_schema_rejects_broken_records():
    doc = load(CI)
    r = doc["records"][0]
    bad = [
        ("missing field", lambda r: r.pop("workload")),
        ("short sha", lambda r: r["build"].update(sha="abc")),
        ("value without missing", lambda r: r.update(missing="x")),
        ("unknown data set", lambda r: r.update(data_set="official")),
        ("unknown metric", lambda r: r.update(metric="qps")),
    ]
    for what, change in bad:
        d = json.loads(json.dumps(doc))
        change(d["records"][0])
        assert schema.check_doc(d, "x"), what
    assert r["data_set"] == "official-ci"


def test_schema_requires_matching_fingerprints():
    doc = load(CI)
    doc["records"][0]["hardware"][
        "server"
    ] = "r7g.2xlarge"  # changes the run's instance type field
    assert any("fingerprints" in e for e in schema.check_doc(doc, "x"))
    doc.pop("fingerprints")
    assert any("fingerprints" in e for e in schema.check_doc(doc, "x"))
    assert schema.check_doc(doc, "x", require_fingerprints=False) == []


def test_location_matches_the_data_set(tmp_path):
    doc = load(SETS)
    ok = tmp_path / "data" / "curated" / "xdk-amz" / "f.json"
    ok.parent.mkdir(parents=True)
    assert schema.check_location(doc, ok, tmp_path / "data") == []
    wrong = tmp_path / "data" / "official-ci" / "f.json"
    wrong.parent.mkdir(parents=True)
    assert schema.check_location(doc, wrong, tmp_path / "data")


# ---------- fingerprint ----------
def test_fingerprint_hashes_the_canonical_json_of_recorded_fields():
    fields = [
        {"field": "commit", "source": "build.sha"},
        {"field": "scenario", "source": "scenario"},
        {"field": "kernel", "source": None},
    ]
    recs = [
        {"build": {"sha": SHA}, "scenario": "b"},
        {"build": {"sha": SHA}, "scenario": "a"},
    ]
    fp = fingerprint.fingerprint(recs, fields)
    text = '{"commit":"' + SHA + '","scenario":["a","b"]}'
    assert fp["json"] == text
    assert fp["hash"] == hashlib.sha256(text.encode()).hexdigest()
    assert fp["missing"] == ["kernel"]


def test_fingerprint_of_the_continuous_run():
    doc = load(CI)
    fp = fingerprint.fingerprint(doc["records"])
    assert sorted(fp["recorded"]) == [
        "commit",
        "measurement",
        "repository",
        "scenario",
        "scenario_family",
    ]
    assert fp["hash"] == doc["fingerprints"][0]["hash"]


# ---------- records ----------
def test_merge_joins_the_runs_of_one_cell():
    doc = load(CI)
    r = next(x for x in doc["records"] if x["metric"] == "ops")
    a, b = json.loads(json.dumps(r)), json.loads(json.dumps(r))
    a["runs"] = [records.run(100.0)]
    b["runs"] = [records.run(300.0), records.run(None, ok=False, why="failed")]
    other = json.loads(json.dumps(r))
    other["pipeline"] += 1
    merged = records.merge([a, b, other])
    assert len(merged) == 2
    m = merged[0]
    assert (m["value"], m["n"], len(m["runs"])) == (200.0, 2, 3)
    assert m["spread"]["min"] == 100.0 and m["missing"] is None


def test_merge_docs_keeps_every_table_and_recomputes_fingerprints():
    sets, ci = load(SETS), load(CI)
    doc = records.merge_docs([sets, ci, ci])
    assert len(doc["records"]) == len(sets["records"]) + len(ci["records"])
    assert all(
        len(r["runs"]) == 2 * len(c["runs"])
        for r, c in zip(doc["records"][-3:], ci["records"][-3:])
    )
    assert schema.check_doc(doc, "merged") == []


# ---------- identifier scan ----------
def test_idscan_flags_internal_identifiers_and_allows_key_names(tmp_path):
    f = tmp_path / "x.json"
    f.write_text(
        '{"a": "10.1.2.3", "b": "i-0123456789abcdef0", "c": "SINTER set:a:000000000000", "d": "127.0.0.1"}'
    )
    hits = list(idscan.scan(f))
    assert len(hits) == 2 and "IPv4" in hits[0] and "cloud resource id" in hits[1]


def test_fixtures_hold_no_internal_identifiers(tmp_path):
    for p in (SETS, CI):
        f = tmp_path / p.stem
        f.write_text(gzip.open(p, "rt", encoding="utf-8").read())
        assert list(idscan.scan(f)) == []


# ---------- CLI ----------
def test_build_writes_the_site_from_data_files(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    for p in (SETS, CI):
        (data / p.stem).write_text(gzip.open(p, "rt", encoding="utf-8").read())
    out = tmp_path / "site"
    assert cli.main(["build", str(data), "--out", str(out)]) == 0
    manifest = json.loads((out / "data" / "manifest.json").read_text())
    assert manifest["files"] == ["results.json"]
    doc = json.loads((out / "data" / "results.json").read_text())
    assert len(doc["records"]) == len(load(SETS)["records"]) + len(load(CI)["records"])
    for f in (
        "index.html",
        "compare.html",
        "common.js",
        "templates/single-run.js",
        "templates/compare.js",
        "fingerprint-fields.json",
    ):
        assert (out / f).exists(), f
    assert 'class="views"' in (out / "index.html").read_text()


def test_build_inline_writes_self_contained_pages(tmp_path):
    data = tmp_path / "ci.json"
    data.write_text(gzip.open(CI, "rt", encoding="utf-8").read())
    out = tmp_path / "site"
    assert cli.main(["build", str(data), "--out", str(out), "--inline"]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["compare.html", "index.html"]
    page = (out / "compare.html").read_text()
    assert "<script src=" not in page and '<link rel="stylesheet"' not in page
    assert 'class="vpb-data"' in page and 'id="vpb-fingerprint-fields"' in page
    assert 'class="views"' not in page


def test_build_reads_vpb_results_directories_and_skips_other_json(tmp_path):
    run = tmp_path / "results" / SHA
    run.mkdir(parents=True)
    (run / "vpb-result.json").write_text(gzip.open(CI, "rt", encoding="utf-8").read())
    (run / "metrics.json").write_text("[]")
    out = tmp_path / "site"
    assert cli.main(["build", str(tmp_path / "results"), "--out", str(out)]) == 0
    with pytest.raises(SystemExit):
        cli.main(["build", str(run / "metrics.json"), "--out", str(out)])


def test_check_fails_on_an_identifier(tmp_path):
    doc = load(CI)
    doc["sources"][0]["name"] += " on 10.1.2.3"
    f = tmp_path / "official-ci" / "x.json"
    f.parent.mkdir()
    f.write_text(json.dumps(doc))
    assert cli.main(["check", str(f), "--data-root", str(tmp_path)]) == 1
    doc["sources"][0]["name"] = "ok"
    f.write_text(json.dumps(doc))
    assert cli.main(["check", str(f), "--data-root", str(tmp_path)]) == 0


# ---------- templates (their own JS, in node) ----------
node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def harness(template, query, *files):
    out = subprocess.run(
        [
            "node",
            str(HERE / "explorer_harness.js"),
            template,
            json.dumps(query),
            *map(str, files),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def series(a_ref, a_sha, a_mix, b_ref, b_sha, b_mix, bar=None):
    h = f"#s=set+commands&ab={a_ref}&ac={a_sha}&al={a_mix}&bb={b_ref}&bc={b_sha}&bl={b_mix}"
    return h + (f"&rel={bar[0]}&abs={bar[1]}" if bar else "")


TTL = ["one", "10pct", "half", "all"]
EXPIRED = ["exp_one", "exp_10pct", "exp_half", "exp_all_but_1"]


@node
def test_compare_reproduces_the_4959_verdicts():
    q = {
        "default": "",
        "plain": series("unstable", BASE, "none", "set-member-ttl", PR, "none"),
    }
    for m in TTL + EXPIRED:
        q[m] = series("set-member-ttl", PR, "none", "set-member-ttl", PR, m)
    got = harness("compare", q, SETS)
    plain = got["plain"]["counts"]
    # PR vs unstable, no TTLs: 69 of 71 cells pass (Fail / Pass / Exceed 2 / 60 / 9)
    assert (plain["fail"], plain["pass"], plain["exceed"]) == (2, 60, 9)
    assert got["default"]["counts"] == plain and got["default"]["bar"] == [2, 20]
    assert got["default"]["a"] == {"br": "unstable", "c": BASE, "sl": "none"}
    assert got["default"]["b"] == {"br": "set-member-ttl", "c": PR, "sl": "none"}

    def summed(mixes):
        cs = [got[m]["counts"] for m in mixes]
        return sum(c["fail"] for c in cs), sum(
            c["fail"] + c["pass"] + c["exceed"] for c in cs
        )

    # within the PR run, each mix against no TTLs: 76 of 344 and 37 of 204 cells slower
    assert summed(TTL) == (76, 344)
    assert summed(EXPIRED) == (37, 204)


@node
def test_compare_bar_is_part_of_the_verdict():
    q = {
        "wide": series("unstable", BASE, "none", "set-member-ttl", PR, "none", (5, 50))
    }
    c = harness("compare", q, SETS)["wide"]
    assert c["bar"] == [5, 50] and c["hash"].endswith("&rel=5&abs=50")
    assert (c["counts"]["fail"], c["counts"]["pass"], c["counts"]["exceed"]) == (
        0,
        64,
        7,
    )


@node
def test_page_fingerprint_matches_python():
    doc = load(SETS)
    want = {
        (f["ref"], f["sha"][:9]): f["hash"]
        for f in fingerprint.for_records(
            [r for r in doc["records"] if r["mix"] == "none"]
        )
    }
    got = harness(
        "compare",
        {"p": series("unstable", BASE, "none", "set-member-ttl", PR, "none")},
        SETS,
    )["p"]
    assert (
        got["fa"] == want[("unstable", BASE)]
        and got["fb"] == want[("set-member-ttl", PR)]
    )


@node
def test_single_run_shows_each_cells_median_and_the_run_fingerprint():
    q = {
        "pr": "#s=set+commands&b=set-member-ttl&c=2605393f2&m=server_ops",
        "ci": "#s=continuous&b=unstable&c=6c11c7fb2&m=ops",
    }
    got = harness("single-run", q, SETS, CI)
    pr, ci = got["pr"], got["ci"]
    assert pr["sections"] == [
        ["No TTLs", 115],
        ["With TTLs", 348],
        ["Expired but still stored", 220],
    ]
    assert ci["sections"] == [[None, 24]]  # the continuous scenario defines no sections
    sets = load(SETS)
    medians = {
        (r["command"], r["shape"]["encoding"], r["shape"]["size"], r["mix"]): r["value"]
        for r in sets["records"]
        if r["build"]["sha"].startswith(PR)
    }
    shown = {
        (row, shape.split(":")[1], int(shape.split(":")[2]), mix): v
        for row, shape, mix, v in pr["values"]
    }
    assert shown == medians
    ci_doc = load(CI)
    assert ci["fingerprint"] == ci_doc["fingerprints"][0]["hash"]
    assert (
        pr["fingerprint"]
        == fingerprint.fingerprint(
            r for r in sets["records"] if r["build"]["sha"].startswith(PR)
        )["hash"]
    )
