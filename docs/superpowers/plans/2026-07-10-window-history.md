# Window History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every quota window (5h / weekly / daily / monthly) gets one durable `window_history` record when it closes — natural rollover, coupon redeem, or provider-side reset — exported as CSV/JSONL and visualized by a self-contained HTML dashboard opened from a new menu item.

**Architecture:** A new pure detector (`backend/window_history.py:detect_closed_windows`) compares consecutive successful `limit_snapshots` rows and is shared verbatim by a live poll hook, a redeem-time direct archive, and a one-time backfill. `store.py` gains the `window_history` table (UNIQUE constraint → idempotent `INSERT OR IGNORE`). `poller.py` gains adaptive cadence (hot accounts ≥70% poll at 60s, Claude capped at 180s, pre-reset capture at `reset_at − 300s` with ~60s retries). `backend/dashboard.py` renders `history/dashboard.html` from the table; the Swift menu app gains "Open Dashboard".

**Tech Stack:** Python 3.12 stdlib only (sqlite3, csv, json, zoneinfo), Swift/Cocoa (single-file app, no test target), stdlib unittest for backend tests.

**Spec:** `docs/superpowers/specs/2026-07-10-window-history-design.md`

## Global Constraints

- Backend is Python stdlib only — no new dependencies.
- Backend tests run as plain scripts: `python3 backend/test_<name>.py` (unittest style). They must not touch the real DB or the real history dir — set `AGENT_POOL_DB`, `AGENT_POOL_STATUS_JSON`, and `AGENT_POOL_HISTORY_DIR` env vars to temp paths BEFORE importing backend modules.
- Detection, exports, and dashboard generation must NEVER fail a poll: every hook is wrapped in try/except that logs and continues (same pattern as the existing `status.cmd_export` call in `poller.py`).
- Exports live in `history/` at the project root (`~/solo/token-status-bar/history/`), configurable via `AGENT_POOL_HISTORY_DIR`; `history/` must be gitignored.
- Scheduler knobs are env vars with these exact names/defaults: `HOT_THRESHOLD_PCT=70`, `HOT_INTERVAL_S=60`, `HOT_INTERVAL_CLAUDE_S=180`, `PRERESET_LEAD_S=300`, `PRERESET_RETRY_S=60`.
- Claude polls consume quota (two real 1-token `/v1/messages` probes). Nothing may poll Claude faster than `HOT_INTERVAL_CLAUDE_S` (180s), and no task may add extra Claude probes.
- Swift app is one file (`app/TokenStatusBar.swift`); no test target — Swift verification is `swiftc` compile via `bash build.sh` plus visual check. Every new user-facing string gets entries in all 4 L10n tables (en, ko, zh, ja).
- The poller runs under LaunchAgent `com.tonye.agentpool-poller` (runs `pool.py poll-loop` with the default `secrets/pool.db`): after backend edits, `pkill` the running poller so it respawns with new code. NEVER start a second poller by hand.
- Per AGENTS.md: build the `.dmg` (`bash build.sh --dmg`) and confirm success before any PR; after building, quit the old app (`osascript -e 'tell application "TokenStatusBar" to quit'`) and reopen `/Applications/TokenStatusBar.app`.
- Commit after each task with a Conventional-style message ending in `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- The repo has pre-existing dirty files on `main` (README, oauth.py, store.py, etc.) — `git add` only the paths each task names, never `git add -A`.

---

### Task 1: `window_history` table + store accessors

**Files:**
- Modify: `backend/store.py` (SCHEMA string ~line 108; new accessors after `latest_snapshot` ~line 256)
- Test: `backend/test_window_history.py` (new)

**Interfaces:**
- Produces (all consumed by Tasks 2–7):
  - `store.save_window_history(conn, account_id, window_kind, window_start, window_end, final_used_pct, final_snapshot_ts, reset_cause, details=None) -> bool` — `INSERT OR IGNORE`; True iff a row was added. `details` is a dict (JSON-encoded into the TEXT column).
  - `store.latest_successful_snapshot(conn, account_id) -> dict | None` — newest snapshot with status in (`active`, `rate_limited`).
  - `store.iter_snapshots(conn, account_id)` — generator of snapshot dicts, oldest first, constant memory.
  - `store.list_window_history(conn, provider=None, account_id=None) -> list[dict]` — rows joined with `accounts` (adds `provider`, `email`, `label` keys), ordered by `window_end`.
  - `store.window_history_conflict(conn, account_id, window_kind, lo_ts, hi_ts) -> bool` — True when a row for that kind already has `window_end` strictly inside `(lo_ts, hi_ts)`.

- [ ] **Step 1: Write the failing test**

Create `backend/test_window_history.py`. The `_insert_snap` helper and `BASE` constant are reused by every later task's tests in this file.

```python
#!/usr/bin/env python3
"""Tests for window history: store accessors, detection, archiving, exports."""
from __future__ import annotations
import csv, datetime, json, os, sys, tempfile, unittest

_TMP = tempfile.mkdtemp(prefix="tsb-test-")
os.environ["AGENT_POOL_DB"] = os.path.join(_TMP, "pool.db")
os.environ["AGENT_POOL_STATUS_JSON"] = os.path.join(_TMP, "status.json")
os.environ["AGENT_POOL_HISTORY_DIR"] = os.path.join(_TMP, "history")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

BASE = 1_000_000.0


def _insert_snap(conn, account_id, ts, **fields):
    """Insert a limit_snapshots row with an explicit ts (save_snapshot stamps now())."""
    cols = ["account_id", "ts"] + list(fields)
    conn.execute(
        f"INSERT INTO limit_snapshots({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
        [account_id, ts] + list(fields.values()),
    )
    conn.commit()


class StoreWindowHistoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect()
        self.acct = store.upsert_account(self.conn, "codex", f"wh-{id(self)}@example.com", "wh")

    def test_save_is_idempotent(self):
        kwargs = dict(window_kind="5h", window_start=900.0, window_end=1000.0,
                      final_used_pct=88.0, final_snapshot_ts=990.0,
                      reset_cause="natural", details={"staleness_s": 10.0})
        self.assertTrue(store.save_window_history(self.conn, self.acct, **kwargs))
        self.assertFalse(store.save_window_history(self.conn, self.acct, **kwargs))
        rows = store.list_window_history(self.conn, account_id=self.acct)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "codex")
        self.assertEqual(json.loads(rows[0]["details"]), {"staleness_s": 10.0})

    def test_latest_successful_skips_error_rows(self):
        _insert_snap(self.conn, self.acct, 100.0, status="active", primary_used_pct=40.0)
        _insert_snap(self.conn, self.acct, 200.0, status="error")
        snap = store.latest_successful_snapshot(self.conn, self.acct)
        self.assertEqual(snap["ts"], 100.0)
        self.assertEqual(store.latest_snapshot(self.conn, self.acct)["ts"], 200.0)

    def test_iter_snapshots_oldest_first(self):
        for ts in (300.0, 100.0, 200.0):
            _insert_snap(self.conn, self.acct, ts, status="active")
        self.assertEqual([s["ts"] for s in store.iter_snapshots(self.conn, self.acct)],
                         [100.0, 200.0, 300.0])

    def test_conflict_window_detection(self):
        store.save_window_history(self.conn, self.acct, "5h", None, 150.0, 90.0, 100.0,
                                  "coupon", None)
        self.assertTrue(store.window_history_conflict(self.conn, self.acct, "5h", 100.0, 200.0))
        # strictly-inside: a row ending exactly at a bound does not conflict
        self.assertFalse(store.window_history_conflict(self.conn, self.acct, "5h", 150.0, 200.0))
        self.assertFalse(store.window_history_conflict(self.conn, self.acct, "weekly", 100.0, 200.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 backend/test_window_history.py`
Expected: FAIL/ERROR with `AttributeError: module 'store' has no attribute 'save_window_history'` (the first test may also error on the missing table).

- [ ] **Step 3: Implement in `backend/store.py`**

Append to the `SCHEMA` string (inside the triple-quoted literal, after the `subscription_meta` table, before the closing `"""`):

```sql
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
```

(No PRAGMA migration needed — `CREATE TABLE IF NOT EXISTS` inside `connect()`'s `executescript` covers both fresh and existing DBs, same as every other table.)

Add after `latest_snapshot` (line ~256), before the "reset credits" section:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 backend/test_window_history.py`
Expected: `Ran 4 tests ... OK`

Run: `python3 backend/test_onboarding.py`
Expected: OK (regression — store schema change must not break existing flows).

- [ ] **Step 5: Commit**

```bash
git add backend/store.py backend/test_window_history.py
git commit -m "feat(history): Add window_history table and accessors

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Pure reset detection (`backend/window_history.py`)

**Files:**
- Create: `backend/window_history.py`
- Test: `backend/test_window_history.py` (append)

**Interfaces:**
- Consumes: nothing from other tasks (pure module; no store import yet).
- Produces (consumed by Tasks 3, 5, 6, 7):
  - `ClosedWindow` dataclass: `window_kind: str`, `window_start: float | None`, `window_end: float`, `final_used_pct: float`, `final_snapshot_ts: float`, `reset_cause: str`, `details: dict`.
  - `detect_closed_windows(provider, prev_snap, new_snap, *, coupon_hint=False) -> list[ClosedWindow]` — the one detector shared by live polling and backfill. (`provider` is a parameter because `limit_snapshots` rows don't carry it.)
  - `timed_windows(provider, snap) -> list[dict]` with keys `kind/used_pct/reset_at/window_s/start` — windows that have a reset timestamp.
  - `drop_windows(provider, snap) -> list[dict]` with keys `kind/used_pct/boundary` — windows without one (`boundary` is a unix ts, `"midnight"`, or None).
  - Constants: `RESET_TOLERANCE_S=120`, `EARLY_RESET_S=600`, `DROP_THRESHOLD_PCT=10.0`, `BOUNDARY_MATCH_S=3600`, `SUCCESS_STATUSES=("active", "rate_limited")`.

Per-provider window mapping (from the spec — snapshot columns → `window_kind`):

| provider    | window_kind       | source fields |
|-------------|-------------------|---------------|
| codex/claude/antigravity | `5h`, `weekly` | `primary_*`, `secondary_*` |
| claude      | `weekly_fable`    | `raw_json["fable"]` (`used_pct`, `reset_at`; skip if absent) |
| copilot     | `monthly_premium` | `primary_used_pct` + `primary_reset_at` |
| copilot     | `monthly_chat`    | `secondary_used_pct`, no reset ts → rule 3, boundary hint = `primary_reset_at` (premium reset date) |
| xai         | `monthly`         | `monthly_used_pct`, `monthly_period_start/end` (ISO strings) |
| devin       | `daily`, `weekly` | `daily/weekly_quota_remaining_percent` (used = 100 − remaining) → rule 3; boundary = local midnight (daily) / `plan_reset_unix` (weekly) |

- [ ] **Step 1: Write the failing tests**

Append to `backend/test_window_history.py` (before the `if __name__` block). Add `import window_history  # noqa: E402` directly under `import store`.

```python
def _snap(ts, status="active", **fields):
    d = {"ts": ts, "status": status}
    d.update(fields)
    return d


class DetectTimedWindowTests(unittest.TestCase):
    def test_natural_rollover_codex_5h(self):
        prev = _snap(BASE, primary_used_pct=85.0, primary_reset_at=BASE + 300,
                     primary_window_s=18000)
        new = _snap(BASE + 600, primary_used_pct=3.0,
                    primary_reset_at=BASE + 300 + 18000, primary_window_s=18000)
        out = window_history.detect_closed_windows("codex", prev, new)
        self.assertEqual(len(out), 1)
        cw = out[0]
        self.assertEqual(cw.window_kind, "5h")
        self.assertEqual(cw.reset_cause, "natural")
        self.assertEqual(cw.window_end, BASE + 300)
        self.assertEqual(cw.window_start, BASE + 300 - 18000)
        self.assertEqual(cw.final_used_pct, 85.0)
        self.assertEqual(cw.final_snapshot_ts, BASE)
        self.assertEqual(cw.details["staleness_s"], 300.0)

    def test_sleep_gap_spanning_boundary_is_natural(self):
        prev = _snap(BASE, primary_used_pct=85.0, primary_reset_at=BASE + 300,
                     primary_window_s=18000)
        new = _snap(BASE + 30000, primary_used_pct=3.0,  # 8h gap (laptop asleep)
                    primary_reset_at=BASE + 30000 + 12000, primary_window_s=18000)
        out = window_history.detect_closed_windows("codex", prev, new)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].reset_cause, "natural")
        self.assertEqual(out[0].window_end, BASE + 300)
        self.assertEqual(out[0].details["staleness_s"], 300.0)

    def test_early_reset_with_coupon_evidence(self):
        prev = _snap(BASE, primary_used_pct=90.0, primary_reset_at=BASE + 10000,
                     primary_window_s=18000, banked_resets=2)
        new = _snap(BASE + 300, primary_used_pct=0.5,
                    primary_reset_at=BASE + 300 + 18000, primary_window_s=18000,
                    banked_resets=1)
        out = window_history.detect_closed_windows("codex", prev, new)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].reset_cause, "coupon")
        self.assertEqual(out[0].window_end, BASE + 150)  # midpoint of the two ts

    def test_early_reset_without_evidence_is_provider_reset(self):
        prev = _snap(BASE, primary_used_pct=90.0, primary_reset_at=BASE + 10000,
                     primary_window_s=18000, banked_resets=2)
        new = _snap(BASE + 300, primary_used_pct=0.5,
                    primary_reset_at=BASE + 300 + 18000, primary_window_s=18000,
                    banked_resets=2)
        out = window_history.detect_closed_windows("codex", prev, new)
        self.assertEqual(out[0].reset_cause, "provider_reset")

    def test_coupon_hint_forces_coupon(self):
        prev = _snap(BASE, primary_used_pct=90.0, primary_reset_at=BASE + 10000,
                     primary_window_s=18000)
        new = _snap(BASE + 300, primary_used_pct=0.5,
                    primary_reset_at=BASE + 300 + 18000, primary_window_s=18000)
        out = window_history.detect_closed_windows("codex", prev, new, coupon_hint=True)
        self.assertEqual(out[0].reset_cause, "coupon")

    def test_error_snapshots_never_participate(self):
        good = _snap(BASE, primary_used_pct=85.0, primary_reset_at=BASE + 300,
                     primary_window_s=18000)
        bad = _snap(BASE + 600, status="error")
        self.assertEqual(window_history.detect_closed_windows("codex", good, bad), [])
        self.assertEqual(window_history.detect_closed_windows("codex", bad, good), [])
        self.assertEqual(window_history.detect_closed_windows("codex", None, good), [])

    def test_tolerance_jitter_is_ignored(self):
        prev = _snap(BASE, primary_used_pct=85.0, primary_reset_at=BASE + 300,
                     primary_window_s=18000)
        new = _snap(BASE + 300, primary_used_pct=86.0,
                    primary_reset_at=BASE + 360, primary_window_s=18000)  # +60s < 120s
        self.assertEqual(window_history.detect_closed_windows("codex", prev, new), [])

    def test_zero_usage_window_is_skipped(self):
        prev = _snap(BASE, primary_used_pct=0.0, primary_reset_at=BASE + 300,
                     primary_window_s=18000)
        new = _snap(BASE + 600, primary_used_pct=0.0,
                    primary_reset_at=BASE + 300 + 18000, primary_window_s=18000)
        self.assertEqual(window_history.detect_closed_windows("codex", prev, new), [])

    def test_claude_fable_window_from_raw_json(self):
        prev = _snap(BASE, raw_json=json.dumps(
            {"fable": {"label": "7d_oi", "used_pct": 42.0, "reset_at": BASE + 400}}))
        new = _snap(BASE + 600, raw_json=json.dumps(
            {"fable": {"label": "7d_oi", "used_pct": 1.0, "reset_at": BASE + 400 + 604800}}))
        out = window_history.detect_closed_windows("claude", prev, new)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].window_kind, "weekly_fable")
        self.assertEqual(out[0].reset_cause, "natural")
        self.assertEqual(out[0].window_end, BASE + 400)
        # fable missing on one side → skipped without crashing
        self.assertEqual(window_history.detect_closed_windows("claude", prev, _snap(BASE + 600)), [])

    def test_xai_monthly_uses_period_fields(self):
        r_old = datetime.datetime(2026, 7, 10, tzinfo=datetime.timezone.utc).timestamp()
        start_old = datetime.datetime(2026, 6, 10, tzinfo=datetime.timezone.utc).timestamp()
        prev = _snap(r_old - 200, monthly_used_pct=60.0,
                     monthly_period_start="2026-06-10T00:00:00Z",
                     monthly_period_end="2026-07-10T00:00:00Z")
        new = _snap(r_old + 300, monthly_used_pct=0.5,
                    monthly_period_start="2026-07-10T00:00:00Z",
                    monthly_period_end="2026-08-10T00:00:00Z")
        out = window_history.detect_closed_windows("xai", prev, new)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].window_kind, "monthly")
        self.assertEqual(out[0].reset_cause, "natural")
        self.assertEqual(out[0].window_end, r_old)
        self.assertEqual(out[0].window_start, start_old)


class DetectDropWindowTests(unittest.TestCase):
    def test_copilot_chat_drop_near_premium_reset_is_natural(self):
        prev = _snap(BASE, secondary_used_pct=45.0, primary_used_pct=10.0,
                     primary_reset_at=BASE + 500)
        new = _snap(BASE + 300, secondary_used_pct=2.0, primary_used_pct=10.0,
                    primary_reset_at=BASE + 500)
        out = window_history.detect_closed_windows("copilot", prev, new)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].window_kind, "monthly_chat")
        self.assertEqual(out[0].reset_cause, "natural")
        self.assertEqual(out[0].window_end, BASE + 300)  # new snapshot ts

    def test_copilot_chat_drop_far_from_boundary_is_unknown(self):
        prev = _snap(BASE, secondary_used_pct=45.0, primary_reset_at=BASE + 100000)
        new = _snap(BASE + 300, secondary_used_pct=2.0, primary_reset_at=BASE + 100000)
        out = window_history.detect_closed_windows("copilot", prev, new)
        self.assertEqual(out[0].reset_cause, "unknown")

    def test_small_drop_does_not_close(self):
        prev = _snap(BASE, secondary_used_pct=45.0)
        new = _snap(BASE + 300, secondary_used_pct=38.0)  # -7 < 10 points
        self.assertEqual(window_history.detect_closed_windows("copilot", prev, new), [])

    def test_devin_daily_drop_near_local_midnight_is_natural(self):
        mid = datetime.datetime(2026, 7, 10).timestamp()  # local midnight
        prev = _snap(mid - 200, daily_quota_remaining_percent=30.0,
                     weekly_quota_remaining_percent=50.0)
        new = _snap(mid + 200, daily_quota_remaining_percent=99.0,
                    weekly_quota_remaining_percent=50.0)
        out = window_history.detect_closed_windows("devin", prev, new)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].window_kind, "daily")
        self.assertEqual(out[0].reset_cause, "natural")
        self.assertEqual(out[0].final_used_pct, 70.0)  # 100 - 30 remaining

    def test_devin_weekly_uses_plan_reset_hint(self):
        prev = _snap(BASE, weekly_quota_remaining_percent=20.0,
                     plan_reset_unix=BASE + 400)
        new = _snap(BASE + 300, weekly_quota_remaining_percent=99.0,
                    plan_reset_unix=BASE + 400)
        out = window_history.detect_closed_windows("devin", prev, new)
        self.assertEqual(out[0].window_kind, "weekly")
        self.assertEqual(out[0].reset_cause, "natural")
        far = window_history.detect_closed_windows(
            "devin",
            _snap(BASE, weekly_quota_remaining_percent=20.0, plan_reset_unix=BASE + 900000),
            _snap(BASE + 300, weekly_quota_remaining_percent=99.0, plan_reset_unix=BASE + 900000))
        self.assertEqual(far[0].reset_cause, "unknown")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 backend/test_window_history.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'window_history'`

- [ ] **Step 3: Write the detector**

Create `backend/window_history.py`:

```python
"""Closed quota-window detection for window history.

detect_closed_windows() is a pure function shared verbatim by the live poll
hook and the one-time backfill: it compares two consecutive successful
snapshots of one account and returns the quota windows that closed between
them. Error snapshots never participate, so a connection failure can never
fake or corrupt a reset.
"""
from __future__ import annotations
import datetime, json
from dataclasses import dataclass, field

RESET_TOLERANCE_S = 120     # reset_at must move forward by more than this
EARLY_RESET_S = 600         # roll observed >10 min before the old boundary = early
DROP_THRESHOLD_PCT = 10.0   # used-pct drop that closes a timestamp-less window
BOUNDARY_MATCH_S = 3600     # drop within 1h of a known boundary = natural
SUCCESS_STATUSES = ("active", "rate_limited")


@dataclass
class ClosedWindow:
    window_kind: str            # 5h|weekly|weekly_fable|daily|monthly|monthly_premium|monthly_chat
    window_start: float | None  # unix; reset_at - window_s when known
    window_end: float           # boundary the window closed at
    final_used_pct: float       # last successful reading before close
    final_snapshot_ts: float    # ts of the snapshot supplying it
    reset_cause: str            # natural|coupon|provider_reset|unknown
    details: dict = field(default_factory=dict)


def _parse_iso_ts(s) -> float | None:
    if not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def _fable(snap) -> dict | None:
    """Claude's model-scoped weekly window from raw_json (best-effort)."""
    try:
        rj = json.loads(snap.get("raw_json") or "{}")
    except Exception:
        return None
    f = rj.get("fable") if isinstance(rj, dict) else None
    if isinstance(f, dict) and f.get("used_pct") is not None and f.get("reset_at"):
        return f
    return None


def timed_windows(provider, snap) -> list[dict]:
    """Snapshot windows that carry a reset timestamp (detection rule 2)."""
    out: list[dict] = []

    def add(kind, used, reset_at, window_s, start=None):
        if used is None or not reset_at:
            return
        out.append({"kind": kind, "used_pct": float(used), "reset_at": float(reset_at),
                    "window_s": window_s, "start": start})

    if provider in ("codex", "claude", "antigravity"):
        add("5h", snap.get("primary_used_pct"), snap.get("primary_reset_at"),
            snap.get("primary_window_s"))
        add("weekly", snap.get("secondary_used_pct"), snap.get("secondary_reset_at"),
            snap.get("secondary_window_s"))
        if provider == "claude":
            f = _fable(snap)
            if f:
                add("weekly_fable", f.get("used_pct"), f.get("reset_at"), 604800)
    elif provider == "copilot":
        add("monthly_premium", snap.get("primary_used_pct"), snap.get("primary_reset_at"), None)
    elif provider == "xai":
        add("monthly", snap.get("monthly_used_pct"),
            _parse_iso_ts(snap.get("monthly_period_end")), None,
            start=_parse_iso_ts(snap.get("monthly_period_start")))
    return out


def drop_windows(provider, snap) -> list[dict]:
    """Snapshot windows without a reset timestamp (detection rule 3).

    boundary is a unix ts hint or "midnight" for devin's daily window.
    """
    out: list[dict] = []
    if provider == "copilot" and snap.get("secondary_used_pct") is not None:
        out.append({"kind": "monthly_chat", "used_pct": float(snap["secondary_used_pct"]),
                    "boundary": snap.get("primary_reset_at")})
    elif provider == "devin":
        if snap.get("daily_quota_remaining_percent") is not None:
            out.append({"kind": "daily",
                        "used_pct": 100.0 - float(snap["daily_quota_remaining_percent"]),
                        "boundary": "midnight"})
        if snap.get("weekly_quota_remaining_percent") is not None:
            out.append({"kind": "weekly",
                        "used_pct": 100.0 - float(snap["weekly_quota_remaining_percent"]),
                        "boundary": snap.get("plan_reset_unix")})
    return out


def _banked_decreased(prev_snap, new_snap) -> bool:
    b0, b1 = prev_snap.get("banked_resets"), new_snap.get("banked_resets")
    return b0 is not None and b1 is not None and b1 < b0


def _near_boundary(prev_ts, new_ts, boundary_ts) -> bool:
    return (boundary_ts is not None
            and prev_ts - BOUNDARY_MATCH_S <= float(boundary_ts) <= new_ts + BOUNDARY_MATCH_S)


def _near_local_midnight(prev_ts, new_ts) -> bool:
    day = datetime.datetime.fromtimestamp(new_ts).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return any(_near_boundary(prev_ts, new_ts, (day + datetime.timedelta(days=n)).timestamp())
               for n in (0, 1))


def detect_closed_windows(provider, prev_snap, new_snap, *, coupon_hint=False) -> list[ClosedWindow]:
    """Windows that closed between two consecutive successful snapshots.

    Rule 1: only successful snapshots participate (status active/rate_limited).
    Rule 2: a window with a reset timestamp closed when reset_at moved forward
            by more than RESET_TOLERANCE_S. Natural when the roll was observed
            near or after the old boundary (poll gaps spanning it stay natural,
            with details.staleness_s recording how stale the final reading is);
            when observed more than EARLY_RESET_S before the old boundary it is
            coupon (with evidence: banked_resets decreased, or coupon_hint from
            a reset_credits flip) else provider_reset, and window_end is the
            midpoint of the two snapshot timestamps.
    Rule 3: a window without a reset timestamp closed when used_pct dropped by
            more than DROP_THRESHOLD_PCT. Natural when a related boundary
            (copilot quota_reset_date, devin plan_reset_unix, local midnight
            for daily) falls within BOUNDARY_MATCH_S of the pair, else unknown.
    Zero-usage windows (final_used_pct <= 0) are never reported.
    """
    if not prev_snap or not new_snap:
        return []
    if prev_snap.get("status") not in SUCCESS_STATUSES:
        return []
    if new_snap.get("status") not in SUCCESS_STATUSES:
        return []
    prev_ts, new_ts = float(prev_snap["ts"]), float(new_snap["ts"])
    if new_ts <= prev_ts:
        return []
    closed: list[ClosedWindow] = []
    coupon_evidence = coupon_hint or _banked_decreased(prev_snap, new_snap)

    new_timed = {w["kind"]: w for w in timed_windows(provider, new_snap)}
    for w in timed_windows(provider, prev_snap):
        nw = new_timed.get(w["kind"])
        if nw is None or w["used_pct"] <= 0:
            continue
        r_old, r_new = w["reset_at"], nw["reset_at"]
        if r_new - r_old <= RESET_TOLERANCE_S:
            continue
        if new_ts < r_old - EARLY_RESET_S:
            window_end = (prev_ts + new_ts) / 2
            cause = "coupon" if coupon_evidence else "provider_reset"
        else:
            window_end = r_old
            cause = "natural"
        start = w["start"]
        if start is None and w.get("window_s"):
            start = r_old - w["window_s"]
        closed.append(ClosedWindow(
            w["kind"], start, window_end, w["used_pct"], prev_ts, cause,
            {"staleness_s": round(max(0.0, window_end - prev_ts), 1),
             "prev_ts": prev_ts, "new_ts": new_ts,
             "old_reset_at": r_old, "new_reset_at": r_new}))

    new_drop = {w["kind"]: w for w in drop_windows(provider, new_snap)}
    for w in drop_windows(provider, prev_snap):
        nw = new_drop.get(w["kind"])
        if nw is None or w["used_pct"] <= 0:
            continue
        if w["used_pct"] - nw["used_pct"] <= DROP_THRESHOLD_PCT:
            continue
        boundary = w["boundary"]
        if boundary == "midnight":
            natural = _near_local_midnight(prev_ts, new_ts)
        else:
            natural = _near_boundary(prev_ts, new_ts, boundary)
        closed.append(ClosedWindow(
            w["kind"], None, new_ts, w["used_pct"], prev_ts,
            "natural" if natural else "unknown",
            {"staleness_s": round(new_ts - prev_ts, 1),
             "prev_ts": prev_ts, "new_ts": new_ts,
             "prev_used_pct": w["used_pct"], "new_used_pct": nw["used_pct"]}))
    return closed
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 backend/test_window_history.py`
Expected: `Ran 19 tests ... OK`

- [ ] **Step 5: Commit**

```bash
git add backend/window_history.py backend/test_window_history.py
git commit -m "feat(history): Detect closed quota windows from snapshot pairs

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Archiving + CSV/JSONL exports (`window_history.py`, part 2)

**Files:**
- Modify: `backend/window_history.py` (imports + append functions)
- Modify: `.gitignore` (add `history/`)
- Test: `backend/test_window_history.py` (append)

**Interfaces:**
- Consumes: Task 1 store accessors; Task 2 `detect_closed_windows`/`timed_windows`/`ClosedWindow`.
- Produces (consumed by Tasks 4, 5, 7):
  - `HISTORY_DIR: Path` — `AGENT_POOL_HISTORY_DIR` env or `~/solo/token-status-bar/history`.
  - `fmt_local(ts) -> str` — `"YYYY-MM-DD HH:MM:SS"` in KST, `""` for None.
  - `CSV_FIELDS` tuple (exact CSV header order).
  - `archive(conn, account, closed) -> list[ClosedWindow]` — inserts, returns rows actually added; suppresses early-cause duplicates via `store.window_history_conflict`.
  - `record_closed_windows(conn, account, prev_snap, new_snap, *, coupon_hint=False) -> int` — live hook: detect + archive + append JSONL + rewrite provider CSV; returns count.
  - `archive_coupon_redeem(conn, account, credit_id, now_ts=None) -> int` — direct archive at redeem time from the latest successful snapshot.
  - `append_jsonl(account, closed)` / `write_provider_csv(conn, provider) -> Path | None`.
  - `account` parameters are account dicts from `store.get_account`/`store.list_accounts` (need `id`, `provider`, `email`, `label`).

- [ ] **Step 1: Write the failing tests**

Append to `backend/test_window_history.py` (before `if __name__`):

```python
class ArchiveExportTests(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect()
        self.acct_id = store.upsert_account(self.conn, "codex", f"exp-{id(self)}@example.com", "exp")
        self.account = store.get_account(self.conn, self.acct_id)

    def test_record_appends_jsonl_and_rewrites_csv(self):
        prev = _snap(BASE, primary_used_pct=85.0, primary_reset_at=BASE + 300,
                     primary_window_s=18000)
        new = _snap(BASE + 600, primary_used_pct=3.0,
                    primary_reset_at=BASE + 300 + 18000, primary_window_s=18000)
        self.assertEqual(window_history.record_closed_windows(self.conn, self.account, prev, new), 1)
        # re-running is idempotent: no new row, no duplicate JSONL line
        self.assertEqual(window_history.record_closed_windows(self.conn, self.account, prev, new), 0)

        jsonl = window_history.HISTORY_DIR / f"codex-{self.acct_id}.jsonl"
        lines = [json.loads(l) for l in jsonl.read_text().splitlines()]
        ours = [l for l in lines if l["email"] == self.account["email"]]
        self.assertEqual(len(ours), 1)
        self.assertEqual(ours[0]["window_kind"], "5h")
        self.assertEqual(ours[0]["reset_cause"], "natural")
        self.assertIn("staleness_s", ours[0]["details"])

        with open(window_history.HISTORY_DIR / "codex.csv") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(list(rows[0].keys()), list(window_history.CSV_FIELDS))
        ours_csv = [r for r in rows if r["email"] == self.account["email"]]
        self.assertEqual(len(ours_csv), 1)
        self.assertEqual(float(ours_csv[0]["final_used_pct"]), 85.0)

    def test_coupon_redeem_archives_from_last_good_snapshot(self):
        _insert_snap(self.conn, self.acct_id, BASE, status="active",
                     primary_used_pct=91.0, primary_reset_at=BASE + 9000, primary_window_s=18000,
                     secondary_used_pct=40.0, secondary_reset_at=BASE + 500000,
                     secondary_window_s=604800)
        n = window_history.archive_coupon_redeem(self.conn, self.account, "credit-abc",
                                                 now_ts=BASE + 100)
        self.assertEqual(n, 2)
        rows = store.list_window_history(self.conn, account_id=self.acct_id)
        self.assertEqual({r["window_kind"] for r in rows}, {"5h", "weekly"})
        for r in rows:
            self.assertEqual(r["reset_cause"], "coupon")
            self.assertEqual(r["window_end"], BASE + 100)
            self.assertEqual(json.loads(r["details"])["credit_id"], "credit-abc")

    def test_detection_after_redeem_does_not_duplicate(self):
        # Direct archive at redeem time (rule 4)...
        _insert_snap(self.conn, self.acct_id, BASE, status="active",
                     primary_used_pct=91.0, primary_reset_at=BASE + 9000, primary_window_s=18000)
        window_history.archive_coupon_redeem(self.conn, self.account, "credit-abc",
                                             now_ts=BASE + 100)
        # ...then the next poll pair sees the same early roll (confirm re-poll failed).
        prev = _snap(BASE, primary_used_pct=91.0, primary_reset_at=BASE + 9000,
                     primary_window_s=18000, banked_resets=2)
        new = _snap(BASE + 300, primary_used_pct=0.5,
                    primary_reset_at=BASE + 300 + 18000, primary_window_s=18000,
                    banked_resets=1)
        self.assertEqual(window_history.record_closed_windows(self.conn, self.account, prev, new), 0)
        rows = [r for r in store.list_window_history(self.conn, account_id=self.acct_id)
                if r["window_kind"] == "5h"]
        self.assertEqual(len(rows), 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 backend/test_window_history.py`
Expected: FAIL with `AttributeError: module 'window_history' has no attribute 'record_closed_windows'`

- [ ] **Step 3: Implement**

In `backend/window_history.py`, replace the import block:

```python
from __future__ import annotations
import csv, datetime, json, os, time, zoneinfo
from dataclasses import dataclass, field
from pathlib import Path
import store
```

Add module constants right after the imports (before `RESET_TOLERANCE_S`):

```python
HISTORY_DIR = Path(os.environ.get("AGENT_POOL_HISTORY_DIR",
                                  str(Path.home() / "solo/token-status-bar" / "history")))
KST = zoneinfo.ZoneInfo("Asia/Seoul")
CSV_FIELDS = ("email", "label", "window_kind", "window_start", "window_end",
              "final_used_pct", "reset_cause", "final_snapshot_ts", "staleness_s")
```

Append at the end of the module:

```python
def fmt_local(ts) -> str:
    if ts is None:
        return ""
    return datetime.datetime.fromtimestamp(float(ts), tz=KST).strftime("%Y-%m-%d %H:%M:%S")


def archive(conn, account, closed) -> list[ClosedWindow]:
    """INSERT OR IGNORE each closed window; returns the ones actually added.

    Early-cause rows (coupon/provider_reset) are additionally skipped when a
    row for the same window kind already ends inside the same snapshot pair:
    the redeem-time direct archive and the next poll's detection describe the
    same close with slightly different window_end values.
    """
    inserted = []
    for cw in closed:
        if cw.reset_cause in ("coupon", "provider_reset"):
            lo, hi = cw.details.get("prev_ts"), cw.details.get("new_ts")
            if lo is not None and hi is not None and \
                    store.window_history_conflict(conn, account["id"], cw.window_kind, lo, hi):
                continue
        if store.save_window_history(conn, account["id"], cw.window_kind, cw.window_start,
                                     cw.window_end, cw.final_used_pct, cw.final_snapshot_ts,
                                     cw.reset_cause, cw.details or None):
            inserted.append(cw)
    return inserted


def record_closed_windows(conn, account, prev_snap, new_snap, *, coupon_hint=False) -> int:
    """Live hook: detect + archive + refresh exports for one poll step."""
    closed = detect_closed_windows(account["provider"], prev_snap, new_snap,
                                   coupon_hint=coupon_hint)
    inserted = archive(conn, account, closed)
    if inserted:
        append_jsonl(account, inserted)
        write_provider_csv(conn, account["provider"])
    return len(inserted)


def archive_coupon_redeem(conn, account, credit_id, now_ts=None) -> int:
    """Archive at coupon-redeem time (HTTP 200), before the confirmation
    re-poll, so the coupon row exists even if that re-poll fails."""
    now_ts = now_ts or time.time()
    snap = store.latest_successful_snapshot(conn, account["id"])
    if not snap:
        return 0
    closed = []
    for w in timed_windows(account["provider"], snap):
        if w["used_pct"] <= 0:
            continue
        start = w["start"]
        if start is None and w.get("window_s"):
            start = w["reset_at"] - w["window_s"]
        closed.append(ClosedWindow(
            w["kind"], start, now_ts, w["used_pct"], float(snap["ts"]), "coupon",
            {"credit_id": credit_id,
             "staleness_s": round(max(0.0, now_ts - float(snap["ts"])), 1),
             "old_reset_at": w["reset_at"]}))
    inserted = archive(conn, account, closed)
    if inserted:
        append_jsonl(account, inserted)
        write_provider_csv(conn, account["provider"])
    return len(inserted)


def _row_fields(cw: ClosedWindow, email, label) -> dict:
    return {
        "email": email or "",
        "label": label or "",
        "window_kind": cw.window_kind,
        "window_start": fmt_local(cw.window_start),
        "window_end": fmt_local(cw.window_end),
        "final_used_pct": cw.final_used_pct,
        "reset_cause": cw.reset_cause,
        "final_snapshot_ts": fmt_local(cw.final_snapshot_ts),
        "staleness_s": cw.details.get("staleness_s", ""),
    }


def append_jsonl(account, closed) -> None:
    """O(1) per-account append: one JSON object per closed window."""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = HISTORY_DIR / f"{account['provider']}-{account['id']}.jsonl"
    with open(path, "a") as f:
        for cw in closed:
            obj = _row_fields(cw, account.get("email"), account.get("label"))
            obj["details"] = cw.details
            f.write(json.dumps(obj) + "\n")


def write_provider_csv(conn, provider):
    """Rewrite history/<provider>.csv from the window_history table.

    Called only when the table gains rows — files are KB-sized, a few
    writes per day."""
    rows = store.list_window_history(conn, provider=provider)
    if not rows:
        return None
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = HISTORY_DIR / f"{provider}.csv"
    with open(path, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(CSV_FIELDS))
        wr.writeheader()
        for r in rows:
            details = json.loads(r["details"]) if r["details"] else {}
            wr.writerow({
                "email": r["email"] or "",
                "label": r["label"] or "",
                "window_kind": r["window_kind"],
                "window_start": fmt_local(r["window_start"]),
                "window_end": fmt_local(r["window_end"]),
                "final_used_pct": r["final_used_pct"],
                "reset_cause": r["reset_cause"],
                "final_snapshot_ts": fmt_local(r["final_snapshot_ts"]),
                "staleness_s": details.get("staleness_s", ""),
            })
    return path
```

In `.gitignore`, extend the runtime-data section (lines 1-5):

```gitignore
# ─── Runtime data (machine-local, contains account state) ─────────────
# All secrets / local runtime data live in secrets/ (db, status.json, logs)
secrets/
/pool.db
history/
*.log
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 backend/test_window_history.py`
Expected: `Ran 22 tests ... OK`

Also confirm the ignore rule: `git check-ignore -v history/x.csv`
Expected: a line citing `.gitignore` with pattern `history/`.

- [ ] **Step 5: Commit**

```bash
git add backend/window_history.py backend/test_window_history.py .gitignore
git commit -m "feat(history): Archive closed windows with CSV/JSONL exports

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Dashboard generator (`backend/dashboard.py`) + `pool.py dashboard`

**Files:**
- Create: `backend/dashboard.py`
- Modify: `backend/pool.py` (docstring usage block ~line 23; command dispatch in `main` before the final "Unknown command" line)
- Test: `backend/test_window_history.py` (append)

**Interfaces:**
- Consumes: `store.list_window_history(conn)` (Task 1), `window_history.HISTORY_DIR` / `window_history.fmt_local` (Task 3).
- Produces (consumed by Tasks 5, 7, 8):
  - `dashboard.generate(conn) -> Path` — writes `HISTORY_DIR / "dashboard.html"` and returns its path. Handles an empty table (renders an empty state, still returns the path).
  - `dashboard.dashboard_data(conn) -> list[dict]` — rows shaped `{"provider", "account", "account_id", "window_kind", "window_start", "window_end", "window_end_label", "final_used_pct", "reset_cause", "staleness_s"}` (`window_start`/`window_end` are unix floats; `window_end_label` is KST text).
  - CLI: `python3 backend/pool.py dashboard [--open]` — regenerates; `--open` additionally runs `open <path>`.

- [ ] **Step 1: REQUIRED — load the `dataviz` skill before writing any chart code**

Invoke the `dataviz` skill (Skill tool) and follow it for chart form, color, axes, legends, and dashboard layout. The bar-chart palette (one color per provider) and cause-badge colors must come from its guidance. Do not write the HTML/JS before reading it.

- [ ] **Step 2: Write the Python generator**

Create `backend/dashboard.py`:

```python
"""Self-contained window-history dashboard (history/dashboard.html).

Static file: inline CSS/JS, data embedded as a JSON blob, no CDN — works
offline from file://. Regenerated only when window_history gains rows or on
demand via `pool.py dashboard [--open]`, never on ordinary poll ticks.
"""
from __future__ import annotations
import json
import store
from window_history import HISTORY_DIR, fmt_local


def dashboard_data(conn) -> list[dict]:
    rows = []
    for r in store.list_window_history(conn):
        details = json.loads(r["details"]) if r["details"] else {}
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
        })
    return rows


