/* Valkey benchmark results: code shared by every template (helpers, labels, data, cell parts).
   Plain JS, no library, no network beyond the page's own data files. Every number comes from vpb-result
   run files (SCHEMA.md). All data is rendered as text, never as HTML. */
"use strict";

// ---------- small helpers ----------
const median = (xs) => { const s = [...xs].sort((a, b) => a - b); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
const ok = (r) => r.ok !== false && r.v != null;
const fnv = (s) => { let h = 0x811c9dc5; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 0x01000193); } return (h >>> 0).toString(16).padStart(8, "0"); };
const human = (n) => n >= 1e6 && n % 1e6 === 0 ? `${n / 1e6}M` : n >= 1e3 && n % 1e3 === 0 ? `${n / 1e3}k` : String(n);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat(Infinity)) if (k != null && k !== false) el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  return el;
}
function svg(tag, attrs, ...kids) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, v);
  for (const k of kids.flat(Infinity)) if (k != null && k !== false) el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  return el;
}

// ---------- labels ----------
const MIX_ORDER = ["none", "one", "10pct", "half", "all", "exp_one", "exp_10pct", "exp_half", "exp_all_but_1"];
const MIX_LABEL = { one: "1 member", "10pct": "10%", half: "50%", all: "100%",
  exp_one: "1 member", exp_10pct: "10%", exp_half: "50%", exp_all_but_1: "all but 1" };
// Sections come only from a scenario's own definition: the set scenario's set_matrix ttl_mixes split into no
// TTLs, TTL mixes and expired mixes ("expired": true). A scenario that defines none has one untitled section.
const SCENARIO_SECTIONS = {
  "set commands": [
    { id: "plain", title: "No TTLs", head: null, test: (m) => m === "none" },
    { id: "ttl", title: "With TTLs", head: "Members with a TTL", test: (m) => m !== "none" && !m.startsWith("exp_") },
    { id: "expired", title: "Expired but still stored", head: "Members expired, still stored", test: (m) => m.startsWith("exp_") },
  ],
};
const sectionsOf = (s) => SCENARIO_SECTIONS[s] || [{ id: "all", title: null, head: null, test: () => true }];
// The dimension a scenario's own definition declares as variable: a series takes one value of it (a slice).
// The set scenario varies the TTL mix (set_matrix ttl_mixes); a scenario that declares none has no slices.
const SCENARIO_SLICE = {
  "set commands": { dim: "mix", name: "TTL mix", order: MIX_ORDER,
    label: (m) => (m === "none" ? "none" : m.startsWith("exp_") ? `expired ${MIX_LABEL[m]}` : `TTL ${MIX_LABEL[m]}`) },
};
const sliceOf = (s) => SCENARIO_SLICE[s] || null;
const shapeKey = (r) => [r.shape.type, r.shape.encoding || "", r.shape.size ?? "", r.shape.size_unit].join(":");
function shapeLabel(k) {
  const [type, enc, size, unit] = k.split(":");
  const sz = size === "" ? "size not recorded" : unit === "members" ? `${human(+size)} members` : `${size} B`;
  return [type, enc, sz].filter(Boolean).join(" · ");
}
const shapeOrder = (a, b) => {
  const [ta, ea, sa] = a.split(":"), [tb, eb, sb] = b.split(":");
  return ta.localeCompare(tb) || ea.localeCompare(eb) || (+sa || 0) - (+sb || 0);
};
const mixOrder = (a, b) => { const i = MIX_ORDER.indexOf(a), j = MIX_ORDER.indexOf(b); return (i < 0 ? 99 : i) - (j < 0 ? 99 : j) || a.localeCompare(b); };
// throughput first, then latency, then memory; client ops/s only when the server-side rate is absent
const METRIC_ORDER = ["server_ops", "ops", "p50_ms", "p99_ms", "p999_ms", "bytes_per_key"];
// fields that can vary inside one run besides command, shape and mix; they extend the row label
// (io-threads, cores and hardware are run-level, in the run's fingerprint)
const ROW_DIMS = [
  ["pipeline", "pipeline", (c) => c.pipeline],
  ["clients", "clients", (c) => c.clients],
];

