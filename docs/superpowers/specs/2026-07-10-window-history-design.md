# Window history: archive usage before limit resets

Date: 2026-07-10
Status: approved

## Goal

Never lose "how much of a limit was used" when the limit resets. Every quota
window (5h / weekly / daily / monthly) gets one durable record when it closes,
whatever caused the close:

1. natural rollover (weekly reset, monthly reset after a new payment),
2. a reset coupon redeemed (Codex banked reset credits — from this tool or
   from another device),
3. a provider-side ("company") reset before the expected boundary.

The records live in `pool.db`, are exported as CSV + JSONL files, and are
visualized by a self-contained HTML dashboard opened from a new menu item.

## Background / findings

- `limit_snapshots` already stores every 5-minute poll (~34k rows over ~10
  days, nothing prunes it), so raw history exists — but nothing marks reset
  boundaries. Reconstructing "final usage before reset" today means scanning
  thousands of rows and inferring where `used_pct` dropped.
- Error snapshots (connection failures, expired tokens) are saved with NULL
  usage fields; any detection must ignore them or a network blip near a reset
  would corrupt classification.
- `poll_claude` costs two real 1-token `/v1/messages` probes per poll, so
  polling Claude more often consumes quota and inflates the measurement.
  All other providers use read-only metadata endpoints.
- `poller.redeem_reset()` is the in-tool coupon event; coupons redeemed
  elsewhere are only observable via polling (`banked_resets` decreasing,
  `reset_credits` status flips).

## Design

### 1. Data model (`store.py`)

New table:

```sql
CREATE TABLE IF NOT EXISTS window_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    window_kind TEXT NOT NULL,      -- see mapping below
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

The UNIQUE constraint makes archiving idempotent (`INSERT OR IGNORE`), so
live detection, pre-reset capture, and backfill can overlap safely.

Per-provider window mapping (snapshot columns → `window_kind`):

| provider    | window_kind      | source fields                                      |
|-------------|------------------|----------------------------------------------------|
| codex       | `5h`, `weekly`   | `primary_*`, `secondary_*`                         |
| claude      | `5h`, `weekly`   | `primary_*`, `secondary_*`                         |
| claude      | `weekly_fable`   | `raw_json.fable` (best-effort; skip if absent)     |
| antigravity | `5h`, `weekly`   | `primary_*`, `secondary_*`                         |
| copilot     | `monthly_premium`| `primary_used_pct` + `primary_reset_at`            |
| copilot     | `monthly_chat`   | `secondary_used_pct` (no reset ts → rule 3; cause hint = premium reset date) |
| xai         | `monthly`        | `monthly_used_pct`, `monthly_period_start/end`     |
| devin       | `daily`, `weekly`| `daily/weekly_quota_remaining_percent` (used = 100 − remaining), cycle hint = `plan_reset_unix` |

Zero-usage windows are not archived (`final_used_pct > 0` required) — they
carry no information and would add ~5 rows/day/account of noise.

### 2. Reset detection (`backend/window_history.py`, new module)

One pure function shared verbatim by live polling and backfill:

```python
detect_closed_windows(prev_snap, new_snap) -> list[ClosedWindow]
```

Rules:

1. **Only successful snapshots participate** (`status` in
   `active`/`rate_limited`). Error rows are invisible to detection — a
   connection failure can never fake or corrupt a reset.
2. **Windows with a reset timestamp** (codex/claude/antigravity 5h+weekly,
   copilot premium, xai monthly): the window closed when `reset_at` moved
   forward by more than a tolerance (120s). Then:
   - `window_end` = old `reset_at` (natural) or, for early resets, the
     midpoint between the two snapshots' `ts`,
   - `final_used_pct` = prev snapshot's value,
   - cause: `natural` if the roll happened at/after old `reset_at`
     (poll gaps spanning the boundary — sleep, outage — are still `natural`,
     with `details.staleness_s` recording how stale the final reading is);
     if clearly early (new window started >10 min before old `reset_at`):
     `coupon` when coupon evidence exists (`banked_resets` decreased between
     the two snapshots, or a `reset_credits` row flipped from available),
     else `provider_reset`.
3. **Windows without a reset timestamp** (copilot chat, devin daily/weekly):
   the window closed when `used_pct` dropped by >10 points. `window_end` =
   new snapshot ts; cause `natural` if within 1h of a related known boundary
   (copilot `quota_reset_date`, devin `plan_reset_unix`, local midnight for
   `daily`), else `unknown`.
4. `redeem_reset()` archives directly on HTTP 200 — cause `coupon`, exact
   `credit_id` in `details` — before its confirmation re-poll, so the
   coupon row exists even if that re-poll fails.

The live hook runs inside the poll cycle right after `save_snapshot`,
comparing against the account's previous successful snapshot (single indexed
row fetch — never a history scan).

### 3. Poll cadence: adaptive + pre-reset capture (`poller.py`)

Goal: catch unpredictable resets (hard reset, coupon redeemed elsewhere)
close to the moment they happen, and have a fresh reading just before known
boundaries — without wasting resources when nothing is at stake.

- **Base cadence unchanged:** every account polls every `POLL_INTERVAL`
  (300s).
- **Hot accounts poll every 60s:** an account is hot while any of its windows
  is ≥ 70% used. Rationale: an early reset only destroys meaningful data when
  usage is high. **Claude exception:** hot cadence capped at 180s because its
  poll consumes quota (two 1-token probes).
- **Pre-reset capture:** when any account's next `reset_at` is within 5 min,
  wake and poll just that account; on failure retry every ~60s until the
  boundary (last attempt ≥30s before it). First success wins. If every
  attempt fails, the archive still happens from the last good snapshot
  (rule 1/2 above), just staler.
- `run_loop` replaces its flat `time.sleep(POLL_INTERVAL)` with: compute the
  earliest of (next base tick, next hot tick per hot account, next pre-reset
  wake), sleep until then, poll only the accounts that are due, export
  `status.json` once per wake.
- Knobs (env vars with these defaults): `HOT_THRESHOLD_PCT=70`,
  `HOT_INTERVAL_S=60`, `HOT_INTERVAL_CLAUDE_S=180`, `PRERESET_LEAD_S=300`,
  `PRERESET_RETRY_S=60`.

### 4. Exports (`backend/window_history.py`)

Written to `history/` at the project root (gitignored, like `pool.db`):

- `history/<provider>.csv` — one row per closed window across that
  provider's accounts: account email/label, window_kind, window_start,
  window_end (both ISO local time), final_used_pct, reset_cause,
  final_snapshot_ts, staleness_s. Rewritten from `window_history` whenever
  it gains rows (files are KB-sized, a few writes per day).
- `history/<provider>-<account_id>.jsonl` — append-only, one JSON object per
  closed window, same fields plus `details`. Appended at archive time (O(1)).

### 5. Dashboard (`backend/dashboard.py`, new module)

- `history/dashboard.html`, fully self-contained (inline CSS/JS, data
  embedded as a JSON blob, no CDN — works offline).
- Content: per-window usage history over time (bar per closed window,
  colored by provider), provider/account/window-kind filters, reset-cause
  badges (natural / coupon / provider reset / unknown), and a sortable table
  of all rows.
- Regenerated only when `window_history` gains rows or on demand
  (`python3 backend/pool.py dashboard [--open]`) — never on ordinary poll
  ticks. `--open` opens it via `open`.

### 6. Menu item (Swift, `app/TokenStatusBar.swift`)

- New "Open Dashboard" item in the root menu (near "Poll Now"), localized
  like existing items. Action: run the backend `pool.py dashboard --open`
  the same way Poll Now invokes the backend, so the file is regenerated
  fresh and then opened in the default browser.

### 7. Backfill (`pool.py backfill-history`)

- One-time command: for each account, stream `limit_snapshots` in `ts` order
  (row iterator, constant memory) and feed consecutive successful-snapshot
  pairs through `detect_closed_windows`. Coupon evidence comes from the
  `banked_resets` column, which exists historically.
- Idempotent (UNIQUE constraint), so re-running is safe. Regenerates exports
  and dashboard at the end.

## Resource budget

- No new processes; everything runs in the existing poller daemon. The
  dashboard is a static file — zero idle cost.
- Idle steady state is identical to today: one wake per 5 min. Faster ticks
  occur only while an account is ≥70% used or within 5 min of a known reset;
  each tick is 1–2 small HTTP requests + one SQLite insert, sub-second.
- Detection is O(1) per poll (compare against last successful snapshot via
  the existing `idx_snap_ts` index). No history scans in steady state.
- JSONL appends are O(1); CSV rewrites and dashboard regeneration happen only
  when a window actually closes (a few times per day).
- Backfill is a single streaming pass, run once.

## Error handling

- Detection ignores error/NULL snapshots entirely (rule 1).
- Pre-reset capture failures fall back to the last good snapshot; the archive
  row is always written, with `details.staleness_s` marking fidelity.
- Export/dashboard generation failures are logged and never fail the poll
  (same pattern as the existing `status.cmd_export` try/except).
- Archiving uses `INSERT OR IGNORE`; duplicates are structurally impossible.

## Testing

- Unit tests for `detect_closed_windows` (new `backend/test_window_history.py`,
  stdlib unittest like existing tests): natural rollover, early reset with
  coupon evidence, early reset without evidence, error-snapshot gap, sleep
  gap spanning a boundary, used-pct-drop windows (copilot chat / devin),
  zero-usage skip, tolerance jitter.
- Scheduler unit test: next-wake computation across base/hot/pre-reset inputs.
- Backfill test against an in-memory fixture DB; export format tests
  (CSV headers, JSONL round-trip).
- Manual verification: run backfill on the real DB, open the dashboard,
  confirm ~10 days of windows appear; then `build.sh` → `.dmg` → relaunch the
  app and click "Open Dashboard" (per AGENTS.md; kill the poller so the
  LaunchAgent respawns it with the new code).

## Non-goals

- Pruning/retention of raw `limit_snapshots` (disk growth is slow; can be a
  later follow-up now that boundaries are preserved).
- xAI daily request/token limits (transient rate limits, not quota windows).
- Live/server-backed dashboard; per-model Antigravity window archiving
  (`usage_windows` raw data stays available in snapshots if wanted later).
- Alerting/notifications on resets.