def generate(conn):
    """Write history/dashboard.html and return its path."""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = HISTORY_DIR / "dashboard.html"
    data = json.dumps(dashboard_data(conn)).replace("</", "<\\/")
    path.write_text(_HTML_TEMPLATE.replace("/*__DATA__*/[]", data))
    return path


_HTML_TEMPLATE = """..."""  # written in Step 3
```

- [ ] **Step 3: Write `_HTML_TEMPLATE` (guided by the dataviz skill)**

The template is one Python triple-quoted string containing a complete HTML document. Hard requirements (each is verified by the Step 5 test or the Task 7/9 visual check):

1. **Self-contained**: all CSS in one `<style>` block, all JS in one `<script>` block. No external `src=`, `href=`, `@import`, web fonts, or fetch/XHR — the file must render fully offline from `file://`.
2. **Data embedding**: the script starts with `const ROWS = /*__DATA__*/[];` — `generate()` substitutes the literal `/*__DATA__*/[]`. All rendering is driven from `ROWS`.
3. **Chart**: a usage-history timeline — one bar per closed window, x = `window_end` (time), height = `final_used_pct` (y axis 0–100%), colored by provider (consistent legend). Render as inline SVG built by JS (no canvas needed at this scale). Time axis labels use `window_end_label`. A tooltip (SVG `<title>` is sufficient) shows account, window_kind, used %, cause, and end time.
4. **Filters**: three `<select>` controls — provider, account, window_kind — each with an "all" option, populated from `ROWS`; changing any re-renders chart and table.
5. **Cause badges**: `reset_cause` rendered as a colored badge (natural / coupon / provider_reset / unknown) in the table and as the chart legend's secondary key.
6. **Table**: all visible rows with columns Provider, Account, Window, Closed at (`window_end_label`), Final used %, Cause, Staleness (s); clicking a header sorts by that column (toggle asc/desc).
7. **Empty state**: when `ROWS` is empty, show a short message ("No closed windows archived yet — run `pool.py backfill-history`.") instead of an empty chart.
8. Page `<title>`: "Token window history".

