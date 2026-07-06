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
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
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
    ]:
        if col not in scols:
            conn.execute(f"ALTER TABLE limit_snapshots ADD COLUMN {col} {decl}")
    return conn


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
              "monthly_period_start","monthly_period_end")
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
