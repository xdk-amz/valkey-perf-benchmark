/* Template: Compare. Series B against series A, cell by cell (TEMPLATES.md).
   A series is a run file and, when the run's scenario declares a variable dimension (sliceOf), one value of it:
   the slice. Cells match on their scenario and their cell id with the slice left out, so any two run files
   compare; cells only one series has are listed once. */
"use strict";

let S = null;
let lastHash = "";

const familyOf = (id) => (entry(id) ? entry(id).families[0] : null);
const slicesOf = (id) => {
  const sl = sliceOf(familyOf(id));
  if (!sl) return [];
  return [...entry(id).mixes].sort((a, b) => sl.order.indexOf(a) - sl.order.indexOf(b));
};
// B is the newest run of the first scenario off unstable; A is an unstable run of the same scenario, else another
function defaults() {
  const rs = runsOf(families()[0]);
  const b = rs.find((r) => r.build.ref !== "unstable") || rs[0];
  const a = rs.find((r) => r.build.ref === "unstable" && r.id !== b.id) || rs.find((r) => r.id !== b.id) || b;
  return { a: a.id, b: b.id };
}
function parseHash(hash) {
  const q = new URLSearchParams(hash.replace(/^#/, ""));
  const d = defaults();
  const side = (rk, lk, def, slDefault) => {
    const r = entry(q.get(rk)) ? q.get(rk) : def;
    const sls = slicesOf(r);
    const sl = !sls.length ? null : sls.includes(q.get(lk)) ? q.get(lk) : sls.includes(slDefault) ? slDefault : sls[0];
    return { r, sl };
  };
  const a = side("a", "al", d.a, "none");
  const b = side("b", "bl", d.b, a.sl); // B's slice defaults to A's
  const cell = /^[0-9a-f]{8}$/.test(q.get("cell") || "") ? q.get("cell") : "";
  const num = (k) => { const v = Number(q.get(k)); return q.has(k) && Number.isFinite(v) && v >= 0 ? v : null; };
  return { a, b, bar: [num("rel"), num("abs")], cell };
}
function toHash(st) {
  const q = new URLSearchParams({ a: st.a.r });
  if (st.a.sl) q.set("al", st.a.sl);
  q.set("b", st.b.r);
  if (st.b.sl) q.set("bl", st.b.sl);
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
const throughputMetric = (docs) => (docs.some((d) => d.cells.some((c) => c.metrics.server_ops)) ? "server_ops" : "ops");
// a cell id with the slice value blanked wherever it is a segment (the set scenarios' ids carry the mix)
const unslice = (id, sl) => (sl ? id.split(".").map((x) => (x === sl ? "*" : x)).join(".") : id);
const seriesCells = (doc, side) => {
  const fam = familyOf(side.r), sl = sliceOf(fam);
  return doc.cells.filter((c) => c.family === fam && (!sl || c[sl.dim] === side.sl));
};

function cellsOf(docA, docB) {
  const cs = { a: seriesCells(docA, S.a), b: seriesCells(docB, S.b) };
  const all = [...cs.a, ...cs.b];
  const varying = ROW_DIMS.filter(([, , get]) => new Set(all.map(get)).size > 1);
  const rowOf = (c) => [c.command, ...varying.map(([, name, get]) => `${name} ${get(c)}`)].join(" · ");
  const cells = new Map();
  for (const side of ["a", "b"]) {
    for (const c of cs[side]) {
      const key = `${c.family}\u0001${unslice(c.id, S[side].sl)}`;
      let x = cells.get(key);
      if (!x) { x = { key, hash: fnv(key), row: rowOf(c), shape: shapeKey(c), a: null, b: null, ca: null, cb: null }; cells.set(key, x); }
      x[side] = cellView(c);
      x[`c${side}`] = c;
    }
  }
  return cells;
}

// B against A for one metric: paired runs when they share pair ids, else the medians with non-overlapping ranges
function judge(ra, rb, m) {
  if (!ra) return { status: "onlyB" };
  if (!rb) return { status: "onlyA" };
  if (ra.value == null || rb.value == null) return { status: "missing", why: ra.value == null ? `A: ${ra.missing}` : `B: ${rb.missing}` };
  const [relBar, absBar] = S.bar;
  const per = (a, b) => 1e9 / b - 1e9 / a;
  const am = new Map(ra.runs.filter(ok).filter((x) => x.p).map((x) => [x.p, x.v]));
  const pairs = rb.runs.filter(ok).filter((x) => x.p && am.has(x.p)).map((x) => ({ p: x.p, a: am.get(x.p), b: x.v }));
  let pct, dns, ref, agree, how;
  if (pairs.length) {
    const ps = pairs.map((x) => 100 * (x.b / x.a - 1));
    pct = median(ps);
    dns = median(pairs.map((x) => per(x.a, x.b)));
    ref = median(pairs.map((x) => x.a));
    agree = pairs.length >= 5 && (ps.every((x) => x > 0) || ps.every((x) => x < 0));
    how = `${pairs.length} pairs`;
  } else {
    const va = ra.runs.filter(ok).map((x) => x.v), vb = rb.runs.filter(ok).map((x) => x.v);
    pct = 100 * (rb.value / ra.value - 1);
    dns = per(ra.value, rb.value);
    ref = ra.value;
    agree = va.length >= 5 && vb.length >= 5 && (Math.max(...vb) < Math.min(...va) || Math.min(...vb) > Math.max(...va));
    how = `${va.length} and ${vb.length} unpaired runs`;
  }
  const flag = agree && Math.abs(pct) > relBar && Math.abs(dns) > absBar;
  const t = 1e9 / ref;
  const band = [Math.min(1 - relBar / 100, t / (t + absBar)), Math.max(1 + relBar / 100, t > absBar ? t / (t - absBar) : 99)];
  return { status: flag ? (pct < 0 ? "slower" : "faster") : "pass", pct, dns, pairs, band, how, a: ra.value, b: rb.value, na: ra.n, nb: rb.n };
}
const compared = (j) => ["pass", "slower", "faster"].includes(j.status);

// ---------- formatting ----------
const pctText = (v) => `${v >= 0 ? "+" : "\u2212"}${Math.abs(v).toFixed(1)}%`;
function nsText(v) {
  const a = Math.abs(v);
  const s = a >= 1e6 ? `${(a / 1e6).toFixed(2)} ms` : a >= 1e3 ? `${(a / 1e3).toFixed(1)} µs` : `${a.toFixed(0)} ns`;
  return `${v >= 0 ? "+" : "\u2212"}${s}`;
}
const STATUS_TEXT = { slower: "Fail", faster: "Exceed", pass: "Pass",
  onlyB: "no run on A", onlyA: "no run on B", missing: "missing" };

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
      g.append(svg("text", { class: `val ${j.status}`, x: PX1 + 12, y: y + 4 }, `${pctText(j.pct)} ${nsText(j.dns)}`));
      tip += `A ${exact(j.a, DATA.metrics[m].unit)} (n ${j.na}) · B ${exact(j.b, DATA.metrics[m].unit)} (n ${j.nb})\n` +
        `${pctText(j.pct)}, ${nsText(j.dns)} per call, ${j.how}\n${STATUS_TEXT[j.status]}`;
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
  const cs = [...cells.values()].filter(onBoth).map((c) => ({ c, j: judge(c.a[m], c.b[m], m) })).filter((x) => x.j.status !== "onlyA" && x.j.status !== "onlyB");
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
  const cs = [...cells.values()].filter(onBoth).map((c) => ({ c, j: judge(c.a[m], c.b[m], m) })).filter((x) => compared(x.j));
  if (!cs.length) return null;
  const k = (xs) => ({ slower: xs.filter((x) => x.j.status === "slower").length, pass: xs.filter((x) => x.j.status === "pass").length,
    faster: xs.filter((x) => x.j.status === "faster").length });
  const row = (label, t, cls) => h("tr", { class: cls }, h("th", { scope: "row" }, label),
    ["slower", "pass", "faster"].map((s) => h("td", { class: `num v-${s}` }, t[s])));
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

const seriesLabel = (side) => {
  const e = entry(side.r);
  return [familyOf(side.r), runLabel(e), side.sl && sliceOf(familyOf(side.r)).label(side.sl)].filter(Boolean).join(" · ");
};
function header(docA, docB) {
  const id = (name, doc, sd) => {
    const b = doc.run.build;
    return [`${name}: `, h("a", { href: `https://github.com/${b.repo}/tree/${b.ref}` }, `${b.repo} ${b.ref}`), " ",
      h("a", { href: `https://github.com/${b.repo}/commit/${b.sha}`, class: "mono" }, b.sha.slice(0, 9)),
      sd.sl ? ` · ${sliceOf(familyOf(sd.r)).label(sd.sl)}` : "", ` · run ${doc.run.id}`];
  };
  const fa = fingerprint(docA.run), fb = fingerprint(docB.run);
  // the binary and its version follow the commit
  const diff = fingerprintDiff(fa, fb, ["repository", "commit", "build_version", "build_binary_sha256"]);
  const sliced = S.a.sl !== S.b.sl;
  const same = ["repository", "commit", ...(sliced ? [sliceOf(familyOf(S.a.r)).name] : [])];
  const sameText = same.length > 2 ? `${same.slice(0, -1).join(", ")} and ${same[same.length - 1]}` : same.join(" and ");
  return [h("p", { class: "ids" }, id("A", docA, S.a), " · ", id("B", docB, S.b)),
    fingerprintLine(fa, "A Benchmark Fingerprint"), fingerprintLine(fb, "B Benchmark Fingerprint"),
    h("p", { class: "fp-match" }, diff.length
      ? `Fingerprints differ beyond ${sameText}: ${diff.map(fieldName).join(", ")}`
      : `Fingerprints match on every field except ${sameText}`)];
}

// ---------- cell detail ----------
function renderCell(docs, m, cell) {
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
      h("thead", {}, h("tr", {}, [mlabel(m), "A", "B", "Change", "Per call", "Pairs", "Verdict"].map((x, i) => h("th", { scope: "col", class: i && i < 5 ? "num" : "" }, x)))),
      h("tbody", {}, h("tr", {}, h("th", { scope: "row" }, "median"),
        h("td", { class: "num" }, cell.a && cell.a[m] ? exact(cell.a[m].value, unit) : "not on A"),
        h("td", { class: "num" }, cell.b && cell.b[m] ? exact(cell.b[m].value, unit) : "not on B"),
        h("td", { class: `num v-${j.status}` }, compared(j) ? pctText(j.pct) : ""),
        h("td", { class: "num" }, compared(j) ? nsText(j.dns) : ""),
        h("td", {}, compared(j) ? j.how : ""), h("td", {}, j.why || STATUS_TEXT[j.status])))),
    compared(j) && j.pairs.length ? h("details", { open: j.pairs.length <= 12, style: "margin-top: 12px" }, h("summary", {}, `Pairs (${j.pairs.length})`),
      h("table", {}, h("thead", {}, h("tr", {}, ["Pair", "A", "B", "Change"].map((x, i) => h("th", { scope: "col", class: i ? "num" : "" }, x)))),
        h("tbody", {}, j.pairs.map((p) => h("tr", {}, h("th", { scope: "row" }, p.p), h("td", { class: "num" }, compact(p.a, unit)),
          h("td", { class: "num" }, compact(p.b, unit)), h("td", { class: "num" }, pctText(100 * (p.b / p.a - 1)))))))) : null,
    h("div", { class: "sides" }, side("A", S.a, cell.a, cell.ca, docs[0]), side("B", S.b, cell.b, cell.cb, docs[1])))];
}

