/* Template: Compare. Series B against series A, cell by cell (TEMPLATES.md).
   A series is a build (every run file of one commit in one scenario) or one run file, and, when the scenario
   declares a variable dimension (sliceOf), one value of it: the slice. Each cell of A pairs with B's cell of the
   same scenario and cell id (the slice left out) from the run on the same setup, the fingerprint apart from the
   build; cells only one series has are listed once. */
"use strict";

let S = null;
let lastHash = "";

// ---------- series ----------
const buildKey = (b) => `${b.repo}@${b.sha}`;
const BUILD_KEY = /^[\w.-]+\/[\w.-]+@[0-9a-f]{40}$/;
// a side is { r: a build key or a run id, f: its scenario, sl: its slice }
const sideRuns = (side) => (entry(side.r) ? [entry(side.r)] : DATA.runs.filter((r) => buildKey(r.build) === side.r && r.families.includes(side.f)));
const slicesOf = (side) => {
  const sl = sliceOf(side.f);
  if (!sl) return [];
  return [...new Set(sideRuns(side).flatMap((r) => r.mixes))].sort((a, b) => sl.order.indexOf(a) - sl.order.indexOf(b));
};
// every build of each scenario, newest first: [scenario, build key, its newest run]
function buildsAll() {
  const out = [];
  for (const f of families()) {
    const seen = new Set();
    for (const r of runsOf(f)) {
      const k = buildKey(r.build);
      if (!seen.has(k)) { seen.add(k); out.push([f, k, r]); }
    }
  }
  return out;
}
// B is the newest build of the first scenario off unstable; A another build on one of B's setups (the manifest's
// setup facts), unstable first, else any other build
function defaults() {
  const bs = buildsAll().filter(([f]) => f === families()[0]);
  const b = bs.find(([, , r]) => r.build.ref !== "unstable") || bs[0];
  const setup = (r) => JSON.stringify([r.fingerprint.io_threads, r.fingerprint.cores, r.fingerprint.server, r.fingerprint.client, r.fingerprint.network_path]);
  const ofB = new Set(DATA.runs.filter((r) => buildKey(r.build) === b[1]).map(setup));
  const near = bs.filter(([, k]) => k !== b[1] && DATA.runs.some((r) => buildKey(r.build) === k && ofB.has(setup(r))));
  const a = near.find(([, , r]) => r.build.ref === "unstable") || near[0] ||
    bs.find(([, k, r]) => r.build.ref === "unstable" && k !== b[1]) || bs.find(([, k]) => k !== b[1]) || b;
  return { a: { r: a[1], f: a[0] }, b: { r: b[1], f: b[0] } };
}
function parseHash(hash) {
  const q = new URLSearchParams(hash.replace(/^#/, ""));
  const d = defaults();
  const side = (rk, fk, lk, def, slDefault) => {
    const r = q.get(rk), f = q.get(fk);
    let x;
    if (entry(r)) x = { r, f: entry(r).families[0] }; // one run file
    else if (BUILD_KEY.test(r || "") && DATA.runs.some((e) => buildKey(e.build) === r && e.families.includes(f))) x = { r, f };
    else x = def;
    const sls = slicesOf(x);
    const sl = !sls.length ? null : sls.includes(q.get(lk)) ? q.get(lk) : sls.includes(slDefault) ? slDefault : sls[0];
    return { ...x, sl };
  };
  const a = side("a", "af", "al", d.a, "none");
  const b = side("b", "bf", "bl", d.b, a.sl); // B's slice defaults to A's
  const cell = /^[0-9a-f]{8}$/.test(q.get("cell") || "") ? q.get("cell") : "";
  const num = (k) => { const v = Number(q.get(k)); return q.has(k) && Number.isFinite(v) && v >= 0 ? v : null; };
  return { a, b, m: parseTab(q), bar: [num("rel"), num("abs")], cell };
}
function toHash(st) {
  const q = new URLSearchParams();
  for (const k of ["a", "b"]) {
    q.set(k, st[k].r);
    if (!entry(st[k].r)) q.set(`${k}f`, st[k].f); // a run file names its scenario itself
    if (st[k].sl) q.set(`${k}l`, st[k].sl);
  }
  q.set("m", st.m || "tps");
  if (st.bar && st.bar[0] != null) { q.set("rel", st.bar[0]); q.set("abs", st.bar[1]); }
  if (st.cell) q.set("cell", st.cell);
  return "#" + q.toString();
}
function commit() {
  const hsh = toHash(S);
  if (hsh !== location.hash) { lastHash = hsh; location.hash = hsh; }
  render();
}

// ---------- cells and the comparison ----------
// a cell id with the slice value blanked wherever it is a segment (the set scenarios' ids carry the mix)
const unslice = (id, sl) => (sl ? id.split(".").map((x) => (x === sl ? "*" : x)).join(".") : id);
const seriesCells = (doc, side) => {
  const sl = sliceOf(side.f);
  return doc.cells.filter((c) => c.family === side.f && (!sl || c[sl.dim] === side.sl));
};

// The setups both series ran on: each of A's runs with B's run on the same setup (the fingerprint apart from the
// build fields), then each run left with the other side's closest one (the fewest other fields apart), so two
// releases whose default settings differ still pair by client, and any two run files compare. The Fingerprint
// section names the fields each pair differs in.
function setupsOf(docs) {
  const skip = buildFields();
  const vals = { a: docs.a.map((d) => fingerprint(d.run).values), b: docs.b.map((d) => fingerprint(d.run).values) };
  const apart = (i, j) => FP.fields.filter((f) => !skip.includes(f.field) &&
    canonical(vals.a[i][f.field] ?? null) !== canonical(vals.b[j][f.field] ?? null)).length;
  const cand = docs.a.flatMap((_, i) => docs.b.map((_, j) => [apart(i, j), i, j])).sort((x, y) => x[0] - y[0] || x[1] - y[1] || x[2] - y[2]);
  const ofA = new Map(), usedB = new Set();
  for (const [, i, j] of cand) if (!ofA.has(i) && !usedB.has(j)) { ofA.set(i, j); usedB.add(j); }
  return [...docs.a.map((d, i) => ({ a: d, b: ofA.has(i) ? docs.b[ofA.get(i)] : null })),
    ...docs.b.filter((_, j) => !usedB.has(j)).map((d) => ({ a: null, b: d }))];
}

function cellsOf(docs) {
  const setups = setupsOf(docs);
  // runs of one build that hold the same cell on two setups (CI at io-threads 1 and 9) keep their rows apart
  const split = [...new Set([...splitFields(docs.a, S.a.f), ...splitFields(docs.b, S.b.f)])];
  const cs = setups.flatMap((p, g) => ["a", "b"].filter((k) => p[k]).flatMap((k) =>
    seriesCells(p[k], S[k]).map((c) => ({ c, side: k, g, doc: p[k], setup: setupLabel(p.a ? p.a.run : p.b.run, split) }))));
  const varying = ROW_DIMS.filter(([, , get]) => new Set(cs.map((x) => get(x.c))).size > 1);
  const rowOf = (x) => [x.c.command, ...varying.map(([, name, get]) => `${name} ${get(x.c)}`), ...(x.setup ? [x.setup] : [])].join(" · ");
  const cells = new Map();
  for (const x of cs) {
    // a cell id is one cell per setup; one run file a side keeps its old key, so its cell links still open
    const key = `${x.c.family}\u0001${unslice(x.c.id, S[x.side].sl)}${x.setup ? `\u0001${x.setup}` : ""}`;
    const k = `${key}\u0001${x.g}`;
    let y = cells.get(k);
    if (!y) { y = { key, hash: fnv(key), row: rowOf(x), shape: shapeKey(x.c), a: null, b: null, ca: null, cb: null, da: null, db: null }; cells.set(k, y); }
    // shortcut: two runs of one side on one setup holding the same cell keep the first; merge their reps if
    // such runs are ever published apart
    if (y[x.side]) continue;
    y[x.side] = cellView(x.c);
    y[`c${x.side}`] = x.c;
    y[`d${x.side}`] = x.doc;
  }
  return cells;
}

// B against A for one metric, against that metric's bar (metrics.json: a relative % and an absolute part, in ns
// per call for throughput, else in the metric's own unit) and in its direction (better: higher or lower): paired
// reps when they share pair ids, else the medians with non-overlapping ranges. A side without the metric is
// "no data".
function judge(ra, rb, m) {
  if (!ra || !rb || ra.value == null || rb.value == null) {
    const side = !ra || ra.value == null ? "A" : "B";
    const r = side === "A" ? ra : rb;
    return { status: "nodata", why: `no data on ${side}${r && r.missing ? `: ${r.missing}` : ""}` };
  }
  const md = DATA.metrics[m];
  const [relBar, absBar] = S.bar;
  const perCall = md.unit === "ops/s";
  const delta = perCall ? (a, b) => 1e9 / b - 1e9 / a : (a, b) => b - a;
  const am = new Map(ra.runs.filter(ok).filter((x) => x.p).map((x) => [x.p, x.v]));
  const pairs = rb.runs.filter(ok).filter((x) => x.p && am.has(x.p)).map((x) => ({ p: x.p, a: am.get(x.p), b: x.v }));
  let pct, dabs, ref, agree, how;
  if (pairs.length) {
    const ps = pairs.map((x) => 100 * (x.b / x.a - 1));
    pct = median(ps);
    dabs = median(pairs.map((x) => delta(x.a, x.b)));
    ref = median(pairs.map((x) => x.a));
    agree = pairs.length >= 5 && (ps.every((x) => x > 0) || ps.every((x) => x < 0));
    how = `${pairs.length} pairs`;
  } else {
    const va = ra.runs.filter(ok).map((x) => x.v), vb = rb.runs.filter(ok).map((x) => x.v);
    pct = 100 * (rb.value / ra.value - 1);
    dabs = delta(ra.value, rb.value);
    ref = ra.value;
    agree = va.length >= 5 && vb.length >= 5 && (Math.max(...vb) < Math.min(...va) || Math.min(...vb) > Math.max(...va));
    how = `${va.length} and ${vb.length} unpaired reps`;
  }
  const flag = agree && Math.abs(pct) > relBar && Math.abs(dabs) > absBar;
  const worse = md.better === "higher" ? pct < 0 : pct > 0;
  let band;
  if (perCall) {
    const t = 1e9 / ref;
    band = [Math.min(1 - relBar / 100, t / (t + absBar)), Math.max(1 + relBar / 100, t > absBar ? t / (t - absBar) : 99)];
  } else {
    band = [Math.max(0.01, Math.min(1 - relBar / 100, 1 - absBar / ref)), Math.max(1 + relBar / 100, 1 + absBar / ref)];
  }
  return { status: flag ? (worse ? "fail" : "exceed") : "pass", pct, dabs, pairs, band, how, a: ra.value, b: rb.value, na: ra.n, nb: rb.n };
}
const compared = (j) => ["pass", "fail", "exceed"].includes(j.status);

// ---------- formatting ----------
const pctText = (v) => `${v >= 0 ? "+" : "\u2212"}${Math.abs(v).toFixed(1)}%`;
const sign = (v) => (v >= 0 ? "+" : "\u2212");
function nsText(v) {
  const a = Math.abs(v);
  const s = a >= 1e6 ? `${(a / 1e6).toFixed(2)} ms` : a >= 1e3 ? `${(a / 1e3).toFixed(1)} µs` : `${a.toFixed(0)} ns`;
  return `${sign(v)}${s}`;
}
// the absolute change in the unit of the metric's bar: ns per call for throughput, else the metric's own unit
function absText(m, v) {
  const u = DATA.metrics[m].unit, a = Math.abs(v);
  if (u === "ops/s") return `${nsText(v)} per call`;
  if (u === "ms") return `${sign(v)}${a < 1 ? `${+(a * 1000).toPrecision(3)} µs` : `${+a.toPrecision(3)} ms`}`;
  return `${sign(v)}${+a.toPrecision(3)} ${u}`;
}
const STATUS_TEXT = { fail: "Fail", exceed: "Exceed", pass: "Pass", nodata: "no data" };

// ---------- panels (the #4959 report's per-command panels) ----------
const TICKS = [-75, -50, -25, -10, -5, -2, 0, 2, 5, 10, 25, 50, 100, 200, 300];
function cmpPanel(rows, title, m) {
  const W = 440, LW = 150, RW = 120, RH = 17, TOP = 6, PX0 = LW, PX1 = W - RW;
  let lim = Math.log2(1.06);
  for (const r of rows) {
    if (!compared(r.j)) continue;
    lim = Math.max(lim, ...r.j.pairs.map((x) => Math.abs(Math.log2(x.b / x.a))), Math.abs(Math.log2(1 + r.j.pct / 100)),
      Math.abs(Math.log2(r.j.band[0])), Math.log2(r.j.band[1]));
  }
  lim = Math.min(lim * 1.12, 2.0);
  const X = (ratio) => PX0 + (PX1 - PX0) * (0.5 + Math.max(-lim, Math.min(lim, Math.log2(ratio))) / (2 * lim));
  const y1 = TOP + RH * rows.length, H = y1 + 16;
  const kids = [];
  const keep = [0];
  for (const side of [TICKS.filter((t) => t > 0), TICKS.filter((t) => t < 0).reverse()]) {
    let last = X(1);
    for (const t of side) {
      if (Math.abs(Math.log2(1 + t / 100)) > lim * 0.97) break;
      if (Math.abs(X(1 + t / 100) - last) >= 30) { keep.push(t); last = X(1 + t / 100); }
    }
  }
  for (const t of keep) {
    const x = X(1 + t / 100);
    kids.push(svg("line", { class: t === 0 ? "zero" : "grid", x1: x, x2: x, y1: TOP, y2: y1 }));
    kids.push(svg("text", { class: "tick", x, y: y1 + 12, "text-anchor": "middle" }, t === 0 ? "0" : `${t > 0 ? "+" : "\u2212"}${Math.abs(t)}%`));
  }
  rows.forEach((r, i) => {
    const y = TOP + RH * i + RH / 2, j = r.j;
    if (r.gstart && i) kids.push(svg("line", { class: "sep", x1: 0, x2: W, y1: TOP + RH * i, y2: TOP + RH * i }));
    const g = svg("g", { class: "row" });
    g.append(svg("text", { class: "lab", x: LW - 6, y: y + 4, "text-anchor": "end" }, r.label));
    let tip = `${title} · ${r.label}\n`;
    if (!compared(j)) {
      g.append(svg("text", { class: "gap", x: PX0 + 4, y: y + 4 }, STATUS_TEXT[j.status]));
      tip += j.why || STATUS_TEXT[j.status];
    } else {
      g.append(svg("rect", { class: "cband", x: X(j.band[0]), y: y - 6, width: X(j.band[1]) - X(j.band[0]), height: 12 }));
      for (const p of j.pairs) g.append(svg("circle", { class: "pair", cx: X(p.b / p.a), cy: y, r: 2.2 }));
      const x = X(1 + j.pct / 100);
      g.append(svg("path", { class: `med ${j.status}`, d: `M${x},${y - 5}l5,5l-5,5l-5,-5z` }));
      g.append(svg("text", { class: `val ${j.status}`, x: PX1 + 12, y: y + 4 }, `${pctText(j.pct)} ${absText(m, j.dabs).replace(" per call", "")}`));
      tip += `A ${exact(j.a, DATA.metrics[m].unit)} (n ${j.na}) · B ${exact(j.b, DATA.metrics[m].unit)} (n ${j.nb})\n` +
        `${pctText(j.pct)}, ${absText(m, j.dabs)}, ${j.how}\n${STATUS_TEXT[j.status]}`;
    }
    const hit = svg("rect", { class: "hit", x: 0, y: TOP + RH * i, width: W, height: RH, tabindex: 0, role: "button",
      "aria-label": tip.replace(/\n/g, "; ") }, svg("title", {}, tip));
    hit.addEventListener("click", () => openCell(r.cell));
    hit.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openCell(r.cell); } });
    g.append(hit);
    kids.push(g);
  });
  return h("figure", { class: "panel" }, h("figcaption", {}, title),
    svg("svg", { class: "bars dots", viewBox: `0 0 ${W} ${H}`, width: "100%", role: "img", "aria-label": `${title}: B vs A` }, kids));
}
function openCell(cell) { S.cell = cell.hash; commit(); window.scrollTo(0, 0); }

