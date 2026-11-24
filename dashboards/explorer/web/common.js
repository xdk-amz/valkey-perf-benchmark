/* Valkey benchmark results: code shared by every template (helpers, labels, data, cell parts).
   Plain JS, no library, no network beyond the page's own data files. Every number comes from vpb-result/1
   records (SCHEMA.md). All data is rendered as text, never as HTML. */
"use strict";

// ---------- small helpers ----------
const median = (xs) => { const s = [...xs].sort((a, b) => a - b); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
const ok = (r) => r.ok !== false && r.v != null;
const fnv = (s) => { let h = 0x811c9dc5; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 0x01000193); } return (h >>> 0).toString(16).padStart(8, "0"); };
const human = (n) => n >= 1e6 && n % 1e6 === 0 ? `${n / 1e6}M` : n >= 1e3 && n % 1e3 === 0 ? `${n / 1e3}k` : String(n);
const sha9 = (r) => r.build.sha.slice(0, 9);

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
const ROW_DIMS = [
  ["pipeline", "pipeline", (r) => r.pipeline],
  ["clients", "clients", (r) => r.clients],
  ["io_threads", "io-threads", (r) => r.io_threads ?? "not recorded"],
  ["workload", "", (r) => r.workload],
  ["server", "server", (r) => r.hardware.server || "not recorded"],
  ["cores", "cores", (r) => r.hardware.cores ?? "not recorded"],
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

// ---------- data ----------
const DATA = { records: [], metrics: {}, reproduce: {} };

async function load() {
  const inline = [...document.querySelectorAll("script.vpb-data")];
  let docs, spec;
  if (inline.length) {
    docs = inline.map((s) => JSON.parse(s.textContent));
    spec = JSON.parse(document.getElementById("vpb-fingerprint-fields").textContent);
  } else {
    const manifest = await (await fetch("data/manifest.json")).json();
    docs = await Promise.all(manifest.files.map(async (f) => (await fetch(`data/${f}`)).json()));
    spec = await (await fetch("fingerprint-fields.json")).json();
  }
  if (spec.schema !== "vpb-fingerprint-fields/1") throw new Error(`unknown fingerprint spec ${spec.schema}`);
  FP.fields = spec.fields;
  for (const d of docs) {
    if (d.schema !== "vpb-result/1") throw new Error(`unknown schema ${d.schema}`);
    Object.assign(DATA.metrics, d.metrics);
    Object.assign(DATA.reproduce, d.reproduce);
    DATA.records.push(...d.records);
  }
}

// scenario -> branch -> commit, each list ordered for the selectors
function catalogue() {
  const sc = new Map();
  for (const r of DATA.records) {
    if (!sc.has(r.family)) sc.set(r.family, new Map());
    const br = sc.get(r.family);
    if (!br.has(r.build.ref)) br.set(r.build.ref, new Map());
    br.get(r.build.ref).set(sha9(r), r.build);
  }
  return sc;
}
let CAT = null;
const scenarios = () => [...CAT.keys()].sort((a, b) => (a === "set commands" ? -1 : b === "set commands" ? 1 : a.localeCompare(b)));
// newest first; commit times carry their own zone offsets, so compare them as instants
const when = (b) => Date.parse(b.time || "") || 0;
const branches = (s) => [...CAT.get(s).keys()].sort((a, b) => when(latest(s, b)) - when(latest(s, a)) || a.localeCompare(b));
const commits = (s, b) => [...CAT.get(s).get(b).entries()].sort((x, y) => when(y[1]) - when(x[1])).map(([k]) => k);
const latest = (s, b) => CAT.get(s).get(b).get(commits(s, b)[0]);
function jump(e) {
  e.preventDefault();
  const id = e.currentTarget.getAttribute("href").slice(1);
  const el = document.getElementById(id);
  if (el) el.scrollIntoView();
}

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

function runsTable(cell, ms) {
  const len = Math.max(...ms.map((m) => cell.by[m].runs.length));
  if (!len) return h("p", { class: "note" }, "No per-run values in the source.");
  const aligned = ms.every((m) => [0, len].includes(cell.by[m].runs.length));
  const first = cell.by[ms.find((m) => cell.by[m].runs.length)];
  const rows = [];
  for (let i = 0; i < len; i++) {
    const notes = new Set();
    const tds = ms.map((m) => {
      const r = cell.by[m].runs[i];
      if (r && !ok(r)) notes.add(`invalid: ${r.why}`);
      return h("td", { class: "num" }, r ? (r.v == null ? "" : compact(r.v, DATA.metrics[m].unit)) : "");
    });
    rows.push(h("tr", {}, h("th", { scope: "row" }, i + 1), h("td", {}, first.runs[i].p || ""), tds, h("td", {}, [...notes].join("; "))));
  }
  return h("details", { open: len <= 12, style: "margin-top: 16px" }, h("summary", {}, `Runs (${len})`),
    h("div", { class: "scroll" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Run"), h("th", { scope: "col" }, "Pair"),
        ms.map((m) => h("th", { scope: "col", class: "num" }, mlabel(m))), h("th", { scope: "col" }, "Note"))),
      h("tbody", {}, rows))),
    null);
}

