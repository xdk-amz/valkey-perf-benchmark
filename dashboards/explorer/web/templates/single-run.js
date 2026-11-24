/* Template: Single run. One scenario run on one commit, shown as absolute numbers (TEMPLATES.md). */
"use strict";

// ---------- state <-> URL ----------
let S = null;
let lastHash = "";

function parseHash(hash) {
  const q = new URLSearchParams(hash.replace(/^#/, ""));
  const ss = scenarios();
  const s = ss.includes(q.get("s")) ? q.get("s") : ss[0];
  const bs = branches(s);
  const b = bs.includes(q.get("b")) ? q.get("b") : bs[0];
  const cs = commits(s, b);
  const c = cs.includes(q.get("c")) ? q.get("c") : cs[0];
  const cell = /^[0-9a-f]{8}$/.test(q.get("cell") || "") ? q.get("cell") : "";
  const ms = runMetrics({ s, b, c });
  const m = ms.includes(q.get("m")) ? q.get("m") : ms[0];
  return { s, b, c, m, cell };
}
function toHash(st) {
  const q = new URLSearchParams({ s: st.s, b: st.b, c: st.c });
  if (st.m) q.set("m", st.m);
  if (st.cell) q.set("cell", st.cell);
  return "#" + q.toString();
}
function commit() {
  const hsh = toHash(S);
  if (hsh !== location.hash) { lastHash = hsh; location.hash = hsh; }
  render();
}

// ---------- the run ----------
function runOf() {
  const recs = DATA.records.filter((r) => r.family === S.s && r.build.ref === S.b && sha9(r) === S.c);
  const varying = ROW_DIMS.filter(([, , get]) => new Set(recs.map(get)).size > 1);
  const rowOf = (r) => [r.command, ...varying.map(([, name, get]) => (name ? `${name} ${get(r)}` : String(get(r))))].join(" · ");
  const cells = new Map();
  for (const r of recs) {
    const key = [rowOf(r), shapeKey(r), r.mix].join("\u0001");
    let c = cells.get(key);
    if (!c) { c = { key, hash: fnv(key), row: rowOf(r), shape: shapeKey(r), mix: r.mix, by: {} }; cells.set(key, c); }
    c.by[r.metric] = r;
  }
  const metrics = METRIC_ORDER.filter((m) => recs.some((r) => r.metric === m));
  const shown = metrics.includes("server_ops") ? metrics.filter((m) => m !== "ops") : metrics;
  return { recs, cells, metrics, shown, varying };
}

// ---------- render ----------
function renderSelectors() {
  const fill = (id, values, cur, lab) => {
    const el = document.getElementById(id);
    el.replaceChildren(...values.map((v) => h("option", { value: v }, lab(v))));
    el.value = cur;
  };
  fill("sel-scenario", scenarios(), S.s, (v) => v);
  fill("sel-branch", branches(S.s), S.b, (v) => {
    const b = latest(S.s, v);
    return `${v} (${b.repo})`;
  });
  fill("sel-commit", commits(S.s, S.b), S.c, (v) => {
    const t = CAT.get(S.s).get(S.b).get(v).time;
    return t ? `${v} · ${t.slice(0, 10)}` : v;
  });
  fill("sel-metric", runMetrics(S), S.m, (v) => mlabel(v));
}

function runHeader(run) {
  const r = run.recs[0];
  const types = [...new Set(run.recs.map((x) => x.hardware.server).filter(Boolean))];
  const servers = types.length ? types.join(", ") : "a server whose type is not recorded";
  const runs = run.recs.filter((x) => x.metric === S.m && x.n > 0).map((x) => x.n);
  return [
    h("p", { class: "ids" }, `${S.s} · `, h("a", { href: `https://github.com/${r.build.repo}/tree/${r.build.ref}` }, `${r.build.repo} ${r.build.ref}`),
      ` · commit `, h("a", { href: `https://github.com/${r.build.repo}/commit/${r.build.sha}`, class: "mono" }, r.build.sha.slice(0, 9)),
      r.build.time ? ` (${r.build.time.slice(0, 10)})` : "",
      ` · ${r.harness.name} `, h("span", { class: "mono" }, (r.harness.sha || "").slice(0, 7))),
    fingerprintLine(fingerprint(run.recs)),

  ];
}

function coverage(run) {
  const m = S.m;
  const secs = sectionsOf(S.s);
  const rows = secs.map((sec) => {
    const cs = [...run.cells.values()].filter((c) => sec.test(c.mix) && c.by[m]);
    return cs.length ? [sec, cs.filter((c) => c.by[m].value != null).length, cs.filter((c) => c.by[m].value == null).length] : null;
  }).filter(Boolean);
  return h("table", { class: "coverage" },
    h("thead", {}, h("tr", {}, h("th", { scope: "col" }, secs[0].title ? "Section" : "Scenario"), h("th", { scope: "col", class: "num" }, "Cells measured"), h("th", { scope: "col", class: "num" }, "Missing"))),
    h("tbody", {}, rows.map(([sec, a, b]) => h("tr", {}, h("th", { scope: "row" }, h("a", { href: `#sec-${sec.id}`, target: "_self", onclick: jump }, sec.title || S.s)),
      h("td", { class: "num" }, a), h("td", { class: "num" }, b)))));
}

function sectionTables(run, sec) {
  const cs = [...run.cells.values()].filter((c) => sec.test(c.mix));
  if (!cs.length) return null;
  const rows = [...new Set(cs.map((c) => c.row))].sort((a, b) => a.localeCompare(b));
  const shapes = [...new Set(cs.map((c) => c.shape))].sort(shapeOrder);
  const cols = shapes.flatMap((s) => [...new Set(cs.filter((c) => c.shape === s).map((c) => c.mix))].sort(mixOrder).map((m) => [s, m]));
  const at = new Map(cs.map((c) => [`${c.row}\u0001${c.shape}\u0001${c.mix}`, c]));
  const gstart = (i) => i > 0 && cols[i][0] !== cols[i - 1][0] ? "gstart" : null;
  const multiMix = sec.head != null;
  return h("section", { class: "block", id: `tab-${sec.id}` },
    h("h3", {}, sec.title),
    run.shown.filter((m) => cs.some((c) => c.by[m])).map((m) => {
      const md = DATA.metrics[m];
      return h("div", { class: "metric" },
        h("h4", {}, mlabel(m)),
        h("div", { class: "scroll" }, h("table", { class: "grid" },
          h("thead", {},
            h("tr", { class: "shape" }, h("th", { scope: "col", rowspan: multiMix ? 2 : 1 }, "Command"),
              shapes.map((s) => h("th", { scope: "colgroup", class: multiMix ? null : "single", colspan: cols.filter(([x]) => x === s).length }, shapeLabel(s)))),
            multiMix && h("tr", { class: "mix" }, cols.map(([, mx], i) => h("th", { scope: "col", class: gstart(i), title: `${sec.head}: ${mx}` }, MIX_LABEL[mx] || mx)))),
          h("tbody", {}, rows.map((row) => h("tr", {},
            h("th", { scope: "row" }, row),
            cols.map(([s, mx], i) => {
              const c = at.get(`${row}\u0001${s}\u0001${mx}`);
              const r = c && c.by[m];
              const cls = ["num", gstart(i)].filter(Boolean).join(" ");
              if (!r) return h("td", { class: cls }, "");
              const tip = r.value == null ? `missing: ${r.missing}`
                : `${exact(r.value, md.unit)}, median of ${r.n} runs` + (r.spread && r.spread.cv != null ? `, CV ${(r.spread.cv * 100).toFixed(1)}%` : "");
              return h("td", { class: cls }, h("button", { type: "button", class: r.value == null ? "cellbtn miss" : "cellbtn", title: tip,
                onclick: () => { S.cell = c.hash; commit(); window.scrollTo(0, 0); } }, compact(r.value, md.unit)));
            })))))),
        null);
    }));
}

function panel(rows, metric, title) {
  const md = DATA.metrics[metric];
  const sense = md.better === "higher" ? 1 : -1;
  const vals = rows.filter((r) => r.rec && r.rec.value > 0);
  if (!vals.length) return null;
  // reference: the first column of the smallest shape (listpack 16 for sets), else the leftmost measured column
  const ref = vals.find((r) => r.shape === rows[0].shape) || vals[0];
  const R0 = ref.rec.value;
  // log y domain over the panel's values, at least ×1.25 tall, padded so bar ends clear the frame
  let lo = Math.min(...vals.map((r) => r.rec.value)), hi = Math.max(...vals.map((r) => r.rec.value));
  if (hi / lo < 1.25) { const f = Math.sqrt(1.25 / (hi / lo)); lo /= f; hi *= f; }
  const pad = (Math.log2(hi) - Math.log2(lo)) * 0.08;
  const L2 = Math.log2(lo) - pad, H2 = Math.log2(hi) + pad;
  const grouped = rows.some((r) => r.sub);
  const n = rows.length, W = 440, L = 52, R = 6, TOP = 8, PH = 150;
  const step = (W - L - R) / n;
  const xlab = grouped ? 54 : 18, glab = grouped ? 16 : 0;
  const yb = TOP + PH, H = yb + xlab + glab + 4;
  const Y = (v) => TOP + PH * (1 - (Math.log2(v) - L2) / (H2 - L2));
  const X = (i) => L + step * (i + 0.5);
  const kids = [];
  // y ticks: the reference value, then absolute values at ratio steps from it, 16 px apart at least
  const ticks = [[R0, true]];
  for (const side of [1, -1]) {
    let last = Y(R0);
    for (const r of RATIOS) {
      const v = R0 * r ** side;
      if (Math.log2(v) < L2 || Math.log2(v) > H2) break;
      if (Math.abs(Y(v) - last) < 16) continue;
      last = Y(v);
      ticks.push([v, false]);
    }
  }
  for (const [v, isRef] of ticks) {
    kids.push(svg("line", { class: isRef ? "refline" : "grid", x1: L, x2: W - R, y1: Y(v), y2: Y(v) }));
    kids.push(svg("text", { class: isRef ? "tick ref" : "tick", x: L - 5, y: Y(v) + 3.5, "text-anchor": "end" }, fmtv(v, md.unit)));
  }
  kids.push(svg("line", { class: "axis", x1: L, x2: W - R, y1: yb, y2: yb }));
  // x: shape groups, with the mixes inside each group in the TTL and expired sections
  const groups = [];
  rows.forEach((r, i) => { const g = groups[groups.length - 1]; if (!g || g.name !== r.group) groups.push({ name: r.group, a: i, b: i }); else g.b = i; });
  groups.forEach((g, gi) => {
    if (gi) kids.push(svg("line", { class: "sep", x1: L + step * g.a, x2: L + step * g.a, y1: TOP, y2: yb + (grouped ? xlab : 0) }));
    const xc = L + step * (g.a + g.b + 1) / 2;
    if (grouped) kids.push(svg("text", { class: "glab", x: xc, y: yb + xlab + 12, "text-anchor": "middle" }, g.name));
  });
  const bw = Math.min(26, step * 0.62), narrow = step < 34;
  rows.forEach((r, i) => {
    const g = svg("g", { class: "col" });
    const x = X(i);
    g.append(grouped
      ? svg("text", { class: "lab", transform: `translate(${x + 3} ${yb + 8}) rotate(-60)`, "text-anchor": "end" }, r.sub)
      : svg("text", { class: "lab", x, y: yb + 13, "text-anchor": "middle" }, r.group));
    const v = r.rec && r.rec.value;
    if (!(v > 0)) {
      g.append(svg("text", { class: "gap", transform: `translate(${x + 3} ${Y(R0) - 4}) rotate(-90)` }, r.rec ? "missing" : "not measured"));
    } else if (r === ref) {
      g.append(svg("rect", { class: "bar centre", x: x - bw / 2, y: Y(R0) - 2, width: bw, height: 4 }));
    } else {
      const y = Y(v), y0 = Y(R0);
      const cls = sense * Math.log2(v / R0) > 0 ? "bar better" : "bar worse";
      g.append(svg("rect", { class: cls, x: x - bw / 2, y: Math.min(y, y0), width: bw, height: Math.max(1, Math.abs(y - y0)) }));
    }
    if (v > 0) {
      const y = Y(v);
      // a label hanging below a short downward column would run into the x axis: put it above the column instead
      let up = v >= R0, ty = up ? y - 4 : y + 4;
      if (!up && narrow && ty + fmtv(v, md.unit).length * 5.6 > yb - 2) { up = true; ty = Math.min(y, Y(R0)) - 4; }
      g.append(narrow
        ? svg("text", { class: "val", transform: `translate(${x + 3} ${ty}) rotate(-90)`, "text-anchor": up ? "start" : "end" }, fmtv(v, md.unit))
        : svg("text", { class: "val", x, y: up ? ty : ty + 9, "text-anchor": "middle" }, fmtv(v, md.unit)));
    }
    const sp = r.rec && r.rec.spread;
    const tip = `${title} · ${r.label}\n` + (v > 0
      ? `${exact(v, md.unit)}, median of ${r.rec.n} runs` +
        (sp ? `\nrange ${exact(sp.min, md.unit)} to ${exact(sp.max, md.unit)}${sp.cv != null ? `, CV ${(sp.cv * 100).toFixed(1)}%` : ""}` : "")
      : r.rec ? `missing: ${r.rec.missing}` : "not measured");
    const hit = svg("rect", { class: "hit", x: L + step * i, y: TOP, width: step, height: PH + xlab, tabindex: r.cell ? 0 : null,
      role: r.cell ? "button" : null, "aria-label": tip.replace(/\n/g, "; ") }, svg("title", {}, tip));
    if (r.cell) {
      hit.addEventListener("click", () => openCell(r.cell));
      hit.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openCell(r.cell); } });
    }
    g.append(hit);
    kids.push(g);
  });
  return h("figure", { class: "panel" },
    h("figcaption", {}, title),
    svg("svg", { class: "bars", viewBox: `0 0 ${W} ${H}`, width: "100%", role: "img", "aria-label": `${title}: ${mlabel(metric)}` }, kids));
}
function openCell(cell) { S.cell = cell.hash; commit(); window.scrollTo(0, 0); }

