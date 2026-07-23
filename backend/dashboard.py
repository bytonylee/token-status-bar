"""Self-contained window-history dashboard (history/dashboard.html).

Static file: inline CSS/JS, data embedded as a JSON blob, no CDN — works
offline from file://. Regenerated only when window_history gains rows or on
demand via `pool.py dashboard [--open]`, never on ordinary poll ticks.
"""
from __future__ import annotations
import datetime
import json
import reset_announcements
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


def _coupon_status(row: dict, now: datetime.datetime | None = None) -> str:
    """Normalize ledger states and expire stale available rows at render time."""
    state = row.get("final_state") or "available"
    if state == "expired_unused":
        return "expired"
    if state != "available":
        return state
    expires_at = row.get("expires_at")
    if not expires_at:
        return state
    try:
        expires = datetime.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return state
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return "expired" if expires <= now.astimezone(datetime.timezone.utc) else state


def coupon_data(conn, now: datetime.datetime | None = None) -> list[dict]:
    """Codex reset-credit ledger: every credit ever seen, with its final state."""
    rows = []
    accounts = {a["id"]: a for a in store.list_accounts(conn)}
    for r in store.list_credit_history(conn, provider="codex"):
        account = accounts.get(r["account_id"], {})
        snap = store.latest_snapshot(conn, r["account_id"])
        description = r["description"] or ""
        credit_type = "referral" if "invit" in description.lower() else "usage reward"
        rows.append({
            "provider": r["provider"],
            "account": f"#{r['account_id']} " + (
                r["email"] or r["label"] or f"account {r['account_id']}"
            ),
            "plan": (snap.get("plan") if snap else None) or account.get("plan") or "",
            "credit_id": r["credit_id"],
            "title": r["title"] or "",
            "credit_type": credit_type,
            "description": description or r["title"] or "",
            "granted_at": r["granted_at"] or "",
            "expires_at": r["expires_at"] or "",
            "first_seen_at": r["first_seen_at"],
            "last_seen_at": r["last_seen_at"],
            "final_state": r["final_state"] or "available",
            "status": _coupon_status(r, now=now),
            "final_seen_at": r["final_seen_at"] or None,
            "redeemed_at": r["redeemed_at"] or None,
        })
    rows.sort(key=lambda row: (row["granted_at"], row["account"], row["credit_id"]))
    for number, row in enumerate(rows, start=1):
        row["number"] = number
    return rows


def account_data(conn) -> list[dict]:
    """Codex subscription rows for the account table, kept in raw UTC/ISO."""
    rows = []
    for account in store.list_accounts(conn):
        if account["provider"] != "codex":
            continue
        snap = store.latest_snapshot(conn, account["id"])
        meta = store.get_subscription_meta(conn, account["id"]) or {}
        active = meta.get("has_active_subscription")
        rows.append({
            "account_id": account["id"],
            "email": account["email"] or account["label"] or f"#{account['id']}",
            "plan": (snap.get("plan") if snap else None) or account["plan"] or "",
            "paid_since": meta.get("paid_since") or "",
            "renews_at": meta.get("renews_at") or meta.get("expires_at") or "",
            "auto_renew": "yes" if active and meta.get("renews_at") else (
                "no" if active is not None else ""
            ),
            "account_created_at": meta.get("account_created_at") or "",
        })
    return rows