function reproBlock(block, rec) {
  if (!block) return null;
  const status = h("span", { class: "status", role: "status" });
  const cfg = block.config.inline ? JSON.stringify(block.config.inline, null, 2)
    : `${block.config.path}\noverrides: ${JSON.stringify(block.config.overrides)}`;
  const steps = block.steps.join("\n");
  const pre = h("pre", {}, steps || "No rerun command was published with this result.");
  const copy = h("button", { type: "button", class: "copy", onclick: async () => {
    try { await navigator.clipboard.writeText(steps); status.textContent = " copied"; }
    catch { const sel = getSelection(), rg = document.createRange(); rg.selectNodeContents(pre); sel.removeAllRanges(); sel.addRange(rg); status.textContent = " selected: press Ctrl+C or \u2318C"; }
  } }, "Copy the steps");
  return h("div", { class: "repro" },
    h("dl", { class: "kv" },
      h("dt", {}, "Harness"), h("dd", {}, h("a", { href: block.harness.repo }, block.harness.repo.replace("https://github.com/", "")), ` at ${block.harness.sha}`),
      h("dt", {}, "Build"), h("dd", {}, h("a", { href: block.build.repo }, block.build.repo.replace("https://github.com/", "")), ` at ${block.build.sha} (${rec.build.ref})`),
      h("dt", {}, "Setup"), h("dd", {}, block.setup),
      block.provenance && [h("dt", {}, "Source"), h("dd", {}, h("a", { href: block.provenance }, block.provenance))],
      block.gaps && block.gaps.length && [h("dt", {}, "Not recorded"), h("dd", {}, h("ul", {}, block.gaps.map((g) => h("li", {}, g))))]),
    h("div", { class: "note" }, "Steps"), pre, steps && copy, status,
    h("div", { class: "note" }, "Config"), h("pre", {}, cfg));
}

