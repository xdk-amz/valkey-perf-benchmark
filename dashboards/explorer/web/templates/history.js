/* Template: History. One branch and scenario over time: every run of it as a point, one graph per command
   (TEMPLATES.md). Runs of one commit with different fingerprints are separate series, never averaged. */
"use strict";

let S = null;
let lastHash = "";

// ---------- state <-> URL ----------
const branchKey = (b) => `${b.repo}:${b.ref}`;
const branchName = (k) => k.replace(":", " ");
// branches with the most runs first, then the newest
function branchesAll() {
  const by = new Map();
  for (const r of DATA.runs) {
    const k = branchKey(r.build);
    const e = by.get(k) || { k, n: 0, t: 0 };
    e.n++; e.t = Math.max(e.t, when(r.started));
    by.set(k, e);
  }
  return [...by.values()].sort((a, b) => b.n - a.n || b.t - a.t || a.k.localeCompare(b.k)).map((e) => e.k);
}
const scenariosOn = (b) => families().filter((f) => DATA.runs.some((r) => branchKey(r.build) === b && r.families.includes(f)));
function parseHash(hash) {
  const q = new URLSearchParams(hash.replace(/^#/, ""));
  const bs = branchesAll();
  const b = bs.includes(q.get("b")) ? q.get("b") : bs[0];
  const ss = scenariosOn(b);
  const s = ss.includes(q.get("s")) ? q.get("s") : ss[0];
  return { b, s, m: parseTab(q) };
}
const toHash = (st) => "#" + new URLSearchParams({ b: st.b, s: st.s, m: st.m || "tps" }).toString();
function commit() {
  const hsh = toHash(S);
  if (hsh !== location.hash) { lastHash = hsh; location.hash = hsh; }
  render();
}
const runsInView = () => DATA.runs.filter((r) => branchKey(r.build) === S.b && r.families.includes(S.s));

// ---------- the model ----------
const BUILD_FIELDS = ["repository", "commit", "build_version", "build_settings", "build_args", "build_binary_sha256"];
const fieldLabel = (k, v) => `${k === "io_threads" ? "io-threads" : fieldName(k)} ${v == null ? "not recorded" : typeof v === "object" ? canonical(v) : v}`;
// the setup fields that tell apart runs of one commit: a run's series carries their values
function splitFields(docs) {
  const vals = docs.map((d) => fingerprint(d.run).values);
  const out = new Set();
  docs.forEach((d, i) => docs.forEach((e, j) => {
    if (j <= i || d.run.build.sha !== e.run.build.sha) return;
    for (const k of Object.keys(vals[i])) if (!BUILD_FIELDS.includes(k) && canonical(vals[i][k] ?? null) !== canonical(vals[j][k] ?? null)) out.add(k);
  }));
  return FP.fields.map((f) => f.field).filter((k) => out.has(k));
}

// graphs (one per command row, as Single run's panels), series (shape, mix and setup) and points (one per run)
function historyOf(docs, m) {
  const split = splitFields(docs);
  const setupOf = (d) => { const v = fingerprint(d.run).values; return split.map((k) => fieldLabel(k, v[k])).join(", "); };
  const commits = [...new Map(docs.map((d) => [d.run.build.sha, d.run.build])).values()]
    .sort((a, b) => when(a.time) - when(b.time) || a.sha.localeCompare(b.sha));
  const at = new Map(commits.map((b, i) => [b.sha, i]));
  const cs = docs.flatMap((d) => d.cells.filter((c) => c.family === S.s).map((c) => ({ c, d })));
  const varying = ROW_DIMS.filter(([, , get]) => new Set(cs.map((x) => get(x.c))).size > 1);
  const rowOf = (c) => [c.command, ...varying.map(([, name, get]) => `${name} ${get(c)}`)].join(" · ");
  const multiMix = new Set(cs.map((x) => x.c.mix)).size > 1;
  const sl = sliceOf(S.s);
  const graphs = new Map();
  for (const { c, d } of cs) {
    const row = rowOf(c);
    const g = graphs.get(row) || { row, title: panelName(row), series: new Map() };
    graphs.set(row, g);
    const setup = setupOf(d);
    const key = [shapeKey(c), c.mix, setup].join("\u0001");
    const label = [shapeShort(shapeKey(c)), multiMix ? (sl ? sl.label(c.mix) : c.mix) : null, setup || null].filter(Boolean).join(" · ");
    const sr = g.series.get(key) || { key, label, shape: shapeKey(c), mix: c.mix, setup, points: [] };
    g.series.set(key, sr);
    const s = c.metrics[m];
    if (!s) continue;
    const reps = c.runs.filter((r) => r.ok && r[m] != null).length;
    sr.points.push({ x: at.get(d.run.build.sha), sha: d.run.build.sha, time: d.run.build.time, run: d.run.id, started: d.run.started,
      value: s.median, min: s.min, max: s.max, reps });
  }
  const out = [...graphs.values()].sort((a, b) => panelOrder(a.row, b.row)).map((g) => {
    const series = [...g.series.values()].sort((a, b) => panelShapeOrder(a.shape, b.shape) || mixOrder(a.mix, b.mix) || a.setup.localeCompare(b.setup));
    for (const s of series) s.points.sort((a, b) => a.x - b.x);
    return { ...g, series, hasData: series.some((s) => s.points.length) };
  });
  return { commits, graphs: out, split, metric: m };
}

// ---------- graphs ----------
const SERIES = ["oklch(45% 0.11 255)", "oklch(62% 0.14 52)", "oklch(52% 0.11 155)", "oklch(48% 0.14 330)",
  "oklch(58% 0.09 210)", "oklch(55% 0.12 100)", "oklch(40% 0.03 260)", "oklch(62% 0.13 15)"];
function graph(g, model) {
  const m = model.metric, md = DATA.metrics[m];
  const fig = (...kids) => h("figure", { class: "panel" }, h("figcaption", {}, g.title), ...kids);
  if (!g.hasData) return fig(h("p", { class: "nodata" }, "no data"));
  const pts = g.series.flatMap((s) => s.points);
  let lo = Math.min(...pts.map((p) => p.min)), hi = Math.max(...pts.map((p) => p.max));
  const pad = (hi - lo) * 0.08 || Math.abs(hi) * 0.05 || 1;
  lo = Math.max(0, lo - pad); hi += pad;
  const n = model.commits.length, W = 440, L = 58, R = 10, TOP = 8, PH = 150;
  const rot = n > 6;
  const yb = TOP + PH, H = yb + (rot ? 62 : 34);
  const step = (W - L - R) / n;
  const X = (i) => L + step * (i + 0.5);
  const Y = (v) => TOP + PH * (1 - (v - lo) / (hi - lo));
  const ns = g.series.length, dx = Math.min(5, (step * 0.5) / Math.max(1, ns));
  // a colour per series while the colours last; past that, a colour per shape (and setup), the mix in the hover table
  const byShape = ns > SERIES.length;
  const group = (s) => (byShape ? [s.shape, s.setup].join("\u0001") : s.key);
  const groupLabel = (s) => (byShape ? [shapeShort(s.shape), s.setup || null].filter(Boolean).join(" · ") : s.label);
  const groups = [...new Map(g.series.map((s) => [group(s), groupLabel(s)])).entries()];
  const colorOf = (s) => SERIES[groups.findIndex(([k]) => k === group(s)) % SERIES.length];
  const kids = [];
  for (const t of niceTicks(lo, hi, 5)) {
    kids.push(svg("line", { class: "grid", x1: L, x2: W - R, y1: Y(t), y2: Y(t) }));
    kids.push(svg("text", { class: "tick", x: L - 5, y: Y(t) + 3.5, "text-anchor": "end" }, fmtv(t, md.unit)));
  }
  kids.push(svg("line", { class: "axis", x1: L, x2: W - R, y1: yb, y2: yb }));
  kids.push(svg("text", { class: "atitle", transform: `translate(11 ${TOP + PH / 2}) rotate(-90)`, "text-anchor": "middle" }, mlabel(m)));
  model.commits.forEach((b, i) => kids.push(rot
    ? svg("text", { class: "lab mono", transform: `translate(${X(i) + 3} ${yb + 8}) rotate(-50)`, "text-anchor": "end" }, b.sha.slice(0, 9))
    : svg("text", { class: "lab mono", x: X(i), y: yb + 13, "text-anchor": "middle" }, b.sha.slice(0, 9))));
  kids.push(svg("text", { class: "atitle", x: L + (W - L - R) / 2, y: H - 3, "text-anchor": "middle" }, "commit id"));
  g.series.forEach((s, si) => {
    const color = colorOf(s);
    const off = (si - (ns - 1) / 2) * dx;
    const px = (p) => X(p.x) + off;
    if (s.points.length > 1) kids.push(svg("polyline", { class: "series", stroke: color, points: s.points.map((p) => `${px(p)},${Y(p.value)}`).join(" ") }));
    for (const p of s.points) {
      const pt = svg("g", { class: "pt", tabindex: 0, role: "link", "aria-label": `${s.label}, ${p.sha.slice(0, 9)}: ${exact(p.value, md.unit)}, open the run` },
        svg("line", { class: "range", stroke: color, x1: px(p), x2: px(p), y1: Y(p.min), y2: Y(p.max) }),
        svg("circle", { class: "dot", fill: color, cx: px(p), cy: Y(p.value), r: 3.2 }),
        svg("circle", { class: "hit", cx: px(p), cy: Y(p.value), r: 8 }));
      const show = (e) => showTip(e, s, p, md);
      pt.addEventListener("mouseenter", show);
      pt.addEventListener("focus", show);
      pt.addEventListener("mouseleave", hideTip);
      pt.addEventListener("blur", hideTip);
      pt.addEventListener("click", () => openRun(p));
      pt.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openRun(p); } });
      kids.push(pt);
    }
  });
  const key = groups.length > 1
    ? h("p", { class: "skey" }, groups.map(([, label], i) => h("span", {}, h("span", { class: "sw", style: `background: ${SERIES[i % SERIES.length]}` }), label)))
    : null;
  return fig(svg("svg", { class: "history", viewBox: `0 0 ${W} ${H}`, width: "100%", role: "img", "aria-label": `${g.title}: ${mlabel(m)} by commit` }, kids), key);
}
const openRun = (p) => { location.href = `index.html#${new URLSearchParams({ s: S.s, run: p.run, m: S.m })}`; };