- [ ] **Step 4: Add the `dashboard` command to `backend/pool.py`**

Add to the usage docstring (after the `export-status` line):

```
  pool.py dashboard [--open]           Regenerate history/dashboard.html (--open opens it)
```

Add to `main` (before the final `print(f"Unknown command: ...")`):

```python
    if cmd == "dashboard":
        import subprocess
        import dashboard
        path = dashboard.generate(DB)
        print(f"Wrote {path}")
        if "--open" in argv[1:]:
            subprocess.run(["open", str(path)], check=False)
        return 0
```

- [ ] **Step 5: Add the test and verify**

Append to `backend/test_window_history.py` (before `if __name__`), adding `import dashboard  # noqa: E402` next to the other backend imports at the top of the file:

```python
class DashboardTests(unittest.TestCase):
    def test_generate_is_self_contained(self):
        conn = store.connect()
        acct_id = store.upsert_account(conn, "codex", f"dash-{id(self)}@example.com", "dash")
        store.save_window_history(conn, acct_id, "5h", BASE - 18000, BASE, 66.0, BASE - 60,
                                  "coupon", {"staleness_s": 60.0, "credit_id": "c1"})
        path = dashboard.generate(conn)
        html = path.read_text()
        self.assertIn("dash-", html)             # embedded data blob
        self.assertIn("coupon", html)
        self.assertIn("const ROWS =", html)
        self.assertIn("<table", html)
        self.assertNotIn('src="http', html)      # no CDN / external requests
        self.assertNotIn("src='http", html)
        self.assertNotIn('href="http', html)
        self.assertNotIn("@import", html)
```