function sectionPanels(run, sec, m) {
  const cs = [...run.cells.values()].filter((c) => sec.test(c.mix) && c.by[m]);
  if (!cs.length) return null;
  const md = DATA.metrics[m];
  const names = [...new Set(cs.map((c) => c.row))].sort(panelOrder);
  const shapes = [...new Set(cs.map((c) => c.shape))].sort(panelShapeOrder);
  const figs = names.map((name) => {
    const mine = cs.filter((c) => c.row === name);
    const rows = [];
    for (const s of shapes) {
      const ms = mine.filter((c) => c.shape === s).sort((a, b) => mixOrder(a.mix, b.mix));
      ms.forEach((c, j) => rows.push({ label: sec.head ? `${shapeShort(s)} · ${mixShort(c.mix)}` : shapeShort(s),
        shape: s, group: shapeShort(s), sub: sec.head ? mixShort(c.mix) : null,
        rec: c.by[m], cell: c, gstart: j === 0 && ms.length > 1 }));
    }
    return panel(rows, m, panelName(name));
  }).filter(Boolean);
  if (!figs.length) return null;
  const w = words(m);
  return h("section", { class: "block", id: `sec-${sec.id}` },
    sec.title && h("h2", {}, sec.title),
    h("p", { class: "legend" },
      h("span", { class: "sw better" }), w.better,
      h("span", { class: "sw worse" }), w.worse,
      h("span", { class: "sw centre" }), shapeShort(shapes[0])),
    h("div", { class: "panels" }, figs));
}

