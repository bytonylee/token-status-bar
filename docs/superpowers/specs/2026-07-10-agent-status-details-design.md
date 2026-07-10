# Agent status details: plan dates, Antigravity 5h/weekly, heartbeat upgrades

Date: 2026-07-10
Status: approved

## Goal

Three improvements to the TokenStatusBar menu app:

1. Antigravity submenu shows the same 5h/weekly limit breakdown as the IDE's
   `/usage` panel (Gemini models and Claude/GPT models, each with a five-hour
   and a weekly window).
2. Every agent's submenu has an identical 5-row Status top section: Plan,
   Plan started, Plan resets, Token expires, Last poll — `n/a` when unknown.
3. Heartbeat surfaces success/fail, last attempt, last success, next run, and
   adds a manual "Run heartbeat now" action (global and per-agent).

## Background / findings

- Antigravity's `/usage` data comes from
  `cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary`. The
  `QuotaSummaryBucket` proto has a `window` field, but with the pool's OAuth
  token the server always returns a collapsed "All Models" group with
  per-model `remainingFraction` only — no 5h/weekly split, regardless of
  User-Agent, project, or request shape. The rich view appears gated to
  Antigravity's own client identity.
- Driving the interactive `agy` CLI through a pseudo-terminal and sending
  `/usage` renders the full breakdown (verified 2026-07-10):
  group headers `GEMINI MODELS` / `CLAUDE AND GPT MODELS`, each with
  `Weekly Limit` and `Five Hour Limit`, a percentage, and either
  `Refreshes in <Nh Nm>` or `Quota available`.

## Design

### 1. Antigravity 5h/weekly (`backend/agy_usage.py`, `poller.py`, `status.py`, Swift)

- New module `backend/agy_usage.py`:
  - `fetch_usage() -> list[dict] | None`. Forks `agy` in a pty (stdlib `pty`,
    `TERM=xterm-256color`, explicit winsize), waits for the prompt, types
    `/usage`, presses Enter, reads until the panel is captured, then kills
    the child.
  - Parses ANSI-stripped panel text into
    `[{"group": "Gemini" | "Claude and GPT", "window": "5h" | "weekly",
       "remaining_pct": float, "reset_at": epoch | None}]`.
    `Refreshes in 151h 36m` → `reset_at = poll time + delta`;
    `Quota available` → `reset_at = None`, `remaining_pct = 100.0`.
  - Hard timeout 60s. Any failure (agy missing, timeout, parse miss) returns
    `None` — never raises into the poller.
- `poller.poll_antigravity` calls `agy_usage.fetch_usage()` and stores the
  result under `raw_json.extra.usage_windows`. Existing API-based snapshot
  fields are unchanged and remain the fallback.
- `status.provider_extra("antigravity", …)` exports `usage_windows` with
  formatted reset strings.
- Swift `buildAntigravitySubmenu`: when `usage_windows` is present, the
  "Limit session" group shows four rows (Gemini 5h / Gemini weekly /
  Claude & GPT 5h / Claude & GPT weekly) using the existing used-percent line
  style (`used %` accent, low-quota warn). When absent, keep today's single
  most-constrained row.
- Accepted limits: uses the machine's logged-in agy session (single account,
  same as heartbeat); adds ~20–30s per antigravity poll; parser is coupled to
  the CLI's panel text and fails soft.

### 2. Uniform Status section (all providers)

- Swift `statusGroup` always renders 5 rows: Plan, Plan started, Plan resets,
  Token expires, Last poll. Missing values render a localized `n/a`
  (en/ko/zh/ja).
- `buildCodexSubmenu` and `buildClaudeSubmenu` switch to the shared
  `statusGroup`; Codex keeps its extra `account created` and
  `payment history` rows via the existing `extra` mechanism.
- Derivations in `status.py`:
  - Claude: `plan_reset` = next monthly anniversary of
    `subscription_created_at` (same day-of-month; clamp to month end),
    computed at export time.
  - Copilot: `plan_start` = `quota_reset_date` minus one month (same
    clamping).
  - Antigravity: neither is available; stays `n/a`.

### 3. Heartbeat

- `status.heartbeat_meta` adds `heartbeat_last_success` (the
  `latest_success` row is already queried; export its timestamp).
- Swift shows, in both the global Heartbeat submenu (per account) and each
  agent submenu's heartbeat section: status (Success/Fail), last attempt,
  last success, next run.
- `pool.py heartbeat` accepts `--account <id>`; `heartbeat.run_once` filters
  to that account.
- Swift adds "Run heartbeat now" action items:
  - bottom of the global Heartbeat submenu → `pool.py heartbeat`
  - inside each codex/claude/antigravity agent submenu →
    `pool.py heartbeat --account <id>`
  Both use the existing `poolProcess` plumbing, run async, then run
  `pool.py export` and reload the menu.

## Error handling

- agy parse failure: log to poller log, keep API fallback view.
- Manual heartbeat failure: refresh_log records it as today; menu shows the
  failure after reload. No extra UI.
- Date derivation with missing/invalid inputs: emit nothing → `n/a` row.

## Testing

- New Python tests: `/usage` panel parser against captured pty fixture text;
  Claude anniversary and Copilot start-date derivation (incl. month-end
  clamps). Existing `test_onboarding.py` must stay green.
- Manual: kill poller (LaunchAgent respawns with new code), `pool.py poll`
  + `pool.py export`, inspect `status.json`; build `.app`/`.dmg` via
  `build.sh`, relaunch app, verify all three menu areas.
