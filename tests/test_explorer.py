"""Tests for dashboards/explorer: the vpb-result run format and its checks, the fingerprint, the CLI and the two
templates' own code (run in node through tests/explorer_harness.js).

Fixtures in tests/fixtures/explorer are run files VPB's writer (vpb_result.py) made from published runs'
metrics.json rows: the valkey PR #4959 set scenarios on unstable (merge-base 8c82072a6) and on set-member-ttl
(head 2605393f2), server-side throughput only, and one valkey-io CI commit of the continuous benchmark, whose
io-threads 1 and 9 are two runs.
"""

import copy
import gzip
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from dashboards.explorer import cells, cli, fingerprint, idscan, schema

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures" / "explorer"
BASE = FIX / "pr4959-unstable.json.gz"
PR = FIX / "pr4959-set-member-ttl.json.gz"
CI1 = FIX / "ci-6c11c7fb2-iot1.json.gz"
CI9 = FIX / "ci-6c11c7fb2-iot9.json.gz"
ALL = (BASE, PR, CI1, CI9)
BASE_ID, PR_ID = "2026-11-19-pr4959-unstable", "2026-11-19-pr4959-set-member-ttl"
CI1_ID, CI9_ID = (
    "2026-11-20-ci-unstable-6c11c7fb2-iot1",
    "2026-11-20-ci-unstable-6c11c7fb2-iot9",
)


def load(p):
    return json.loads(gzip.open(p, "rt", encoding="utf-8").read())


def unzip(p, d: Path) -> Path:
    f = d / p.name[: -len(".gz")]
    f.write_text(gzip.open(p, "rt", encoding="utf-8").read())
    return f


# ---------- the format ----------
def test_fixtures_pass_the_checks():
    for p in ALL:
        assert schema.check_doc(load(p), p.name) == []


def test_one_file_is_one_run():
    doc = load(PR)
    assert set(doc) == {"schema", "run", "cells"} and doc["schema"] == "vpb-result"
    assert (
        doc["run"]["build"]["ref"] == "set-member-ttl"
        and doc["run"]["fingerprint"]["io_threads"] == 4
    )
    c = doc["cells"][0]
    assert (
        set(c) == set(cells.CELL_KEYS) and "io_threads" not in c and "hardware" not in c
    )
    assert set(c["metrics"]["server_ops"]) == {"median", "min", "max", "cv"}
    assert {"rep", "pair", "t", "ok", "server_ops"} <= set(c["runs"][0])


def broken():
    def bump(d):
        d["cells"][0]["runs"][0]["server_ops"] += 1

    return [
        ("missing run field", lambda d: d["run"].pop("reproduce")),
        ("short sha", lambda d: d["run"]["build"].update(sha="abc")),
        ("unknown data set", lambda d: d["run"].update(data_set="official")),
        ("uppercase run id", lambda d: d["run"].update(id="Run 1")),
        (
            "fingerprint fact changed",
            lambda d: d["run"]["fingerprint"]["server"].update(kernel="7.0"),
        ),
        ("summary stale", bump),
        ("unknown metric", lambda d: d["cells"][0]["runs"][0].update(qps=1.0)),
        ("why on a valid rep", lambda d: d["cells"][0]["runs"][0].update(why="x")),
        (
            "rep repeated",
            lambda d: d["cells"][0]["runs"].append(dict(d["cells"][0]["runs"][0])),
        ),
        ("cell id repeated", lambda d: d["cells"].append(copy.deepcopy(d["cells"][0]))),
        ("per-cell io-threads", lambda d: d["cells"][0].update(io_threads=4)),
        (
            "host identifier key",
            lambda d: d["run"]["fingerprint"]["server"].update(hostname="h"),
        ),
        ("missing without reason", lambda d: d["cells"][0].update(missing="x")),
        ("an older format", lambda d: d.update(schema="vpb-result/1")),
    ]


@pytest.mark.parametrize("what,change", broken())
def test_check_rejects(what, change):
    d = load(PR)
    change(d)
    assert schema.check_doc(d, "x"), what