function runMetrics(st) {
  const ms = METRIC_ORDER.filter((m) => DATA.records.some((r) => r.family === st.s && r.build.ref === st.b && sha9(r) === st.c && r.metric === m && r.value != null));
  return ms.includes("server_ops") ? ms.filter((m) => m !== "ops") : ms;
}

function renderRun() {
  const run = runOf();
  if (!run.recs.length) return [h("p", {}, "No results for this commit.")];
  const cell = S.cell && [...run.cells.values()].find((c) => c.hash === S.cell);
  if (S.cell) return renderCell(run, cell);
  const appendix = h("details", { class: "appendix", id: "exact" }, h("summary", {}, "Exact values (tables)"));
  appendix.addEventListener("toggle", () => {
    if (appendix.open && appendix.childElementCount === 1) appendix.append(...sectionsOf(S.s).map((sec) => sectionTables(run, sec)).filter(Boolean));
  });
  return [h("section", { class: "block", id: "run" }, runHeader(run), coverage(run)),
    ...sectionsOf(S.s).map((sec) => sectionPanels(run, sec, S.m)), appendix];
}

// ---------- cell detail ----------
function renderCell(run, cell) {
  const back = h("button", { type: "button", class: "back", onclick: () => { S.cell = ""; commit(); } }, "\u2190 Back to the run");
  if (!cell) return [back, h("p", {}, "This cell is not in the selected run.")];
  const ms = run.metrics.filter((m) => cell.by[m]);
  const any = cell.by[ms[0]];
  const rp = DATA.reproduce[any.reproduce];
  const sec = sectionsOf(S.s).find((s) => s.test(cell.mix));
  return [h("section", { class: "block", id: "cell" },
    back,
    h("h2", { style: "margin-top: 12px" }, [cell.row, shapeLabel(cell.shape), sec.head ? `${sec.head}: ${MIX_LABEL[cell.mix] || cell.mix}` : sec.title].filter(Boolean).join(" · ")),
    h("dl", { class: "kv" }, [
      ["Scenario", S.s], ["Branch", `${any.build.repo} ${any.build.ref}`], ["Commit", any.build.sha],
      ["Mix id", cell.mix], ["Pipeline", any.pipeline], ["Clients", any.clients], ["io-threads", any.io_threads ?? "not recorded"],
      ["Server", any.hardware.server || "not recorded"], ["Client host", any.hardware.client || "not recorded"],
      ["Scenario id", any.scenario], ["Command sent", any.command_line || "not recorded"],
      ["Harness", `${any.harness.repo} ${any.harness.sha}`], ["Data set", any.data_set],
    ].map(([k, v]) => [h("dt", {}, k), h("dd", {}, String(v))])),
    h("h3", {}, "Values"),
    h("table", {},
      h("thead", {}, h("tr", {}, ["Metric", "Median", "Valid runs", "Min", "Max", "CV"].map((x, i) => h("th", { scope: "col", class: i ? "num" : "" }, x)))),
      h("tbody", {}, ms.map((m) => {
        const r = cell.by[m], u = DATA.metrics[m].unit, sp = r.spread;
        return h("tr", {}, h("th", { scope: "row" }, mlabel(m)),
          h("td", { class: "num" }, r.value == null ? `missing: ${r.missing}` : exact(r.value, u)), h("td", { class: "num" }, r.n),
          h("td", { class: "num" }, sp ? exact(sp.min, u) : ""), h("td", { class: "num" }, sp ? exact(sp.max, u) : ""),
          h("td", { class: "num" }, sp && sp.cv != null ? `${(sp.cv * 100).toFixed(1)}%` : ""));
      }))),
    strip(cell.by[S.m] || cell.by[run.shown[0]] || any),
    h("h3", { style: "margin-top: 20px" }, "Rerun"),
    reproBlock(rp, any),
    runsTable(cell, ms),
    h("details", {}, h("summary", {}, "Records as stored"), h("pre", { class: "mono raw" }, JSON.stringify(ms.map((m) => cell.by[m]), null, 1))))];
}

// ---------- method and sources ----------
function render() {
  renderSelectors();
  closeDrawer();
  document.getElementById("out").replaceChildren(...renderRun().filter(Boolean));
}

function wire() {
  document.getElementById("sel-scenario").addEventListener("change", (e) => { S = parseHash(toHash({ s: e.target.value, b: "", c: "", m: S.m })); commit(); });
  document.getElementById("sel-branch").addEventListener("change", (e) => { S = parseHash(toHash({ s: S.s, b: e.target.value, c: "", m: S.m })); commit(); });
  document.getElementById("sel-commit").addEventListener("change", (e) => { S = parseHash(toHash({ s: S.s, b: S.b, c: e.target.value, m: S.m })); commit(); });
  document.getElementById("sel-metric").addEventListener("change", (e) => { S.m = e.target.value; S.cell = ""; commit(); });
  window.addEventListener("hashchange", () => {
    if (location.hash === lastHash) return;
    lastHash = location.hash;
    S = parseHash(location.hash);
    render();
  });
}

start(() => {
  S = parseHash(location.hash);
  lastHash = location.hash;
  document.getElementById("meta").textContent = `${DATA.records.length.toLocaleString("en-US")} results · ${scenarios().length} scenarios`;
  wire();
  render();
});