function compact(v, unit) {
  if (v == null) return "missing";
  if (unit === "ms") return v < 1 ? v.toFixed(3) : v < 10 ? v.toFixed(2) : v < 100 ? v.toFixed(1) : Math.round(v).toLocaleString("en-US");
  const a = Math.abs(v);
  if (a >= 1e6) return `${+(v / 1e6).toPrecision(3)}M`;
  if (a >= 1e3) return `${+(v / 1e3).toPrecision(3)}k`;
  return String(+v.toPrecision(3));
}
function exact(v, unit) {
  if (v == null) return "missing";
  if (unit === "ms") return `${v < 1 ? v.toFixed(3) : v.toFixed(2)} ms`;
  return `${Math.round(v).toLocaleString("en-US")} ${unit}`;
}

// metric labels on the page: the name and unit only
const METRIC_LABEL = { ops: "Throughput (ops/s)", server_ops: "Throughput (ops/s)", avg_ms: "Average latency (ms)",
  p50_ms: "p50 latency (ms)", p95_ms: "p95 latency (ms)", p99_ms: "p99 latency (ms)", p999_ms: "p99.9 latency (ms)",
  bytes_per_key: "Memory per key (B)" };
const mlabel = (m) => METRIC_LABEL[m] || `${DATA.metrics[m].name} (${DATA.metrics[m].unit})`;
// absolute values for rows and ticks: latency in µs below 1 ms, everything else 3 significant digits
function fmtv(v, unit) {
  if (unit === "ms") return v < 1 ? `${+(v * 1000).toPrecision(3)} µs` : `${+v.toPrecision(3)} ms`;
  return compact(v, unit);
}

// ---------- metric tabs (both pages) ----------
// A tab names a metric; the page shows that metric everywhere and keeps the tab in the URL (m=).
const TABS = [["tps", "TPS"], ["p50", "p50"], ["p99", "p99"], ["mem", "memory per key (B)"]];
const parseTab = (q) => (TABS.some(([t]) => t === q.get("m")) ? q.get("m") : "tps");
// the metric a tab shows in these run files: TPS is the server-side rate when any of them has it
function tabMetric(tab, docs) {
  if (tab === "p50") return "p50_ms";
  if (tab === "p99") return "p99_ms";
  if (tab === "mem") return "bytes_per_key";
  return docs.some((d) => d.cells.some((c) => c.metrics.server_ops)) ? "server_ops" : "ops";
}
function tabBar(cur, pick) {
  return h("div", { class: "tabs", role: "tablist", "aria-label": "Metric" }, TABS.map(([t, label]) =>
    h("button", { type: "button", role: "tab", class: "tab", "aria-selected": t === cur ? "true" : "false",
      onclick: () => pick(t) }, label)));
}

// ---------- data ----------
// The site holds one vpb-result file per run (SCHEMA.md) and data/manifest.json listing them; a page loads only
// the runs it shows. With --inline every run is in the page.
const DATA = { metrics: {}, runs: [], files: new Map() };

function manifestEntry(doc) {
  const run = doc.run, fp = run.fingerprint;
  return { id: run.id, file: `${run.id}.json`, data_set: run.data_set, families: [...new Set(doc.cells.map((c) => c.family))].sort(),
    mixes: [...new Set(doc.cells.map((c) => c.mix))].sort(), build: run.build, started: run.started, cells: doc.cells.length,
    fingerprint: { hash: fp.hash, io_threads: fp.io_threads, cores: fp.cores, server: fp.server.instance_type,
      client: fp.client.instance_type, network_path: fp.network_path } };
}

async function load() {
  const inline = [...document.querySelectorAll("script.vpb-run")];
  let spec;
  if (inline.length) {
    for (const s of inline) {
      const d = JSON.parse(s.textContent);
      DATA.files.set(d.run.id, Promise.resolve(d));
      DATA.runs.push(manifestEntry(d));
    }
    DATA.metrics = JSON.parse(document.getElementById("vpb-metrics").textContent).metrics;
    spec = JSON.parse(document.getElementById("vpb-fingerprint-fields").textContent);
  } else {
    const [manifest, metrics] = await Promise.all([fetch("data/manifest.json"), fetch("metrics.json")].map(async (p) => (await p).json()));
    DATA.runs = manifest.runs;
    DATA.metrics = metrics.metrics;
    spec = await (await fetch("fingerprint-fields.json")).json();
  }
  if (spec.schema !== "vpb-fingerprint-fields/3") throw new Error(`unknown fingerprint spec ${spec.schema}`);
  FP.fields = spec.fields;
}