// ---------- benchmark fingerprint ----------
// Field spec: fingerprint-fields.json (vpb-fingerprint-fields/1). A field is recorded when its "source" path
// resolves to a value in the run's records. The run's value of a field is that value when every record agrees,
// else the sorted list of its distinct values. The fingerprint is sha256 over the canonical JSON of the recorded
// fields only (keys sorted, no whitespace, UTF-8).
const FP = { fields: [] };
const getPath = (r, path) => path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), r);
function canonical(v) {
  if (Array.isArray(v)) return `[${v.map(canonical).join(",")}]`;
  if (v && typeof v === "object") return `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${canonical(v[k])}`).join(",")}}`;
  return JSON.stringify(v);
}
function sha256(text) {
  const K = [0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01,
    0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
    0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b, 0xc24b8b70,
    0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
    0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2];
  const H = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
  const bytes = new TextEncoder().encode(text);
  const len = Math.ceil((bytes.length + 9) / 64) * 64;
  const m = new Uint8Array(len);
  m.set(bytes);
  m[bytes.length] = 0x80;
  const bits = bytes.length * 8;
  const dv = new DataView(m.buffer);
  dv.setUint32(len - 8, Math.floor(bits / 2 ** 32));
  dv.setUint32(len - 4, bits >>> 0);
  const w = new Uint32Array(64);
  const rotr = (x, n) => (x >>> n) | (x << (32 - n));
  for (let o = 0; o < len; o += 64) {
    for (let i = 0; i < 16; i++) w[i] = dv.getUint32(o + i * 4);
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, hh] = H;
    for (let i = 0; i < 64; i++) {
      const t1 = (hh + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) + ((e & f) ^ (~e & g)) + K[i] + w[i]) >>> 0;
      const t2 = ((rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) >>> 0;
      hh = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0;
    }
    [a, b, c, d, e, f, g, hh].forEach((x, i) => { H[i] = (H[i] + x) >>> 0; });
  }
  return H.map((x) => x.toString(16).padStart(8, "0")).join("");
}
function fingerprint(recs) {
  const fields = FP.fields.map((f) => {
    const vals = f.source ? [...new Set(recs.map((r) => getPath(r, f.source)).filter((v) => v != null && v !== "").map((v) => canonical(v)))] : [];
    const value = !vals.length ? undefined : vals.length === 1 ? JSON.parse(vals[0]) : vals.sort().map((v) => JSON.parse(v));
    const partial = f.source ? [] : (f.partial_source || []).map((p) => [p, [...new Set(recs.map((r) => getPath(r, p)).filter((v) => v != null && v !== ""))]]).filter(([, v]) => v.length);
    return { ...f, value, partial };
  });
  const recorded = Object.fromEntries(fields.filter((f) => f.value !== undefined).map((f) => [f.field, f.value]));
  const json = canonical(recorded);
  return { fields, recorded, json, hash: sha256(json), missing: fields.filter((f) => f.value === undefined).map((f) => f.field) };
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
function fieldsTable(fp, other) {
  const rows = fp.fields.filter((f) => f.value !== undefined);
  return h("table", { class: "fptable" },
    h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Field"), h("th", { scope: "col" }, "Value"))),
    h("tbody", {}, rows.map((f) => h("tr", {}, h("th", { scope: "row" }, fieldName(f.field)), h("td", {}, fieldValue(f.value))))));
}
function openDrawer(fp, title) {
  const d = document.getElementById("drawer");
  const missing = fp.fields.filter((f) => f.value === undefined);
  d.replaceChildren(
    h("div", { class: "drawer-head" }, h("h2", {}, title),
      h("button", { type: "button", class: "copy", onclick: closeDrawer, "aria-label": "Close" }, "Close")),
    h("p", {}, h("code", { class: "fp-full" }, fp.hash)),
    h("h3", {}, "Recorded"), fieldsTable(fp),
    h("h3", {}, "Not recorded"),
    h("table", { class: "fptable" }, h("tbody", {}, missing.map((f) => h("tr", {}, h("th", { scope: "row" }, fieldName(f.field)),
      h("td", {}, h("span", { class: "nr" }, "not recorded"),
        f.partial.length ? h("div", { class: "partial" }, f.partial.map(([p, vs]) => `${p}: ${vs.slice(0, 6).join(", ")}${vs.length > 6 ? ` and ${vs.length - 6} more` : ""}`).join(" · ")) : null))))),
    h("h3", {}, "Hashed JSON"), h("pre", { class: "mono raw fp-json" }, fp.json), copyButton(fp.json, "Copy JSON"));
  d.hidden = false;
  d.querySelector("button").focus();
}
function closeDrawer() { const d = document.getElementById("drawer"); if (d) d.hidden = true; }
addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
// fields that differ between two fingerprints, ignoring the ones that name the build
function fingerprintDiff(a, b, ignore) {
  return FP.fields.map((f) => f.field).filter((k) => !ignore.includes(k) && canonical(a.recorded[k]) !== canonical(b.recorded[k]));
}

// load the data, then hand over to the page's template
function start(init) {
  load().then(() => {
    CAT = catalogue();
    init();
  }).catch((e) => {
    document.getElementById("out").replaceChildren(h("p", {}, `The data did not load: ${e.message}`));
  });
}
