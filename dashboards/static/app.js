/* Static benchmark dashboard: reads data.json (or inline data) and charts it.
 * All state lives in the URL query string so a view can be shared as a link.
 * No network access besides the relative data.json fetch. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const NONE = "(none)";
  const NO_GROUP = "_none";
  const CONFIG = "_config";
  const MAX_TABLE_ROWS = 2000;
  const PALETTE = [
    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
    "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac",
  ];
  const DEFAULTS = {
    view: "chart", m: "", x: "", g: "", agg: "median", pts: "0", logy: "0",
    cc: "", ca: "", cb: "", band: "2", sort: "config", all: "0", ts: "", td: "asc",
  };

  let columns = [];
  let colByName = new Map();
  let rows = [];
  let distinct = new Map();
  let configCandidates = [];
  let configLabelCols = [];
  let state = {};
  let lastTable = { name: "table", header: [], body: [] };
  const charts = new Map();
  const dark = window.matchMedia("(prefers-color-scheme: dark)");
  const resizer = new ResizeObserver(() => charts.forEach((c) => c.resize()));

  // ---------- small helpers ----------

  function key(v) {
    return v === null || v === undefined || v === "" ? NONE : String(v);
  }
  function isNum(v) {
    return typeof v === "number" && Number.isFinite(v);
  }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }
  function natural(a, b) {
    const na = Number(a);
    const nb = Number(b);
    if (a !== NONE && b !== NONE && a !== "" && b !== "" && !isNaN(na) && !isNaN(nb)) {
      return na - nb;
    }
    return String(a).localeCompare(String(b), "en", { numeric: true });
  }
  const nf = [0, 2, 3].map((d) => new Intl.NumberFormat("en-US", { maximumFractionDigits: d }));
  const compactNf = new Intl.NumberFormat("en-US", { notation: "compact", maximumSignificantDigits: 4 });
  function fmt(v) {
    if (!isNum(v)) return "";
    const a = Math.abs(v);
    return (a >= 1000 ? nf[0] : a >= 10 ? nf[1] : nf[2]).format(v);
  }
  function compact(v) {
    return isNum(v) ? (Math.abs(v) >= 10000 ? compactNf.format(v) : fmt(v)) : "";
  }
  function pct(v) {
    return isNum(v) ? (v > 0 ? "+" : "") + v.toFixed(2) + "%" : "";
  }
  function label(name) {
    if (name === CONFIG) return "config";
    if (name === "rps") return "ops/s (rps)";
    if (name === "server_ops_s") return "server ops/s";
    let m = /^p(\d+)_latency_(ms|us)$/.exec(name);
    if (m) {
      const d = m[1];
      return "p" + (d.length > 2 ? d.slice(0, 2) + "." + d.slice(2) : d) + " latency (" + m[2] + ")";
    }
    m = /^(avg|min|max)_latency_(ms|us)$/.exec(name);
    if (m) return m[1] + " latency (" + m[2] + ")";
    return name;
  }
  function lowerIsBetter(name) {
    return /latency|(_ms|_us|_bytes|_per_key)$/.test(name);
  }
  function colType(name) {
    if (name === CONFIG) return "string";
    const c = colByName.get(name);
    return c ? c.type : "string";
  }
  function stats(values) {
    const v = values.slice().sort((a, b) => a - b);
    const n = v.length;
    const median = n % 2 ? v[(n - 1) / 2] : (v[n / 2 - 1] + v[n / 2]) / 2;
    const mean = v.reduce((s, x) => s + x, 0) / n;
    return { n, median, mean, min: v[0], max: v[n - 1] };
  }
  function el(tag, attrs, text) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function setStatus(text) {
    $("status").textContent = text;
  }

  // ---------- data ----------

  async function loadData() {
    const inline = $("vpb-data");
    if (inline && inline.textContent.trim()) return JSON.parse(inline.textContent);
    const res = await fetch("data.json", { cache: "no-cache" });
    if (!res.ok) throw new Error("data.json: HTTP " + res.status);
    return res.json();
  }

  function value(row, name) {
    return name === CONFIG ? row._config : row[name];
  }

  function prepare(data) {
    columns = data.columns;
    colByName = new Map(columns.map((c) => [c.name, c]));
    const names = columns.map((c) => c.name);
    rows = data.rows.map((r) => {
      const o = {};
      names.forEach((n, i) => { o[n] = r[i]; });
      return o;
    });
    distinct = new Map();
    for (const n of names) {
      const counts = new Map();
      for (const r of rows) {
        const k = key(r[n]);
        counts.set(k, (counts.get(k) || 0) + 1);
      }
      distinct.set(n, counts);
    }
    // Label configs by the fewest config columns that tell them apart.
    configCandidates = columns
      .filter((c) => c.config && [...distinct.get(c.name).keys()].filter((k) => k !== NONE).length > 1)
      .map((c) => c.name);
    configLabelCols = distinguishing(rows, configCandidates);
    const configCounts = new Map();
    for (const r of rows) {
      r._config = configLabel(r, configLabelCols);
      configCounts.set(r._config, (configCounts.get(r._config) || 0) + 1);
    }
    distinct.set(CONFIG, configCounts);
  }

  const LABEL_PRIORITY = ["command", "pipeline", "clients", "data_size", "io_threads", "cell", "scenario", "test"];

  // Pick, in priority order, the columns that each tell more configs apart.
  function distinguishing(subset, cols) {
    const ordered = cols.slice().sort((a, b) => {
      const pa = LABEL_PRIORITY.indexOf(a);
      const pb = LABEL_PRIORITY.indexOf(b);
      return (pa < 0 ? 99 : pa) - (pb < 0 ? 99 : pb);
    });
    const hashes = new Set(subset.map((r) => r.config_hash)).size;
    const chosen = [];
    let count = 1;
    for (const c of ordered) {
      if (count >= hashes) break;
      const n = new Set(subset.map((r) => [...chosen, c].map((k) => key(r[k])).join("\u0000"))).size;
      if (n > count) {
        chosen.push(c);
        count = n;
      }
    }
    return chosen;
  }

  function configLabel(row, cols) {
    const parts = [];
    for (const c of cols) {
      const v = row[c];
      if (v === null || v === undefined || v === "") continue;
      parts.push(typeof v === "string" && isNaN(Number(v)) ? v : c + "=" + v);
    }
    return parts.length ? parts.join(" · ") : "all";
  }

  // Distinct keys of a column in display order. Commits sort by first timestamp.
  function orderedKeys(name, subset) {
    const keys = subset ? [...new Set(subset.map((r) => key(value(r, name))))] : [...distinct.get(name).keys()];
    if (colByName.has("timestamp") && /commit|branch|build/.test(name)) {
      const first = new Map();
      for (const r of rows) {
        const k = key(value(r, name));
        const t = r.timestamp || "";
        if (!first.has(k) || t < first.get(k)) first.set(k, t);
      }
      return keys.sort((a, b) => (first.get(a) || "").localeCompare(first.get(b) || "") || natural(a, b));
    }
    return keys.sort(natural);
  }

  function varies(name) {
    const d = distinct.get(name);
    return d && d.size > 1;
  }
  function metricCols() {
    return columns.filter((c) => c.role === "metric" && rows.some((r) => isNum(r[c.name]))).map((c) => c.name);
  }
  function dimensionCols() {
    const dims = columns.filter((c) => c.role === "dimension" && varies(c.name)).map((c) => c.name);
    const config = dims.filter((n) => colByName.get(n).config);
    const other = dims.filter((n) => !colByName.get(n).config);
    return (configLabelCols.length > 1 ? [CONFIG] : []).concat(config, other);
  }

  // ---------- state ----------

  function readState(defaultView) {
    const search = window.location.search;
    const p = new URLSearchParams(search && search !== "?" ? search : defaultView || "");
    const s = {};
    for (const k of Object.keys(DEFAULTS)) s[k] = p.get(k) !== null ? p.get(k) : DEFAULTS[k];
    s.filters = {};
    s.ranges = {};
    for (const [k, v] of p) {
      if (k.startsWith("f.") && (colByName.has(k.slice(2)) || k.slice(2) === CONFIG)) {
        try {
          const arr = JSON.parse(v);
          if (Array.isArray(arr)) s.filters[k.slice(2)] = arr.map(String);
        } catch (e) { /* ignore a malformed filter */ }
      } else if (k.startsWith("r.") && colByName.has(k.slice(2))) {
        const [lo, hi] = v.split(",");
        s.ranges[k.slice(2)] = [lo || "", hi || ""];
      }
    }
    return s;
  }

  function writeState() {
    const p = new URLSearchParams();
    for (const [k, d] of Object.entries(DEFAULTS)) if (state[k] !== d) p.set(k, state[k]);
    for (const [c, vals] of Object.entries(state.filters)) p.set("f." + c, JSON.stringify(vals));
    for (const [c, [lo, hi]] of Object.entries(state.ranges)) p.set("r." + c, lo + "," + hi);
    const qs = p.toString();
    try {
      window.history.replaceState(null, "", qs ? "?" + qs : window.location.pathname);
    } catch (e) { /* sandboxed viewers may refuse; the view still works */ }
  }

  function firstOf(preferred, available) {
    return preferred.find((n) => available.includes(n)) || available[0] || "";
  }

  function resolveState() {
    const metrics = metricCols();
    const dims = dimensionCols();
    if (!["chart", "compare", "table"].includes(state.view)) state.view = "chart";
    if (!metrics.includes(state.m)) {
      state.m = firstOf(["server_ops_s", "rps", "p50_latency_ms", "p99_latency_ms"], metrics);
    }
    if (!dims.includes(state.x)) {
      state.x = firstOf([CONFIG, "command", "pipeline", "clients", "data_size", "io_threads", "scenario", "timestamp"], dims);
    }
    if (state.g !== NO_GROUP && !dims.includes(state.g)) {
      const g = firstOf(["branch", "build", "commit", "command", "pipeline"], dims.filter((d) => d !== state.x));
      state.g = g || NO_GROUP;
    }
    if (!["median", "mean", "min", "max"].includes(state.agg)) state.agg = "median";
    if (!dims.includes(state.cc) || state.cc === CONFIG) {
      state.cc = firstOf(["branch", "build", "commit", "module_commit"], dims.filter((d) => d !== CONFIG));
    }
    const vals = state.cc ? orderedKeys(state.cc) : [];
    if (!vals.includes(state.ca)) state.ca = vals[0] || "";
    if (!vals.includes(state.cb) || state.cb === state.ca) state.cb = vals.find((v) => v !== state.ca) || "";
    if (!isNum(Number(state.band)) || Number(state.band) < 0 || state.band === "") state.band = DEFAULTS.band;
    if (!["config", "delta"].includes(state.sort)) state.sort = "config";
    for (const c of Object.keys(state.filters)) if (!distinct.has(c)) delete state.filters[c];
  }

  // ---------- filtering ----------

  function filtered(except) {
    const sets = Object.entries(state.filters)
      .filter(([c, vals]) => c !== except && vals.length)
      .map(([c, vals]) => [c, new Set(vals)]);
    const ranges = Object.entries(state.ranges)
      .map(([c, [lo, hi]]) => [c, lo === "" ? -Infinity : Number(lo), hi === "" ? Infinity : Number(hi)])
      .filter(([, lo, hi]) => !(lo === -Infinity && hi === Infinity));
    return rows.filter((r) => {
      for (const [c, set] of sets) if (!set.has(key(value(r, c)))) return false;
      for (const [c, lo, hi] of ranges) {
        const v = r[c];
        if (!isNum(v) || v < lo || v > hi) return false;
      }
      return true;
    });
  }

  // ---------- controls ----------

  function fillSelect(select, options, selected) {
    select.replaceChildren();
    for (const [val, text] of options) select.add(new Option(text, val, false, val === selected));
  }

  function buildControls() {
    const metrics = metricCols();
    const dims = dimensionCols();
    fillSelect($("metric"), metrics.map((m) => [m, label(m)]), state.m);
    fillSelect($("x"), dims.map((d) => [d, label(d)]), state.x);
    fillSelect($("group"), [[NO_GROUP, "(none)"]].concat(dims.map((d) => [d, label(d)])), state.g);
    fillSelect($("cmpcol"), dims.filter((d) => d !== CONFIG).map((d) => [d, label(d)]), state.cc);
    const vals = state.cc ? orderedKeys(state.cc) : [];
    fillSelect($("cmpa"), vals.map((v) => [v, v]), state.ca);
    fillSelect($("cmpb"), vals.map((v) => [v, v]), state.cb);
    $("agg").value = state.agg;
    $("points").checked = state.pts === "1";
    $("logy").checked = state.logy === "1";
    $("allcols").checked = state.all === "1";
    $("band").value = state.band;
    $("sort").value = state.sort;
    renderFilters();
  }

  function renderFilters() {
    const box = $("filters");
    box.replaceChildren();
    for (const [c, selected] of Object.entries(state.filters)) {
      const wrap = el("div", { class: "filter" });
      const lab = el("label", {}, label(c));
      const counts = distinct.get(c);
      const keys = orderedKeys(c);
      const sel = el("select", { multiple: "", size: String(Math.min(8, Math.max(2, keys.length))) });
      for (const k of keys) sel.add(new Option(k + " (" + counts.get(k) + ")", k, false, selected.includes(k)));
      sel.addEventListener("change", () => {
        state.filters[c] = [...sel.selectedOptions].map((o) => o.value);
        update();
      });
      lab.appendChild(sel);
      wrap.appendChild(lab);
      wrap.appendChild(removeButton(() => delete state.filters[c]));
      box.appendChild(wrap);
    }
    for (const [c, [lo, hi]] of Object.entries(state.ranges)) {
      const wrap = el("div", { class: "filter" });
      const range = el("div", { class: "range" });
      for (const [i, v, text] of [[0, lo, "min"], [1, hi, "max"]]) {
        const lab = el("label", {}, label(c) + " " + text);
        const input = el("input", { type: "number", step: "any", value: v });
        input.addEventListener("change", () => {
          state.ranges[c][i] = input.value;
          update(false);
        });
        lab.appendChild(input);
        range.appendChild(lab);
      }
      wrap.appendChild(range);
      wrap.appendChild(removeButton(() => delete state.ranges[c]));
      box.appendChild(wrap);
    }
    const active = Object.values(state.filters).filter((v) => v.length).length +
      Object.values(state.ranges).filter(([lo, hi]) => lo !== "" || hi !== "").length;
    $("filter-count").textContent = active ? "(" + active + " active)" : "";
    if (active) $("filter-box").open = true;

    const used = new Set([...Object.keys(state.filters), ...Object.keys(state.ranges)]);
    const options = [["", "choose a column…"]];
    for (const d of dimensionCols()) if (!used.has(d)) options.push([d, label(d)]);
    for (const m of metricCols()) if (!used.has(m)) options.push(["r:" + m, label(m) + " (range)"]);
    fillSelect($("add-filter"), options, "");
  }

  function removeButton(remove) {
    const b = el("button", { type: "button", "aria-label": "Remove filter" }, "✕");
    b.addEventListener("click", () => {
      remove();
      update();
    });
    return b;
  }

  function bindControls() {
    const bind = (id, field, rebuild) => $(id).addEventListener("change", (e) => {
      const t = e.target;
      state[field] = t.type === "checkbox" ? (t.checked ? "1" : "0") : t.value;
      if (rebuild) resolveState();
      update(rebuild);
    });
    bind("metric", "m");
    bind("x", "x");
    bind("group", "g");
    bind("agg", "agg");
    bind("points", "pts");
    bind("logy", "logy");
    bind("allcols", "all");
    bind("cmpcol", "cc", true);
    bind("cmpa", "ca");
    bind("cmpb", "cb");
    bind("band", "band");
    bind("sort", "sort");
    $("add-filter").addEventListener("change", (e) => {
      const v = e.target.value;
      if (v.startsWith("r:")) state.ranges[v.slice(2)] = ["", ""];
      else if (v) state.filters[v] = [];
      $("filter-box").open = true;
      update();
    });
    $("clear-filters").addEventListener("click", () => {
      state.filters = {};
      state.ranges = {};
      update();
    });
    for (const b of document.querySelectorAll(".tabs button")) {
      b.addEventListener("click", () => {
        state.view = b.dataset.view;
        update(false);
      });
    }
    $("copy-link").addEventListener("click", copyLink);
    $("download-csv").addEventListener("click", downloadCsv);
    dark.addEventListener("change", render);
  }

  function update(rebuild = true) {
    if (rebuild) buildControls();
    writeState();
    render();
  }

  async function copyLink() {
    const url = window.location.href;
    try {
      await navigator.clipboard.writeText(url);
      setStatus("Link copied.");
    } catch (e) {
      const out = $("link-out");
      out.hidden = false;
      out.value = url;
      out.select();
      setStatus("Copy the link from the box.");
    }
  }

  function csvCell(v) {
    const s = v === null || v === undefined ? "" : String(v);
    return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }

  function downloadCsv() {
    const lines = [lastTable.header, ...lastTable.body].map((r) => r.map(csvCell).join(","));
    const blob = new Blob([lines.join("\n") + "\n"], { type: "text/csv" });
    const a = el("a", { href: URL.createObjectURL(blob), download: "vpb-" + lastTable.name + ".csv" });
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    setStatus("CSV of " + lastTable.body.length + " rows. If the download is blocked, data.csv sits next to this page.");
  }

  // ---------- rendering ----------

  function chartFor(id, height) {
    const node = $(id);
    node.style.height = height ? height + "px" : "";
    const theme = dark.matches ? "dark" : null;
    let chart = charts.get(id);
    if (chart && chart.vpbTheme !== theme) {
      chart.dispose();
      chart = null;
    }
    if (!chart) {
      chart = window.echarts.init(node, theme);
      chart.vpbTheme = theme;
      charts.set(id, chart);
      resizer.observe(node);
    }
    chart.resize();
    return chart;
  }

  function baseOption() {
    return {
      backgroundColor: "transparent",
      color: PALETTE,
      aria: { enabled: true },
      animation: false,
      textStyle: { fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif" },
      toolbox: { right: 8, feature: { saveAsImage: { title: "PNG", name: "vpb-chart" } } },
    };
  }

  function axisLabelFor(count) {
    return {
      interval: count <= 40 ? 0 : "auto",
      rotate: count > 6 ? 30 : 0,
      width: 170,
      overflow: "truncate",
      hideOverlap: true,
    };
  }

  function render() {
    for (const b of document.querySelectorAll(".tabs button")) {
      b.setAttribute("aria-selected", String(b.dataset.view === state.view));
    }
    $("view-chart").hidden = state.view !== "chart";
    $("view-compare").hidden = state.view !== "compare";
    for (const n of document.querySelectorAll("#controls label, #controls .row")) {
      const c = n.classList;
      n.hidden = (c.contains("not-compare") && state.view === "compare") ||
        (c.contains("not-table") && state.view === "table") ||
        (c.contains("compare-only") && state.view !== "compare") ||
        (c.contains("table-only") && state.view !== "table");
    }
    setStatus("");
    if (state.view === "chart") renderChart();
    else if (state.view === "compare") renderCompare();
    else renderRawTable();
  }

  function renderChart() {
    const m = state.m;
    const x = state.x;
    const g = state.g === NO_GROUP ? null : state.g;
    const data = filtered().filter((r) => isNum(r[m]) && value(r, x) !== null && value(r, x) !== undefined);
    const xKind = colType(x) === "number" ? "value" : colType(x) === "time" ? "time" : "category";
    const xKeys = xKind === "category" ? orderedKeys(x, data) : [];
    const groupKeys = g ? orderedKeys(g, data) : ["all"];

    const cells = new Map(groupKeys.map((k) => [k, new Map()]));
    const hashes = new Map();
    for (const r of data) {
      const gk = g ? key(value(r, g)) : "all";
      const xk = xKind === "category" ? key(value(r, x)) : value(r, x);
      const byX = cells.get(gk);
      if (!byX.has(xk)) byX.set(xk, []);
      byX.get(xk).push(r[m]);
      const cell = gk + "\u0000" + xk;
      if (!hashes.has(cell)) hashes.set(cell, new Set());
      hashes.get(cell).add(r.config_hash);
    }
    const mixed = [...hashes.values()].filter((s) => s.size > 1).length;
    const summary = new Map();
    for (const [gk, byX] of cells) {
      const s = new Map();
      for (const [xk, vals] of byX) s.set(xk, stats(vals));
      summary.set(gk, s);
    }

    const series = [];
    const dotPlot = xKind === "category" && state.pts === "1";
    groupKeys.forEach((gk, i) => {
      const color = PALETTE[i % PALETTE.length];
      const s = summary.get(gk);
      if (xKind === "category") {
        const agg = xKeys.map((xk, j) => (s.has(xk) ? [j, s.get(xk)[state.agg]] : null)).filter(Boolean);
        series.push(dotPlot
          ? { name: gk, type: "scatter", data: agg, symbol: "rect", symbolSize: [18, 3], itemStyle: { color }, z: 3 }
          : { name: gk, type: "bar", data: xKeys.map((xk) => (s.has(xk) ? s.get(xk)[state.agg] : null)), itemStyle: { color } });
      } else {
        const xs = [...s.keys()].sort(xKind === "time" ? (a, b) => String(a).localeCompare(String(b)) : (a, b) => a - b);
        series.push({
          name: gk, type: "line", data: xs.map((xk) => [xk, s.get(xk)[state.agg]]),
          symbolSize: 7, itemStyle: { color }, lineStyle: { color }, connectNulls: true,
        });
      }
      if (state.pts === "1") {
        const pts = [];
        for (const [xk, vals] of cells.get(gk)) {
          const xv = xKind === "category" ? xKeys.indexOf(xk) : xk;
          for (const v of vals) pts.push([xv, v]);
        }
        series.push({ name: gk, type: "scatter", data: pts, symbolSize: 5, itemStyle: { color, opacity: 0.55 }, z: 2 });
      }
    });

    const zoom = xKeys.length > 25 || xKind !== "category";
    const option = Object.assign(baseOption(), {
      legend: { type: "scroll", top: 0, left: 0, right: 60, show: groupKeys.length > 1 },
      grid: { top: groupKeys.length > 1 ? 56 : 34, left: 12, right: 24, bottom: zoom ? 40 : 12, containLabel: true },
      tooltip: {
        trigger: "axis",
        confine: true,
        axisPointer: { type: xKind === "category" ? "shadow" : "line" },
        formatter: (params) => tooltip(params, xKind, xKeys, summary, groupKeys),
      },
      xAxis: {
        type: xKind,
        // A category axis shows its values; its name would collide with rotated labels.
        name: xKind === "category" ? "" : label(x),
        nameLocation: "middle",
        nameGap: 28,
        data: xKind === "category" ? xKeys : undefined,
        axisLabel: xKind === "category" ? axisLabelFor(xKeys.length) : { hideOverlap: true },
        scale: xKind === "value",
      },
      yAxis: {
        type: state.logy === "1" ? "log" : "value",
        name: label(m),
        nameTextStyle: { align: "left" },
        scale: state.logy !== "1" && !(xKind === "category" && !dotPlot),
        axisLabel: { formatter: (v) => compact(v) },
      },
      dataZoom: zoom
        ? [{ type: "inside", xAxisIndex: 0 }, { type: "slider", xAxisIndex: 0, height: 16, bottom: 4 }]
        : [],
      series,
    });
    chartFor("chart", null).setOption(option, true);

    const header = [g ? label(g) : "group", label(x), state.agg + " " + label(m), "min", "max", "runs", "spread %"];
    const body = [];
    for (const gk of groupKeys) {
      const s = summary.get(gk);
      const xs = xKind === "category" ? xKeys.filter((k) => s.has(k)) : [...s.keys()].sort(natural);
      for (const xk of xs) {
        const st = s.get(xk);
        const a = st[state.agg];
        body.push([gk, xk, a, st.min, st.max, st.n, a ? ((st.max - st.min) / a) * 100 : null]);
      }
    }
    renderTable("summary", header, body, new Set([2, 3, 4, 5]), { 6: (v) => (isNum(v) ? v.toFixed(2) : "") });
    $("table-note").textContent = data.length + " runs after filters. Spread % = (max − min) / " + state.agg + "." +
      (mixed ? " " + mixed + " point(s) pool runs of different configs (config_hash); pick X axis \"config\" or add a filter." : "");
    if (mixed) setStatus("Some points pool different configs, see the note under the chart.");
  }

  function tooltip(params, xKind, xKeys, summary, groupKeys) {
    const list = Array.isArray(params) ? params : [params];
    if (!list.length) return "";
    const p = list[0];
    let xk;
    if (xKind === "category") {
      xk = typeof p.axisValue === "string" ? p.axisValue : xKeys[p.dataIndex];
    } else {
      xk = Array.isArray(p.data) ? p.data[0] : p.axisValue;
    }
    const lines = ["<b>" + esc(label(state.x)) + ": " + esc(xKind === "value" ? fmt(xk) : xk) + "</b>"];
    groupKeys.forEach((gk, i) => {
      const st = summary.get(gk).get(xk);
      if (!st) return;
      const dot = '<span style="display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px;background:' +
        PALETTE[i % PALETTE.length] + '"></span>';
      lines.push(dot + esc(gk) + ": <b>" + esc(fmt(st[state.agg])) + "</b> " +
        esc("(" + fmt(st.min) + "–" + fmt(st.max) + ", n=" + st.n + ")"));
    });
    return lines.join("<br>");
  }

  function renderCompare() {
    const m = state.m;
    const cc = state.cc;
    const band = Number(state.band);
    const a = state.ca;
    const b = state.cb;
    if (!cc || !a || !b) {
      $("cmp-abs-title").textContent = "Compare needs a column with at least two values.";
      $("cmp-delta-title").textContent = "";
      renderTable("compare", [], [], new Set());
      return;
    }
    const data = filtered(cc).filter((r) => isNum(r[m]) && [a, b].includes(key(r[cc])));
    const groups = new Map();
    for (const r of data) {
      const h = r.config_hash || r._config;
      if (!groups.has(h)) groups.set(h, { rows: [], a: [], b: [] });
      const e = groups.get(h);
      e.rows.push(r);
      (key(r[cc]) === a ? e.a : e.b).push(r[m]);
    }
    const labelCols = distinguishing(data, configCandidates.filter((c) => c !== cc));
    let onlyA = 0;
    let onlyB = 0;
    let items = [];
    for (const e of groups.values()) {
      if (!e.a.length) { onlyB++; continue; }
      if (!e.b.length) { onlyA++; continue; }
      const sa = stats(e.a);
      const sb = stats(e.b);
      const va = sa[state.agg];
      const vb = sb[state.agg];
      items.push({ label: configLabel(e.rows[0], labelCols), sa, sb, va, vb, delta: va ? ((vb - va) / va) * 100 : null });
    }
    if (state.sort === "delta") items.sort((p, q) => (p.delta ?? 0) - (q.delta ?? 0));
    else items.sort((p, q) => natural(p.label, q.label));

    const lower = lowerIsBetter(m);
    const worse = (d) => (lower ? d > band : d < -band);
    const better = (d) => (lower ? d < -band : d > band);
    const cats = items.map((it) => it.label);
    const height = Math.max(240, cats.length * 26 + 90);
    const wide = $("view-compare").clientWidth > 720;

    $("cmp-abs-title").textContent = state.agg + " " + label(m) + ": A = " + a + ", B = " + b;
    chartFor("cmp-abs", Math.max(260, cats.length * 38 + 90)).setOption(Object.assign(baseOption(), {
      legend: { top: 0, left: 0 },
      grid: { top: 34, left: 12, right: 28, bottom: 10, containLabel: true },
      tooltip: { trigger: "axis", confine: true, axisPointer: { type: "shadow" }, formatter: (ps) => compareTip(ps, items, a, b) },
      yAxis: { type: "category", data: cats, inverse: true, axisLabel: { width: wide ? 300 : 140, overflow: "truncate" } },
      xAxis: {
        type: state.logy === "1" ? "log" : "value",
        splitNumber: wide ? 6 : 3,
        axisLabel: { formatter: (v) => compact(v), hideOverlap: true },
      },
      series: [
        { name: "A " + a, type: "bar", data: items.map((it) => it.va), itemStyle: { color: PALETTE[0] } },
        { name: "B " + b, type: "bar", data: items.map((it) => it.vb), itemStyle: { color: PALETTE[1] } },
      ],
    }), true);

    $("cmp-delta-title").textContent = "Δ % = (B − A) / A, band ±" + band + "% shaded" + (lower ? " (lower is better)" : " (higher is better)");
    chartFor("cmp-delta", height).setOption(Object.assign(baseOption(), {
      grid: { top: 20, left: 12, right: wide ? 150 : 50, bottom: 10, containLabel: true },
      tooltip: { trigger: "axis", confine: true, axisPointer: { type: "shadow" }, formatter: (ps) => compareTip(ps, items, a, b) },
      yAxis: { type: "category", data: cats, inverse: true, axisLabel: { width: wide ? 300 : 140, overflow: "truncate" } },
      xAxis: { type: "value", axisLabel: { formatter: (v) => v + "%" } },
      series: [{
        type: "bar",
        name: "Δ %",
        data: items.map((it) => ({
          value: it.delta,
          itemStyle: { color: worse(it.delta) ? "#d1495b" : better(it.delta) ? "#2f8f5b" : "#8a94a6" },
        })),
        // Labels sit right of each bar (right of zero for a drop), clear of the axis labels.
        label: {
          show: true,
          position: "right",
          formatter: (p) => {
            const it = items[p.dataIndex];
            return pct(it.delta) + (wide ? "  " + compact(it.va) + " → " + compact(it.vb) : "");
          },
        },
        markArea: { silent: true, itemStyle: { color: "rgba(120,140,170,0.18)" }, data: [[{ xAxis: -band }, { xAxis: band }]] },
        markLine: { silent: true, symbol: "none", lineStyle: { type: "solid", opacity: 0.6 }, label: { show: false }, data: [{ xAxis: 0 }] },
      }],
    }), true);

    const header = ["config", "A " + a + " " + state.agg, "A min", "A max", "A runs",
      "B " + b + " " + state.agg, "B min", "B max", "B runs", "Δ %", "outside ±" + band + "%"];
    const body = items.map((it) => [it.label, it.va, it.sa.min, it.sa.max, it.sa.n, it.vb, it.sb.min, it.sb.max, it.sb.n,
      it.delta, worse(it.delta) ? "worse" : better(it.delta) ? "better" : ""]);
    renderTable("compare", header, body, new Set([1, 2, 3, 4, 5, 6, 7, 8]), { 9: pct });
    const notes = [items.length + " configs in both A and B, matched by config_hash."];
    if (onlyA || onlyB) notes.push(onlyA + " only in A, " + onlyB + " only in B (not shown).");
    $("table-note").textContent = notes.join(" ");
  }

  function compareTip(params, items, a, b) {
    const list = Array.isArray(params) ? params : [params];
    const it = items[list[0].dataIndex];
    if (!it) return "";
    return [
      "<b>" + esc(it.label) + "</b>",
      esc("A " + a + ": " + fmt(it.va) + " (" + fmt(it.sa.min) + "–" + fmt(it.sa.max) + ", n=" + it.sa.n + ")"),
      esc("B " + b + ": " + fmt(it.vb) + " (" + fmt(it.sb.min) + "–" + fmt(it.sb.max) + ", n=" + it.sb.n + ")"),
      "Δ <b>" + esc(pct(it.delta)) + "</b>",
    ].join("<br>");
  }

  function renderRawTable() {
    const data = filtered();
    const names = columns.map((c) => c.name);
    const shown = state.all === "1"
      ? names
      : names.filter((n) => n === state.m || n === "commit" || new Set(data.map((r) => key(r[n]))).size > 1);
    const sortCol = shown.includes(state.ts) ? state.ts : null;
    const sorted = sortCol ? data.slice().sort((p, q) => {
      const d = natural(key(p[sortCol]), key(q[sortCol]));
      return state.td === "desc" ? -d : d;
    }) : data;
    const numeric = new Set(shown.map((n, i) => (colType(n) === "number" ? i : -1)).filter((i) => i >= 0));
    renderTable("rows", shown, sorted.map((r) => shown.map((n) => r[n])), numeric, {}, true);
    $("table-note").textContent = data.length + " of " + rows.length + " rows after filters" +
      (data.length > MAX_TABLE_ROWS ? "; the first " + MAX_TABLE_ROWS + " are shown, the CSV has all" : "") +
      (state.all === "1" ? "." : ". Columns with one value are hidden; tick All columns to show them.");
  }

  function renderTable(name, header, body, numeric, formatters, sortable) {
    lastTable = { name, header, body };
    const table = $("table");
    table.replaceChildren();
    const thead = el("thead");
    const tr = el("tr");
    header.forEach((h, i) => {
      const th = el("th", { scope: "col", class: numeric.has(i) ? "num" : "" });
      if (sortable) {
        const btn = el("button", { type: "button" }, label(h) + (state.ts === h ? (state.td === "desc" ? " ▼" : " ▲") : ""));
        btn.addEventListener("click", () => {
          state.td = state.ts === h && state.td === "asc" ? "desc" : "asc";
          state.ts = h;
          update(false);
        });
        th.setAttribute("aria-sort", state.ts === h ? (state.td === "desc" ? "descending" : "ascending") : "none");
        th.appendChild(btn);
      } else {
        th.textContent = h;
      }
      tr.appendChild(th);
    });
    thead.appendChild(tr);
    const tbody = el("tbody");
    for (const r of body.slice(0, MAX_TABLE_ROWS)) {
      const row = el("tr");
      r.forEach((v, i) => {
        const f = (formatters || {})[i];
        const text = f ? f(v) : numeric.has(i) && isNum(v) ? fmt(v) : v === null || v === undefined ? "" : String(v);
        const cls = numeric.has(i) || f ? "num" : typeof v === "string" && /^(worse|better)$/.test(v) ? "flag" : "";
        row.appendChild(el("td", cls ? { class: cls } : {}, text));
      });
      tbody.appendChild(row);
    }
    table.appendChild(thead);
    table.appendChild(tbody);
  }

  function showError(message) {
    const box = el("div", { class: "error", role: "alert" });
    box.appendChild(el("p", {}, message));
    box.appendChild(el("p", { class: "muted" },
      "Serve this folder over HTTP (for example python3 -m http.server --bind 127.0.0.1 " +
      "--directory site) or build a single file with build_static_dashboard.py --inline."));
    $("controls").replaceWith(box);
    $("summary").textContent = "";
  }

  async function main() {
    if (!window.echarts) {
      showError("The chart library did not load.");
      return;
    }
    let data;
    try {
      data = await loadData();
    } catch (e) {
      showError("Could not load data.json: " + e.message);
      return;
    }
    if (!data || data.format !== "vpb-dashboard-data" || !Array.isArray(data.columns) || !Array.isArray(data.rows)) {
      showError("data.json is not a vpb-dashboard-data file.");
      return;
    }
    prepare(data);
    if (data.title) {
      $("title").textContent = data.title;
      document.title = data.title;
    }
    const commits = distinct.has("commit") ? distinct.get("commit").size : 0;
    $("summary").textContent = rows.length + " rows from " + data.files + " metrics file(s), " + commits +
      " commit(s). Data generated " + (data.generated_at || "?") + ".";
    if (!rows.length || !metricCols().length) {
      showError("The data has no rows with a numeric metric.");
      return;
    }
    state = readState(typeof data.view === "string" ? data.view : "");
    resolveState();
    buildControls();
    bindControls();
    writeState();
    render();
  }

  main();
})();