const onBoth = (c) => c.a && c.b;
function panels(cells, m) {
  const cs = [...cells.values()].filter(onBoth).map((c) => ({ c, j: judge(c.a[m], c.b[m], m) }));
  const names = [...new Set(cs.map((x) => x.c.row))].sort(panelOrder);
  const shapes = [...new Set(cs.map((x) => x.c.shape))].sort(panelShapeOrder);
  const figs = names.map((name) => {
    const rows = shapes.map((s) => cs.find((x) => x.c.row === name && x.c.shape === s)).filter(Boolean)
      .map((x) => ({ label: shapeShort(x.c.shape), j: x.j, cell: x.c, gstart: false }));
    return rows.length ? cmpPanel(rows, panelName(name), m) : null;
  }).filter(Boolean);
  const [rel, abs] = S.bar, unit = DATA.metrics[m].bar[2];
  return h("section", { class: "block", id: "panels" },
    h("p", { class: "legend" },
      h("span", { class: "sw worse" }), "Fail",
      h("span", { class: "sw inside" }), "Pass",
      h("span", { class: "sw better" }), "Exceed",
      h("span", { class: "sw cband" }), `${rel}% and ${abs} ${unit}`,
      h("span", { class: "sw pairdot" }), "pairs"),
    h("div", { class: "panels" }, figs));
}