Run: `python3 backend/test_window_history.py`
Expected: `Ran 23 tests ... OK`

Run: `python3 backend/pool.py dashboard`
Expected: `Wrote /Users/tonylee/solo/token-status-bar/history/dashboard.html` (empty state — no rows archived yet). Then `python3 backend/pool.py dashboard --open` and visually confirm the empty-state page renders in the browser with no console errors.

- [ ] **Step 6: Commit**

```bash
git add backend/dashboard.py backend/pool.py backend/test_window_history.py
git commit -m "feat(history): Add self-contained HTML dashboard command

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Poller live hook + redeem-time archive (`poller.py`)

**Files:**
- Modify: `backend/poller.py` — imports (line 13), the dispatch section (`poll_account` + `run_once`, lines ~1007-1073), `redeem_reset` (~line 1121)
- Test: `backend/test_window_history.py` (append)

**Interfaces:**
- Consumes: `store.latest_successful_snapshot` / `store.latest_snapshot` / `store.list_reset_credits` (Task 1 / existing), `window_history.record_closed_windows` / `archive_coupon_redeem` (Task 3), `dashboard.generate` (Task 4).
- Produces (consumed by Task 6):
  - `_poll_one(conn, account) -> bool` — single-account poll with detection wrapped around it.
  - `poll_some(conn, accounts)` — polls a list, exports status.json once at the end.
  - `poll_account(conn, account) -> bool` and `run_once(conn) -> int` keep their existing signatures/behavior (now built on `_poll_one`); `run_loop` is untouched until Task 6.

- [ ] **Step 1: Write the failing tests**

Append to `backend/test_window_history.py` (before `if __name__`), adding `import poller  # noqa: E402` next to the other backend imports at the top of the file:

```python
class PollerHookTests(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect()
        self.acct_id = store.upsert_account(self.conn, "claude", f"hook-{id(self)}@example.com", "hook")
        self.account = store.get_account(self.conn, self.acct_id)

    def test_archives_after_natural_roll(self):
        _insert_snap(self.conn, self.acct_id, BASE, status="active",
                     primary_used_pct=88.0, primary_reset_at=BASE + 200, primary_window_s=18000)
        prev = store.latest_successful_snapshot(self.conn, self.acct_id)
        _insert_snap(self.conn, self.acct_id, BASE + 500, status="active",
                     primary_used_pct=1.0, primary_reset_at=BASE + 200 + 18000,
                     primary_window_s=18000)
        poller._archive_closed_windows(self.conn, self.account, prev, {})
        rows = store.list_window_history(self.conn, account_id=self.acct_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reset_cause"], "natural")
        self.assertEqual(rows[0]["window_end"], BASE + 200)

    def test_error_snapshot_never_archives(self):
        _insert_snap(self.conn, self.acct_id, BASE, status="active",
                     primary_used_pct=88.0, primary_reset_at=BASE + 200, primary_window_s=18000)
        prev = store.latest_successful_snapshot(self.conn, self.acct_id)
        _insert_snap(self.conn, self.acct_id, BASE + 500, status="error")
        poller._archive_closed_windows(self.conn, self.account, prev, {})
        self.assertEqual(store.list_window_history(self.conn, account_id=self.acct_id), [])

    def test_unchanged_snapshot_is_a_noop(self):
        # copilot's hold-last-good path saves no new snapshot: prev is new.
        _insert_snap(self.conn, self.acct_id, BASE, status="active",
                     primary_used_pct=88.0, primary_reset_at=BASE + 200, primary_window_s=18000)
        prev = store.latest_successful_snapshot(self.conn, self.acct_id)
        poller._archive_closed_windows(self.conn, self.account, prev, {})
        self.assertEqual(store.list_window_history(self.conn, account_id=self.acct_id), [])
```

