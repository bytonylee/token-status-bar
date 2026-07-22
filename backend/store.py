"""SQLite store for agent-pool: accounts, tokens, limit snapshots, refresh log."""
from __future__ import annotations
import json, sqlite3, time, os
from pathlib import Path
from typing import Any

DB_PATH = Path(os.environ.get("AGENT_POOL_DB", str(Path.home() / "solo/token-status-bar" / "secrets" / "pool.db")))

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,          -- codex|claude|xai|antigravity|devin|copilot
    email TEXT,                      -- account email (nullable for copilot which uses github login)
    label TEXT,                      -- human label e.g. "Codex #1"
    plan TEXT,                       -- pro/plus/team/free etc
    account_id TEXT,                 -- upstream account id (chatgpt account id, github user id, etc)
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    disabled INTEGER DEFAULT 0,
    UNIQUE(provider, email)
);

CREATE TABLE IF NOT EXISTS tokens (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    access_token TEXT,
    refresh_token TEXT,
    id_token TEXT,
    expires_at REAL,                 -- unix epoch seconds
    last_refresh REAL,
    raw_json TEXT                    -- full token payload for provider-specific fields
);

CREATE TABLE IF NOT EXISTS limit_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    status TEXT,                     -- active|error|expired|rate_limited
    status_message TEXT,
    plan TEXT,
    -- codex wham
    primary_used_pct REAL,
    primary_reset_at REAL,
    primary_window_s INTEGER,
    secondary_used_pct REAL,
    secondary_reset_at REAL,
    secondary_window_s INTEGER,
    credits_balance REAL,
    banked_resets INTEGER,
    -- generic rate-limit headers (claude/xai/google/copilot/devin)
    rate_limit_remaining TEXT,
    rate_limit_reset TEXT,
    rate_limit_limit TEXT,
    -- copilot sku / limited-user quotas
    sku TEXT,
    limited_user_quotas TEXT,
    limited_user_reset_date TEXT,
    -- devin daily/weekly quota percent + billing cycle reset
    daily_quota_remaining_percent REAL,
    weekly_quota_remaining_percent REAL,
    plan_reset_unix REAL,
    -- xai monthly billing window
    monthly_used REAL,
    monthly_limit REAL,
    monthly_used_pct REAL,
    monthly_period_start TEXT,
    monthly_period_end TEXT,
    -- raw payload
    raw_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_snap_ts ON limit_snapshots(account_id, ts DESC);

CREATE TABLE IF NOT EXISTS refresh_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    kind TEXT,                       -- token_refresh|limit_poll|onboard|error
    success INTEGER,
    message TEXT
);

CREATE TABLE IF NOT EXISTS reset_credits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    credit_id TEXT,
    title TEXT,
    status TEXT,
    expires_at TEXT,
    granted_at TEXT,
    description TEXT,
    fetched_at REAL NOT NULL,
    UNIQUE(account_id, credit_id)
);

CREATE TABLE IF NOT EXISTS subscription_meta (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    paid_since TEXT,
    renews_at TEXT,
    expires_at TEXT,
    account_created_at TEXT,
    subscription_plan TEXT,
    has_active_subscription INTEGER,
    is_active_subscription_gratis INTEGER,
    has_previously_paid_subscription INTEGER,
    previous_paid_months INTEGER,
    billing_note TEXT,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS window_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    window_kind TEXT NOT NULL,      -- 5h|weekly|weekly_fable|daily|monthly|monthly_premium|monthly_chat
    window_start REAL,              -- unix; reset_at - window_s when known
    window_end REAL NOT NULL,       -- boundary the window closed at
    final_used_pct REAL NOT NULL,   -- last successful reading before close
    final_snapshot_ts REAL NOT NULL,-- ts of the snapshot supplying it
    reset_cause TEXT NOT NULL,      -- natural|coupon|provider_reset|unknown
    details TEXT,                   -- JSON: staleness_s, credit_id, raw values
    created_at REAL NOT NULL,
    UNIQUE(account_id, window_kind, window_end)
);

CREATE TABLE IF NOT EXISTS reset_credit_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    credit_id TEXT NOT NULL,
    title TEXT,
    description TEXT,
    granted_at TEXT,
    expires_at TEXT,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    final_state TEXT,               -- available|redeemed|expired_unused|gone
    final_seen_at REAL,             -- ts of the last poll that still observed it
    redeemed_at REAL,               -- ts a redeem was detected (if any)
    UNIQUE(account_id, credit_id)
);

