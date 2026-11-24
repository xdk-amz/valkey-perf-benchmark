# Explorer

Static views of VPB results: Single run and Compare (`TEMPLATES.md`), built
from `vpb-result/1` files (`SCHEMA.md`). The site is plain HTML, CSS and JS
with no library; the selection is in the URL.

Every VPB run writes `vpb-result.json` beside its `metrics.json`:
`benchmark.py` in `<results>/<commit>/`, `matrix_runner.py` in
`<results>/<run>/`. The writer is `vpb_result.py` at the repo root. Its
options on both runners:

| Option | Records |
|---|---|
| `--data-set` | `official-ci`, `community` (default), `curated:<owner>` or `fork:<owner>/<repo>` |
| `--ref` | the branch or tag of the commits (`benchmark.py`; defaults to a `--commits` value that is not a SHA, else the checkout's branch) |
| `--server-instance-type` | the type of a server on another host (`benchmark.py`; a server on this host is read from the instance metadata) |
| `--repository` | the builds' `owner/name` |

## Views from a VPB run

From the VPB checkout:

```
python -m dashboards.explorer build results/ --out site/
python3 -m http.server 8000 --bind 127.0.0.1 --directory site
```

`build` takes `vpb-result/1` files, or directories it searches for them, and
merges the runs of each cell across files. `--inline` writes `index.html` and
`compare.html` with the data, scripts and styles inlined, for a viewer that
serves single files with no network.

```
python -m dashboards.explorer check data/ --data-root data/
```

checks the schema, the fingerprints and the identifier scan, and with
`--data-root` that each file sits in its data set's directory.

| Path | What it is |
|---|---|
| `cli.py` | `build`, `check` |
| `schema.py` | the `vpb-result/1` checks |
| `records.py` | the metrics table, the record builder and the merge of a cell's runs |
| `fingerprint.py`, `fingerprint-fields.json` | the Benchmark Fingerprint and its field spec |
| `site.py` | writes the site, or the inlined pages |
| `idscan.py` | the identifier scan |
| `web/` | the pages: `common.js` and `app.css` shared, one script per template |

Tests: `tests/test_explorer.py` and `tests/test_vpb_result.py`; the template
tests run the pages' own scripts in node through `tests/explorer_harness.js`.
