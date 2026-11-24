// Runs the explorer's own template code (web/common.js plus one template) in node, with no DOM, over
// vpb-result/1 files, and prints what the template computes as JSON. Used by tests/test_explorer.py.
// usage: node explorer_harness.js compare|single-run <query JSON> <data file>...
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const zlib = require("zlib");

const WEB = path.join(__dirname, "..", "dashboards", "explorer", "web");
const [template, queryJson, ...files] = process.argv.slice(2);
const read = (f) => JSON.parse(f.endsWith(".gz") ? zlib.gunzipSync(fs.readFileSync(f)).toString("utf8") : fs.readFileSync(f, "utf8"));
const noop = () => {};
const ctx = vm.createContext({ console, TextEncoder, URLSearchParams, addEventListener: noop, location: { hash: "" }, history: { replaceState: noop } });
const run = (code, name) => vm.runInContext(code, ctx, { filename: name });
run(fs.readFileSync(path.join(WEB, "common.js"), "utf8"), "common.js");
ctx.start = noop; // the template's boot step needs a page; the harness drives it instead
run(fs.readFileSync(path.join(WEB, "templates", `${template}.js`), "utf8"), `${template}.js`);
ctx.__docs = files.map(read);
ctx.__spec = read(path.join(__dirname, "..", "dashboards", "explorer", "fingerprint-fields.json"));
ctx.__query = JSON.parse(queryJson);
run(`
  FP.fields = __spec.fields;
  for (const d of __docs) { Object.assign(DATA.metrics, d.metrics); Object.assign(DATA.reproduce, d.reproduce); DATA.records.push(...d.records); }
  CAT = catalogue();
`, "load");

const COMPARE = `(() => {
  const out = {};
  for (const [name, hash] of Object.entries(__query)) {
    S = parseHash(hash);
    const m = throughputMetric(S.s);
    const { recs, cells } = cellsOf();
    const counts = { fail: 0, pass: 0, exceed: 0, notOnBoth: 0 };
    for (const c of cells.values()) {
      if (!(c.a && c.b)) { counts.notOnBoth++; continue; }
      const j = judge(c.a[m], c.b[m], m);
      if (j.status === "slower") counts.fail++; else if (j.status === "faster") counts.exceed++; else if (j.status === "pass") counts.pass++;
    }
    out[name] = { a: S.a, b: S.b, bar: S.bar, counts, fa: fingerprint(recs.a).hash, fb: fingerprint(recs.b).hash, hash: toHash(S) };
  }
  return JSON.stringify(out);
})()`;
const SINGLE = `(() => {
  const out = {};
  for (const [name, hash] of Object.entries(__query)) {
    S = parseHash(hash);
    const run = runOf();
    const cells = [...run.cells.values()];
    out[name] = { s: S.s, b: S.b, c: S.c, m: S.m, cells: cells.length,
      sections: sectionsOf(S.s).map((x) => [x.title, cells.filter((c) => x.test(c.mix)).length]),
      values: cells.map((c) => [c.row, c.shape, c.mix, c.by[S.m] ? c.by[S.m].value : null]),
      fingerprint: fingerprint(run.recs).hash };
  }
  return JSON.stringify(out);
})()`;
process.stdout.write(run(template === "compare" ? COMPARE : SINGLE, "query"));