Run: `python3 backend/test_window_history.py`
Expected: FAIL with `AttributeError: module 'poller' has no attribute '_archive_closed_windows'`

- [ ] **Step 2: Refactor the dispatch section**

In `backend/poller.py`, change line 13 to:

```python
import store, oauth, window_history, work_queue
```

Replace the whole block from `def poll_account(conn, account) -> bool:` (line ~1007) through the end of `def run_once(conn) -> int:` (line ~1073, keeping `run_loop` below untouched) with:

```python
def _poll_one(conn, account) -> bool:
    """Poll one account. Returns True when the provider poll succeeded.

    Wraps the provider poller with closed-window detection: the previous
    successful snapshot is captured before the poll and compared with the
    freshly saved one right after, archiving any windows that closed in
    between. Detection failures never fail the poll.
    """
    token = store.get_token(conn, account["id"])
    if not token:
        store.save_snapshot(conn, account["id"], {"status": "error", "status_message": "no token"})
        return False
    token = _refresh_if_needed(conn, account, token)
    poller = POLLERS.get(account["provider"])
    if not poller:
        store.save_snapshot(conn, account["id"],
                            {"status": "error", "status_message": f"no poller for {account['provider']}"})
        return False
    prev = store.latest_successful_snapshot(conn, account["id"])
    prev_credits = ({c["credit_id"]: c["status"] for c in store.list_reset_credits(conn, account["id"])}
                    if account["provider"] == "codex" else {})
    try:
        poller(conn, account, token)
        print(f"  ✓ {account['provider']:12} {account['email'] or account['label']}")
    except Exception as e:
        store.save_snapshot(conn, account["id"], {"status": "error", "status_message": str(e)[:200]})
        store.log_event(conn, account["id"], "limit_poll", False, str(e))
        print(f"  ✗ {account['provider']:12} {account['email'] or account['label']}: {e}")
        return False
    _archive_closed_windows(conn, account, prev, prev_credits)
    return True


def _archive_closed_windows(conn, account, prev, prev_credits):
    """Detect + archive windows closed since the previous successful snapshot.

    Coupon hint: a reset_credits row that was "available" before the poll and
    isn't afterwards (consumed or gone) marks a redeem from another device.
    """
    try:
        new = store.latest_snapshot(conn, account["id"])
        if not new or (prev and new["id"] == prev["id"]):
            return
        coupon_hint = False
        if prev_credits:
            cur = {c["credit_id"]: c["status"] for c in store.list_reset_credits(conn, account["id"])}
            coupon_hint = any(st == "available" and cur.get(cid) != "available"
                              for cid, st in prev_credits.items())
        n = window_history.record_closed_windows(conn, account, prev, new, coupon_hint=coupon_hint)
        if n:
            print(f"  ⤷ archived {n} closed window(s): {account['provider']} #{account['id']}")
            try:
                import dashboard
                dashboard.generate(conn)
            except Exception as e:
                print(f"  dashboard generation failed: {e}")
    except Exception as e:
        print(f"  window-history detection failed: {e}")


def poll_some(conn, accounts) -> None:
    """Poll the given accounts and export status.json once."""
    for a in accounts:
        _poll_one(conn, a)
    try:
        import status
        status.cmd_export(conn)
    except Exception as e:
        print(f"  export-status failed: {e}")


def poll_account(conn, account) -> bool:
    """Poll a single account and refresh status.json. Returns True on success.

    Used by onboarding so a freshly-added account immediately has subscription
    data instead of waiting for the next 5-minute poll cycle.
    """
    ok = _poll_one(conn, account)
    try:
        import status
        status.cmd_export(conn)
    except Exception as e:
        print(f"  export-status failed: {e}")
    return ok


def run_once(conn) -> int:
    with work_queue.single_worker("poll") as acquired:
        if not acquired:
            print("poll already running; queued worker skipped")
            return 0
        accounts = store.list_accounts(conn)
        if not accounts:
            print("(no accounts to poll)")
            return 0
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Polling {len(accounts)} accounts...")
        poll_some(conn, accounts)
    return 0
```

- [ ] **Step 3: Archive directly in `redeem_reset` (detection rule 4)**

In `redeem_reset` (~line 1121), replace the success branch:

```python
    if st == 200:
        print(f"✓ Redeemed: {credit['title']}")
        # Archive the closing windows now — before the confirmation re-poll —
        # so the coupon row exists even if that re-poll fails.
        try:
            n = window_history.archive_coupon_redeem(conn, a, credit["credit_id"])
            if n:
                print(f"  ⤷ archived {n} window(s) as coupon reset")
        except Exception as e:
            print(f"  window-history archive failed: {e}")
        # Re-poll to show updated state
        poll_codex(conn, a, token)
        return 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 backend/test_window_history.py`
Expected: `Ran 26 tests ... OK`

Run: `python3 backend/test_onboarding.py`
Expected: OK — this is the critical regression: it asserts `cmd_add` → `poller.poll_account` wiring and that each provider poller, fed canned HTTP responses, still saves full snapshots through the refactored path.

Live smoke (hits real providers once, same cost as a normal 5-min tick):
Run: `python3 backend/pool.py poll`
Expected: the usual `✓ provider email` lines and `Wrote .../status.json`; no `window-history detection failed` lines.

- [ ] **Step 5: Commit**

```bash
git add backend/poller.py backend/test_window_history.py
git commit -m "feat(history): Archive closed windows live after each poll

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Adaptive poll cadence + pre-reset capture (`poller.py`)

**Files:**
- Modify: `backend/poller.py` — module docstring (line 1), knobs after `POLL_INTERVAL` (line 16), new scheduler helpers before `run_loop`, `run_loop` itself (~line 1076)
- Test: `backend/test_window_history.py` (append)

**Interfaces:**
- Consumes: `window_history.timed_windows` / `drop_windows` (Task 2), `poll_some` (Task 5), `work_queue.single_worker` (existing).
- Produces:
  - Knobs: `HOT_THRESHOLD_PCT=70`, `HOT_INTERVAL_S=60`, `HOT_INTERVAL_CLAUDE_S=180`, `PRERESET_LEAD_S=300`, `PRERESET_RETRY_S=60` (env-overridable), `PRERESET_FINAL_GAP_S=30` (constant).
  - `max_used_pct(provider, snap) -> float` — hotness signal across all tracked windows.
  - `next_reset_at(provider, snap, now) -> float | None` — earliest future reset among timed windows.
  - `compute_next_due(now, *, provider, last_poll_ts, last_success_ts, hot, reset_at) -> float` — pure, unit-tested next-wake computation.

- [ ] **Step 1: Write the failing scheduler tests**

Append to `backend/test_window_history.py` (before `if __name__`):

```python
class SchedulerTests(unittest.TestCase):
    def test_base_cadence(self):
        due = poller.compute_next_due(1000.0, provider="codex", last_poll_ts=900.0,
                                      last_success_ts=900.0, hot=False, reset_at=None)
        self.assertEqual(due, 900.0 + poller.POLL_INTERVAL)

    def test_hot_account_polls_at_hot_interval(self):
        due = poller.compute_next_due(1000.0, provider="codex", last_poll_ts=900.0,
                                      last_success_ts=900.0, hot=True, reset_at=None)
        self.assertEqual(due, 900.0 + poller.HOT_INTERVAL_S)

    def test_hot_claude_capped(self):
        due = poller.compute_next_due(1000.0, provider="claude", last_poll_ts=900.0,
                                      last_success_ts=900.0, hot=True, reset_at=None)
        self.assertEqual(due, 900.0 + poller.HOT_INTERVAL_CLAUDE_S)

    def test_prereset_wakes_at_lead_start(self):
        reset = 2000.0
        due = poller.compute_next_due(1650.0, provider="codex", last_poll_ts=1600.0,
                                      last_success_ts=1600.0, hot=False, reset_at=reset)
        self.assertEqual(due, reset - poller.PRERESET_LEAD_S)  # 1700

    def test_prereset_retries_inside_window(self):
        reset = 2000.0
        now = reset - 100.0  # inside the lead window, no capture yet
        due = poller.compute_next_due(now, provider="codex", last_poll_ts=now - 10,
                                      last_success_ts=1000.0, hot=False, reset_at=reset)
        self.assertEqual(due, now - 10 + poller.PRERESET_RETRY_S)

    def test_prereset_first_success_wins(self):
        reset = 2000.0
        now = reset - 100.0
        due = poller.compute_next_due(now, provider="codex", last_poll_ts=now - 10,
                                      last_success_ts=now - 10,  # success inside lead window
                                      hot=False, reset_at=reset)
        self.assertEqual(due, now - 10 + poller.POLL_INTERVAL)

    def test_prereset_last_attempt_before_final_gap(self):
        reset = 2000.0
        now = reset - 20.0  # past deadline (reset - 30)
        due = poller.compute_next_due(now, provider="codex", last_poll_ts=now - 5,
                                      last_success_ts=1000.0, hot=False, reset_at=reset)
        self.assertEqual(due, now - 5 + poller.POLL_INTERVAL)

    def test_hotness_and_next_reset_helpers(self):
        s = {"status": "active", "ts": 1000.0,
             "primary_used_pct": 20.0, "primary_reset_at": 4000.0, "primary_window_s": 18000,
             "secondary_used_pct": 75.0, "secondary_reset_at": 9000.0,
             "secondary_window_s": 604800}
        self.assertEqual(poller.max_used_pct("codex", s), 75.0)
        self.assertEqual(poller.next_reset_at("codex", s, 1000.0), 4000.0)
        self.assertEqual(poller.next_reset_at("codex", s, 5000.0), 9000.0)
        self.assertIsNone(poller.next_reset_at("codex", s, 10000.0))
