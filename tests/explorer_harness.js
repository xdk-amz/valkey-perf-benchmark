// Runs the explorer's own template code (web/common.js plus one template) in node, with no DOM, over vpb-result
// run files, and prints what the template computes as JSON. Used by tests/test_explorer.py.
// usage: node explorer_harness.js compare|single-run|history <query JSON> <run file>...
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const zlib = require("zlib");

const EXPLORER = path.join(__dirname, "..", "dashboards", "explorer");
const WEB = path.join(EXPLORER, "web");
const [template, queryJson, ...files] = process.argv.slice(2);
const read = (f) => JSON.parse(f.endsWith(".gz") ? zlib.gunzipSync(fs.readFileSync(f)).toString("utf8") : fs.readFileSync(f, "utf8"));
const noop = () => {};
const ctx = vm.createContext({ console, URLSearchParams, Promise, addEventListener: noop, location: { hash: "" }, history: { replaceState: noop } });
const run = (code, name) => vm.runInContext(code, ctx, { filename: name });
run(fs.readFileSync(path.join(WEB, "common.js"), "utf8"), "common.js");
ctx.start = noop; // the template's boot step needs a page; the harness drives it instead
run(fs.readFileSync(path.join(WEB, "templates", `${template}.js`), "utf8"), `${template}.js`);
ctx.__docs = files.map(read);
ctx.__spec = read(path.join(EXPLORER, "fingerprint-fields.json"));
ctx.__metrics = read(path.join(EXPLORER, "metrics.json"));
ctx.__query = JSON.parse(queryJson);
run(`
  FP.fields = __spec.fields;
  DATA.metrics = __metrics.metrics;
  for (const d of __docs) { DATA.files.set(d.run.id, Promise.resolve(d)); DATA.runs.push(manifestEntry(d)); }
`, "load");

const COMPARE = `(async () => {
  const out = {};
  for (const [name, hash] of Object.entries(__query)) {
    S = parseHash(hash);
    const { docs, m, cells } = await compareState();
    const counts = { fail: 0, pass: 0, exceed: 0, nodata: 0, notOnBoth: 0 };
    for (const c of cells.values()) {
      if (!(c.a && c.b)) { counts.notOnBoth++; continue; }
      counts[judge(c.a[m], c.b[m], m).status]++;
    }
    const fa = fingerprint(docs[0].run), fb = fingerprint(docs[1].run);
    const back = parseHash(toHash(S));
    out[name] = { a: S.a, b: S.b, tab: S.m, bar: S.bar, m, counts, fa: fa.hash, fb: fb.hash, hash: toHash(S),
      roundTrip: JSON.stringify(back) === JSON.stringify(S),
      diff: fingerprintDiff(fa, fb, ["repository", "commit", "build_version", "build_binary_sha256"]) };
  }
  return JSON.stringify(out);
})()`;
const SINGLE = `(async () => {
  const out = {};
  for (const [name, hash] of Object.entries(__query)) {
    S = parseHash(hash);
    const r = runOf(await getRun(S.r));
    const cells = [...r.cells.values()];
    out[name] = { s: S.s, r: S.r, tab: S.m, m: r.m, cells: cells.length, label: runLabel(entry(S.r)),
      hash: toHash(S), roundTrip: JSON.stringify(parseHash(toHash(S))) === JSON.stringify(S),
      measured: cells.filter((c) => c.by[r.m] && c.by[r.m].value != null).length,
      sections: sectionsOf(S.s).map((x) => [x.title, cells.filter((c) => x.test(c.mix)).length]),
      values: cells.map((c) => [c.row, c.shape, c.mix, c.by[r.m] ? c.by[r.m].value : null]),
      fingerprint: fingerprint(r.run).hash };
  }
  return JSON.stringify(out);
})()`;
const HISTORY = `(async () => {
  const out = {};
  for (const [name, hash] of Object.entries(__query)) {
    S = parseHash(hash);
    const docs = await Promise.all(runsInView().map((r) => getRun(r.id)));
    const model = historyOf(docs, tabMetric(S.m, docs));
    out[name] = { b: S.b, s: S.s, tab: S.m, m: model.metric, hash: toHash(S),
      roundTrip: JSON.stringify(parseHash(toHash(S))) === JSON.stringify(S),
      branches: branchesAll(), scenarios: scenariosOn(S.b), split: model.split,
      commits: model.commits.map((b) => [b.sha.slice(0, 9), b.time]),
      graphs: model.graphs.map((g) => ({ title: g.title, hasData: g.hasData,
        series: g.series.map((s) => ({ label: s.label, points: s.points.map((p) => [p.sha.slice(0, 9), p.value, p.min, p.max, p.reps, p.run]) })) })) };
  }
  return JSON.stringify(out);
})()`;
run({ compare: COMPARE, history: HISTORY }[template] || SINGLE, "query").then((s) => process.stdout.write(s)).catch((e) => {
  console.error(e.stack || e);
  process.exit(1);
});
