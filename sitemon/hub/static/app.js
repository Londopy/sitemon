// sitemon dashboard. Plain DOM, no framework: it gets served from a Pi on the mesh.
"use strict";

const NAMES = {
  v: ["voltage", "V"], a: ["current", "A"], w: ["power", "W"],
  temp_c: ["temperature", "°C"], rh: ["humidity", "%"], hpa: ["pressure", "hPa"],
  link_count: ["links", ""], rf_links: ["RF links", ""], queue: ["queued events", ""],
  fwd_w: ["forward power", "W"], ref_w: ["reflected power", "W"], rl_db: ["return loss", "dB"],
  swr: ["SWR", ""],
};
const RANGES = [7, 30, 90, 365];

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v; else e.setAttribute(k, v);
  }
  for (const k of kids) e.append(k instanceof Node ? k : document.createTextNode(String(k)));
  return e;
}

function svg(tag, attrs = {}) {
  const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}

function label(metric) {
  const [src, key] = [metric.slice(0, metric.indexOf(".")), metric.slice(metric.indexOf(".") + 1)];
  const n = NAMES[key];
  return n ? `${src} ${n[0]}` : metric;
}

function unit(metric) {
  const n = NAMES[metric.slice(metric.indexOf(".") + 1)];
  return n ? n[1] : "";
}

function fmt(v, metric) {
  if (typeof v === "number") {
    const u = unit(metric);
    if (!u && Number.isInteger(v)) return String(v);  // counts: "0", not "0.00"
    // 12.05 V vs 12.0 V can be the difference between fine and a warning
    const s = u === "V" ? v.toFixed(2)
      : Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 10 ? v.toFixed(1) : v.toFixed(2);
    return u ? `${s} ${u}` : s;
  }
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (Array.isArray(v)) return v.join(", ") || "none";
  if (v && typeof v === "object") {
    // e.g. agent.hubs: {url: {lag, last_ok_age_s}} -> "url: lag 0"
    return Object.entries(v).map(([k, x]) =>
      x && typeof x === "object" && "lag" in x ? `${k}: lag ${x.lag}` : `${k}: ${fmt(x, "")}`
    ).join("; ") || "none";
  }
  return v === null ? "n/a" : String(v);
}

