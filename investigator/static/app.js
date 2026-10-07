"use strict";
// Lucide icon geometry (ISC licence), drawn inline: no icon font, no CDN.
const ICONS = {
  search: [["circle", {cx: 11, cy: 11, r: 8}], "m21 21-4.3-4.3"],
  plus: ["M5 12h14", "M12 5v14"],
  menu: ["M4 6h16", "M4 12h16", "M4 18h16"],
  arrowUp: ["m5 12 7-7 7 7", "M12 19V5"],
  check: ["M20 6 9 17l-5-5"],
  x: ["M18 6 6 18", "m6 6 12 12"],
  circleCheck: [["circle", {cx: 12, cy: 12, r: 10}], "m9 12 2 2 4-4"],
  alert: ["m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3", "M12 9v4", "M12 17h.01"],
  pause: [["circle", {cx: 12, cy: 12, r: 10}], "M10 15V9", "M14 15V9"],
  circleX: [["circle", {cx: 12, cy: 12, r: 10}], "m15 9-6 6", "m9 9 6 6"],
  shield: ["M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z", "m9 12 2 2 4-4"],
  chevron: ["m6 9 6 6 6-6"],
  copy: [["rect", {x: 8, y: 8, width: 14, height: 14, rx: 2}], "M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"],
  replay: ["M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8", "M3 3v5h5"],
  download: ["M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4", "m7 10 5 5 5-5", "M12 15V3"],
  database: [["ellipse", {cx: 12, cy: 5, rx: 9, ry: 3}], "M3 5V19A9 3 0 0 0 21 19V5", "M3 12A9 3 0 0 0 21 12"],
  trend: ["M22 7 13.5 15.5 8.5 10.5 2 17", "M16 7h6v6"],
  users: ["M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2", ["circle", {cx: 9, cy: 7, r: 4}], "M22 21v-2a4 4 0 0 0-3-3.87", "M16 3.13a4 4 0 0 1 0 7.75"],
  lineChart: ["M3 3v16a2 2 0 0 0 2 2h16", "m19 9-5 5-4-4-3 3"],
  barChart: ["M3 3v16a2 2 0 0 0 2 2h16", "M18 17V9", "M13 17V5", "M8 17v-3"],
  table: [["rect", {x: 3, y: 3, width: 18, height: 18, rx: 2}], "M3 9h18", "M3 15h18", "M12 3v18"],
  message: ["M7.9 20A9 9 0 1 0 4 16.1L2 22Z"],
  ban: [["circle", {cx: 12, cy: 12, r: 10}], "m4.9 4.9 14.2 14.2"],
  logOut: ["M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4", "m16 17 5-5-5-5", "M21 12H9"],
};
const SUGGESTIONS = [
  {icon: "trend", title: "Explain a change", text: "Why did net sales change between August and September 2026?"},
  {icon: "users", title: "Find what drove it", text: "Which customer segment drove the change in refunds from August to September 2026?"},
  {icon: "lineChart", title: "Visualize by month", text: "Visualize gross sales, refunds and net sales by month"},
  {icon: "barChart", title: "Compare segments", text: "Plot September 2026 net sales by customer segment"}];
const STATUS = {
  verified: {label: "Verified", icon: "circleCheck", note: "Every figure is in its query's result and equals the business rules."},
  unverified: {label: "Unverified", icon: "alert", note: "Some checks failed. The problems are listed here; do not rely on those figures."},
  incomplete: {label: "Incomplete", icon: "pause", note: "A limit stopped the model before it finished. What it found so far is shown below."},
  failed: {label: "Failed", icon: "circleX", note: "This run failed. Any queries that ran are shown below."},
  answered: {label: "Answered", icon: "message", note: "No new figures. Any amount it repeats was checked earlier in this chat."},
  blocked: {label: "Out of scope", icon: "ban", note: "The guard stopped this message before the model saw it."}};
const QUIET = ["verified", "answered", "blocked"];  // no warning banner
const FIG = {query_error: "the query computed a wrong value", unsupported: "not in the cited result", wrong: "wrong and not in the cited result"};
const METRICS = ["gross", "refunds", "net"];
const $ = id => document.getElementById(id);
// thread = {root, runs: [full run records]}. nav changes on every navigation, so a late reply cannot draw into another view.
let config = {}, runs = [], thread = null, pending = null, notice = null, nav = 0, key = null;
// The app key lives in this browser only; a header, not a cookie, so other sites cannot use it.
try { key = localStorage.getItem("investigator-key"); } catch { /* storage blocked: sign in each visit */ }
function setKey(k) {
  key = k;
  try { if (k) localStorage.setItem("investigator-key", k); else localStorage.removeItem("investigator-key"); } catch { /* see above */ }
}

