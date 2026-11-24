/* Template: Compare. Series B against series A of one scenario, cell by cell (TEMPLATES.md).
   A series is (scenario, branch, commit, slice); the slice is one value of the dimension the scenario declares as
   variable (sliceOf). Cells match on every other dimension, so the slice is left out of the match key. */
"use strict";

let S = null;
let lastHash = "";

const slicesOf = (s, br, c) => {
  const sl = sliceOf(s);
  if (!sl) return [];
  const vs = new Set(DATA.records.filter((r) => r.family === s && r.build.ref === br && sha9(r) === c).map((r) => r[sl.dim]));
  return [...vs].sort((a, b) => sl.order.indexOf(a) - sl.order.indexOf(b));
};
// A is unstable's newest commit when another branch exists, else the commit before B on B's branch
function defaults(s) {
  const bs = branches(s);
  const b = { br: bs[0], c: commits(s, bs[0])[0] };
  if (bs.includes("unstable") && b.br !== "unstable") return { a: { br: "unstable", c: commits(s, "unstable")[0] }, b };
  const cs = commits(s, b.br);
  return { a: { br: b.br, c: cs[1] || cs[0] }, b };
}
function parseHash(hash) {
  const q = new URLSearchParams(hash.replace(/^#/, ""));
  const ss = scenarios();
  const s = ss.includes(q.get("s")) ? q.get("s") : ss[0];
  const d = defaults(s);
  const side = (bk, ck, lk, def, slDefault) => {
    const br = branches(s).includes(q.get(bk)) ? q.get(bk) : def.br;
    const c = commits(s, br).includes(q.get(ck)) ? q.get(ck) : br === def.br ? def.c : commits(s, br)[0];
    const sls = slicesOf(s, br, c);
    const sl = !sls.length ? null : sls.includes(q.get(lk)) ? q.get(lk) : sls.includes(slDefault) ? slDefault : sls[0];
    return { br, c, sl };
  };
  const a = side("ab", "ac", "al", d.a, "none");
  const b = side("bb", "bc", "bl", d.b, a.sl); // B's slice defaults to A's
  const cell = /^[0-9a-f]{8}$/.test(q.get("cell") || "") ? q.get("cell") : "";
  const [r0, a0] = DATA.metrics[throughputMetric(s)].bar;
  const num = (k, d) => { const v = Number(q.get(k)); return q.has(k) && Number.isFinite(v) && v >= 0 ? v : d; };
  return { s, a, b, bar: [num("rel", r0), num("abs", a0)], cell };
}
function toHash(st) {
  const q = new URLSearchParams({ s: st.s, ab: st.a.br, ac: st.a.c });
  if (st.a.sl) q.set("al", st.a.sl);
  q.set("bb", st.b.br); q.set("bc", st.b.c);
  if (st.b.sl) q.set("bl", st.b.sl);
  if (st.bar) { q.set("rel", st.bar[0]); q.set("abs", st.bar[1]); }
  if (st.cell) q.set("cell", st.cell);
  return "#" + q.toString();
}
function commit() {
  const hsh = toHash(S);
  if (hsh !== location.hash) { lastHash = hsh; location.hash = hsh; }
  render();
}

// ---------- cells and the comparison ----------
const throughputMetric = (s) => (DATA.records.some((r) => r.family === s && r.metric === "server_ops") ? "server_ops" : "ops");
const seriesRecs = (side) => {
  const sl = sliceOf(S.s);
  return DATA.records.filter((r) => r.family === S.s && r.build.ref === side.br && sha9(r) === side.c && (!sl || r[sl.dim] === side.sl));
};

function cellsOf() {
  const recs = { a: seriesRecs(S.a), b: seriesRecs(S.b) };
  const all = [...recs.a, ...recs.b];
  const varying = ROW_DIMS.filter(([, , get]) => new Set(all.map(get)).size > 1);
  const rowOf = (r) => [r.command, ...varying.map(([, name, get]) => (name ? `${name} ${get(r)}` : String(get(r))))].join(" · ");
  const cells = new Map();
  for (const side of ["a", "b"]) {
    for (const r of recs[side]) {
      const key = [rowOf(r), shapeKey(r)].join("\u0001");
      let c = cells.get(key);
      if (!c) { c = { key, hash: fnv(key), row: rowOf(r), shape: shapeKey(r), a: null, b: null }; cells.set(key, c); }
      c[side] = c[side] || {};
      c[side][r.metric] = r;
    }
  }
  return { recs, cells };
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
const runLabel = (side) => `${side.br} ${side.c}`;
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

const seriesLabel = (side) => [side.br, side.c, side.sl && sliceOf(S.s).label(side.sl)].filter(Boolean).join(" ");
// a record with the slice value blanked wherever it names the slice (the scenario id carries the mix as a segment)
function unslice(r, sl) {
  if (!sl) return r;
  const seg = (v) => (typeof v === "string" ? v.split(".").map((x) => (x === sl ? "*" : x)).join(".") : v);
  return { ...r, scenario: seg(r.scenario), [sliceOf(S.s).dim]: "*" };
}
function header(recs, cells) {
  const id = (side, rs) => {
    const r = rs[0];
    if (!r) return `${side}: ${seriesLabel(S[side.toLowerCase()])}`;
    const sd = S[side.toLowerCase()];
    return [`${side}: `, h("a", { href: `https://github.com/${r.build.repo}/tree/${r.build.ref}` }, `${r.build.repo} ${r.build.ref}`), " ",
      h("a", { href: `https://github.com/${r.build.repo}/commit/${r.build.sha}`, class: "mono" }, r.build.sha.slice(0, 9)),
      sd.sl ? ` · ${sliceOf(S.s).label(sd.sl)}` : ""];
  };
  const fa = fingerprint(recs.a), fb = fingerprint(recs.b);
  // match check: over the cells on both sides, with each side's slice blanked, so a differing slice is named once
  const both = [...cells.values()].filter(onBoth);
  const ca = fingerprint(both.flatMap((c) => Object.values(c.a)).map((r) => unslice(r, S.a.sl)));
  const cb = fingerprint(both.flatMap((c) => Object.values(c.b)).map((r) => unslice(r, S.b.sl)));
  // the binary and its version follow the commit; a differing slice also changes the scenario's parameters
  const sliced = S.a.sl !== S.b.sl;
  const diff = fingerprintDiff(ca, cb, ["repository", "commit", "build_version", "build_binary_sha256", ...(sliced ? ["scenario_parameters"] : [])]);
  const same = ["repository", "commit", ...(sliced ? [sliceOf(S.s).name] : [])];
  const sameText = same.length > 2 ? `${same.slice(0, -1).join(", ")} and ${same[same.length - 1]}` : same.join(" and ");
  return [h("p", { class: "ids" }, `${S.s} · `, id("A", recs.a), " · ", id("B", recs.b)),
    fingerprintLine(fa, "A Benchmark Fingerprint"), fingerprintLine(fb, "B Benchmark Fingerprint"),
    h("p", { class: "fp-match" }, diff.length
      ? `Fingerprints differ beyond ${sameText}: ${diff.map(fieldName).join(", ")}`
      : `Fingerprints match on every field except ${sameText}`)];
}

// ---------- cell detail ----------
function renderCell(cells, m, cell) {
  const back = h("button", { type: "button", class: "back", onclick: () => { S.cell = ""; commit(); } }, "\u2190 Back to the comparison");
  if (!cell) return [back, h("p", {}, "This cell is not in the selected series.")];
  const j = judge(cell.a && cell.a[m], cell.b && cell.b[m], m);
  const unit = DATA.metrics[m].unit;
  const any = (cell.b || cell.a)[m] || Object.values(cell.b || cell.a)[0];
  const side = (name, st, by) => {
    if (!by) return h("div", { class: "side" }, h("h3", {}, `${name}: ${seriesLabel(st)}`), h("p", { class: "note" }, "not on this series"));
    const ms = METRIC_ORDER.filter((x) => by[x]);
    const rec = by[m] || by[ms[0]];
    return h("div", { class: "side" },
      h("h3", {}, `${name}: ${seriesLabel(st)}`),
      h("p", { class: "note mono" }, rec.scenario),
      strip(rec),
      runsTable({ by }, ms),
      h("h3", { style: "margin-top: 16px" }, `Rerun ${name}`),
      reproBlock(DATA.reproduce[rec.reproduce], rec));
  };
  return [h("section", { class: "block", id: "cell" },
    back,
    h("h2", { style: "margin-top: 12px" }, [panelName(cell.row), shapeLabel(cell.shape)].join(" · ")),
    h("dl", { class: "kv" }, [
      ["Scenario", S.s], ["A", seriesLabel(S.a)], ["B", seriesLabel(S.b)], ["Command sent", any.command_line || "not recorded"],
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
    h("div", { class: "sides" }, side("A", S.a, cell.a), side("B", S.b, cell.b)))];
}

// ---------- render ----------
function renderSelectors() {
  const fill = (id, values, cur, lab) => {
    const el = document.getElementById(id);
    el.replaceChildren(...values.map((v) => h("option", { value: v }, lab(v))));
    el.value = cur;
  };
  const commitLabel = (br) => (v) => { const t = CAT.get(S.s).get(br).get(v).time; return t ? `${v} · ${t.slice(0, 10)}` : v; };
  const sl = sliceOf(S.s);
  const [ir, ia] = [document.getElementById("bar-rel"), document.getElementById("bar-abs")];
  if (document.activeElement !== ir) ir.value = S.bar[0];
  if (document.activeElement !== ia) ia.value = S.bar[1];
  fill("sel-scenario", scenarios(), S.s, (v) => v);
  for (const k of ["a", "b"]) {
    fill(`sel-${k}-branch`, branches(S.s), S[k].br, (v) => `${v} (${latest(S.s, v).repo})`);
    fill(`sel-${k}-commit`, commits(S.s, S[k].br), S[k].c, commitLabel(S[k].br));
    const box = document.getElementById(`sel-${k}-slice`);
    box.closest("label").hidden = !sl;
    if (sl) {
      fill(`sel-${k}-slice`, slicesOf(S.s, S[k].br, S[k].c), S[k].sl, sl.label);
      box.previousElementSibling.textContent = `${k.toUpperCase()} ${sl.name}`;
    }
  }
}

function render() {
  renderSelectors();
  const m = throughputMetric(S.s);
  const { recs, cells } = cellsOf();
  closeDrawer();
  const out = document.getElementById("out");
  if (S.cell) out.replaceChildren(...renderCell(cells, m, [...cells.values()].find((c) => c.hash === S.cell)).filter(Boolean));
  else out.replaceChildren(...[h("section", { class: "block", id: "summary" }, header(recs, cells)), tally(cells, m), panels(cells, m), notOnBoth(cells)].filter(Boolean));
}

function wire() {
  document.getElementById("sel-scenario").addEventListener("change", (e) => { S = parseHash(toHash({ s: e.target.value, a: {}, b: {}, bar: S.bar })); commit(); });
  for (const [id, i] of [["bar-rel", 0], ["bar-abs", 1]]) {
    document.getElementById(id).addEventListener("input", (e) => {
      const v = Number(e.target.value);
      if (e.target.value === "" || !Number.isFinite(v) || v < 0) return;
      S.bar = i ? [S.bar[0], v] : [v, S.bar[1]];
      commit();
    });
  }
  for (const k of ["a", "b"]) {
    const keep = (br, c) => { const sls = slicesOf(S.s, br, c); return sls.includes(S[k].sl) ? S[k].sl : sls[0] || null; };
    document.getElementById(`sel-${k}-branch`).addEventListener("change", (e) => {
      const br = e.target.value, c = commits(S.s, br)[0];
      S[k] = { br, c, sl: keep(br, c) }; S.cell = ""; commit();
    });
    document.getElementById(`sel-${k}-commit`).addEventListener("change", (e) => {
      const c = e.target.value;
      S[k] = { br: S[k].br, c, sl: keep(S[k].br, c) }; S.cell = ""; commit();
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

start(() => {
  S = parseHash(location.hash);
  // the URL always carries the full state, the bar included, so a copied link keeps it
  lastHash = toHash(S);
  if (location.hash !== lastHash) history.replaceState(null, "", lastHash);
  document.getElementById("meta").textContent = `${DATA.records.length.toLocaleString("en-US")} results · ${scenarios().length} scenarios`;
  wire();
  render();
});
