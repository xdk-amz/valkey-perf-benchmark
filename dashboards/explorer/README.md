# Explorer

Static views of VPB results: Single run and Compare (`TEMPLATES.md`), built
from `vpb-result` run files (`SCHEMA.md`), one file per run. The site is plain
HTML, CSS and JS with no library; the selection is in the URL.

Every VPB run writes its run files beside its `metrics.json`: `benchmark.py`
in `<results>/<commit>/vpb-runs/`, `matrix_runner.py` in
`<results>/<run>/vpb-runs/`. The writer is `vpb_result.py` at the repo root;
it reads the host, build and server facts every run carries. Its options on
both runners:

| Option | Records |
|---|---|
| `--data-set` | `official-ci`, `community` (default), `curated:<owner>` or `fork:<owner>/<repo>` |
| `--ref` | the branch or tag of the commits (`benchmark.py`; defaults to a `--commits` value that is not a SHA, else the checkout's branch) |
| `--server-ssh` | the ssh target of a server on another host, so its host facts and binary are read there (`benchmark.py`; `matrix_runner.py` uses `--server-host`) |
| `--server-instance-type` | the type of a server on another host when it cannot be read there (`benchmark.py`) |
| `--repository` | the builds' `owner/name` |

## Views from a VPB run

From the VPB checkout:

```
python -m dashboards.explorer build results/ --out site/
python3 -m http.server 8000 --bind 127.0.0.1 --directory site
```

`build` takes run files, or directories it searches for them. `--inline`
writes `index.html` and `compare.html` with the data, scripts and styles
inlined, for a viewer that serves single files with no network. `--metrics`
names the metric definitions (default `metrics.json` here).

```
python -m dashboards.explorer check data/ --data-root data/ --metrics metrics.json
```

checks the format, the fingerprint and the identifier scan, and with
`--data-root` that each run sits at `data/<data set>/<run id>.json`.

| Path | What it is |
|---|---|
| `cli.py` | `build`, `check` |
| `schema.py` | the `vpb-result` checks |
| `cells.py` | the metric definitions, the summary of a cell's reps, the merge of two writes of one run |
| `metrics.json` | the metric definitions and bars |
| `fingerprint.py`, `fingerprint-fields.json` | the Benchmark Fingerprint and its field spec |
| `site.py` | writes the site, or the inlined pages |
| `idscan.py` | the identifier scan |
| `web/` | the pages: `common.js` and `app.css` shared, one script per template |

Tests: `tests/test_explorer.py` and `tests/test_vpb_result.py`; the template
tests run the pages' own scripts in node through `tests/explorer_harness.js`.