// the small table a point shows
function showTip(e, s, p, md) {
  const tip = document.getElementById("tip");
  tip.replaceChildren(h("table", {}, h("caption", {}, s.label), h("tbody", {}, [
    ["Commit", p.sha.slice(0, 9)], ["Commit time", p.time || "not recorded"], ["Run date", p.started ? p.started.slice(0, 10) : "not recorded"],
    ["Median", exact(p.value, md.unit)], ["Min", exact(p.min, md.unit)], ["Max", exact(p.max, md.unit)], ["Reps", p.reps],
  ].map(([k, v]) => h("tr", {}, h("th", { scope: "row" }, k), h("td", {}, String(v)))))));
  tip.hidden = false;
  const r = e.clientX != null && e.type === "mouseenter" ? { x: e.clientX, y: e.clientY } : (() => { const b = e.currentTarget.getBoundingClientRect(); return { x: b.right, y: b.bottom }; })();
  const w = tip.offsetWidth, ht = tip.offsetHeight;
  tip.style.left = `${Math.min(r.x + 12, innerWidth - w - 8)}px`;
  tip.style.top = `${r.y + 12 + ht > innerHeight ? r.y - ht - 12 : r.y + 12}px`;
}
function hideTip() { document.getElementById("tip").hidden = true; }