// ---------- render ----------
function renderSelectors() {
  const fill = (id, values, cur, lab) => {
    const el = document.getElementById(id);
    el.replaceChildren(...values.map((v) => h("option", { value: v }, lab(v))));
    el.value = cur;
  };
  const [ir, ia] = [document.getElementById("bar-rel"), document.getElementById("bar-abs")];
  if (document.activeElement !== ir) ir.value = S.bar[0];
  if (document.activeElement !== ia) ia.value = S.bar[1];
  const all = families().flatMap((f) => runsOf(f).filter((r) => r.families[0] === f)).map((r) => r.id);
  for (const k of ["a", "b"]) {
    fill(`sel-${k}-run`, all, S[k].r, (v) => `${familyOf(v)} · ${runLabel(entry(v))}`);
    const sl = sliceOf(familyOf(S[k].r));
    const box = document.getElementById(`sel-${k}-slice`);
    box.closest("label").hidden = !sl;
    if (sl) {
      fill(`sel-${k}-slice`, slicesOf(S[k].r), S[k].sl, sl.label);
      box.previousElementSibling.textContent = `${k.toUpperCase()} ${sl.name}`;
    }
  }
}

// the comparison of the selected series: both run files, the metric, the matched cells
async function compareState() {
  const docs = await Promise.all([getRun(S.a.r), getRun(S.b.r)]);
  const m = throughputMetric(docs);
  if (S.bar[0] == null) S.bar = DATA.metrics[m].bar.slice(0, 2);
  return { docs, m, cells: cellsOf(docs[0], docs[1]) };
}

async function render() {
  const { docs, m, cells } = await compareState();
  renderSelectors();
  closeDrawer();
  const out = document.getElementById("out");
  if (S.cell) out.replaceChildren(...renderCell(docs, m, [...cells.values()].find((c) => c.hash === S.cell)).filter(Boolean));
  else out.replaceChildren(...[h("section", { class: "block", id: "summary" }, header(docs[0], docs[1])), tally(cells, m), panels(cells, m), notOnBoth(cells)].filter(Boolean));
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
      const r = e.target.value, sls = slicesOf(r);
      S[k] = { r, sl: sls.includes(S[k].sl) ? S[k].sl : sls.includes("none") ? "none" : sls[0] || null }; S.cell = ""; commit();
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