```

Run: `python3 backend/test_window_history.py`
Expected: FAIL with `AttributeError: module 'poller' has no attribute 'compute_next_due'`

- [ ] **Step 2: Add the knobs and scheduler helpers**

In `backend/poller.py`, after the `POLL_INTERVAL` line (16), add:

```python
# Adaptive cadence: hot accounts (any window >= HOT_THRESHOLD_PCT used) poll
# faster — an early reset only destroys meaningful data when usage is high —
# and accounts within PRERESET_LEAD_S of a known reset get a fresh capture
# with retries. Claude's hot cadence is capped because its poll consumes
# quota (two real 1-token probes).
HOT_THRESHOLD_PCT = float(os.environ.get("HOT_THRESHOLD_PCT", "70"))
HOT_INTERVAL_S = int(os.environ.get("HOT_INTERVAL_S", "60"))
HOT_INTERVAL_CLAUDE_S = int(os.environ.get("HOT_INTERVAL_CLAUDE_S", "180"))
PRERESET_LEAD_S = int(os.environ.get("PRERESET_LEAD_S", "300"))
PRERESET_RETRY_S = int(os.environ.get("PRERESET_RETRY_S", "60"))
PRERESET_FINAL_GAP_S = 30
```

Add the helpers right before `run_loop`:

```python
def max_used_pct(provider, snap) -> float:
    """Highest used% across a snapshot's tracked windows (hotness signal)."""
    vals = [w["used_pct"] for w in window_history.timed_windows(provider, snap)]
    vals += [w["used_pct"] for w in window_history.drop_windows(provider, snap)]
    return max(vals, default=0.0)


def next_reset_at(provider, snap, now) -> float | None:
    """Earliest future reset timestamp among the snapshot's timed windows."""
    future = [w["reset_at"] for w in window_history.timed_windows(provider, snap)
              if w["reset_at"] > now]
    return min(future) if future else None


def compute_next_due(now, *, provider, last_poll_ts, last_success_ts, hot, reset_at) -> float:
    """Pure next-poll-time computation for one account.

    Base cadence POLL_INTERVAL; hot accounts use HOT_INTERVAL_S
    (HOT_INTERVAL_CLAUDE_S for claude). When reset_at is within
    PRERESET_LEAD_S and no success has landed inside that lead window yet,
    wake at the lead start and retry every PRERESET_RETRY_S, last attempt no
    later than reset_at - PRERESET_FINAL_GAP_S. First success wins.
    """
    interval = POLL_INTERVAL
    if hot:
        interval = HOT_INTERVAL_CLAUDE_S if provider == "claude" else HOT_INTERVAL_S
    due = last_poll_ts + interval
    if reset_at:
        lead_start = reset_at - PRERESET_LEAD_S
        deadline = reset_at - PRERESET_FINAL_GAP_S
        captured = last_success_ts is not None and last_success_ts >= lead_start
        if not captured and now <= deadline:
            if now < lead_start:
                due = min(due, lead_start)
            else:
                due = min(due, min(max(last_poll_ts + PRERESET_RETRY_S, now), deadline))
    return due
```

- [ ] **Step 3: Replace `run_loop`**

Replace the whole `run_loop` function:

```python
def run_loop(conn) -> int:
    print(f"Poller daemon started. Base interval: {POLL_INTERVAL}s, hot: {HOT_INTERVAL_S}s "
          f"(claude {HOT_INTERVAL_CLAUDE_S}s), pre-reset lead: {PRERESET_LEAD_S}s. Ctrl+C to stop.")
    # In-memory attempt times: copilot's hold-last-good path saves no snapshot
    # on a transient failure, so DB timestamps alone would re-poll it instantly.
    last_attempt: dict[int, float] = {}
    while True:
        try:
            now = time.time()
            accounts = store.list_accounts(conn)
            if not accounts:
                time.sleep(POLL_INTERVAL)
                continue
            due, wake = [], now + POLL_INTERVAL
            for a in accounts:
                snap = store.latest_snapshot(conn, a["id"])
                good = store.latest_successful_snapshot(conn, a["id"])
                last_poll_ts = max(float(snap["ts"]) if snap else 0.0,
                                   last_attempt.get(a["id"], 0.0))
                t_due = compute_next_due(
                    now,
                    provider=a["provider"],
                    last_poll_ts=last_poll_ts,
                    last_success_ts=float(good["ts"]) if good else None,
                    hot=bool(good) and max_used_pct(a["provider"], good) >= HOT_THRESHOLD_PCT,
                    reset_at=next_reset_at(a["provider"], good, now) if good else None,
                )
                if t_due <= now:
                    due.append(a)
                else:
                    wake = min(wake, t_due)
            if due:
                with work_queue.single_worker("poll") as acquired:
                    if acquired:
                        stamp = datetime.datetime.now().strftime("%H:%M:%S")
                        print(f"[{stamp}] Polling {len(due)}/{len(accounts)} due accounts...")
                        for a in due:
                            last_attempt[a["id"]] = time.time()
                        poll_some(conn, due)
                        continue
                print("poll already running; waiting")
                time.sleep(5)
                continue
            time.sleep(max(1.0, min(wake - time.time(), POLL_INTERVAL)))
        except KeyboardInterrupt:
            print("\nPoller stopped.")
            return 0