// Fail / Pass / Exceed over the cells on both series, one total row
function tally(cells, m) {
  const both = [...cells.values()].filter(onBoth);
  if (!both.length) return null;
  const cs = both.map((c) => ({ c, j: judge(c.a[m], c.b[m], m) })).filter((x) => compared(x.j));
  const k = (xs) => Object.fromEntries(["fail", "pass", "exceed"].map((s) => [s, xs.filter((x) => x.j.status === s).length]));
  const row = (label, t, cls) => h("tr", { class: cls }, h("th", { scope: "row" }, label),
    ["fail", "pass", "exceed"].map((s) => h("td", { class: `num v-${s}` }, t[s])));
  return h("section", { class: "block", id: "tally" },
    h("table", { class: "coverage tally" },
      h("thead", {}, h("tr", {}, h("td", {}),
        ["Fail", "Pass", "Exceed"].map((x) => h("th", { scope: "col", class: "num" }, x)))),
      h("tbody", {}, row("Total", k(cs), "total"))));
}

// cells only one series has, listed once
function notOnBoth(cells) {
  const only = (k) => [...cells.values()].filter((c) => c[k] && !c[k === "a" ? "b" : "a"])
    .sort((x, y) => panelOrder(x.row, y.row) || panelShapeOrder(x.shape, y.shape))
    .map((c) => `${panelName(c.row)} · ${shapeShort(c.shape)}`);
  const a = only("a"), b = only("b");
  if (!a.length && !b.length) return null;
  return h("section", { class: "block", id: "not-on-both" },
    h("h2", {}, "Not on both"),
    h("table", { class: "fptable" }, h("tbody", {},
      a.length ? h("tr", {}, h("th", { scope: "row" }, `Only on A (${a.length})`), h("td", {}, a.join(", "))) : null,
      b.length ? h("tr", {}, h("th", { scope: "row" }, `Only on B (${b.length})`), h("td", {}, b.join(", "))) : null)));
}