def generate(conn):
    """Write history/dashboard.html and return its path."""
    window_history.ensure_history_dir()
    path = HISTORY_DIR / "dashboard.html"
    data = json.dumps(dashboard_data(conn)).replace("</", "<\\/")
    coupons = json.dumps(coupon_data(conn)).replace("</", "<\\/")
    accounts = json.dumps(account_data(conn)).replace("</", "<\\/")
    banked_rows = [
        {**row, "number": index}
        for index, row in enumerate(reset_announcements.BANKED_ISSUANCES, start=1)
    ]
    reset_rows = [
        {**row, "number": index}
        for index, row in enumerate(reset_announcements.RESET_POSTS, start=1)
    ]
    banked = json.dumps(banked_rows).replace("</", "<\\/")
    reset_posts = json.dumps(reset_rows).replace("</", "<\\/")
    reset_notes = json.dumps(reset_announcements.RESET_NOTES).replace("</", "<\\/")
    html = _HTML_TEMPLATE.replace("/*__DATA__*/[]", data)
    html = html.replace("/*__COUPONS__*/[]", coupons)
    html = html.replace("/*__ACCOUNTS__*/[]", accounts)
    html = html.replace("/*__BANKED__*/[]", banked)
    html = html.replace("/*__RESET_POSTS__*/[]", reset_posts)
    html = html.replace("/*__RESET_NOTES__*/[]", reset_notes)
    html = html.replace("__ARCHIVE_AS_OF__", reset_announcements.AS_OF_UTC)
    html = html.replace("__POLICY_URL__", reset_announcements.POLICY_URL)
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
<title>Codex accounts and reset history</title>
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
    --coupon-expired:          #e34948;
    --coupon-gone:             #898781;
    --accent-blue:             #087bc1;
    --accent-green:            #008f4c;
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
      --coupon-expired:          #e66767;
      --coupon-gone:             #898781;
      --cause-ongoing:        #8a8af0;
      --accent-blue:          #39aaf0;
      --accent-green:         #35d07f;
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
  a { color: var(--accent-blue); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px; margin-bottom: 20px;
  }
  h2 { font-size: 14px; font-weight: 600; margin: 0 0 2px; }
  h2.accounts-title { color: var(--accent-blue); }
  h2.reset-title { color: var(--accent-green); }
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
  .data-table { font-family: ui-monospace, "SFMono-Regular", Menlo, monospace; }
  .data-table td.wrap { min-width: 320px; white-space: normal; }
  .data-table td.source { text-align: center; }
  .policy-note { color: var(--secondary); font-size: 12px; margin: 10px 0 0; }
  .policy-note strong { color: var(--primary); }
  .table-wrap { overflow-x: auto; }
  .empty { color: var(--secondary); padding: 40px 8px; text-align: center; }
  .empty code { background: var(--plane); border: 1px solid var(--border);
    border-radius: 4px; padding: 1px 5px; font-size: 12px; }
  .note { color: var(--muted); padding: 24px 8px; text-align: center; }
  [hidden] { display: none !important; }
</style>
</head>
<body>
<h1>Codex accounts and reset history (UTC)</h1>
<p class="sub">Local subscription and coupon data, plus a source-linked public reset archive checked through __ARCHIVE_AS_OF__ UTC.</p>

<div id="app">
  <section class="card" id="account-card">
    <h2 class="accounts-title">Subscriptions</h2>
    <p class="card-sub">Current Codex account metadata. All dates below are rendered in UTC.</p>
    <div class="table-wrap"><table class="data-table" id="account-table"></table></div>
  </section>

  <section class="card" id="coupon-card">
    <h2 class="reset-title">Reset coupon issuance history</h2>
    <p class="card-sub">Every banked reset credit observed locally, including expired coupons. <span id="coupon-summary"></span></p>
    <div class="table-wrap"><table class="data-table" id="coupon-table"></table></div>
    <p class="note" id="coupon-empty" hidden>No reset credits archived yet.</p>
  </section>

  <section class="card">
    <h2 class="reset-title">Banked reset issuance history</h2>
    <p class="card-sub">Public launches, compensation grants, and milestone grants. This is an announcement archive, not proof that every account received each grant.</p>
    <div class="table-wrap"><table class="data-table" id="banked-table"></table></div>
    <p class="policy-note"><strong>Policy:</strong> banked referral resets normally expire 30 days after grant unless the offer says otherwise. <a href="__POLICY_URL__" target="_blank" rel="noreferrer">OpenAI terms</a></p>
  </section>

  <section class="card">
    <h2 class="reset-title">Full reset and reset-related posts</h2>
    <p class="card-sub"><span id="reset-post-summary"></span> Includes completed resets, banked grants, and explicit reset announcements; original posts are linked per row.</p>
    <div class="table-wrap"><table class="data-table" id="reset-post-table"></table></div>
  </section>

  <section class="card">
    <h2>Interpretation notes</h2>
    <p class="card-sub">Important limits when comparing public announcements with what an individual account received.</p>
    <div class="table-wrap"><table id="reset-note-table"></table></div>
  </section>

  <div class="filters" id="window-filters">
    <label>Provider<select id="f-provider"></select></label>
    <label>Account<select id="f-account"></select></label>
    <label>Window<select id="f-kind"></select></label>
  </div>

  <section class="card" id="window-chart-card">
    <h2>Usage at window close</h2>
    <p class="card-sub">One bar per closed window, ordered by close time. Height is final used %.</p>
    <div class="legend" id="legend"></div>
    <div class="plot-wrap"><svg id="chart"></svg></div>
    <p class="note" id="chart-empty" hidden>No windows match the current filters.</p>
  </section>

  <section class="card" id="window-table-card">
    <div class="table-wrap"><table id="table"></table></div>
    <p class="note" id="table-empty" hidden>No windows match the current filters.</p>
  </section>
