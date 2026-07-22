"""Self-contained window-history dashboard (history/dashboard.html).

Static file: inline CSS/JS, data embedded as a JSON blob, no CDN — works
offline from file://. Regenerated only when window_history gains rows or on
demand via `pool.py dashboard [--open]`, never on ordinary poll ticks.
"""
from __future__ import annotations
import json
import store
import window_history
from window_history import HISTORY_DIR, fmt_local


def dashboard_data(conn) -> list[dict]:
    rows = []
    for r in store.list_window_history(conn):
        if r["window_kind"].startswith("5h"):
            continue  # 5h windows reset often and dwarf the weekly/monthly signal
        try:
            details = json.loads(r["details"]) if r["details"] else {}
        except (TypeError, ValueError):
            details = {}  # one corrupt row must not break the whole dashboard
        if not isinstance(details, dict):
            details = {}
        rows.append({
            "provider": r["provider"],
            "account": r["email"] or r["label"] or f"#{r['account_id']}",
            "account_id": r["account_id"],
            "window_kind": r["window_kind"],
            "window_start": r["window_start"],
            "window_end": r["window_end"],
            "window_end_label": fmt_local(r["window_end"]),
            "final_used_pct": r["final_used_pct"],
            "reset_cause": r["reset_cause"],
            "staleness_s": details.get("staleness_s"),
            "ongoing": False,
        })
    # Append the currently-open weekly window per account so the dashboard
    # shows the in-progress week, not just closed ones. Threshold-based
    # detection only fires at close, so without this the current week is
    # invisible until it resets.
    for a in store.list_accounts(conn):
        snap = store.latest_successful_snapshot(conn, a["id"])
        if not snap:
            continue
        acct = a["email"] or a["label"] or f"#{a['id']}"
        for w in (window_history.timed_windows(a["provider"], snap)
                  + window_history.drop_windows(a["provider"], snap)):
            if w["kind"].startswith("5h") or w["kind"] in (
                    "daily", "monthly", "monthly_premium", "monthly_chat"):
                continue
            if w.get("used_pct") is None:
                continue
            rows.append({
                "provider": a["provider"],
                "account": acct,
                "account_id": a["id"],
                "window_kind": w["kind"],
                "window_start": None,
                "window_end": None,
                "window_end_label": "ongoing",
                "final_used_pct": float(w["used_pct"]),
                "reset_cause": "ongoing",
                "staleness_s": None,
                "ongoing": True,
            })
    return rows


def coupon_data(conn) -> list[dict]:
    """Codex reset-credit ledger: every credit ever seen, with its final state."""
    rows = []
    for r in store.list_credit_history(conn, provider="codex"):
        rows.append({
            "provider": r["provider"],
            "account": r["email"] or r["label"] or f"#{r['account_id']}",
            "credit_id": r["credit_id"],
            "title": r["title"] or "",
            "description": r["description"] or "",
            "granted_at": r["granted_at"] or "",
            "expires_at": r["expires_at"] or "",
            "first_seen_at": fmt_local(r["first_seen_at"]),
            "last_seen_at": fmt_local(r["last_seen_at"]),
            "final_state": r["final_state"] or "available",
            "final_seen_at": fmt_local(r["final_seen_at"]) if r["final_seen_at"] else "",
            "redeemed_at": fmt_local(r["redeemed_at"]) if r["redeemed_at"] else "",
        })
    return rows


def generate(conn):
    """Write history/dashboard.html and return its path."""
    window_history.ensure_history_dir()
    path = HISTORY_DIR / "dashboard.html"
    data = json.dumps(dashboard_data(conn)).replace("</", "<\\/")
    coupons = json.dumps(coupon_data(conn)).replace("</", "<\\/")
    html = _HTML_TEMPLATE.replace("/*__DATA__*/[]", data)
    html = html.replace("/*__COUPONS__*/[]", coupons)
    window_history.atomic_write_text(path, html)
    return path