// Text only: model output never becomes HTML.
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : String(kid));
  return el;
}
function s(tag, attrs = {}, text) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (text != null) el.textContent = text;
  return el;
}
function icon(name, cls = "") {
  const svg = s("svg", {class: `i ${cls}`, viewBox: "0 0 24 24", "aria-hidden": "true"});
  for (const part of ICONS[name]) svg.append(typeof part === "string" ? s("path", {d: part}) : s(part[0], part[1]));
  return svg;
}
// Mirrors report.fmt_cents / report.prose: integer cents in, dollars out.
const fmtCents = c => (c < 0 ? "-" : "") + "$" + Math.floor(Math.abs(c) / 100).toLocaleString("en-US") + "." +
  String(Math.abs(c) % 100).padStart(2, "0");
const prose = t => t.replace(/(-?\d[\d,]*)(?:\s*|-)cents?\b/g, (_, n) => fmtCents(parseInt(n.replace(/,/g, ""), 10)));
const month = (y, m) => new Date(Date.UTC(y, m - 1, 1)).toLocaleString("en", {month: "short", year: "numeric", timeZone: "UTC"});
function period(a, b) {
  const [ya, ma] = a.split("-").map(Number), [yb, mb] = b.split("-").map(Number);
  if (a.endsWith("-01") && b.endsWith("-01") && yb * 12 + mb - (ya * 12 + ma) === 1) return month(ya, ma);
  return `${a} to ${b} (excl.)`;
}
const figName = x => `${x.metric} ${period(x.period_start, x.period_end_exclusive)}${x.segment ? " " + x.segment : ""}`;
const when = iso => new Date(iso).toLocaleString("en", {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23"});
const badge = status => h("span", {class: `badge s-${status}`, title: STATUS[status]?.note || ""}, icon(STATUS[status]?.icon || "alert", "sm"), STATUS[status]?.label || status);

const headers = body => ({...(key ? {Authorization: `Bearer ${key}`} : {}), ...(body ? {"Content-Type": "application/json"} : {})});
function signedOut(r) {  // a missing or revoked key: back to the sign-in screen
  if (r.status !== 401) return;
  setKey(null); runs = []; thread = null; pending = null; render();
  throw new Error("Your app key was not accepted. Sign in again.");
}
async function api(path, body) {
  const r = await fetch(path, body ? {method: "POST", headers: headers(true), body: JSON.stringify(body)} : {headers: headers()});
  signedOut(r);
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}
// /api/ask answers with one JSON object per line: {"progress": ...} after each graph step, then {"run": ...}.
async function askStream(body, onProgress) {
  const r = await fetch("/api/ask", {method: "POST", headers: headers(true), body: JSON.stringify(body)});
  signedOut(r);
  if (!r.ok) {
    let message = r.statusText;
    try { message = (await r.json()).error || message; } catch { /* not JSON */ }
    throw new Error(message);
  }
  const reader = r.body.getReader(), decoder = new TextDecoder();
  let buffer = "", run = null;
  for (;;) {
    const {value, done} = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
    let i;
    while ((i = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, i).trim();
      buffer = buffer.slice(i + 1);
      if (!line) continue;
      const msg = JSON.parse(line);
      if (msg.run) run = msg.run; else onProgress(msg.progress);
    }
    if (done) break;
  }
  if (!run) throw new Error("The connection closed before the answer arrived.");
  return run;
}

// ---- threads: a question plus its follow-ups, found through parent_run_id
function rootOf(id) {
  const byId = new Map(runs.map(r => [r.run_id, r]));
  for (let r = byId.get(id); r?.parent_run_id && byId.has(r.parent_run_id);) { id = r.parent_run_id; r = byId.get(id); }
  return id;
}
function threads() {
  const groups = new Map();
  for (const r of runs) {
    const root = rootOf(r.run_id);
    groups.set(root, [...(groups.get(root) || []), r]);
  }
  return [...groups.entries()].map(([root, items]) => ({root, items: items.sort((a, b) => a.created_at.localeCompare(b.created_at))}))
    .sort((a, b) => b.items.at(-1).created_at.localeCompare(a.items.at(-1).created_at));
}
function renderList() {
  const list = threads();
  $("list").replaceChildren(...(list.length ? list.map(t => {
    const first = t.items[0], last = t.items.at(-1), st = STATUS[last.status] || {};
    const more = t.items.length > 1 ? ` · ${t.items.length - 1} follow-up${t.items.length > 2 ? "s" : ""}` : "";
    return h("button", {type: "button", "aria-current": String(thread?.root === t.root), title: first.question, onclick: () => go(t.root)},
      h("span", {class: "q"}, first.question),
      h("span", {class: "meta"}, h("span", {class: `st s-${last.status}`}, icon(st.icon || "alert", "sm")),
        `${st.label || last.status}${more} · ${when(last.created_at)}`));
  }) : [h("p", {class: "empty-list"}, "Your chats will appear here.")]));
}

// ---- one assistant message per run
const queryButton = (run, qid) => h("button", {type: "button", class: "chip", title: `Show ${qid}: its SQL and result`,
  onclick: () => showQuery(run.run_id, qid)}, icon("database", "sm"), qid);

const resultTable = (columns, rows, fmt = v => v) => rows.length
  ? h("div", {class: "scrollx"}, h("table", {}, h("thead", {}, h("tr", {}, columns.map(c => h("th", {}, c)))),
      h("tbody", {}, rows.map(row => h("tr", {}, row.map((v, i) => h("td", {class: typeof v === "number" ? "num" : ""}, v == null ? "NULL" : fmt(v, columns[i]))))))))
  : h("p", {class: "caption", style: "margin:0"}, "Empty result: no rows matched.");

function figureTable(run) {
  const seen = new Map();
  for (const f of run.verification?.figures || []) {
    if (f.status !== "ok") continue;  // only figures that passed both checks
    const key = `${f.period_start}|${f.period_end_exclusive}|${f.segment ?? ""}`;
    if (!seen.has(key)) seen.set(key, {label: period(f.period_start, f.period_end_exclusive), segment: f.segment || "All customers", start: f.period_start});
    seen.get(key)[f.metric] = f.value_cents;
  }
  const rows = [...seen.values()].sort((a, b) => a.segment.localeCompare(b.segment) || a.start.localeCompare(b.start));
  if (!rows.length) return null;
  const cols = METRICS.filter(m => rows.some(r => r[m] != null));
  return h("section", {}, h("h3", {}, "Checked figures"),
    h("div", {class: "scrollx"}, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Period"), h("th", {}, "Customers"), cols.map(m => h("th", {class: "num"}, m[0].toUpperCase() + m.slice(1))))),
      h("tbody", {}, rows.map(r => h("tr", {}, h("td", {}, r.label), h("td", {}, r.segment),
        cols.map(m => h("td", {class: "num"}, r[m] == null ? "—" : fmtCents(r[m])))))))),
    h("p", {class: "caption"}, "Each value is a cell of its query's result and equals the business rules computed in code."));
}

function queriesView(run) {
  if (!run.queries.length) return h("p", {class: "caption", style: "margin:0"}, "No queries ran.");
  return h("section", {}, h("h3", {}, `Queries · ${run.queries.length} of ${run.config.max_queries} attempts (errors count)`),
    run.queries.map(q => h("details", {class: "query" + (q.error ? " err" : ""), id: `q-${run.run_id}-${q.id}`},
      h("summary", {}, h("span", {class: "qid"}, q.id), h("span", {}, q.purpose),
        h("span", {class: "meta-text", style: "margin-left:auto;white-space:nowrap"},
          q.error ? q.error : `${q.rows.length} row${q.rows.length === 1 ? "" : "s"}${q.truncated ? ", truncated" : ""} · ${q.elapsed_ms} ms`)),
      h("div", {class: "qbody"}, h("pre", {}, q.sql),
        q.error ? h("p", {class: "fail", style: "margin:0"}, q.message) : resultTable(q.columns, q.rows)))));
}

function evidence(run) {
  const figs = run.verification?.figures || [], ok = figs.filter(f => f.status === "ok").length, clean = figs.length > 0 && ok === figs.length;
  const parts = [figs.length ? `${ok} of ${figs.length} figures checked` : "no figures to check", `${run.queries.length} of ${run.config.max_queries} queries`];
  if (run.repairs.length) parts.push(`${run.repairs.length} repair round`);
  const body = h("div", {class: "body"}, figureTable(run),
    run.repairs.map((r, n) => h("section", {}, h("h3", {}, `Repair round ${n + 1}`),
      h("p", {class: "caption", style: "margin:0 0 6px"}, "The first report failed these checks and went back to the model, without the expected values:"),
      h("ul", {style: "margin:0;padding-left:18px;font-size:14px"}, r.verification.issues.map(i => h("li", {}, prose(i)))))),
    queriesView(run));
  return h("details", {class: "card evidence", id: `ev-${run.run_id}`},
    h("summary", {}, h("span", {class: "ev-icon" + (clean ? "" : " bad")}, icon(clean ? "shield" : "alert")),
      h("span", {}, h("strong", {style: "font-weight:600"}, "How this was checked"), h("span", {class: "meta-text"}, " · " + parts.join(" · "))),
      h("span", {class: "chev"}, icon("chevron"))), body);
}

function findingsView(run) {
  const rep = run.report, figs = run.verification?.figures || [];
  if (!rep.findings.length) return null;
  return h("section", {}, h("h3", {}, "Findings"), h("ul", {class: "findings"}, rep.findings.map((f, i) => {
    const mine = figs.filter(x => x.finding === i), good = mine.filter(x => x.status === "ok"), bad = mine.filter(x => x.status !== "ok");
    return h("li", {}, h("span", {class: `kind k-${f.kind}`}, f.kind),
      h("div", {}, prose(f.statement),
        mine.length ? h("div", {class: "proof"},
          good.length ? h("span", {class: "ok", title: good.map(x => `${figName(x)}: ${fmtCents(x.value_cents)}`).join("\n")},
            icon("check", "sm"), `${good.length} figure${good.length > 1 ? "s" : ""} checked`) : null,
          bad.map(x => h("span", {class: "fail"}, `${figName(x)}: ${fmtCents(x.value_cents)} is ${FIG[x.status]}` +
            (x.expected_cents == null ? "" : `; the rules give ${fmtCents(x.expected_cents)}`))),
          [...new Set(mine.map(x => x.query_id))].map(q => queryButton(run, q))) : null));
  })));
}

function botMsg(run) {
  const rep = run.report, ver = run.verification || {}, st = STATUS[run.status] || {};
  const replayOut = h("span", {role: "status"});
  const parts = [h("div", {class: "bot-head"}, h("span", {class: "avatar"}, icon("search", "sm")), h("span", {class: "who"}, "Assistant"),
    badge(run.status), h("span", {class: "meta-text"}, run.config.model))];
  if (!QUIET.includes(run.status))
    parts.push(h("div", {class: `banner s-${run.status}`, role: "note"}, icon(st.icon || "alert"),
      h("div", {}, h("p", {}, h("strong", {}, st.note || "")), run.error ? h("p", {}, run.error) : null,
        ver.issues?.length ? h("ul", {}, ver.issues.map(i => h("li", {}, prose(i)))) : null)));
  if (rep) {
    parts.push(h("p", {class: "answer"}, prose(rep.summary)), chartView(run), findingsView(run));
    if (rep.assumptions.length || rep.open_questions.length)
      parts.push(h("div", {class: "notes"},
        h("section", {}, h("h3", {}, "Assumptions"), rep.assumptions.length ? h("ul", {}, rep.assumptions.map(a => h("li", {}, prose(a)))) : h("p", {style: "margin:0"}, "None stated.")),
        h("section", {}, h("h3", {}, "Open questions"), rep.open_questions.length ? h("ul", {}, rep.open_questions.map(a => h("li", {}, prose(a)))) : h("p", {style: "margin:0"}, "None."))));
  } else if (run.answer != null) parts.push(h("p", {class: "answer"}, prose(run.answer)));
  if (run.status === "blocked" && run.guard) parts.push(h("p", {class: "caption"}, `${st.note} Reason: ${run.guard.reason}`));
  if (run.compacted) parts.push(h("p", {class: "caption"}, `${run.compacted} earlier messages were summarized to keep this chat within ${run.config.max_messages} messages.`));
  parts.push(rep || run.queries.length ? evidence(run) : null, h("div", {class: "actions"},
    run.answer ? h("button", {type: "button", class: "ghost", onclick: e => copyAnswer(e.currentTarget, run.answer)}, icon("copy", "sm"), h("span", {}, "Copy answer")) : null,
    h("button", {type: "button", class: "ghost", title: "Re-run the recorded model responses: same SQL, results and checks, no API call",
      onclick: () => replay(run.run_id, replayOut)}, icon("replay", "sm"), "Replay"),
    h("a", {class: "ghost", href: `/api/runs/${run.run_id}/report.md`}, icon("download", "sm"), "Export .md"),
    replayOut));
  return h("article", {class: "bot", id: `run-${run.run_id}`, "aria-label": "Answer"}, parts);
}

// The chart spec names a query and its columns; the marks are that query's executed rows.
function chartView(run) {
  const c = run.report?.chart, q = c && run.queries.find(x => x.id === c.query_id);
  if (!q || q.error || !q.rows.length) return null;
  const at = n => q.columns.indexOf(n), xi = at(c.x), gi = c.group ? at(c.group) : -1, yi = c.y.map(at);
  if (xi < 0 || yi.includes(-1) || (c.group && gi < 0)) return null;
  const key = v => String(v ?? "NULL"), xs = [...new Set(q.rows.map(r => key(r[xi])))];
  const series = c.group
    ? [...new Set(q.rows.map(r => key(r[gi])))].map(g => ({name: g, at: new Map(q.rows.filter(r => key(r[gi]) === g).map(r => [key(r[xi]), r[yi[0]]]))}))
    : c.y.map((name, k) => ({name: name.replace(/_cents$/, ""), at: new Map(q.rows.map(r => [key(r[xi]), r[yi[k]]]))}));
  const vals = series.flatMap(sr => [...sr.at.values()]);
  if (series.length > 4 || vals.some(v => typeof v !== "number")) return null;
  const fmt = v => c.unit === "cents" ? fmtCents(v) : v.toLocaleString("en-US");
  const compact = v => (v < 0 ? "-" : "") + (c.unit === "cents" ? "$" : "") +
    (Math.abs(v) / (c.unit === "cents" ? 100 : 1)).toLocaleString("en-US", {notation: "compact", maximumFractionDigits: 1});
  const fmtX = x => { const m = /^(\d{4})-(\d{2})$/.exec(x); return m ? month(+m[1], +m[2]) : x; };
  const color = k => `var(--c${k + 1})`;
  const valuesAt = x => series.map(sr => `${sr.name} ${sr.at.has(x) ? fmt(sr.at.get(x)) : "none"}`).join(", ");

  // Drawn at the plot's real width (and again when it changes), so text stays 12px from phone to desktop.
  const tip = h("div", {class: "tip", "aria-hidden": "true"}), plot = h("div", {class: "plot"});
  tip.hidden = true;
  let drawnAt = 0;
  new ResizeObserver(([entry]) => {
    const width = Math.round(entry.contentRect.width);
    if (width > 0 && Math.abs(width - drawnAt) > 4) { drawnAt = width; plot.replaceChildren(draw(width), tip); }
  }).observe(plot);
  function draw(W) {
    const H = 240, L = 56, R = 8, T = 8, B = 28, pw = W - L - R, ph = H - T - B;
    let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
    const raw = (hi - lo) / 4 || 1, p = 10 ** Math.floor(Math.log10(raw)), f = raw / p;
    const step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
    lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step || step;
    const y = v => T + (hi - v) / (hi - lo) * ph, bw = pw / xs.length, cx = i => L + bw * (i + .5);
    const svg = s("svg", {viewBox: `0 0 ${W} ${H}`, role: "group", "aria-label": `${c.title}. ` + xs.map(x => `${fmtX(x)}: ${valuesAt(x)}`).join("; ")});
    const hl = c.kind === "bar" ? s("rect", {class: "hl", y: T, width: bw, height: ph, rx: 6}) : s("line", {class: "cross", y1: T, y2: T + ph});
    hl.style.visibility = "hidden";
    svg.append(hl);
    for (let k = 0, n = Math.round((hi - lo) / step); k <= n; k++) {
      const v = lo + k * step;
      svg.append(s("line", {class: Math.abs(v) < step / 1e6 ? "axis" : "grid", x1: L, x2: W - R, y1: y(v), y2: y(v)}),
        s("text", {class: "tick", x: L - 8, y: y(v) + 4, "text-anchor": "end"}, compact(v)));
    }
    const every = Math.ceil(xs.length / Math.max(2, Math.floor(pw / 90)));  // skip labels rather than crowd them
    xs.forEach((x, i) => { if (i % every === 0) svg.append(s("text", {class: "tick", x: cx(i), y: H - 6, "text-anchor": "middle"}, fmtX(x))); });
    if (c.kind === "bar") {
      const gw = Math.min(bw * .72, series.length * 44), w = (gw - 2 * (series.length - 1)) / series.length;  // 2px gap between bars
      series.forEach((sr, k) => xs.forEach((x, i) => {
        const v = sr.at.get(x);
        if (v == null) return;
        const x0 = cx(i) - gw / 2 + k * (w + 2), y0 = y(0), y1 = y(v), r = Math.min(4, w / 2, Math.abs(y0 - y1)), d = v < 0 ? -1 : 1;
        svg.append(s("path", {style: `fill:${color(k)}`,  // anchored at zero, rounded at the data end
          d: `M${x0},${y0}V${y1 + d * r}Q${x0},${y1} ${x0 + r},${y1}H${x0 + w - r}Q${x0 + w},${y1} ${x0 + w},${y1 + d * r}V${y0}Z`}));
      }));
    } else series.forEach((sr, k) => {
      const pts = xs.map((x, i) => [i, sr.at.get(x)]).filter(([, v]) => v != null);
      svg.append(s("polyline", {style: `stroke:${color(k)}`, fill: "none", "stroke-width": 2, "stroke-linejoin": "round",
        points: pts.map(([i, v]) => `${cx(i)},${y(v)}`).join(" ")}));
      for (const [i, v] of pts) svg.append(s("circle", {style: `fill:${color(k)};stroke:var(--panel)`, "stroke-width": 2, r: 4.5, cx: cx(i), cy: y(v)}));
    });

    const show = (i, px, py) => {
      if (c.kind === "bar") hl.setAttribute("x", L + bw * i); else { hl.setAttribute("x1", cx(i)); hl.setAttribute("x2", cx(i)); }
      hl.style.visibility = "visible";
      tip.replaceChildren(h("strong", {style: "display:block;margin-bottom:2px"}, fmtX(xs[i])),
        ...series.map((sr, k) => h("div", {}, h("span", {}, h("i", {style: `background:${color(k)}`}), sr.name), h("b", {}, sr.at.has(xs[i]) ? fmt(sr.at.get(xs[i])) : "—"))));
      tip.hidden = false;
      const width = plot.clientWidth;
      tip.style.left = Math.max(0, px + 14 + tip.offsetWidth > width ? px - 14 - tip.offsetWidth : px + 14) + "px";
      tip.style.top = Math.max(0, py - tip.offsetHeight - 12) + "px";
    };
    const hide = () => { tip.hidden = true; hl.style.visibility = "hidden"; };
    xs.forEach((x, i) => {  // one hit area per x value: bigger than the marks, reachable by Tab
      const hit = s("rect", {class: "hit", x: L + bw * i, y: 0, width: bw, height: H, tabindex: 0, rx: 6, role: "img", "aria-label": `${fmtX(x)}: ${valuesAt(x)}`});
      hit.addEventListener("pointermove", e => { const b = plot.getBoundingClientRect(); show(i, e.clientX - b.left, e.clientY - b.top); });
      hit.addEventListener("focus", () => { const scale = plot.clientWidth / W; show(i, cx(i) * scale, (T + ph / 3) * scale); });
      hit.addEventListener("blur", hide);
      svg.append(hit);
    });
    svg.addEventListener("pointerleave", hide);
    return svg;
  }

  const data = h("div", {hidden: true, style: "margin-top:10px"}, resultTable(q.columns, q.rows,
    (v, col) => c.unit === "cents" && c.y.includes(col) && typeof v === "number" ? fmtCents(v) : v));
  const label = h("span", {}, "Show data");
  const toggle = h("button", {type: "button", class: "ghost", "aria-expanded": "false", onclick: () => {
    data.hidden = !data.hidden; toggle.setAttribute("aria-expanded", String(!data.hidden));
    label.textContent = data.hidden ? "Show data" : "Hide data";
  }}, icon("table", "sm"), label);
  return h("section", {class: "card chart", "aria-label": "Chart"},
    h("p", {class: "chart-title"}, c.title),
    series.length > 1 ? h("div", {class: "legend"}, series.map((sr, k) => h("span", {}, h("i", {style: `background:${color(k)}`}), sr.name))) : null,
    plot,
    h("div", {class: "chart-foot"}, h("span", {class: "caption", style: "margin:0"}, "Drawn from the rows of ", queryButton(run, c.query_id),
      " as returned. The chart's values are not rule-checked; the checked figures are."), toggle),
    data);
}

function showQuery(runId, qid) {
  const ev = $(`ev-${runId}`), el = $(`q-${runId}-${qid}`);
  if (ev) ev.open = true;
  if (!el) return;
  el.open = true;
  el.scrollIntoView({behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "center"});
  el.querySelector("summary").focus({preventScroll: true});
}
async function copyAnswer(btn, text) {
  const label = btn.lastChild;
  try { await navigator.clipboard.writeText(prose(text)); label.textContent = "Copied"; }
  catch { label.textContent = "Copy failed"; }
  setTimeout(() => { label.textContent = "Copy answer"; }, 2000);
}
async function replay(id, out) {
  out.textContent = "Replaying the recorded model responses…";
  try {
    const r = await api(`/api/runs/${id}/replay`, {});
    out.textContent = r.diffs.length ? `Replay differs: ${r.diffs.join("; ")}` : `Reproduced: same queries, results and checks (${r.status}), no API call.`;
  } catch (e) { out.textContent = `Replay failed: ${e.message}`; }
}

// ---- the message shown while the model works, fed by the streamed progress
function pendingView() {
  const p = pending.progress || {guard: null, queries: [], submitted: false, repairs: 0};
  const now = !p.guard ? "Checking that the message is about the sales data" :
    p.repairs ? "Some checks failed: the model is revising its report" :
    p.submitted ? "Checking every figure against the business rules" :
    p.queries.length ? "Reading the results and choosing the next step" : "Deciding whether to reply or query the data";
  return [h("div", {class: "bot-head"}, h("span", {class: "avatar"}, icon("search", "sm")), h("span", {class: "who"}, "Assistant"),
      h("span", {class: "meta-text", id: "elapsed"}, elapsed())),
    h("ol", {class: "steps", "aria-label": "Progress"},
      p.guard ? h("li", {class: "done"}, icon("check", "sm"), h("span", {}, "Message checked")) : null,
      p.queries.map(q => h("li", {class: q.error ? "err" : "done"}, icon(q.error ? "x" : "check", "sm"),
        h("span", {}, h("span", {class: "qid"}, q.id), ` ${q.purpose} · `, q.error ? q.error : `${q.rows} row${q.rows === 1 ? "" : "s"}`))),
      h("li", {class: "now"}, h("span", {class: "spinner", "aria-hidden": "true"}), now))];
}
const elapsed = () => pending ? `${Math.round((Date.now() - pending.t0) / 1000)} s · at most ${config.max_queries} read-only queries` : "";
setInterval(() => { const el = $("elapsed"); if (el) el.textContent = elapsed(); }, 1000);

// ---- views
function emptyView() {
  return h("div", {class: "hello"}, h("div", {class: "logo"}, icon("search")),
    h("h2", {}, "What do you want to know?"),
    h("p", {}, "Ask about sales, refunds or customer segments. The model explores the data with read-only SQL, and code checks every figure it reports. Ask for a chart when you want one."),
    h("div", {class: "suggest"}, SUGGESTIONS.map(sg => h("button", {type: "button", disabled: !config.live, onclick: () => ask(sg.text)},
      h("span", {class: "si"}, icon(sg.icon)), h("span", {}, h("strong", {}, sg.title), h("span", {class: "d"}, sg.text))))),
    config.live ? null : h("p", {style: "margin-top:20px;font-size:14px"}, "No API key is set, so new questions are off. Open a saved chat on the left, or set LLM_API_KEY in .env."));
}
function signInView() {
  const input = h("input", {id: "key", class: "keyin", type: "password", autocomplete: "current-password", required: true, "aria-describedby": "key-help"});
  return h("form", {class: "hello", onsubmit: e => { e.preventDefault(); setKey(input.value.trim()); start(); }},
    h("div", {class: "logo"}, icon("shield")), h("h2", {}, "Sign in"),
    h("p", {id: "key-help"}, "Paste your app key. Each key sees only its own chats. To create one: ",
      h("code", {}, "python -m investigator add-user <name>")),
    h("label", {for: "key", class: "sr"}, "App key"),
    h("div", {class: "keyrow"}, input, h("button", {type: "submit", class: "btn"}, "Sign in")),
    notice ? h("p", {class: "fail", role: "alert"}, notice.error) : null);
}
function render() {
  const view = $("thread"), items = [];
  renderMode();
  if (!key) {
    view.classList.add("empty");
    view.replaceChildren(signInView());
    $("title").textContent = "Sign in";
    $("q").disabled = $("composer").querySelector("button").disabled = true;
    $("q").placeholder = "Sign in to ask";
    renderList();
    return $("key").focus();
  }
  for (const r of thread?.runs || []) items.push(h("div", {class: "user", id: `ask-${r.run_id}`}, r.question), botMsg(r));
  if (pending?.nav === nav) items.push(h("div", {class: "user"}, pending.question),
    h("article", {class: "bot", id: "pending", "aria-busy": "true", "aria-live": "polite"}, pendingView()));
  if (notice?.nav === nav) items.push(...(notice.question ? [h("div", {class: "user"}, notice.question)] : []),
    h("div", {class: "banner s-failed", role: "alert"}, icon("circleX"), h("div", {}, h("p", {}, notice.error),
      notice.question && config.live ? h("button", {type: "button", class: "btn", style: "margin-top:8px", onclick: () => ask(notice.question)},
        icon("replay", "sm"), "Try again") : null)));
  view.classList.toggle("empty", !items.length);
  view.replaceChildren(...(items.length ? items : [emptyView()]));
  $("title").textContent = thread ? thread.runs[0].question : pending?.nav === nav ? pending.question : "New chat";
  document.title = thread ? `${thread.runs[0].question} · Data Investigator` : "Data Investigator";
  $("q").placeholder = !config.live ? "No API key: saved chats only" : thread ? "Ask a follow-up…" : "Ask why sales changed, or ask for a chart…";
  $("q").disabled = $("composer").querySelector("button").disabled = !config.live || !!pending;
  renderList();
}
const toBottom = () => { const sc = $("scroll"); sc.scrollTop = sc.scrollHeight; };
const toRun = id => $(`ask-${id}`)?.scrollIntoView({block: "start"});

function setDrawer(open) {
  $("side").classList.toggle("open", open);
  $("scrim").hidden = !open;
  $("menu").setAttribute("aria-expanded", String(open));
}
function go(id) {
  setDrawer(false);
  if (location.hash.slice(1) === id) route(); else location.hash = id;
}
async function route() {
  const id = location.hash.slice(1);
  notice = null;
  if (!id) { nav++; thread = null; render(); $("q").focus(); return; }
  const root = rootOf(id), token = ++nav;
  const ids = runs.filter(r => rootOf(r.run_id) === root).sort((a, b) => a.created_at.localeCompare(b.created_at)).map(r => r.run_id);
  try {
    const full = await Promise.all((ids.length ? ids : [id]).map(i => api(`/api/runs/${i}`)));
    if (token !== nav) return;
    thread = {root, runs: full};
  } catch (e) {
    if (token !== nav) return;
    thread = null; notice = {nav, question: "", error: `Could not open this chat: ${e.message}`};
  }
  render();
  toRun(id === root ? thread?.runs.at(-1).run_id : id);
}

async function ask(question) {
  if (pending || !config.live) return;
  const parent = thread ? thread.runs.at(-1).run_id : null, token = nav;
  notice = null; pending = {question, t0: Date.now(), nav, progress: null};
  $("q").value = ""; grow(); render(); toBottom();
  let run = null;
  try {
    run = await askStream({question, parent_run_id: parent}, progress => {
      pending.progress = progress;
      const el = $("pending");
      if (el && token === nav) { el.replaceChildren(...pendingView()); toBottom(); }
    });
    runs = await api("/api/runs");
  } catch (e) {
    if (token === nav) notice = {nav, question, error: e.message};
  }
  pending = null;
  if (run && token === nav) {
    if (!thread) { thread = {root: run.run_id, runs: []}; history.replaceState(null, "", `#${run.run_id}`); }
    thread.runs.push(run);
  }
  render();
  if (token !== nav) return;
  if (run) toRun(run.run_id);
  else { toBottom(); $("q").value = question; grow(); }
}

function grow() { const q = $("q"); q.style.height = "auto"; q.style.height = Math.min(q.scrollHeight, 200) + "px"; }

async function init() {
  document.querySelectorAll("[data-icon]").forEach(el => el.replaceWith(icon(el.dataset.icon)));
  config = await api("/api/config");

  $("limits").textContent = config.live ? "Figures are checked against the data. Explanations of why can still be wrong."
    : "Set LLM_API_KEY in .env to ask new questions.";
  $("composer").addEventListener("submit", e => { e.preventDefault(); const q = $("q").value.trim(); if (q) ask(q); });
  $("q").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("composer").requestSubmit(); } });
  $("q").addEventListener("input", grow);
  $("new").addEventListener("click", () => { setDrawer(false); if (location.hash) history.pushState(null, "", location.pathname); route(); });
  $("menu").addEventListener("click", () => setDrawer(!$("side").classList.contains("open")));
  $("scrim").addEventListener("click", () => setDrawer(false));
  document.addEventListener("keydown", e => { if (e.key === "Escape" && $("side").classList.contains("open")) { setDrawer(false); $("menu").focus(); } });
  await start();
}
function renderMode() {
  $("mode").replaceChildren(h("span", {class: "dot" + (config.live ? "" : " off"), "aria-hidden": "true"}),
    config.live ? `Live · ${config.model}` : "No API key · saved runs and replay",
    ...(key ? [h("button", {type: "button", class: "ghost signout", onclick: () => { setKey(null); notice = null; start(); }},
      icon("logOut", "sm"), "Sign out")] : []));
}
async function start() {
  notice = null; thread = null; runs = [];
  if (!key) return render();
  try { runs = await api("/api/runs"); } catch (e) { notice = {nav, question: "", error: e.message}; return render(); }
  await route();
}
window.addEventListener("popstate", route);  // back/forward, including hash changes
init().catch(e => { notice = {nav, question: "", error: e.message}; render(); });