// ---------- render ----------
function renderSelectors() {
  const fill = (id, values, cur, lab) => {
    const el = document.getElementById(id);
    el.replaceChildren(...values.map((v) => h("option", { value: v }, lab(v))));
    el.value = cur;
  };
  fill("sel-branch", branchesAll(), S.b, branchName);
  fill("sel-scenario", scenariosOn(S.b), S.s, (v) => v);
}

// the distinct fingerprints in view, each with the run it identifies
function fingerprints(docs, model) {
  const seen = new Set();
  const rows = [];
  for (const d of [...docs].sort((a, b) => when(a.run.build.time) - when(b.run.build.time) || a.run.id.localeCompare(b.run.id))) {
    const fp = fingerprint(d.run);
    if (seen.has(fp.hash)) continue;
    seen.add(fp.hash);
    const b = d.run.build, v = fp.values;
    const setup = model.split.map((k) => fieldLabel(k, v[k])).join(", ");
    rows.push(h("div", { class: "fprun" },
      h("p", { class: "ids" }, h("a", { href: `https://github.com/${b.repo}/commit/${b.sha}`, class: "mono" }, b.sha.slice(0, 9)),
        b.time ? ` (${b.time.slice(0, 10)})` : "", d.run.started ? ` · run ${d.run.started.slice(0, 10)}` : "", setup ? ` · ${setup}` : "",
        ` · valkey-perf-benchmark `, h("span", { class: "mono" }, (d.run.harness.sha || "").slice(0, 7))),
      fingerprintLine(fp)));
  }
  return h("section", { class: "block", id: "fingerprint" }, h("h2", {}, "Fingerprint"), rows);
}

async function render() {
  const want = toHash(S);
  const docs = await Promise.all(runsInView().map((r) => getRun(r.id)));
  if (toHash(S) !== want) return; // a newer selection is rendering
  renderSelectors();
  closeDrawer();
  hideTip();
  const out = document.getElementById("out");
  if (!docs.length) { out.replaceChildren(h("p", {}, "No runs.")); return; }
  const model = historyOf(docs, tabMetric(S.m, docs));
  out.replaceChildren(
    h("section", { class: "block", id: "history" }, tabBar(S.m, (tab) => { S.m = tab; commit(); }),
      h("div", { class: "panels" }, model.graphs.map((g) => graph(g, model)))),
    fingerprints(docs, model));
}

function wire() {
  document.getElementById("sel-branch").addEventListener("change", (e) => { S = parseHash(toHash({ b: e.target.value, s: S.s, m: S.m })); commit(); });
  document.getElementById("sel-scenario").addEventListener("change", (e) => { S = { ...S, s: e.target.value }; commit(); });
  window.addEventListener("hashchange", () => {
    if (location.hash === lastHash) return;
    lastHash = location.hash;
    S = parseHash(location.hash);
    render();
  });
}

start(() => {
  S = parseHash(location.hash);
  lastHash = toHash(S);
  if (location.hash !== lastHash) history.replaceState(null, "", lastHash);
  document.getElementById("meta").textContent = `${DATA.runs.length} runs · ${branchesAll().length} branches · ${families().length} scenarios`;
  wire();
  return render();
});