# The bar palette (one hue per provider) and the cause-badge colors are the
# dataviz-skill reference palette, chosen as the all-pairs colorblind-safe
# 6-subset (validated worst ΔE 11.2 light / 10.3 dark, the floor band — legal
# because identity is always carried by the legend, the SVG <title> tooltip,
# and the table Provider column, never by color alone). Cause chips use the
# reserved status scale as text+dot, so a status color never impersonates a
# provider series.
_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Token window history</title>
<style>
  :root {
    --plane:      #f9f9f7;
    --surface:    #fcfcfb;
    --primary:    #0b0b0b;
    --secondary:  #52514e;
    --muted:      #898781;
    --grid:       #e1e0d9;
    --axis:       #c3c2b7;
    --border:     rgba(11,11,11,0.10);
    --prov-codex:       #008300;
    --prov-claude:      #eb6834;
    --prov-xai:         #e87ba4;
    --prov-antigravity: #eda100;
    --prov-copilot:     #2a78d6;
    --prov-devin:       #e34948;
    --prov-other:       #898781;
    --cause-natural:        #0ca30c;
    --cause-coupon:         #fab219;
    --cause-provider_reset: #ec835a;
    --cause-unknown:        #898781;
    --cause-ongoing:        #6b6bf0;
    --coupon-available:        #0ca30c;
    --coupon-redeemed:         #2a78d6;
    --coupon-expired_unused:   #e34948;
    --coupon-gone:             #898781;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --plane:      #0d0d0d;
      --surface:    #1a1a19;
      --primary:    #ffffff;
      --secondary:  #c3c2b7;
      --muted:      #898781;
      --grid:       #2c2c2a;
      --axis:       #383835;
      --border:     rgba(255,255,255,0.10);
      --prov-codex:       #008300;
      --prov-claude:      #d95926;
      --prov-xai:         #d55181;
      --prov-antigravity: #c98500;
      --prov-copilot:     #3987e5;
      --prov-devin:       #e66767;
      --prov-other:       #898781;
      --coupon-available:        #0ca30c;
      --coupon-redeemed:         #3987e5;
      --coupon-expired_unused:   #e66767;
      --coupon-gone:             #898781;
      --cause-ongoing:        #8a8af0;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px;
    background: var(--plane); color: var(--primary);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    font-size: 14px; line-height: 1.45;
  }
  h1 { font-size: 20px; font-weight: 600; margin: 0 0 4px; }
  .sub { color: var(--secondary); margin: 0 0 20px; }
  .card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 16px; margin-bottom: 20px;
  }
  h2 { font-size: 14px; font-weight: 600; margin: 0 0 2px; }
  .card-sub { color: var(--muted); font-size: 12px; margin: 0 0 12px; }
  .filters { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }
  .filters label { display: flex; flex-direction: column; gap: 4px;
    font-size: 12px; color: var(--secondary); }
  .filters select {
    font: inherit; font-size: 13px; padding: 5px 8px;
    background: var(--surface); color: var(--primary);
    border: 1px solid var(--axis); border-radius: 6px; min-width: 140px;
  }
  .legend { display: flex; flex-wrap: wrap; align-items: center;
    gap: 6px 16px; margin-bottom: 12px; }
  .legend-group { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; }
  .legend-title { font-size: 11px; text-transform: uppercase;
    letter-spacing: 0.04em; color: var(--muted); margin-right: 2px; }
  .key { display: inline-flex; align-items: center; gap: 6px;
    font-size: 12px; color: var(--secondary); }
  .swatch { width: 12px; height: 12px; border-radius: 3px; flex: 0 0 auto; }
  .badge { display: inline-flex; align-items: center; gap: 6px;
    padding: 2px 8px; border-radius: 999px; font-size: 12px;
    color: var(--primary); background: var(--surface);
    border: 1px solid var(--border); white-space: nowrap; }
  .badge .dot { width: 8px; height: 8px; border-radius: 999px; flex: 0 0 auto; }
  .plot-wrap { overflow-x: auto; }
  svg { display: block; }
  svg text { fill: var(--muted); font-size: 11px; }
  svg .y-tick { font-variant-numeric: tabular-nums; }
  svg .bar { transition: opacity .12s; }
  svg .bar:hover { opacity: 0.78; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { text-align: left; padding: 7px 10px;
    border-bottom: 1px solid var(--border); white-space: nowrap; }
  th { color: var(--secondary); font-weight: 600; cursor: pointer;
    user-select: none; position: sticky; top: 0; background: var(--surface); }
  th:hover { color: var(--primary); }
  th .arrow { color: var(--muted); font-size: 10px; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .table-wrap { overflow-x: auto; }
  .empty { color: var(--secondary); padding: 40px 8px; text-align: center; }
  .empty code { background: var(--plane); border: 1px solid var(--border);
    border-radius: 4px; padding: 1px 5px; font-size: 12px; }
  .note { color: var(--muted); padding: 24px 8px; text-align: center; }
  [hidden] { display: none !important; }
</style>
</head>
<body>
<h1>Token window history</h1>
<p class="sub">Final usage of every weekly/monthly quota window at the moment it closed, with why it reset. (5h windows are excluded — they reset too often to be useful here.)</p>

<div id="app">
  <div class="filters">
    <label>Provider<select id="f-provider"></select></label>
    <label>Account<select id="f-account"></select></label>
    <label>Window<select id="f-kind"></select></label>
  </div>

  <section class="card">
    <h2>Usage at window close</h2>
    <p class="card-sub">One bar per closed window, ordered by close time. Height is final used %.</p>
    <div class="legend" id="legend"></div>
    <div class="plot-wrap"><svg id="chart"></svg></div>
    <p class="note" id="chart-empty" hidden>No windows match the current filters.</p>
  </section>

  <section class="card">
    <div class="table-wrap"><table id="table"></table></div>
    <p class="note" id="table-empty" hidden>No windows match the current filters.</p>
  </section>

  <section class="card" id="coupon-card">
    <h2>Reset coupon ledger (Codex)</h2>
    <p class="card-sub">Every banked reset credit ever observed, with its final state. <span id="coupon-summary"></span></p>
    <div class="table-wrap"><table id="coupon-table"></table></div>
    <p class="note" id="coupon-empty" hidden>No reset credits archived yet.</p>
  </section>
</div>

<div class="empty" id="empty" hidden>
  No closed windows archived yet — run <code>pool.py backfill-history</code>.
</div>

<script>
const ROWS = /*__DATA__*/[];

const COUPONS = /*__COUPONS__*/[];
const COUPON_STATES = ["available", "redeemed", "expired_unused", "gone"];
const couponVar = s => "var(" + (COUPON_STATES.includes(s) ? "--coupon-" + s : "--coupon-gone") + ")";
const SVGNS = "http://www.w3.org/2000/svg";
const PROV = ["codex", "claude", "xai", "antigravity", "copilot", "devin"];
const CAUSES = ["natural", "coupon", "provider_reset", "unknown", "ongoing"];
const provVar = p => "var(" + (PROV.includes(p) ? "--prov-" + p : "--prov-other") + ")";
const causeVar = c => "var(" + (CAUSES.includes(c) ? "--cause-" + c : "--cause-unknown") + ")";

function svgEl(name, attrs) {
  const e = document.createElementNS(SVGNS, name);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  return e;
}
function barPath(x, y, w, h, r) {
  r = Math.max(0, Math.min(r, w / 2, h));
  return "M" + x + "," + (y + h) + " L" + x + "," + (y + r) +
         " Q" + x + "," + y + " " + (x + r) + "," + y +
         " L" + (x + w - r) + "," + y +
         " Q" + (x + w) + "," + y + " " + (x + w) + "," + (y + r) +
         " L" + (x + w) + "," + (y + h) + " Z";
}
function pct(v) { return (v == null ? 0 : Number(v)).toFixed(1); }

// ---- filters ------------------------------------------------------------
function uniq(key) {
  return Array.from(new Set(ROWS.map(r => r[key]).filter(v => v != null))).sort();
}
function fillSelect(id, values, allLabel) {
  const sel = document.getElementById(id);
  sel.appendChild(new Option(allLabel, "__all__"));
  for (const v of values) sel.appendChild(new Option(v, v));
  sel.addEventListener("change", render);
}
function visibleRows() {
  const p = document.getElementById("f-provider").value;
  const a = document.getElementById("f-account").value;
  const k = document.getElementById("f-kind").value;
  return ROWS.filter(r =>
    (p === "__all__" || r.provider === p) &&
    (a === "__all__" || r.account === a) &&
    (k === "__all__" || r.window_kind === k));
}

// ---- chart --------------------------------------------------------------
function renderLegend(rows) {
  const legend = document.getElementById("legend");
  legend.textContent = "";
  const provs = PROV.filter(p => rows.some(r => r.provider === p))
    .concat(rows.some(r => !PROV.includes(r.provider)) ? ["other"] : []);
  const pg = document.createElement("div");
  pg.className = "legend-group";
  const pt = document.createElement("span");
  pt.className = "legend-title"; pt.textContent = "Provider";
  pg.appendChild(pt);
  for (const p of provs) {
    const key = document.createElement("span");
    key.className = "key";
    const sw = document.createElement("span");
    sw.className = "swatch";
    sw.style.background = p === "other" ? "var(--prov-other)" : provVar(p);
    const lab = document.createElement("span");
    lab.textContent = p;
    key.append(sw, lab); pg.appendChild(key);
  }
  legend.appendChild(pg);

  const cg = document.createElement("div");
  cg.className = "legend-group";
  const ct = document.createElement("span");
  ct.className = "legend-title"; ct.textContent = "Reset cause";
  cg.appendChild(ct);
  for (const c of CAUSES) cg.appendChild(causeBadge(c));
  legend.appendChild(cg);
}

function causeBadge(cause) {
  const b = document.createElement("span");
  b.className = "badge";
  const dot = document.createElement("span");
  dot.className = "dot";
  dot.style.background = causeVar(cause);
  const lab = document.createElement("span");
  lab.textContent = cause;
  b.append(dot, lab);
  return b;
}

function renderChart(rows) {
  const svg = document.getElementById("chart");
  svg.textContent = "";
  const chartEmpty = document.getElementById("chart-empty");
  if (!rows.length) { svg.setAttribute("width", 0); svg.setAttribute("height", 0);
    chartEmpty.hidden = false; return; }
  chartEmpty.hidden = true;

  const data = rows.slice().sort((a, b) => {
    // ongoing windows sort last (window_end is null)
    if (a.ongoing && !b.ongoing) return 1;
    if (!a.ongoing && b.ongoing) return -1;
    return (a.window_end || 0) - (b.window_end || 0);
  });
  const wrapW = Math.max(320, document.querySelector(".plot-wrap").clientWidth);
  const mL = 44, mR = 16, mT = 16, mB = 60;
  const n = data.length;
  const slot = Math.max(16, Math.min(56, (wrapW - mL - mR) / n));
  const W = Math.max(wrapW, mL + mR + n * slot);
  const plotW = W - mL - mR;
  const plotH = 260, H = mT + plotH + mB;
  const y = v => mT + plotH * (1 - Math.max(0, Math.min(100, v)) / 100);

  svg.setAttribute("width", W);
  svg.setAttribute("height", H);
  svg.setAttribute("viewBox", "0 0 " + W + " " + H);

  // y gridlines + ticks
  for (const t of [0, 25, 50, 75, 100]) {
    const yy = y(t);
    svg.appendChild(svgEl("line", { x1: mL, y1: yy, x2: mL + plotW, y2: yy,
      stroke: "var(--grid)", "stroke-width": 1 }));
    const tx = svgEl("text", { x: mL - 8, y: yy + 3, "text-anchor": "end" });
    tx.setAttribute("class", "y-tick"); tx.textContent = t;
    svg.appendChild(tx);
  }
  // axis title
  const at = svgEl("text", { x: 12, y: mT + plotH / 2,
    "text-anchor": "middle", transform: "rotate(-90 12 " + (mT + plotH / 2) + ")" });
  at.textContent = "Final used %"; svg.appendChild(at);
  // baseline
  svg.appendChild(svgEl("line", { x1: mL, y1: y(0), x2: mL + plotW, y2: y(0),
    stroke: "var(--axis)", "stroke-width": 1 }));

  const barW = Math.min(24, slot - 6);
  const step = Math.max(1, Math.ceil(n / 8));
  data.forEach((r, i) => {
    const cx = mL + slot * (i + 0.5);
    const x = cx - barW / 2;
    const v = Math.max(0, Math.min(100, Number(r.final_used_pct) || 0));
    const top = y(v), h = y(0) - top;
    const p = svgEl("path", { d: barPath(x, top, barW, h, 4),
      "stroke-width": r.ongoing ? 1.5 : 0 });
    p.setAttribute("class", "bar");
    p.style.fill = provVar(r.provider);
    if (r.ongoing) {
      p.style.fillOpacity = "0.35";
      p.style.stroke = provVar(r.provider);
      p.style.strokeDasharray = "4 3";
    }
    const title = svgEl("title", {});
    title.textContent = r.account + " · " + r.provider + " · " + r.window_kind +
      "\\n" + pct(r.final_used_pct) + "% used · " + r.reset_cause +
      "\\n" + (r.ongoing ? "ongoing (current week)" : "closed " + (r.window_end_label || ""));
    p.appendChild(title);
    svg.appendChild(p);
    // sparse rotated time labels
    if (i % step === 0 || i === n - 1) {
      // ongoing rows have no close time ("ongoing" would slice to "ng")
      const lab = r.ongoing ? "" : (r.window_end_label || "").slice(5, 16); // MM-DD HH:MM
      if (lab) {
        const tx = svgEl("text", { x: cx, y: mT + plotH + 14,
          "text-anchor": "end",
          transform: "rotate(-35 " + cx + " " + (mT + plotH + 14) + ")" });
        tx.textContent = lab; svg.appendChild(tx);
      }
    }
  });
}

// ---- table --------------------------------------------------------------
const COLS = [
  { key: "provider", label: "Provider", num: false },
  { key: "account", label: "Account", num: false },
  { key: "window_kind", label: "Window", num: false },
  { key: "window_end", label: "Closed at", num: false,
    display: r => r.ongoing ? "— ongoing —" : r.window_end_label },
  { key: "final_used_pct", label: "Final used %", num: true, display: r => pct(r.final_used_pct) },
  { key: "reset_cause", label: "Cause", num: false, badge: true },
  { key: "staleness_s", label: "Staleness (s)", num: true,
    display: r => r.staleness_s == null ? "" : String(r.staleness_s) },
];
let sortState = { key: "window_end", dir: 1 };

function renderTable(rows) {
  const table = document.getElementById("table");
  const tableEmpty = document.getElementById("table-empty");
  table.textContent = "";
  if (!rows.length) { tableEmpty.hidden = false; return; }
  tableEmpty.hidden = true;

  const thead = document.createElement("thead");
  const htr = document.createElement("tr");
  for (const c of COLS) {
    const th = document.createElement("th");
    th.textContent = c.label + " ";
    if (c.num) th.style.textAlign = "right";
    if (sortState.key === c.key) {
      const a = document.createElement("span");
      a.className = "arrow";
      a.textContent = sortState.dir > 0 ? "\\u25B2" : "\\u25BC";
      th.appendChild(a);
    }
    th.addEventListener("click", () => {
      if (sortState.key === c.key) sortState.dir *= -1;
      else sortState = { key: c.key, dir: 1 };
      render();
    });
    htr.appendChild(th);
  }
  thead.appendChild(htr); table.appendChild(thead);

  const sorted = rows.slice().sort((a, b) => {
    let x = a[sortState.key], y = b[sortState.key];
    if (x == null) x = ""; if (y == null) y = "";
    if (typeof x === "number" && typeof y === "number") return (x - y) * sortState.dir;
    return String(x).localeCompare(String(y)) * sortState.dir;
  });

  const tbody = document.createElement("tbody");
  for (const r of sorted) {
    const tr = document.createElement("tr");
    if (r.ongoing) tr.style.background = "color-mix(in srgb, var(--cause-ongoing) 8%, var(--surface))";
    for (const c of COLS) {
      const td = document.createElement("td");
      if (c.num) td.className = "num";
      if (c.badge) td.appendChild(causeBadge(r[c.key]));
      else td.textContent = c.display ? c.display(r) : (r[c.key] == null ? "" : String(r[c.key]));
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
}

// ---- coupon ledger ------------------------------------------------------
function couponBadge(state) {
  const b = document.createElement("span");
  b.className = "badge";
  const dot = document.createElement("span");
  dot.className = "dot";
  dot.style.background = couponVar(state);
  const lab = document.createElement("span");
  lab.textContent = state;
  b.append(dot, lab);
  return b;
}
const COUPON_COLS = [
  { key: "account", label: "Account", num: false },
  { key: "title", label: "Title", num: false },
  { key: "granted_at", label: "Granted", num: false,
    display: r => (r.granted_at || "").slice(0, 10) },
  { key: "expires_at", label: "Expires", num: false,
    display: r => (r.expires_at || "").slice(0, 10) },
  { key: "first_seen_at", label: "First seen", num: false,
    display: r => (r.first_seen_at || "").slice(5, 16) },
  { key: "last_seen_at", label: "Last seen", num: false,
    display: r => (r.last_seen_at || "").slice(5, 16) },
  { key: "final_state", label: "Final state", num: false, badge: true },
  { key: "redeemed_at", label: "Redeemed at", num: false,
    display: r => (r.redeemed_at || "").slice(5, 16) },
  { key: "description", label: "Reason", num: false },
];
let couponSort = { key: "last_seen_at", dir: -1 };
function renderCouponTable() {
  const card = document.getElementById("coupon-card");
  const table = document.getElementById("coupon-table");
  const empty = document.getElementById("coupon-empty");
  const summary = document.getElementById("coupon-summary");
  if (!COUPONS.length) {
    card.hidden = true; empty.hidden = false; return;
  }
  card.hidden = false; empty.hidden = true;
  const counts = {};
  for (const r of COUPONS) counts[r.final_state] = (counts[r.final_state] || 0) + 1;
  summary.textContent = COUPON_STATES.filter(s => counts[s])
    .map(s => counts[s] + " " + s).join(" · ");

  const thead = document.createElement("thead");
  const htr = document.createElement("tr");
  for (const c of COUPON_COLS) {
    const th = document.createElement("th");
    th.textContent = c.label + " ";
    if (couponSort.key === c.key) {
      const a = document.createElement("span");
      a.className = "arrow";
      a.textContent = couponSort.dir > 0 ? "\\u25B2" : "\\u25BC";
      th.appendChild(a);
    }
    th.addEventListener("click", () => {
      if (couponSort.key === c.key) couponSort.dir *= -1;
      else couponSort = { key: c.key, dir: 1 };
      renderCouponTable();
    });
    htr.appendChild(th);
  }
  thead.appendChild(htr); table.textContent = "";
  table.appendChild(thead);

  const sorted = COUPONS.slice().sort((a, b) => {
    let x = a[couponSort.key], y = b[couponSort.key];
    if (x == null) x = ""; if (y == null) y = "";
    return String(x).localeCompare(String(y)) * couponSort.dir;
  });
  const tbody = document.createElement("tbody");
  for (const r of sorted) {
    const tr = document.createElement("tr");
    for (const c of COUPON_COLS) {
      const td = document.createElement("td");
      if (c.badge) td.appendChild(couponBadge(r[c.key]));
      else td.textContent = c.display ? c.display(r) : (r[c.key] == null ? "" : String(r[c.key]));
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
}

// ---- init ---------------------------------------------------------------
function render() {
  const rows = visibleRows();
  renderLegend(rows);
  renderChart(rows);
  renderTable(rows);
}

function init() {
  if (!ROWS.length && !COUPONS.length) {
    document.getElementById("app").hidden = true;
    document.getElementById("empty").hidden = false;
    return;
  }
  document.getElementById("empty").hidden = true;
  if (ROWS.length) {
    fillSelect("f-provider", uniq("provider"), "All providers");
    fillSelect("f-account", uniq("account"), "All accounts");
    fillSelect("f-kind", uniq("window_kind"), "All windows");
    render();
  } else {
    document.getElementById("chart-empty").hidden = true;
  }
  renderCouponTable();
  let t;
  window.addEventListener("resize", () => {
    clearTimeout(t); t = setTimeout(() => renderChart(visibleRows()), 120);
  });
}
init();
</script>
</body>
</html>"""