const buildName = (b) => `${b.repo === "valkey-io/valkey" ? "" : `${b.repo} `}${b.ref} · ${b.sha.slice(0, 9)}`;
const seriesLabel = (side) => {
  const e = entry(side.r), rs = sideRuns(side);
  const name = e ? runLabel(e) : rs.length ? buildName(rs[0].build) : side.r;
  return [side.f, name, side.sl && sliceOf(side.f).label(side.sl)].filter(Boolean).join(" · ");
};
// each setup both series ran on: the two runs' identity and fingerprints, and the fields they differ in
function header(docs) {
  const id = (name, doc, sd) => {
    if (!doc) return [`${name}: not on this setup`];
    const b = doc.run.build;
    return [`${name}: `, h("a", { href: `https://github.com/${b.repo}/tree/${b.ref}` }, `${b.repo} ${b.ref}`), " ",
      h("a", { href: `https://github.com/${b.repo}/commit/${b.sha}`, class: "mono" }, b.sha.slice(0, 9)),
      sd.sl ? ` · ${sliceOf(sd.f).label(sd.sl)}` : "", ` · run ${doc.run.id}`];
  };
  // a differing slice is named once; a scenario without slices has none to name
  const sl = sliceOf(S.a.f) || sliceOf(S.b.f);
  const same = ["repository", "commit", ...(sl && S.a.sl !== S.b.sl ? [sl.name] : [])];
  const sameText = same.length > 2 ? `${same.slice(0, -1).join(", ")} and ${same[same.length - 1]}` : same.join(" and ");
  return setupsOf(docs).map((p) => {
    const fa = p.a && fingerprint(p.a.run), fb = p.b && fingerprint(p.b.run);
    // the binary and its version follow the commit
    const diff = fa && fb ? fingerprintDiff(fa, fb, ["repository", "commit", "build_version", "build_binary_sha256"]) : [];
    return h("div", { class: "fprun" }, h("p", { class: "ids" }, id("A", p.a, S.a), " · ", id("B", p.b, S.b)),
      fa ? fingerprintLine(fa, "A Benchmark Fingerprint") : null, fb ? fingerprintLine(fb, "B Benchmark Fingerprint") : null,
      fa && fb ? h("p", { class: "fp-match" }, diff.length
        ? `Fingerprints differ beyond ${sameText}: ${diff.map(fieldName).join(", ")}`
        : `Fingerprints match on every field except ${sameText}`) : null);
  });
}