function ago(s) {
  if (s === null || s === undefined) return "never";
  if (s < 90) return `${s} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

function line(points, w, h, pad = 2) {
  if (points.length < 2) return null;
  const ts = points.map(p => p[0]), vs = points.map(p => p[1]);
  const t0 = Math.min(...ts), t1 = Math.max(...ts);
  let v0 = Math.min(...vs), v1 = Math.max(...vs);
  if (v1 - v0 < 1e-9) { v0 -= 1; v1 += 1; }
  const x = t => pad + (w - 2 * pad) * (t - t0) / Math.max(t1 - t0, 1);
  const y = v => h - pad - (h - 2 * pad) * (v - v0) / (v1 - v0);
  const d = points.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
  return { d, v0, v1, t0, t1 };
}

function sparkline(points) {
  const s = svg("svg", { viewBox: "0 0 120 22", preserveAspectRatio: "none", role: "img" });
  const l = line(points, 120, 22);
  if (l) s.append(svg("path", { d: l.d, class: "spark-line" }));
  return s;
}

function statusText(site) {
  if (site.status === "silent") {
    return `No contact (last heard ${ago(site.last_contact_s)})`;
  }
  if (site.alerts.length) {
    const worst = [...site.alerts].sort((a, b) => (b.severity === "critical") - (a.severity === "critical"))[0];
    return worst.message;
  }
  const bits = [];
  const L = site.latest;
  for (const [m, info] of Object.entries(L)) {
    if (m.endsWith(".v") && typeof info.value === "number") bits.push(`${m.split(".")[0]} ${fmt(info.value, m)}`);
  }
  for (const [m, info] of Object.entries(L)) {
    if (m.endsWith(".temp_c")) bits.push(fmt(info.value, m));
  }
  if (L["ami.link_count"]) bits.push(`${L["ami.link_count"].value} links`);
  return "OK" + (bits.length ? ` · ${bits.join(" · ")}` : "");
}

function tile(site) {
  const sparks = el("div", { class: "sparks" });
  for (const [m, pts] of Object.entries(site.spark)) {
    const cur = site.latest[m];
    sparks.append(el("span", {}, label(m)), sparkline(pts), el("span", { class: "val" }, cur ? fmt(cur.value, m) : ""));
  }
  // the worst alert is already the status line; list them only when there are several
  const alerts = site.alerts.length > 1
    ? el("ul", { class: "alerts" }, ...site.alerts.map(a => el("li", { class: `sev-${a.severity}` }, a.message)))
    : "";
  return el("a", { class: `tile ${site.status}`, href: `/site/${encodeURIComponent(site.id)}` },
    el("h3", {}, site.name),
    el("div", { class: "status" }, statusText(site)),
    alerts,
    sparks,
    el("div", { class: "muted" }, `Last contact ${ago(site.last_contact_s)}`));
}

async function getJSON(url) {
  const r = await fetch(url, { headers: { Accept: "application/json" } });
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json();
}

async function groupPage(slug) {
  const group = await getJSON(`/api/group/${encodeURIComponent(slug)}`);
  document.title = `${group.name} · sitemon`;
  const crumb = document.getElementById("crumb");
  crumb.replaceChildren(group.name);
  const order = { critical: 0, silent: 1, warn: 2, ok: 3 };
  const sites = [...group.sites].sort((a, b) => order[a.status] - order[b.status] || a.name.localeCompare(b.name));
  const main = document.getElementById("main");
  main.replaceChildren(
    el("h1", {}, group.name),
    sites.length ? el("div", { class: "grid" }, ...sites.map(tile)) : el("p", { class: "muted" }, "No sites configured yet."));
  document.getElementById("foot").textContent = `sitemon ${group.version}`;
}

function trendText(perWeek) {
  if (Math.abs(perWeek) < 0.01) return "nightly low steady over 30 d";
  return `nightly low ${perWeek < 0 ? "falling" : "rising"} ${Math.abs(perWeek).toFixed(2)} V a week over 30 d`;
}

function chart(siteId, metric, points, latest, days, trend) {
  const W = 600, H = 140;
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img" });
  const l = line(points, W, H - 14, 4);
  s.append(svg("line", { x1: 0, y1: H - 14, x2: W, y2: H - 14, class: "axis" }));
  if (l) {
    s.append(svg("path", { d: l.d, class: "spark-line" }));
    const hi = svg("text", { x: 4, y: 10, class: "axis-label" }); hi.textContent = fmt(l.v1, metric);
    const lo = svg("text", { x: 4, y: H - 18, class: "axis-label" }); lo.textContent = fmt(l.v0, metric);
    const t = svg("text", { x: W - 4, y: H - 2, class: "axis-label", "text-anchor": "end" }); t.textContent = "now";
    const t0 = svg("text", { x: 4, y: H - 2, class: "axis-label" }); t0.textContent = new Date(l.t0 * 1000).toLocaleDateString();
    s.append(hi, lo, t, t0);
  }
  const q = `site=${encodeURIComponent(siteId)}&metric=${encodeURIComponent(metric)}`;
  return el("div", { class: "chart" },
    el("div", { class: "chart-head" },
      el("b", {}, label(metric)),
      el("span", {}, latest ? fmt(latest.value, metric) : ""),
      trend === undefined ? "" : el("span", { class: "muted" }, trendText(trend)),
      el("a", { href: `/api/csv?${q}&hours=${days * 24}` }, `CSV ${days} d`)),
    s);
}

function commandText(c) {
  const arg = k => typeof c.args[k] === "string" || typeof c.args[k] === "number" ? String(c.args[k]) : "";
  if (c.kind === "announce") return `say "${arg("text")}"`;
  const secs = arg("seconds") ? ` ${arg("seconds")} s` : "";
  return `${arg("relay") || "?"} ${arg("action") || "?"}${secs}`;
}

function rangeLinks(days) {
  const links = RANGES.map(d => d === days
    ? el("b", { "aria-current": "page" }, `${d} d`)
    : el("a", { href: `?days=${d}` }, `${d} d`));
  return el("nav", { class: "ranges", "aria-label": "Chart range" },
    ...links.flatMap((l, i) => i ? [" · ", l] : [l]));
}

function wantedDays() {
  const d = Number(new URLSearchParams(location.search).get("days"));
  return Number.isFinite(d) && d > 0 ? d : 7;
}

async function sitePage(id) {
  const wanted = wantedDays();
  const site = await getJSON(`/api/site/${encodeURIComponent(id)}?days=${wanted}`);
  const days = Math.round(site.days);
  document.title = `${site.name} · sitemon`;
  document.getElementById("crumb").replaceChildren(
    el("a", { href: `/group/${encodeURIComponent(site.group)}` }, site.group_name), " / ", site.name);
  const main = document.getElementById("main");
  const charts = el("div", {});
  for (const [m, pts] of Object.entries(site.spark)) {
    charts.append(chart(site.id, m, pts, site.latest[m], days, site.trends[m]));
  }
  const rows = Object.entries(site.latest)
    // a cleared sensor fault is null: nothing worth a row
    .filter(([m, info]) => !m.startsWith("_") && !(m.endsWith(".fault") && info.value === null))
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([m, info]) => el("tr", {}, el("th", {}, m), el("td", {}, fmt(info.value, m)), el("td", { class: "muted" }, ago(info.age_s))));
  const hist = site.recent_alerts.slice().reverse().map(a => el("tr", {},
    el("td", { class: `sev-${a.severity}` }, a.severity),
    el("td", {}, a.message),
    el("td", { class: "muted" }, new Date(a.opened * 1000).toLocaleString()),
    el("td", { class: "muted" }, a.resolved ? `resolved ${new Date(a.resolved * 1000).toLocaleString()}` : "open")));
  const cmds = site.commands.map(c => el("tr", {},
    el("td", {}, commandText(c)),
    el("td", { class: c.status === "refused" ? "sev-warn" : "" }, c.status),
    el("td", {}, c.detail ?? ""),
    el("td", { class: "muted" }, new Date(c.created * 1000).toLocaleString())));
  main.replaceChildren(
    el("h1", {}, site.name),
    el("div", { class: `tile ${site.status}` },
      el("div", { class: "status" }, statusText(site)),
      el("div", { class: "muted" }, `Last contact ${ago(site.last_contact_s)}`)),
    el("div", { class: "range-head" }, el("h2", {}, `Last ${days} days`), rangeLinks(days)),
    charts.childNodes.length ? charts : el("p", { class: "muted" }, "No readings yet."),
    el("h2", {}, "Latest values"), el("div", { class: "table-wrap" }, el("table", {}, ...rows)),
    el("h2", {}, "Alerts, last 30 days"),
    hist.length ? el("div", { class: "table-wrap" }, el("table", {}, ...hist)) : el("p", { class: "muted" }, "None."),
    ...(cmds.length ? [el("h2", {}, "Commands"), el("div", { class: "table-wrap" }, el("table", {}, ...cmds))] : []));
}

async function groupsPage() {
  const groups = await getJSON("/api/groups");
  document.getElementById("main").replaceChildren(
    el("h1", {}, "Groups"),
    el("ul", {}, ...groups.map(c => el("li", {}, el("a", { href: `/group/${encodeURIComponent(c.slug)}` }, c.name), ` (${c.sites} sites)`))));
}

async function render() {
  const parts = location.pathname.split("/").filter(Boolean).map(decodeURIComponent);
  try {
    if (parts[0] === "group" && parts[1]) await groupPage(parts[1]);
    else if (parts[0] === "site" && parts[1]) await sitePage(parts[1]);
    else await groupsPage();
    document.getElementById("updated").textContent = `updated ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    document.getElementById("updated").textContent = `update failed: ${e.message}`;
  }
}

render();
// Long ranges change slowly and cost the hub more to draw.
setInterval(render, wantedDays() > 7 ? 300000 : 30000);