CREATE TABLE IF NOT EXISTS live_activity (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lifecycle_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    account_id INTEGER,             -- no FK: the audit trail outlives accounts
    event TEXT NOT NULL,            -- window_reset|sub_paid|sub_expired|sub_renewed
                                    -- |quota_exhausted|quota_recovered|account_swapped (§3.2)
    detail TEXT                     -- JSON: event-specific context
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(DB_PATH.parent, 0o700)
    except OSError:
        pass
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    # migrate: add granted_at / description if missing (older DBs)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(reset_credits)")}
    if "granted_at" not in cols:
        conn.execute("ALTER TABLE reset_credits ADD COLUMN granted_at TEXT")
    if "description" not in cols:
        conn.execute("ALTER TABLE reset_credits ADD COLUMN description TEXT")
    # migrate: add tier_override to accounts if missing
    acols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)")}
    if "tier_override" not in acols:
        conn.execute("ALTER TABLE accounts ADD COLUMN tier_override TEXT")
    # migrate: add limit_snapshots columns added after the original schema
    scols = {r["name"] for r in conn.execute("PRAGMA table_info(limit_snapshots)")}
    for col, decl in [
        ("sku", "TEXT"),
        ("limited_user_quotas", "TEXT"),
        ("limited_user_reset_date", "TEXT"),
        ("daily_quota_remaining_percent", "REAL"),
        ("weekly_quota_remaining_percent", "REAL"),
        ("plan_reset_unix", "REAL"),
        ("monthly_used", "REAL"),
        ("monthly_limit", "REAL"),
        ("monthly_used_pct", "REAL"),
        ("monthly_period_start", "TEXT"),
        ("monthly_period_end", "TEXT"),
        ("source", "TEXT"),
    ]:
        if col not in scols:
            conn.execute(f"ALTER TABLE limit_snapshots ADD COLUMN {col} {decl}")
    # migrate: add Codex subscription metadata fields surfaced by
    # /backend-api/accounts/check.
    mcols = {r["name"] for r in conn.execute("PRAGMA table_info(subscription_meta)")}
    for col, decl in [
        ("expires_at", "TEXT"),
        ("subscription_plan", "TEXT"),
        ("has_active_subscription", "INTEGER"),
        ("is_active_subscription_gratis", "INTEGER"),
        ("has_previously_paid_subscription", "INTEGER"),
    ]:
        if col not in mcols:
            conn.execute(f"ALTER TABLE subscription_meta ADD COLUMN {col} {decl}")
    conn.commit()
    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass
    return conn


SNAPSHOT_RETENTION_DAYS = 90
REFRESH_LOG_RETENTION_DAYS = 30


def prune_old_rows(conn, snapshot_days=SNAPSHOT_RETENTION_DAYS,
                   log_days=REFRESH_LOG_RETENTION_DAYS) -> tuple[int, int]:
    """Delete old limit_snapshots / refresh_log rows. Returns rows deleted."""
    snap_cut = now() - snapshot_days * 86400
    log_cut = now() - log_days * 86400
    n_snap = conn.execute("DELETE FROM limit_snapshots WHERE ts < ?", (snap_cut,)).rowcount
    n_log = conn.execute("DELETE FROM refresh_log WHERE ts < ?", (log_cut,)).rowcount
    conn.commit()
    return n_snap, n_log


def now() -> float:
    return time.time()