// ---------- cell detail ----------
function renderCell(m, cell) {
  const back = h("button", { type: "button", class: "back", onclick: () => { S.cell = ""; commit(); } }, "\u2190 Back to the comparison");
  if (!cell) return [back, h("p", {}, "This cell is not in the selected series.")];
  const j = judge(cell.a && cell.a[m], cell.b && cell.b[m], m);
  const unit = DATA.metrics[m].unit;
  const any = cell.cb || cell.ca;
  const side = (name, st, by, c, doc) => {
    if (!by) return h("div", { class: "side" }, h("h3", {}, `${name}: ${seriesLabel(st)}`), h("p", { class: "note" }, "not on this series"));
    const ms = METRIC_ORDER.filter((x) => by[x]);
    return h("div", { class: "side" },
      h("h3", {}, `${name}: ${seriesLabel(st)}`),
      h("p", { class: "note mono" }, c.id),
      strip(by[m] || by[ms[0]]),
      runsTable(c, ms),
      h("h3", { style: "margin-top: 16px" }, `Rerun ${name}`),
      reproBlock(doc.run));
  };
  return [h("section", { class: "block", id: "cell" },
    back,
    h("h2", { style: "margin-top: 12px" }, [panelName(cell.row), shapeLabel(cell.shape)].join(" · ")),
    h("dl", { class: "kv" }, [
      ["Scenario", any.family], ["A", seriesLabel(S.a)], ["B", seriesLabel(S.b)], ["Command sent", any.command_line || "not recorded"],
    ].map(([k, v]) => [h("dt", {}, k), h("dd", {}, String(v))])),
    h("table", {},
      h("thead", {}, h("tr", {}, [mlabel(m), "A", "B", "Change", "Absolute", "Pairs", "Verdict"].map((x, i) => h("th", { scope: "col", class: i && i < 5 ? "num" : "" }, x)))),
      h("tbody", {}, h("tr", {}, h("th", { scope: "row" }, "median"),
        h("td", { class: "num" }, cell.a && cell.a[m] ? exact(cell.a[m].value, unit) : "not on A"),
        h("td", { class: "num" }, cell.b && cell.b[m] ? exact(cell.b[m].value, unit) : "not on B"),
        h("td", { class: `num v-${j.status}` }, compared(j) ? pctText(j.pct) : ""),
        h("td", { class: "num" }, compared(j) ? absText(m, j.dabs) : ""),
        h("td", {}, compared(j) ? j.how : ""), h("td", {}, j.why || STATUS_TEXT[j.status])))),
    compared(j) && j.pairs.length ? h("details", { open: j.pairs.length <= 12, style: "margin-top: 12px" }, h("summary", {}, `Pairs (${j.pairs.length})`),
      h("table", {}, h("thead", {}, h("tr", {}, ["Pair", "A", "B", "Change"].map((x, i) => h("th", { scope: "col", class: i ? "num" : "" }, x)))),
        h("tbody", {}, j.pairs.map((p) => h("tr", {}, h("th", { scope: "row" }, p.p), h("td", { class: "num" }, compact(p.a, unit)),
          h("td", { class: "num" }, compact(p.b, unit)), h("td", { class: "num" }, pctText(100 * (p.b / p.a - 1)))))))) : null,
    h("div", { class: "sides" }, side("A", S.a, cell.a, cell.ca, cell.da), side("B", S.b, cell.b, cell.cb, cell.db)))];
}