// one run file, fetched once
function getRun(id) {
  if (!DATA.files.has(id)) {
    const e = DATA.runs.find((r) => r.id === id);
    DATA.files.set(id, fetch(`data/${e.file}`).then((r) => r.json()).then((d) => {
      if (d.schema !== "vpb-result") throw new Error(`${e.file}: unknown schema ${d.schema}`);
      return d;
    }));
  }
  return DATA.files.get(id);
}

// newest first; times carry their own zone offsets, so compare them as instants
const when = (t) => Date.parse(t || "") || 0;
const newest = (a, b) => when(b.started) - when(a.started) || when(b.build.time) - when(a.build.time) || a.id.localeCompare(b.id);
const families = () => [...new Set(DATA.runs.flatMap((r) => r.families))].sort((a, b) => (a === "set commands" ? -1 : b === "set commands" ? 1 : a.localeCompare(b)));
const runsOf = (f) => DATA.runs.filter((r) => !f || r.families.includes(f)).sort(newest);
const entry = (id) => DATA.runs.find((r) => r.id === id);
// a run's label: branch, commit, start, plus the setup facts that tell it apart from runs of the same commit
function runLabel(e) {
  const base = `${e.build.ref} · ${e.build.sha.slice(0, 9)}${e.started ? ` · ${e.started.slice(0, 10)}` : ""}`;
  const twins = DATA.runs.filter((r) => r.build.sha === e.build.sha && r.id !== e.id && r.families.join() === e.families.join());
  if (!twins.length) return base;
  const keys = [["io_threads", "io-threads"], ["cores", "cores"], ["server", "server"], ["client", "client"], ["network_path", ""]];
  const diff = keys.filter(([k]) => twins.some((t) => t.fingerprint[k] !== e.fingerprint[k])).map(([k, n]) => `${n} ${e.fingerprint[k] ?? "not recorded"}`.trim());
  return `${base} · ${diff.length ? diff.join(", ") : e.fingerprint.hash.slice(0, 8)}`;
}

// The view of one cell: per metric its summary and the reps' values, the shape the panels and verdicts read.
function cellView(cell) {
  const by = {};
  const names = new Set([...Object.keys(cell.metrics), ...cell.runs.flatMap((r) => Object.keys(r).filter((k) => !REP_KEYS.has(k)))]);
  for (const m of names) {
    const s = cell.metrics[m];
    const runs = cell.runs.filter((r) => m in r).map((r) => ({ v: r[m], p: r.pair, t: r.t, ok: r.ok, why: r.why, rep: r.rep }));
    by[m] = { metric: m, value: s ? s.median : null, n: runs.filter((x) => ok(x)).length,
      spread: s ? { min: s.min, max: s.max, cv: s.cv } : null, runs, missing: s ? null : cell.missing || "not measured" };
  }
  return by;
}
const REP_KEYS = new Set(["rep", "pair", "t", "ok", "why"]);

// ---------- panels ----------
// Set commands in the scenario's own order, with their written names.
const CMD_NAMES = {
  sadd_existing: "SADD, existing member", sadd_new: "SADD, new member", srem_sadd: "SREM + SADD pair",
  sismember: "SISMEMBER", sismember_expired: "SISMEMBER, expired member", smismember: "SMISMEMBER, 4 members",
  scard: "SCARD", smembers: "SMEMBERS", srandmember: "SRANDMEMBER", spop: "SPOP", smove: "SMOVE, both ways",
  sinter: "SINTER", sintercard: "SINTERCARD", sinterstore: "SINTERSTORE", sunion: "SUNION", sunionstore: "SUNIONSTORE",
  sdiff: "SDIFF", sdiffstore: "SDIFFSTORE", sscan: "SSCAN COUNT 10", saddex_new: "SADDEX, new member",
  saddex_existing: "SADDEX, existing member", sexpire: "SEXPIRE", spexpire: "SPEXPIRE", sexpireat: "SEXPIREAT",
  spexpireat: "SPEXPIREAT", spersist_sexpire: "SPERSIST + SEXPIRE pair", sttl: "STTL", spttl: "SPTTL",
  sexpiretime: "SEXPIRETIME", spexpiretime: "SPEXPIRETIME",
};
const CMD_ORDER = Object.keys(CMD_NAMES);
const rowCommand = (row) => row.split(" · ")[0];
const panelName = (row) => {
  const [cmd, ...rest] = row.split(" · ");
  return [CMD_NAMES[cmd] || cmd.toUpperCase(), ...rest].join(" · ");
};
function panelOrder(a, b) {
  const i = CMD_ORDER.indexOf(rowCommand(a)), j = CMD_ORDER.indexOf(rowCommand(b));
  return (i < 0 ? 999 : i) - (j < 0 ? 999 : j) || a.localeCompare(b, "en", { numeric: true });
}
// Set shapes in the report's order; other scenarios fall back to type, encoding, size.
const SHAPE_ORDER = ["set:listpack:16:members", "set:listpack:128:members", "set:intset:512:members", "set:hashtable:100000:members"];
const SHAPE_SHORT = { "set:listpack:16:members": "listpack 16", "set:listpack:128:members": "listpack 128",
  "set:intset:512:members": "intset 512", "set:hashtable:100000:members": "hashtable 100k" };