def test_location_is_data_set_and_run_id(tmp_path):
    doc = load(PR)
    ok = tmp_path / "data" / "curated" / "xdk-amz" / f"{PR_ID}.json"
    ok.parent.mkdir(parents=True)
    assert schema.check_location(doc, ok, tmp_path / "data") == []
    for wrong in ("official-ci/" + PR_ID, "curated/xdk-amz/other"):
        assert schema.check_location(
            doc, tmp_path / "data" / f"{wrong}.json", tmp_path / "data"
        )


# ---------- fingerprint ----------
def test_fingerprint_hashes_every_field_of_the_spec_nulls_included():
    run = load(CI1)["run"]
    spec = fingerprint.load_spec()
    vals = {f["field"]: fingerprint.get_path(run, f["path"]) for f in spec}
    text = json.dumps(vals, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert run["fingerprint"]["hash"] == hashlib.sha256(text.encode()).hexdigest()
    assert vals["io_threads"] == 1 and vals["instance_type"] is None and "cores" in vals
    assert {f["path"].split(".")[0] for f in spec} == {
        "build",
        "harness",
        "fingerprint",
    }


def test_runs_of_one_commit_differ_in_their_fingerprint():
    a, b = load(CI1)["run"], load(CI9)["run"]
    assert (
        a["build"] == b["build"]
        and a["fingerprint"]["hash"] != b["fingerprint"]["hash"]
    )
    fa, fb = fingerprint.fields(a), fingerprint.fields(b)
    assert [k for k in fa if fa[k] != fb[k]] == ["io_threads"]


# ---------- cells ----------
def test_summary_and_missing_come_from_the_reps():
    c = {"runs": [cells.rep(1, {"ops": 100.0}), cells.rep(2, {"ops": 300.0}), cells.rep(3, {}, ok=False, why="failed")],
         "metrics": None, "missing": None}  # fmt: skip
    cells.summarize(c)
    assert (
        c["metrics"]
        == {"ops": {"median": 200.0, "min": 100.0, "max": 300.0, "cv": 0.5}}
        and c["missing"] is None
    )
    bad = cells.summarize(
        {
            "runs": [cells.rep(1, {}, ok=False, why="No results")],
            "metrics": None,
            "missing": None,
        }
    )
    assert bad["metrics"] == {} and bad["missing"] == "No results"


def test_merge_numbers_new_reps_after_the_old():
    doc = load(CI1)
    c = doc["cells"][0]
    merged = cells.merge_cells(doc["cells"], [c])
    m = next(x for x in merged if x["id"] == c["id"])
    n = len(c["runs"])
    assert [r["rep"] for r in m["runs"]] == list(range(1, 2 * n + 1))
    assert len(merged) == len(doc["cells"])


# ---------- identifier scan ----------
def test_idscan_flags_internal_identifiers_and_allows_key_names(tmp_path):
    f = tmp_path / "x.json"
    f.write_text(
        '{"a": "10.1.2.3", "b": "i-0123456789abcdef0", "c": "SINTER set:a:000000000000", "d": "127.0.0.1"}'
    )
    hits = list(idscan.scan(f))
    assert len(hits) == 2 and "IPv4" in hits[0] and "cloud resource id" in hits[1]


def test_fixtures_hold_no_internal_identifiers(tmp_path):
    for p in ALL:
        assert list(idscan.scan(unzip(p, tmp_path))) == []


# ---------- CLI ----------
def test_build_writes_one_data_file_per_run(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    for p in ALL:
        unzip(p, data)
    out = tmp_path / "site"
    assert cli.main(["build", str(data), "--out", str(out)]) == 0
    manifest = json.loads((out / "data" / "manifest.json").read_text())
    assert sorted(r["id"] for r in manifest["runs"]) == sorted(
        [BASE_ID, PR_ID, CI1_ID, CI9_ID]
    )
    e = next(r for r in manifest["runs"] if r["id"] == PR_ID)
    assert (e["file"], e["cells"], e["families"], e["fingerprint"]["io_threads"]) == (
        f"{PR_ID}.json",
        683,
        ["set commands"],
        4,
    )
    for f in (
        "index.html",
        "compare.html",
        "common.js",
        "templates/compare.js",
        "fingerprint-fields.json",
        "metrics.json",
    ):
        assert (out / f).exists(), f
    assert json.loads((out / "metrics.json").read_text())["metrics"]["server_ops"][
        "bar"
    ] == [2, 20, "ns per call"]


def test_build_inline_writes_self_contained_pages(tmp_path):
    out = tmp_path / "site"
    assert (
        cli.main(["build", str(unzip(CI1, tmp_path)), "--out", str(out), "--inline"])
        == 0
    )
    assert sorted(p.name for p in out.iterdir()) == ["compare.html", "index.html"]
    page = (out / "compare.html").read_text()
    assert "<script src=" not in page and '<link rel="stylesheet"' not in page
    assert (
        'class="vpb-run"' in page
        and 'id="vpb-metrics"' in page
        and 'id="vpb-fingerprint-fields"' in page
    )


def test_build_reads_vpb_results_directories_and_rejects_other_files(tmp_path):
    runs = tmp_path / "results" / ("c" * 40) / "vpb-runs"
    runs.mkdir(parents=True)
    unzip(CI1, runs)
    (runs.parent / "metrics.json").write_text("[]")
    assert (
        cli.main(["build", str(tmp_path / "results"), "--out", str(tmp_path / "site")])
        == 0
    )
    with pytest.raises(SystemExit):
        cli.main(
            ["build", str(runs.parent / "metrics.json"), "--out", str(tmp_path / "s2")]
        )
    twin = tmp_path / "twin"
    twin.mkdir()
    unzip(CI1, twin)
    with pytest.raises(SystemExit):
        cli.main(
            [
                "build",
                str(tmp_path / "results"),
                str(twin),
                "--out",
                str(tmp_path / "s3"),
            ]
        )


def test_check_needs_every_metric_defined(tmp_path):
    f = unzip(CI1, tmp_path)
    m = tmp_path / "metrics.json"
    m.write_text(
        json.dumps({"schema": "vpb-metrics", "metrics": {"ops": cells.METRICS["ops"]}})
    )
    assert cli.main(["check", str(f), "--metrics", str(m)]) == 1
    assert cli.main(["check", str(f)]) == 0


def test_check_fails_on_an_identifier(tmp_path):
    doc = load(CI1)
    doc["run"]["reproduce"] += " --target-ip 10.1.2.3"
    f = tmp_path / "official-ci" / f"{CI1_ID}.json"
    f.parent.mkdir()
    f.write_text(json.dumps(doc))
    assert cli.main(["check", str(f), "--data-root", str(tmp_path)]) == 1
    f.write_text(json.dumps(load(CI1)))
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


def series(a, al, b, bl, bar=None):
    h = f"#a={a}&al={al}&b={b}&bl={bl}"
    return h + (f"&rel={bar[0]}&abs={bar[1]}" if bar else "")


TTL = ["one", "10pct", "half", "all"]
EXPIRED = ["exp_one", "exp_10pct", "exp_half", "exp_all_but_1"]


@node
def test_compare_reproduces_the_4959_verdicts():
    q = {"default": "", "plain": series(BASE_ID, "none", PR_ID, "none")}
    for m in TTL + EXPIRED:
        q[m] = series(PR_ID, "none", PR_ID, m)
    got = harness("compare", q, BASE, PR)
    plain = got["plain"]["counts"]
    # PR vs unstable, no TTLs: 69 of 71 cells pass (Fail / Pass / Exceed 2 / 60 / 9)
    assert (plain["fail"], plain["pass"], plain["exceed"]) == (2, 60, 9)
    assert got["default"]["counts"] == plain and got["default"]["bar"] == [2, 20]
    assert got["default"]["a"] == {"r": BASE_ID, "sl": "none"} and got["default"][
        "b"
    ] == {"r": PR_ID, "sl": "none"}
    assert (
        got["plain"]["diff"] == []
    )  # same setup: the fingerprints differ only in the build

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
    c = harness(
        "compare", {"wide": series(BASE_ID, "none", PR_ID, "none", (5, 50))}, BASE, PR
    )["wide"]
    assert c["bar"] == [5, 50] and c["hash"].endswith("&rel=5&abs=50")
    assert (c["counts"]["fail"], c["counts"]["pass"], c["counts"]["exceed"]) == (
        0,
        64,
        7,
    )


@node
def test_compare_works_between_any_two_run_files():
    q = {
        "iot": f"#a={CI1_ID}&b={CI9_ID}",
        "back": f"#a={CI9_ID}&b={CI1_ID}",
        "same": f"#a={CI1_ID}&b={CI1_ID}",
        "apart": f"#a={CI1_ID}&b={PR_ID}&bl=none",
    }
    got = harness("compare", q, BASE, PR, CI1, CI9)
    iot = got["iot"]
    assert (
        iot["m"],
        sum(iot["counts"][k] for k in ("fail", "pass", "exceed")),
        iot["counts"]["notOnBoth"],
    ) == ("ops", 12, 0)
    assert iot["diff"] == ["io_threads"] and iot["fa"] != iot["fb"]
    b = got["back"]["counts"]
    assert (b["fail"], b["exceed"]) == (iot["counts"]["exceed"], iot["counts"]["fail"])
    s = got["same"]["counts"]
    assert (s["fail"], s["pass"], s["exceed"]) == (0, 12, 0)
    a = got["apart"]["counts"]
    assert a["fail"] + a["pass"] + a["exceed"] == 0 and a["notOnBoth"] == 12 + 115


@node
def test_single_run_shows_each_cells_median_and_the_run_fingerprint():
    q = {
        "pr": f"#s=set+commands&run={PR_ID}&m=server_ops",
        "ci": f"#s=continuous&run={CI9_ID}&m=ops",
    }
    got = harness("single-run", q, BASE, PR, CI1, CI9)
    pr, ci = got["pr"], got["ci"]
    assert pr["sections"] == [
        ["No TTLs", 115],
        ["With TTLs", 348],
        ["Expired but still stored", 220],
    ]
    assert ci["sections"] == [[None, 12]]  # the continuous scenario defines no sections
    assert ci["label"].endswith("io-threads 9")
    doc = load(PR)
    medians = {
        (c["command"], c["shape"]["encoding"], c["shape"]["size"], c["mix"]): (
            c["metrics"].get("server_ops") or {}
        ).get("median")
        for c in doc["cells"]
    }
    shown = {
        (row, shape.split(":")[1], int(shape.split(":")[2]), mix): v
        for row, shape, mix, v in pr["values"]
    }
    assert shown == medians
    assert pr["fingerprint"] == doc["run"]["fingerprint"]["hash"]
    assert ci["fingerprint"] == load(CI9)["run"]["fingerprint"]["hash"]


def scaled(doc, run_id, factors, memory=None):
    """A copy of a run with each rep's metrics scaled (and a memory value added), as another run."""
    d = copy.deepcopy(doc)
    d["run"]["id"] = run_id
    for c in d["cells"]:
        for r in c["runs"]:
            for m, f in factors.items():
                if f is None:
                    r.pop(m, None)
                elif m in r:
                    r[m] = cells.num(r[m] * f)
            if memory is not None:
                r["bytes_per_key"] = memory + r["rep"]
    return schema.finish(d)


def write(tmp_path, *docs):
    paths = []
    for d in docs:
        assert schema.check_doc(d, d["run"]["id"]) == []
        p = tmp_path / f"{d['run']['id']}.json"
        p.write_text(json.dumps(d))
        paths.append(p)
    return paths


@node
def test_tabs_switch_the_metric_bar_and_url():
    q = {t: f"#a={CI1_ID}&b={CI9_ID}&m={t}" for t in ("tps", "p50", "p99", "mem")}
    q["none"] = f"#a={CI1_ID}&b={CI9_ID}"
    got = harness("compare", q, CI1, CI9)
    assert {t: got[t]["m"] for t in q} == {
        "tps": "ops",
        "p50": "p50_ms",
        "p99": "p99_ms",
        "mem": "bytes_per_key",
        "none": "ops",
    }
    assert (got["tps"]["bar"], got["p50"]["bar"], got["mem"]["bar"]) == (
        [2, 20],
        [2, 0.01],
        [1, 16],
    )
    for t, g in got.items():
        assert g["roundTrip"], t
        assert f"m={'tps' if t == 'none' else t}" in g["hash"]
    # each tab judges its own metric: the counts move with it
    assert (
        got["tps"]["counts"] != got["p50"]["counts"]
        or got["p50"]["counts"] != got["p99"]["counts"]
    )
    assert (
        got["mem"]["counts"]["nodata"] == 12
    )  # the continuous benchmark measures no memory per key


@node
def test_each_metric_judges_in_its_own_direction(tmp_path):
    a = scaled(load(CI1), "a-run", {}, memory=100)
    # B: twice the throughput (better), twice the p50 (worse), half the p99 (better), more memory (worse)
    b = scaled(load(CI1), "b-run", {"ops": 2, "p50_ms": 2, "p99_ms": 0.5}, memory=300)
    files = write(tmp_path, a, b)
    got = harness(
        "compare",
        {t: f"#a=a-run&b=b-run&m={t}" for t in ("tps", "p50", "p99", "mem")},
        *files,
    )
    want = {"tps": "exceed", "p50": "fail", "p99": "exceed", "mem": "fail"}
    for t, status in want.items():
        c = got[t]["counts"]
        assert (
            c[status] == 12
            and sum(c[k] for k in ("fail", "pass", "exceed", "nodata")) == 12
        ), (t, c)


@node
def test_a_metric_missing_on_either_side_is_no_data(tmp_path):
    a = scaled(load(CI1), "a-run", {})
    b = scaled(load(CI1), "b-run", {"p99_ms": None})
    files = write(tmp_path, a, b)
    got = harness(
        "compare",
        {
            "ab": "#a=a-run&b=b-run&m=p99",
            "ba": "#a=b-run&b=a-run&m=p99",
            "p50": "#a=a-run&b=b-run&m=p50",
        },
        *files,
    )
    assert got["ab"]["counts"]["nodata"] == 12 and got["ba"]["counts"]["nodata"] == 12
    assert got["p50"]["counts"]["nodata"] == 0
    sets = harness(
        "compare", {"p50": series(BASE_ID, "none", PR_ID, "none") + "&m=p50"}, BASE, PR
    )["p50"]
    assert sets["counts"] == {
        "fail": 0,
        "pass": 0,
        "exceed": 0,
        "nodata": 72,
        "notOnBoth": 43,
    }


@node
def test_single_run_tabs():
    q = {
        "tps": f"#s=set+commands&run={PR_ID}&m=tps",
        "mem": f"#s=set+commands&run={PR_ID}&m=mem",
        "p50": f"#s=set+commands&run={PR_ID}&m=p50",
        "ci99": f"#s=continuous&run={CI9_ID}&m=p99",
        "bad": f"#s=continuous&run={CI9_ID}&m=qps",
    }
    got = harness("single-run", q, BASE, PR, CI1, CI9)
    assert {k: g["m"] for k, g in got.items()} == {
        "tps": "server_ops",
        "mem": "bytes_per_key",
        "p50": "p50_ms",
        "ci99": "p99_ms",
        "bad": "ops",
    }
    assert (
        got["tps"]["measured"] == 681 and got["p50"]["measured"] == 0
    )  # the fixture keeps server_ops only
    assert got["ci99"]["measured"] == 12 and got["bad"]["tab"] == "tps"
    assert all(g["roundTrip"] for g in got.values()) and "m=p99" in got["ci99"]["hash"]