// ---------- render ----------
function renderSelectors(m) {
  const fill = (id, values, cur, lab) => {
    const el = document.getElementById(id);
    el.replaceChildren(...values.map((v) => h("option", { value: v }, lab(v))));
    el.value = cur;
  };
  const [ir, ia] = [document.getElementById("bar-rel"), document.getElementById("bar-abs")];
  if (document.activeElement !== ir) ir.value = S.bar[0];
  if (document.activeElement !== ia) ia.value = S.bar[1];
  // the bar's absolute part is in its metric's unit: ns per call for throughput, ms for latency, B for memory
  const unit = DATA.metrics[m].bar[2];
  ia.previousElementSibling.textContent = `Bar (${unit})`;
  ia.step = unit === "ms" ? "0.01" : unit === "B" ? "1" : "5";
  // a series is a build; a link to one run file keeps that file as its own choice
  const builds = buildsAll().map(([f, k, r]) => [JSON.stringify({ r: k, f }), `${f} · ${buildName(r.build)}`]);
  for (const k of ["a", "b"]) {
    const el = document.getElementById(`sel-${k}-run`);
    const run = entry(S[k].r) ? [[JSON.stringify({ r: S[k].r, f: S[k].f }), `${S[k].f} · ${runLabel(entry(S[k].r))}`]] : [];
    el.replaceChildren(...[...run, ...builds].map(([v, label]) => h("option", { value: v }, label)));
    el.value = JSON.stringify({ r: S[k].r, f: S[k].f });
    const sl = sliceOf(S[k].f);
    const box = document.getElementById(`sel-${k}-slice`);
    box.closest("label").style.display = sl ? "" : "none";
    if (sl) {
      fill(`sel-${k}-slice`, slicesOf(S[k]), S[k].sl, sl.label);
      box.previousElementSibling.textContent = `${k.toUpperCase()} ${sl.name}`;
    }
  }
}