# ─── accounts ──────────────────────────────────────────────────────────────
def upsert_account(conn, provider, email, label=None, plan=None, account_id=None) -> int:
    cur = conn.execute(
        "INSERT INTO accounts(provider,email,label,plan,account_id,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(provider,email) DO UPDATE SET "
        "label=excluded.label, plan=excluded.plan, account_id=excluded.account_id, updated_at=excluded.updated_at",
        (provider, email, label, plan, account_id, now(), now()),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM accounts WHERE provider=? AND email=?", (provider, email)).fetchone()
    return row["id"]


def list_accounts(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM accounts WHERE disabled=0 ORDER BY provider, label").fetchall()
    return [dict(r) for r in rows]


def get_account(conn, account_id) -> dict | None:
    r = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    return dict(r) if r else None


def get_account_by_provider_email(conn, provider, email) -> dict | None:
    r = conn.execute(
        "SELECT * FROM accounts WHERE provider=? AND email=?",
        (provider, email),
    ).fetchone()
    return dict(r) if r else None


def update_account(conn, account_id, email, plan=None, upstream_account_id=None):
    conn.execute(
        "UPDATE accounts SET email=?, plan=?, account_id=?, updated_at=? WHERE id=?",
        (email, plan, upstream_account_id, now(), account_id),
    )
    conn.commit()


def set_tier(conn, account_id, tier):
    conn.execute("UPDATE accounts SET tier_override=?, updated_at=? WHERE id=?",
                 (tier, now(), account_id))
    conn.commit()


def delete_account(conn, account_id):
    conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
    conn.commit()


# ─── tokens ────────────────────────────────────────────────────────────────
def save_token(conn, account_id, access_token, refresh_token, id_token, expires_at, raw_json=None):
    conn.execute(
        "INSERT INTO tokens(account_id,access_token,refresh_token,id_token,expires_at,last_refresh,raw_json) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
        "access_token=excluded.access_token, refresh_token=excluded.refresh_token, "
        "id_token=excluded.id_token, expires_at=excluded.expires_at, "
        "last_refresh=excluded.last_refresh, raw_json=excluded.raw_json",
        (account_id, access_token, refresh_token, id_token, expires_at, now(),
         json.dumps(raw_json) if raw_json else None),
    )
    conn.commit()


def get_token(conn, account_id) -> dict | None:
    r = conn.execute("SELECT * FROM tokens WHERE account_id=?", (account_id,)).fetchone()
    return dict(r) if r else None


# ─── limit snapshots ───────────────────────────────────────────────────────
def save_snapshot(conn, account_id, snap: dict):
    fields = ("status","status_message","plan","primary_used_pct","primary_reset_at",
              "primary_window_s","secondary_used_pct","secondary_reset_at","secondary_window_s",
              "credits_balance","banked_resets","rate_limit_remaining","rate_limit_reset",
              "rate_limit_limit","raw_json",
              "sku","limited_user_quotas","limited_user_reset_date",
              "daily_quota_remaining_percent","weekly_quota_remaining_percent","plan_reset_unix",
              "monthly_used","monthly_limit","monthly_used_pct",
              "monthly_period_start","monthly_period_end","source")
    vals = [snap.get(f) for f in fields]
    conn.execute(
        f"INSERT INTO limit_snapshots(account_id,ts,{','.join(fields)}) VALUES(?,?,{','.join('?'*len(fields))})",
        (account_id, now(), *vals),
    )
    conn.commit()


def latest_snapshot(conn, account_id) -> dict | None:
    r = conn.execute(
        "SELECT * FROM limit_snapshots WHERE account_id=? ORDER BY ts DESC LIMIT 1", (account_id,)
    ).fetchone()
    return dict(r) if r else None


def latest_successful_snapshot(conn, account_id) -> dict | None:
    """Newest snapshot whose status marks a real reading (active/rate_limited)."""
    r = conn.execute(
        "SELECT * FROM limit_snapshots WHERE account_id=? AND status IN ('active','rate_limited') "
        "ORDER BY ts DESC LIMIT 1", (account_id,)
    ).fetchone()
    return dict(r) if r else None


def iter_snapshots(conn, account_id):
    """Stream one account's snapshots oldest-first (constant memory)."""
    cur = conn.execute(
        "SELECT * FROM limit_snapshots WHERE account_id=? ORDER BY ts ASC", (account_id,))
    for r in cur:
        yield dict(r)


def snapshots_since(conn, account_id, since_ts) -> list[dict]:
    """Successful snapshots newer than since_ts, oldest first."""
    rows = conn.execute(
        "SELECT * FROM limit_snapshots WHERE account_id=? AND ts>? "
        "AND status IN ('active','rate_limited') ORDER BY ts ASC",
        (account_id, since_ts),
    ).fetchall()
    return [dict(r) for r in rows]


# ─── live activity (local session-log sync) ────────────────────────────────
def upsert_live_activity(conn, account_id, payload: dict):
    conn.execute(
        "INSERT INTO live_activity(account_id,ts,payload) VALUES(?,?,?) "
        "ON CONFLICT(account_id) DO UPDATE SET ts=excluded.ts, payload=excluded.payload",
        (account_id, now(), json.dumps(payload)),
    )
    conn.commit()


def get_live_activity(conn, account_id) -> dict | None:
    r = conn.execute("SELECT ts, payload FROM live_activity WHERE account_id=?",
                     (account_id,)).fetchone()
    if not r:
        return None
    try:
        out = json.loads(r["payload"])
    except Exception:
        return None
    out["ts"] = r["ts"]
    return out


# ─── window history ────────────────────────────────────────────────────────
def save_window_history(conn, account_id, window_kind, window_start, window_end,
                        final_used_pct, final_snapshot_ts, reset_cause, details=None) -> bool:
    """INSERT OR IGNORE one closed-window record. True when a row was added."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO window_history(account_id,window_kind,window_start,window_end,"
        "final_used_pct,final_snapshot_ts,reset_cause,details,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (account_id, window_kind, window_start, window_end, final_used_pct,
         final_snapshot_ts, reset_cause, json.dumps(details) if details else None, now()),
    )
    conn.commit()
    return cur.rowcount == 1


def list_window_history(conn, provider=None, account_id=None) -> list[dict]:
    """Closed windows joined with account identity, oldest close first."""
    q = ("SELECT wh.*, a.provider, a.email, a.label FROM window_history wh "
         "JOIN accounts a ON a.id = wh.account_id")
    conds, args = [], []
    if provider:
        conds.append("a.provider=?")
        args.append(provider)
    if account_id is not None:
        conds.append("wh.account_id=?")
        args.append(account_id)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY wh.window_end"
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def window_history_conflict(conn, account_id, window_kind, lo_ts, hi_ts) -> bool:
    """True when a row for this kind already ends strictly inside (lo_ts, hi_ts)."""
    r = conn.execute(
        "SELECT 1 FROM window_history WHERE account_id=? AND window_kind=? "
        "AND window_end>? AND window_end<? LIMIT 1",
        (account_id, window_kind, lo_ts, hi_ts),
    ).fetchone()
    return r is not None


# ─── reset credits ─────────────────────────────────────────────────────────
def replace_reset_credits(conn, account_id, credits: list[dict], fetched_at=None):
    fetched_at = fetched_at or now()
    conn.execute("DELETE FROM reset_credits WHERE account_id=?", (account_id,))
    for c in credits:
        conn.execute(
            "INSERT INTO reset_credits(account_id,credit_id,title,status,expires_at,granted_at,description,fetched_at) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(account_id, credit_id) DO UPDATE SET "
            "title=excluded.title, status=excluded.status, expires_at=excluded.expires_at, "
            "granted_at=excluded.granted_at, description=excluded.description, fetched_at=excluded.fetched_at",
            (account_id, c.get("id"), c.get("title"), c.get("status"), c.get("expires_at"),
             c.get("granted_at"), c.get("description"), fetched_at),
        )
    conn.commit()


def list_reset_credits(conn, account_id) -> list[dict]:
    rows = conn.execute("SELECT * FROM reset_credits WHERE account_id=? ORDER BY expires_at", (account_id,)).fetchall()
    return [dict(r) for r in rows]


# ─── reset credit history (survives the per-poll wipe) ─────────────────────
def upsert_credit_history(conn, account_id, credits: list[dict], fetched_at=None):
    """Record every credit seen this poll. Credits that already have a final
    state are not reopened — a re-grant of the same credit_id is not expected,
    but if it happens the row keeps the newest metadata and last_seen_at."""
    fetched_at = fetched_at or now()
    for c in credits:
        cid = c.get("id") or c.get("credit_id")
        if not cid:
            continue
        conn.execute(
            "INSERT INTO reset_credit_history(account_id,credit_id,title,description,"
            "granted_at,expires_at,first_seen_at,last_seen_at,final_state) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(account_id, credit_id) DO UPDATE SET "
            "title=excluded.title, description=excluded.description, "
            "granted_at=excluded.granted_at, expires_at=excluded.expires_at, "
            "last_seen_at=excluded.last_seen_at, "
            "final_state=COALESCE(reset_credit_history.final_state, 'available')",
            (account_id, cid, c.get("title"), c.get("description"),
             c.get("granted_at"), c.get("expires_at"), fetched_at, fetched_at, "available"),
        )
    conn.commit()


def mark_credit_final(conn, account_id, credit_id, final_state):
    """Record how a credit disappeared. final_seen_at is pinned to the last
    poll that actually observed the credit (last_seen_at), not the poll that
    noticed it gone. A row already marked redeemed is left untouched."""
    conn.execute(
        "UPDATE reset_credit_history SET final_state=?, final_seen_at=last_seen_at "
        "WHERE account_id=? AND credit_id=? AND final_state IS NOT 'redeemed'",
        (final_state, account_id, credit_id),
    )
    conn.commit()


def mark_credit_redeemed(conn, account_id, credit_id, redeemed_at=None):
    redeemed_at = redeemed_at or now()
    conn.execute(
        "UPDATE reset_credit_history SET final_state='redeemed', redeemed_at=? "
        "WHERE account_id=? AND credit_id=?",
        (redeemed_at, account_id, credit_id),
    )
    conn.commit()


def list_credit_history(conn, provider=None) -> list[dict]:
    q = ("SELECT h.*, a.provider, a.email, a.label FROM reset_credit_history h "
         "JOIN accounts a ON a.id=h.account_id ")
    params = ()
    if provider:
        q += "WHERE a.provider=? "
        params = (provider,)
    q += "ORDER BY h.last_seen_at DESC, h.granted_at"
    return [dict(r) for r in conn.execute(q, params).fetchall()]


# ─── subscription metadata ─────────────────────────────────────────────────
def upsert_subscription_meta(conn, account_id, paid_since=None, renews_at=None,
                             account_created_at=None, previous_paid_months=None,
                             billing_note=None, expires_at=None,
                             subscription_plan=None,
                             has_active_subscription=None,
                             is_active_subscription_gratis=None,
                             has_previously_paid_subscription=None):
    def _bool_int(v):
        if v is None:
            return None
        return 1 if bool(v) else 0

    conn.execute(
        "INSERT INTO subscription_meta(account_id,paid_since,renews_at,expires_at,account_created_at,"
        "subscription_plan,has_active_subscription,is_active_subscription_gratis,"
        "has_previously_paid_subscription,previous_paid_months,billing_note,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(account_id) DO UPDATE SET "
        "paid_since=excluded.paid_since, renews_at=excluded.renews_at, expires_at=excluded.expires_at, "
        "account_created_at=excluded.account_created_at, "
        "subscription_plan=excluded.subscription_plan, "
        "has_active_subscription=excluded.has_active_subscription, "
        "is_active_subscription_gratis=excluded.is_active_subscription_gratis, "
        "has_previously_paid_subscription=excluded.has_previously_paid_subscription, "
        "previous_paid_months=excluded.previous_paid_months, "
        "billing_note=excluded.billing_note, updated_at=excluded.updated_at",
        (account_id, paid_since, renews_at, expires_at, account_created_at,
         subscription_plan, _bool_int(has_active_subscription),
         _bool_int(is_active_subscription_gratis),
         _bool_int(has_previously_paid_subscription),
         previous_paid_months, billing_note, now()),
    )
    conn.commit()


def get_subscription_meta(conn, account_id) -> dict | None:
    r = conn.execute("SELECT * FROM subscription_meta WHERE account_id=?", (account_id,)).fetchone()
    return dict(r) if r else None


# ─── refresh log ───────────────────────────────────────────────────────────
def log_event(conn, account_id, kind, success, message=""):
    conn.execute(
        "INSERT INTO refresh_log(account_id,ts,kind,success,message) VALUES(?,?,?,?,?)",
        (account_id, now(), kind, 1 if success else 0, message),
    )
    conn.commit()


def recent_events(conn, account_id, limit=10) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM refresh_log WHERE account_id=? ORDER BY ts DESC LIMIT ?", (account_id, limit)
    ).fetchall()
    return [dict(r) for r in rows]


# ─── lifecycle events ──────────────────────────────────────────────────────
def save_lifecycle_event(conn, ts, account_id, event, detail=None) -> int:
    """Append one lifecycle transition event (spec §1.4). Returns the row id."""
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail)
    cur = conn.execute(
        "INSERT INTO lifecycle_events(ts,account_id,event,detail) VALUES(?,?,?,?)",
        (ts, account_id, event, detail),
    )
    conn.commit()
    return cur.lastrowid


def list_lifecycle_events(conn, account_id=None, since=None, limit=100) -> list[dict]:
    """Lifecycle events, newest first."""
    q, conds, args = "SELECT * FROM lifecycle_events", [], []
    if account_id is not None:
        conds.append("account_id=?")
        args.append(account_id)
    if since is not None:
        conds.append("ts>=?")
        args.append(since)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY ts DESC, id DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(q, args).fetchall()]