const shapeShort = (k) => SHAPE_SHORT[k] || shapeLabel(k);
function panelShapeOrder(a, b) {
  const i = SHAPE_ORDER.indexOf(a), j = SHAPE_ORDER.indexOf(b);
  if (i >= 0 || j >= 0) return (i < 0 ? 99 : i) - (j < 0 ? 99 : j);
  return shapeOrder(a, b);
}
const mixShort = (m) => (m === "all" ? "all" : MIX_LABEL[m] || m);

// ratio steps for ticks on the symmetric log scale; each tick is labelled with its absolute value
const RATIOS = [1.05, 1.1, 1.25, 1.5, 2, 3, 5, 10, 30, 100, 300, 1000, 3000, 1e4, 3e4, 1e5];

function words(metric) {
  const u = DATA.metrics[metric].unit;
  if (u === "ops/s") return { worse: "slower", better: "faster" };
  if (u === "ms") return { worse: "higher latency", better: "lower latency" };
  return { worse: "more memory", better: "less memory" };
}

function strip(r) {
  const vals = r.runs.filter((x) => x.v != null);
  if (!vals.length) return null;
  const md = DATA.metrics[r.metric];
  let lo = Math.min(...vals.map((x) => x.v)), hi = Math.max(...vals.map((x) => x.v));
  const pad = (hi - lo) * 0.08 || Math.abs(hi) * 0.02 || 1;
  lo -= pad; hi += pad;
  const W = 760, L = 20, R = 20, H = 74, y = 24;
  const x = (v) => L + (v - lo) / (hi - lo) * (W - L - R);
  const kids = vals.map((v, j) => svg("circle", { class: ok(v) ? "dot" : "dot bad", cx: x(v.v), cy: y + ((j % 3) - 1) * 5, r: 3.4 },
    svg("title", {}, `${exact(v.v, md.unit)}${v.p ? " · pair " + v.p : ""}${ok(v) ? "" : " · invalid: " + v.why}`)));
  if (r.value != null) kids.push(svg("line", { class: "med", x1: x(r.value), x2: x(r.value), y1: y - 14, y2: y + 14 }, svg("title", {}, `median ${exact(r.value, md.unit)}`)));
  kids.push(svg("line", { class: "axis", x1: L, x2: W - R, y1: y + 20, y2: y + 20 }));
  for (const t of niceTicks(lo, hi, 6)) {
    kids.push(svg("line", { class: "axis", x1: x(t), x2: x(t), y1: y + 20, y2: y + 24 }));
    kids.push(svg("text", { class: "tick", x: x(t), y: y + 36, "text-anchor": "middle" }, compact(t, md.unit)));
  }
  kids.push(svg("text", { class: "tick", x: W - R, y: H - 2, "text-anchor": "end" }, mlabel(r.metric)));
  return h("figure", { style: "margin: 16px 0 0" },
    svg("svg", { class: "strip", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${mlabel(r.metric)} of each run` }, kids),
    h("figcaption", { class: "note" }, "● run  ○ invalid run  | median"));
}
function niceTicks(lo, hi, n) {
  const span = hi - lo, mag = 10 ** Math.floor(Math.log10(span / n));
  const step = [1, 2, 2.5, 5, 10].map((s) => s * mag).find((s) => span / s <= n) || mag * 10;
  const out = [];
  for (let t = Math.ceil(lo / step) * step; t <= hi; t += step) out.push(+t.toPrecision(12));
  return out;
}

// every rep of a cell with each metric's value
function runsTable(cell, ms) {
  const reps = cell.runs;
  if (!reps.length) return h("p", { class: "note" }, "No reps.");
  const rows = reps.map((r) => h("tr", {}, h("th", { scope: "row" }, r.rep), h("td", {}, r.pair || ""),
    ms.map((m) => h("td", { class: "num" }, r[m] == null ? "" : compact(r[m], DATA.metrics[m].unit))),
    h("td", {}, r.ok ? "" : `invalid: ${r.why}`)));
  return h("details", { open: reps.length <= 12, style: "margin-top: 16px" }, h("summary", {}, `Reps (${reps.length})`),
    h("div", { class: "scroll" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Rep"), h("th", { scope: "col" }, "Pair"),
        ms.map((m) => h("th", { scope: "col", class: "num" }, mlabel(m))), h("th", { scope: "col" }, "Note"))),
      h("tbody", {}, rows))));
}

// how to rerun a run: its harness, build and config, and the one command
function reproBlock(run) {
  const status = h("span", { class: "status", role: "status" });
  const hs = run.harness, b = run.build;
  const cmd = run.reproduce || "";
  const pre = h("pre", {}, cmd || "No rerun command was recorded with this run.");
  const copy = h("button", { type: "button", class: "copy", onclick: async () => {
    try { await navigator.clipboard.writeText(cmd); status.textContent = " copied"; }
    catch { const sel = getSelection(), rg = document.createRange(); rg.selectNodeContents(pre); sel.removeAllRanges(); sel.addRange(rg); status.textContent = " selected: press Ctrl+C or \u2318C"; }
  } }, "Copy the command");
  return h("div", { class: "repro" },
    h("dl", { class: "kv" },
      h("dt", {}, "Harness"), h("dd", {}, h("a", { href: `https://github.com/${hs.repo}` }, hs.repo), ` at ${hs.sha || "not recorded"}`),
      h("dt", {}, "Build"), h("dd", {}, h("a", { href: `https://github.com/${b.repo}` }, b.repo), ` at ${b.sha} (${b.ref})`),
      h("dt", {}, "Config"), h("dd", {}, hs.config || "inline, below")),
    h("div", { class: "note" }, "Command"), pre, cmd && copy, status,
    hs.config_inline ? [h("div", { class: "note" }, "Config"), h("pre", {}, JSON.stringify(hs.config_inline, null, 2))] : null);
}

// ---------- benchmark fingerprint ----------
// Field spec: fingerprint-fields.json (vpb-fingerprint-fields/3, fingerprint v5): every field and its path in the
// run. The run file carries the hash (sha256 over the canonical JSON of {field: value}); a null field is not
// recorded.
const FP = { fields: [] };
const getPath = (r, path) => path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), r);
function canonical(v) {
  if (Array.isArray(v)) return `[${v.map(canonical).join(",")}]`;
  if (v && typeof v === "object") return `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${canonical(v[k])}`).join(",")}}`;
  return JSON.stringify(v);
}
function fingerprint(run) {
  const fields = FP.fields.map((f) => ({ ...f, value: getPath(run, f.path) ?? undefined }));
  return { fields, hash: run.fingerprint.hash, values: Object.fromEntries(fields.map((f) => [f.field, f.value])) };
}
const fieldName = (f) => f.replace(/_/g, " ").replace(/\bcpu\b/g, "CPU").replace(/\bos\b/, "OS").replace(/\bnic\b/, "NIC");
function fieldValue(v) {
  if (v === undefined) return h("span", { class: "nr" }, "not recorded");
  if (!Array.isArray(v)) return h("span", { class: "mono" }, typeof v === "string" ? v : canonical(v));
  if (v.length <= 6) return h("span", { class: "mono" }, v.map((x) => (typeof x === "string" ? x : canonical(x))).join(", "));
  return h("details", {}, h("summary", {}, `${v.length} values`), h("div", { class: "mono" }, v.map((x) => (typeof x === "string" ? x : canonical(x))).join(", ")));
}
function copyButton(text, label) {
  const status = h("span", { class: "status", role: "status" });
  return [h("button", { type: "button", class: "copy", onclick: async () => {
    try { await navigator.clipboard.writeText(text); status.textContent = " copied"; } catch { status.textContent = ` ${text}`; }
  } }, label), status];
}
// one line: name, short hash (full on hover), copy, and the button that opens the fields drawer
function fingerprintLine(fp, name) {
  return h("div", { class: "fp" },
    h("span", { class: "fp-name" }, name || "Benchmark Fingerprint"),
    h("code", { class: "fp-hash", title: fp.hash }, fp.hash.slice(0, 12)),
    copyButton(fp.hash, "Copy"),
    h("button", { type: "button", class: "copy", onclick: () => openDrawer(fp, name || "Benchmark Fingerprint") }, "Fields"));
}
function fieldsTable(rows) {
  return h("table", { class: "fptable" },
    h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Field"), h("th", { scope: "col" }, "Value"))),
    h("tbody", {}, rows.map((f) => h("tr", {}, h("th", { scope: "row" }, fieldName(f.field)), h("td", {}, fieldValue(f.value))))));
}
function openDrawer(fp, title) {
  const d = document.getElementById("drawer");
  const groups = [...new Set(fp.fields.map((f) => f.group))];
  const missing = fp.fields.filter((f) => f.value === undefined);
  // replaceChildren takes nodes, not arrays of them: flatten the groups first
  d.replaceChildren(...[
    h("div", { class: "drawer-head" }, h("h2", {}, title),
      h("button", { type: "button", class: "copy", onclick: closeDrawer, "aria-label": "Close" }, "Close")),
    h("p", {}, h("code", { class: "fp-full" }, fp.hash)),
    groups.map((g) => {
      const rows = fp.fields.filter((f) => f.group === g && f.value !== undefined);
      return rows.length ? [h("h3", {}, g[0].toUpperCase() + g.slice(1)), fieldsTable(rows)] : null;
    }),
    missing.length ? [h("h3", {}, "Not recorded"), h("p", { class: "note" }, missing.map((f) => fieldName(f.field)).join(", "))] : null,
    h("h3", {}, "Hashed JSON"), h("pre", { class: "mono raw fp-json" }, canonical(Object.fromEntries(fp.fields.map((f) => [f.field, f.value ?? null]))))].flat(Infinity).filter(Boolean));
  d.hidden = false;
  d.querySelector("button").focus();
}
function closeDrawer() { const d = document.getElementById("drawer"); if (d) d.hidden = true; }
addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
// fields that differ between two fingerprints, ignoring the ones that name the build
function fingerprintDiff(a, b, ignore) {
  return FP.fields.map((f) => f.field).filter((k) => !ignore.includes(k) && canonical(a.values[k] ?? null) !== canonical(b.values[k] ?? null));
}

// A run's setup: its fingerprint without the build fields. Runs of two builds on one setup compare cell by cell.
const buildFields = () => FP.fields.filter((f) => f.group === "build").map((f) => f.field);
function setupKey(run) {
  const v = fingerprint(run).values, skip = buildFields();
  return canonical(Object.fromEntries(FP.fields.map((f) => f.field).filter((k) => !skip.includes(k)).map((k) => [k, v[k] ?? null])));
}
const fieldLabel = (k, v) => `${k === "io_threads" ? "io-threads" : fieldName(k)} ${v == null ? "not recorded" : typeof v === "object" ? canonical(v) : v}`;
// the setup fields that tell apart runs of one commit holding the same cell (CI at io-threads 1 and 9): a cell's
// series or row carries their values, so those runs are never averaged
function splitFields(docs, family) {
  const vals = docs.map((d) => fingerprint(d.run).values);
  const ids = docs.map((d) => new Set(d.cells.filter((c) => !family || c.family === family).map((c) => c.id)));
  const skip = buildFields(), out = new Set();
  docs.forEach((d, i) => docs.forEach((e, j) => {
    if (j <= i || d.run.build.sha !== e.run.build.sha || ![...ids[i]].some((x) => ids[j].has(x))) return;
    for (const k of Object.keys(vals[i])) if (!skip.includes(k) && canonical(vals[i][k] ?? null) !== canonical(vals[j][k] ?? null)) out.add(k);
  }));
  return FP.fields.map((f) => f.field).filter((k) => out.has(k));
}
const setupLabel = (run, split) => { const v = fingerprint(run).values; return split.map((k) => fieldLabel(k, v[k])).join(", "); };

// load the run list, then hand over to the page's template
function start(init) {
  load().then(init).catch((e) => {
    document.getElementById("out").replaceChildren(h("p", {}, `The data did not load: ${e.message}`));
  });
}