// the comparison of the selected series: every run file of each, the metric, the matched cells
async function compareState() {
  const load = (side) => Promise.all(sideRuns(side).map((r) => getRun(r.id)));
  const [a, b] = await Promise.all([load(S.a), load(S.b)]);
  const docs = { a, b };
  const m = tabMetric(S.m, [...a, ...b]);
  if (S.bar[0] == null) S.bar = DATA.metrics[m].bar.slice(0, 2);
  return { docs, m, cells: cellsOf(docs) };
}

async function render() {
  const { docs, m, cells } = await compareState();
  const hsh = toHash(S); // a new tab takes its own metric's bar: keep it in the URL too
  if (hsh !== location.hash) { lastHash = hsh; history.replaceState(null, "", hsh); }
  renderSelectors(m);
  closeDrawer();
  const out = document.getElementById("out");
  if (S.cell) out.replaceChildren(...renderCell(m, [...cells.values()].find((c) => c.hash === S.cell)).filter(Boolean));
  // the top: the comparison's title and the metric tabs; both runs' identity and fingerprints close the page
  else out.replaceChildren(...[h("section", { class: "block", id: "summary" },
    h("h2", { class: "run-title" }, `${seriesLabel(S.a)} vs ${seriesLabel(S.b)}`),
    tabBar(S.m, (tab) => { S.m = tab; S.bar = [null, null]; S.cell = ""; commit(); })),
    tally(cells, m), panels(cells, m), notOnBoth(cells),
    h("section", { class: "block", id: "fingerprint" }, h("h2", {}, "Fingerprint"), header(docs))].filter(Boolean));
}

function wire() {
  for (const [id, i] of [["bar-rel", 0], ["bar-abs", 1]]) {
    document.getElementById(id).addEventListener("input", (e) => {
      const v = Number(e.target.value);
      if (e.target.value === "" || !Number.isFinite(v) || v < 0) return;
      S.bar = i ? [S.bar[0], v] : [v, S.bar[1]];
      commit();
    });
  }
  for (const k of ["a", "b"]) {
    document.getElementById(`sel-${k}-run`).addEventListener("change", (e) => {
      const x = JSON.parse(e.target.value), sls = slicesOf(x);
      S[k] = { ...x, sl: sls.includes(S[k].sl) ? S[k].sl : sls.includes("none") ? "none" : sls[0] || null }; S.cell = ""; commit();
    });
    document.getElementById(`sel-${k}-slice`).addEventListener("change", (e) => { S[k] = { ...S[k], sl: e.target.value }; S.cell = ""; commit(); });
  }
  window.addEventListener("hashchange", () => {
    if (location.hash === lastHash) return;
    lastHash = location.hash;
    S = parseHash(location.hash);
    render();
  });
}

start(async () => {
  S = parseHash(location.hash);
  await compareState(); // sets the default bar, so the URL always carries the full state and a copied link keeps it
  lastHash = toHash(S);
  if (location.hash !== lastHash) history.replaceState(null, "", lastHash);
  document.getElementById("meta").textContent = `${DATA.runs.length} runs · ${families().length} scenarios`;
  wire();
  return render();
});