```

Update the module docstring's first line (line 1) to:

```python
"""Poller — hits limit endpoints for every account (adaptive: 300s base,
60s hot / 180s hot-claude, pre-reset capture near known boundaries).
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 backend/test_window_history.py`
Expected: `Ran 34 tests ... OK`

Run: `python3 backend/test_onboarding.py && python3 backend/test_status_dates.py && python3 backend/test_agy_usage.py`
Expected: all OK.

- [ ] **Step 5: Restart the poller and observe one adaptive cycle**

The LaunchAgent respawns the poller automatically after a kill. Do NOT start one by hand.

Run: `pkill -f "pool.py poll-loop" || true; sleep 5; pgrep -fl "pool.py poll-loop"`
Expected: a fresh `pool.py poll-loop` PID.

Run: `sleep 30 && tail -20 /Users/tonylee/solo/token-status-bar/poller.log`
Expected: the new startup banner (`Base interval: 300s, hot: 60s ...`) followed by `Polling N/M due accounts...` lines. With all accounts idle (<70%), subsequent wakes stay ~300s apart — confirm no rapid-fire polling of the same account.

- [ ] **Step 6: Commit**

```bash
git add backend/poller.py backend/test_window_history.py
git commit -m "feat(poller): Adaptive cadence with hot and pre-reset capture

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Backfill (`pool.py backfill-history`) + real-DB run

**Files:**
- Modify: `backend/window_history.py` (append `backfill`)
- Modify: `backend/pool.py` (docstring + command)
- Test: `backend/test_window_history.py` (append)

**Interfaces:**
- Consumes: `store.iter_snapshots` (Task 1), `detect_closed_windows`/`archive`/`append_jsonl`/`write_provider_csv` (Tasks 2-3), `dashboard.generate` (Task 4).
- Produces: `window_history.backfill(conn) -> int` (total rows inserted); CLI `python3 backend/pool.py backfill-history`.

- [ ] **Step 1: Write the failing test**

Append to `backend/test_window_history.py` (before `if __name__`):

```python
class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect()
        self.acct_id = store.upsert_account(self.conn, "claude", f"bf-{id(self)}@example.com", "bf")

    def test_backfill_streams_pairs_and_is_idempotent(self):
        _insert_snap(self.conn, self.acct_id, BASE, status="active",
                     primary_used_pct=80.0, primary_reset_at=BASE + 600, primary_window_s=18000)
        _insert_snap(self.conn, self.acct_id, BASE + 300, status="error")  # invisible (rule 1)
        _insert_snap(self.conn, self.acct_id, BASE + 900, status="active",
                     primary_used_pct=5.0, primary_reset_at=BASE + 600 + 18000,
                     primary_window_s=18000)
        window_history.backfill(self.conn)
        rows = store.list_window_history(self.conn, account_id=self.acct_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["window_kind"], "5h")
        self.assertEqual(rows[0]["reset_cause"], "natural")
        self.assertEqual(rows[0]["window_end"], BASE + 600)
        window_history.backfill(self.conn)  # re-run: UNIQUE constraint dedupes
        self.assertEqual(len(store.list_window_history(self.conn, account_id=self.acct_id)), 1)
```

Run: `python3 backend/test_window_history.py`
Expected: FAIL with `AttributeError: module 'window_history' has no attribute 'backfill'`

- [ ] **Step 2: Implement `backfill`**

Append to `backend/window_history.py`:

```python
def backfill(conn) -> int:
    """One-time replay: stream each account's snapshots oldest-first and feed
    consecutive successful pairs through detect_closed_windows.

    Idempotent — the UNIQUE constraint makes re-runs safe. Coupon evidence
    comes from the banked_resets column, which exists historically.
    """
    total = 0
    accounts = store.list_accounts(conn)
    for a in accounts:
        inserted: list[ClosedWindow] = []
        prev = None
        for s in store.iter_snapshots(conn, a["id"]):
            if s.get("status") not in SUCCESS_STATUSES:
                continue
            if prev is not None:
                inserted += archive(conn, a, detect_closed_windows(a["provider"], prev, s))
            prev = s
        if inserted:
            append_jsonl(a, inserted)
        print(f"  {a['provider']:12} {a['email'] or a['label']}: {len(inserted)} windows")
        total += len(inserted)
    for provider in sorted({a["provider"] for a in accounts}):
        write_provider_csv(conn, provider)
    return total
```

- [ ] **Step 3: Add the CLI command**

In `backend/pool.py`, add to the usage docstring (after the `dashboard` line):

```
  pool.py backfill-history             Archive historical windows from limit_snapshots
```

Add to `main` (next to the `dashboard` command):

```python
    if cmd == "backfill-history":
        import dashboard
        import window_history
        print("Backfilling window history from limit_snapshots...")
        n = window_history.backfill(DB)
        path = dashboard.generate(DB)
        print(f"Backfilled {n} closed windows. Dashboard: {path}")
        return 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 backend/test_window_history.py`
Expected: `Ran 35 tests ... OK`

- [ ] **Step 5: Run the backfill on the real DB (manual verification, part 1)**

This only inserts rows into the new table and writes files under `history/` — safe and idempotent.

Run: `python3 backend/pool.py backfill-history`
Expected: one line per account with a window count (the DB holds ~10 days of 5-minute snapshots, so expect at least a few dozen 5h windows for codex/claude), then `Backfilled N closed windows. Dashboard: .../history/dashboard.html`.

Run: `head -5 history/codex.csv && ls history/`
Expected: the CSV header matches `CSV_FIELDS`, rows show plausible KST timestamps and `natural` causes; `history/` contains `<provider>.csv`, `<provider>-<id>.jsonl`, `dashboard.html`.

Run: `python3 backend/pool.py dashboard --open`
Expected: browser opens the dashboard showing ~10 days of windows; filters and table sorting work; check the browser console for errors. If a real coupon redeem happened in the archived period, its bar shows the coupon badge.

- [ ] **Step 6: Commit**

```bash
git add backend/window_history.py backend/pool.py backend/test_window_history.py
git commit -m "feat(history): Backfill closed windows from snapshot history

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Swift — "Open Dashboard" menu item

**Files:**
- Modify: `app/TokenStatusBar.swift` — L10n tables (en ~line 633, ko ~744, zh ~855, ja ~966 — locate each by searching for `"poll_now"`), `StatusLoader` (after `runHeartbeat`, ~line 247), `addFooter` (~line 1834)

**Interfaces:**
- Consumes: `poolProcess(_:)` (existing — runs `pool.py <args>` with `AGENT_POOL_DB`/`AGENT_POOL_STATUS_JSON` pointed at `~/solo/token-status-bar/secrets`), `actionItem`, `t(_:)`; the `pool.py dashboard --open` command from Task 4.
- Produces: root-menu "Open Dashboard" item next to "Poll Now" that regenerates and opens `history/dashboard.html`.

- [ ] **Step 1: Add the L10n key to all four language tables**

Directly under each table's `"poll_now"` line:

```swift
            // en table:
            "open_dashboard": "Open Dashboard",
            // ko table:
            "open_dashboard": "대시보드 열기",
            // zh table:
            "open_dashboard": "打开仪表盘",
            // ja table:
            "open_dashboard": "ダッシュボードを開く",
```

- [ ] **Step 2: Add `runDashboard` to `StatusLoader`**

After `runHeartbeat` (~line 247), add:

```swift
    func runDashboard() {
        DispatchQueue.global(qos: .userInitiated).async {
            // Regenerates history/dashboard.html fresh; the backend's --open
            // flag then opens it in the default browser.
            let task = self.poolProcess(["dashboard", "--open"])
            try? task.run()
            task.waitUntilExit()
        }
    }
```

- [ ] **Step 3: Add the footer item**

In `addFooter` (~line 1834), directly after the `poll_now` line:

```swift
        menu.addItem(actionItem(t("open_dashboard")) { [weak self] in self?.loader.runDashboard() })
```

- [ ] **Step 4: Compile**

Run: `bash build.sh`
Expected: "Building TokenStatusBar..." then bundling with no swiftc errors.

- [ ] **Step 5: Commit**

```bash
git add app/TokenStatusBar.swift
git commit -m "feat(status-bar): Add Open Dashboard menu item

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: End-to-end verification, poller restart, build + relaunch

**Files:**
- No source changes expected (fixes go back to the owning task's files).

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Run the full backend test suite**

Run: `python3 backend/test_window_history.py && python3 backend/test_onboarding.py && python3 backend/test_status_dates.py && python3 backend/test_agy_usage.py`
Expected: all OK.

- [ ] **Step 2: Restart the poller with the final code**

The LaunchAgent `com.tonye.agentpool-poller` respawns the poller automatically after a kill. Do NOT start one by hand.

Run: `pkill -f "pool.py poll-loop" || true; sleep 5; pgrep -fl "pool.py poll-loop"`
Expected: a fresh `pool.py poll-loop` PID.

Run: `sleep 30 && tail -30 /Users/tonylee/solo/token-status-bar/poller.log`
Expected: adaptive startup banner; `Polling N/M due accounts...`; no `window-history detection failed` or `dashboard generation failed` lines; `Wrote .../secrets/status.json` still appears after each wake.

- [ ] **Step 3: Build the .dmg and relaunch the app (AGENTS.md requirement)**

Run: `bash build.sh --dmg`
Expected: `.app` installed to /Applications and `build/TokenStatusBar.dmg` created without error. Do not proceed to any PR if this fails.

Run: `osascript -e 'tell application "TokenStatusBar" to quit'; sleep 2; open /Applications/TokenStatusBar.app`

- [ ] **Step 4: Visual verification in the menu bar**

1. Open the menu: "Open Dashboard" appears next to "Poll Now" in the footer.
2. Click it → the default browser opens `history/dashboard.html` with the backfilled windows; provider/account/window-kind filters and table sorting work; reset-cause badges render.
3. Switch language (e.g. 한국어) and confirm the item reads "대시보드 열기"; switch back.
4. Leave the menu open ~10s and confirm the app itself stays responsive while the dashboard regenerates (it runs off the main queue).

- [ ] **Step 5: Overnight/spot check of live archiving (informational)**

When any account's window next rolls over (Claude/Codex 5h boundaries occur several times a day), confirm one new row appears:

Run: `sqlite3 ~/solo/token-status-bar/secrets/pool.db "SELECT window_kind, datetime(window_end,'unixepoch','localtime'), final_used_pct, reset_cause FROM window_history ORDER BY created_at DESC LIMIT 5"`
Expected: recent rows with plausible values; `history/<provider>.csv` mtime updates at the same moment.

- [ ] **Step 6: Final commit (if any fixes were made) and wrap up**

```bash
git status
```

If fixes were needed during verification, commit them in the style of the owning task. Then use the superpowers:finishing-a-development-branch skill flow (note: work is on `main` with pre-existing dirty files — do not commit unrelated dirty changes).

---

## Notes for reviewer

Spec ambiguities and judgment calls made while planning — none change approved design decisions, but each deserves a conscious sign-off:

1. **Antigravity `weekly` never fires today.** The spec maps antigravity `5h`/`weekly` to `primary_*`/`secondary_*`, but `poll_antigravity` only populates `primary_*` (the worst per-model rolling window; `secondary_*` is always NULL — the agy 5h/weekly split lives in `raw_json.extra.usage_windows`, out of scope per the spec's non-goals). The plan implements the mapping as specced: antigravity archives only the `5h` kind (which is really "most-constrained per-model window") until the poller someday stores the agy windows in snapshot columns.
2. **`detect_closed_windows` takes a `provider` argument.** The spec signature is `detect_closed_windows(prev_snap, new_snap)`, but `limit_snapshots` rows don't carry the provider, and the per-provider window mapping needs it. Mechanical extension, flagged for completeness.
3. **Redeem-vs-detection double archive.** Rule 4's direct archive stamps `window_end = redeem time`; if the confirmation re-poll fails, the next poll's detection would produce the same close with `window_end = snapshot midpoint` — a different timestamp, so the UNIQUE constraint alone would NOT dedupe it. The plan adds a narrow guard (`store.window_history_conflict`): early-cause rows are skipped when a row for the same (account, kind) already ends strictly inside the same snapshot pair. This goes slightly beyond the spec's "UNIQUE makes archiving idempotent" claim; the alternative is accepting occasional duplicate coupon rows.
4. **Rule 2 assumes fixed-window reset semantics.** Detection fires whenever `reset_at` moves forward >120s. Codex/Claude windows behave as fixed windows (reset epoch constant until rollover), but if any provider's reset timestamp drifts forward with usage (sliding window), spurious `natural` archives could occur. No "used_pct must also drop" guard was added because the spec doesn't specify one — worth a conscious decision.
5. **Gray zone between "natural" and "clearly early".** The spec defines natural as "roll happened at/after old reset_at" and early as "new window started >10 min before old reset_at", leaving rolls observed within 10 min *before* the boundary unassigned. The plan treats that band as natural (clock-skew tolerance).
6. **`history/` location.** "Project root" is implemented as env-overridable `AGENT_POOL_HISTORY_DIR` defaulting to `~/solo/token-status-bar/history` (mirrors `AGENT_POOL_DB`) so tests never touch the real directory. The bundled app and the LaunchAgent poller both leave the env unset, so both use the same real path.
7. **Copilot hold-last-good vs the scheduler.** `poll_copilot` sometimes saves no snapshot (transient 403 holds the previous one). A DB-timestamp-only scheduler would re-poll such an account in a tight loop; `run_loop` therefore keeps an in-memory `last_attempt` map. Implementation detail the spec didn't anticipate, load-bearing against hammering GitHub.
8. **Dashboard HTML is a contract, not verbatim code.** Task 4 gives the Python generator in full but specifies the HTML/JS as an 8-point checklist plus the mandatory `dataviz` skill load (per orchestrator instruction), rather than embedding several hundred lines of markup in the plan. This is a deliberate exception to the plan's no-placeholder rule; the self-containment properties are still machine-verified by the Step 5 test.
9. **Pre-reset capture reuses the ordinary poll.** The spec says "wake and poll just that account" — implemented as scheduling only that account as due (`poll_some([account])`), which also exports status.json on that wake (spec §3 requires the export once per wake). Claude pre-reset attempts do consume probe quota; at most ~5 attempts per boundary, only when the prior attempts failed.