</div>

<script>
const ROWS = /*__DATA__*/[];

const COUPONS = /*__COUPONS__*/[];
const ACCOUNTS = /*__ACCOUNTS__*/[];
const BANKED = /*__BANKED__*/[];
const RESET_POSTS = /*__RESET_POSTS__*/[];
const RESET_NOTES = /*__RESET_NOTES__*/[];
const COUPON_STATES = ["available", "redeemed", "expired", "gone"];
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
function utcStamp(raw) {
  if (raw == null || raw === "") return "";
  let date;
  if (typeof raw === "number") {
    date = new Date(raw * 1000);
  } else {
    let value = String(raw);
    if (/^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}(:\\d{2})?$/.test(value)) {
      value = value.replace(" ", "T") + (value.length === 16 ? ":00Z" : "Z");
    }
    date = new Date(value);
  }
  if (Number.isNaN(date.getTime())) return String(raw);
  return date.toISOString().replace("T", " ").slice(0, 19);
}

const tableSorts = {};
function renderDataTable(id, rows, columns, defaultSort) {
  const table = document.getElementById(id);
  if (!table) return;
  if (!tableSorts[id]) tableSorts[id] = { ...defaultSort };
  const state = tableSorts[id];
  table.textContent = "";

  const thead = document.createElement("thead");
  const htr = document.createElement("tr");
  for (const col of columns) {
    const th = document.createElement("th");
    th.textContent = col.label + " ";
    if (col.num) th.style.textAlign = "right";
    if (state.key === col.key) {
      const arrow = document.createElement("span");
      arrow.className = "arrow";
      arrow.textContent = state.dir > 0 ? "\\u25B2" : "\\u25BC";
      th.appendChild(arrow);
    }
    th.addEventListener("click", () => {
      if (state.key === col.key) state.dir *= -1;
      else { state.key = col.key; state.dir = 1; }
      renderDataTable(id, rows, columns, defaultSort);
    });
    htr.appendChild(th);
  }
  thead.appendChild(htr);
  table.appendChild(thead);

  const sorted = rows.slice().sort((a, b) => {
    let x = a[state.key], y = b[state.key];
    if (x == null) x = ""; if (y == null) y = "";
    if (typeof x === "number" && typeof y === "number") return (x - y) * state.dir;
    return String(x).localeCompare(String(y)) * state.dir;
  });
  const tbody = document.createElement("tbody");
  for (const row of sorted) {
    const tr = document.createElement("tr");
    for (const col of columns) {
      const td = document.createElement("td");
      if (col.num) td.classList.add("num");
      if (col.wrap) td.classList.add("wrap");
      if (col.source) {
        td.classList.add("source");
        const link = document.createElement("a");
        link.href = row[col.key];
        link.target = "_blank";
        link.rel = "noreferrer";
        link.textContent = "open";
        link.setAttribute("aria-label", "Open original source");
        td.appendChild(link);
      } else {
        const value = col.display ? col.display(row) : row[col.key];
        td.textContent = value == null ? "" : String(value);
      }
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
}

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

// ---- account and public reset archive -----------------------------------
const ACCOUNT_COLS = [
  { key: "account_id", label: "#", num: true },
  { key: "email", label: "Email" },
  { key: "plan", label: "Plan" },
  { key: "paid_since", label: "Paid since (UTC)", display: r => utcStamp(r.paid_since) },
  { key: "renews_at", label: "Renews (UTC)", display: r => utcStamp(r.renews_at) },
  { key: "auto_renew", label: "Auto-renew" },
  { key: "account_created_at", label: "Account created (UTC)",
    display: r => utcStamp(r.account_created_at) },
];
const ANNOUNCEMENT_COLS = [
  { key: "number", label: "#", num: true },
  { key: "posted_at_utc", label: "Issued at (UTC)" },
  { key: "kind", label: "Type" },
  { key: "audience", label: "Audience" },
  { key: "summary", label: "Reason and details", wrap: true },
  { key: "source_url", label: "Source", source: true },
];
const RESET_POST_COLS = [
  { key: "number", label: "#", num: true },
  { key: "posted_at_utc", label: "Posted at (UTC)" },
  { key: "kind", label: "Type" },
  { key: "audience", label: "Audience" },
  { key: "summary", label: "Announcement and cause", wrap: true },
  { key: "source_url", label: "Source", source: true },
];
const NOTE_COLS = [
  { key: "topic", label: "Topic" },
  { key: "detail", label: "What the evidence supports", wrap: true },
];

function renderArchiveTables() {
  const accountCard = document.getElementById("account-card");
  accountCard.hidden = !ACCOUNTS.length;
  if (ACCOUNTS.length) {
    renderDataTable("account-table", ACCOUNTS, ACCOUNT_COLS,
      { key: "account_id", dir: 1 });
  }
  renderDataTable("banked-table", BANKED, ANNOUNCEMENT_COLS,
    { key: "posted_at_utc", dir: -1 });
  renderDataTable("reset-post-table", RESET_POSTS, RESET_POST_COLS,
    { key: "posted_at_utc", dir: -1 });
  renderDataTable("reset-note-table", RESET_NOTES, NOTE_COLS,
    { key: "topic", dir: 1 });
  document.getElementById("reset-post-summary").textContent =
    RESET_POSTS.length + " source-linked posts through " +
    (RESET_POSTS.length ? RESET_POSTS[RESET_POSTS.length - 1].posted_at_utc : "n/a") + " UTC.";
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
  { key: "number", label: "#", num: true },
  { key: "account", label: "Account", num: false },
  { key: "plan", label: "Plan", num: false },
  { key: "granted_at", label: "Granted at (UTC)", num: false,
    display: r => utcStamp(r.granted_at) },
  { key: "credit_type", label: "Type", num: false },
  { key: "description", label: "Reason / invited", num: false, wrap: true },
  { key: "expires_at", label: "Expires at (UTC)", num: false,
    display: r => utcStamp(r.expires_at) },
  { key: "status", label: "Status", num: false, badge: true },
];
let couponSort = { key: "granted_at", dir: 1 };
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
  for (const r of COUPONS) counts[r.status] = (counts[r.status] || 0) + 1;
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
      if (c.wrap) td.classList.add("wrap");
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
  renderArchiveTables();
  if (ROWS.length) {
    fillSelect("f-provider", uniq("provider"), "All providers");
    fillSelect("f-account", uniq("account"), "All accounts");
    fillSelect("f-kind", uniq("window_kind"), "All windows");
    render();
  } else {
    document.getElementById("window-filters").hidden = true;
    document.getElementById("window-chart-card").hidden = true;
    document.getElementById("window-table-card").hidden = true;
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
