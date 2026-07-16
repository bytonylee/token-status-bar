# Real-time token sync + unified window display

Date: 2026-07-16
Status: approved

## Goal

Make the menu bar reflect token usage in near-real time and surface the most
decision-relevant number at all times, using two newly verified sources:

1. Claude's quota-free usage endpoint (`GET api.anthropic.com/api/oauth/usage`)
   replaces the two quota-consuming 1-token probes per poll.
2. Local CLI session logs (`~/.codex/sessions`, `~/.claude/projects`) provide
   second-level freshness between API polls, at zero network/quota cost.

On top of those sources, every provider's quota data is normalized into one
"window" model, and the UI is redesigned around the binding window: the menu
bar title always shows the riskiest active window (`41% · 1h12m`, colored),
and the dropdown shows per-account gauge bars with live countdowns and a
burn-rate projection.

## Background / findings (verified 2026-07-16 on this machine)

- `GET https://api.anthropic.com/api/oauth/usage` with the stored OAuth token
  (`anthropic-beta: oauth-2025-04-20`) returns HTTP 200 with:
  - `five_hour` / `seven_day`: `utilization` (percent), `resets_at` (ISO).
  - `limits[]`: one entry per window — `kind` (session / weekly_all /
    weekly_scoped), `percent`, `severity`, `resets_at`, `is_active`, and
    `scope.model.display_name` (the Fable weekly window appears here as
    `weekly_scoped` with display_name "Fable").
  - `extra_usage` (monthly extra-usage limit/credits) and `spend` (usage
    credit balance, purchase flags) — not collected today.
  - This supersedes both 1-token probes in `poll_claude` and the
    `_claude_fable_window` probe entirely.
- Codex CLI writes a `token_count` event to the active session JSONL
  (`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`) after every turn,
  containing `rate_limits` (used_percent, window_minutes, resets_at, credits
  balance, plan_type) — the server's own header values — plus real token
  counts (input / cached / output / reasoning) and the model context window.
- `~/.codex/auth.json` → `tokens.account_id` identifies which pool account
  the local Codex session belongs to (matches `accounts.account_id`).
- Claude Code writes per-message `usage` (input / output / cache_creation /
  cache_read tokens) to `~/.claude/projects/**/*.jsonl`.
- `status.json` currently exports only KST-formatted strings; the Swift app
  cannot tick countdowns between reloads.

## Design

### 1. Claude poller swap (`poller.py`)

`poll_claude` becomes a single `GET /api/oauth/usage` (Bearer token +
`anthropic-version` + `anthropic-beta: oauth-2025-04-20`):

- Map `five_hour` → primary window, `seven_day` → secondary window
  (used_pct, reset epoch parsed from ISO `resets_at`).
- Parse `limits[]` into normalized windows (section 3), including the
  Fable `weekly_scoped` entry; keep storing them in `raw_json` for
  `claude_extra`. `severity` and `is_active` are captured per window;
  `is_active` replaces the header-derived `binding_window` claim.
- New raw_json fields: `extra_usage`, `spend`.
- On 401: refresh token once and retry; then error snapshot. The old probe
  path is deleted, not kept as fallback (a fallback that silently spends
  quota is worse than an error).
- `_claude_profile` stays (plan / subscription metadata).
- Remove `HOT_INTERVAL_CLAUDE_S` special-casing and the claude-specific
  pre-reset floor in `compute_next_due`; Claude now uses the standard
  60s hot cadence since polls are free.

### 2. Local sync module (`local_sync.py`, new)

One scan function called from the poller daemon loop every
`LOCAL_SYNC_INTERVAL_S` (default 15s; env-overridable):

- **Codex**: list `~/.codex/sessions/**/*.jsonl` with mtime within 10
  minutes; for each, read the last 64KB and take the final `token_count`
  event. Attribute via `~/.codex/auth.json` `tokens.account_id` →
  `accounts.account_id`; if no match, skip (never guess).
  From `rate_limits` build a snapshot marked `source="local"` with the same
  primary/secondary fields the API poll produces; real token counts and
  context-window usage go to a `live` blob for the ticker.
- **Claude**: same mtime scan over `~/.claude/projects/**/*.jsonl`; extract
  last-event timestamp and a rolling 60-minute token total. Claude window
  percentages remain API-owned; local data feeds only the ticker and
  burn rate. Attribution: single claude account, else match via
  `~/.claude.json` oauth account email when multiple exist.
- Local snapshots are stored with `source` + `as_of` so they never overwrite
  a newer API reading; an API poll success always becomes the new baseline.
- `status.json` is re-exported only when scan output changed since the last
  export (dirty check), to avoid disk churn every 15s.
- Directories missing / unreadable / no recent files → silently API-only
  (status quo behavior).

### 3. Unified window model (`status.py`, `store.py`)

Each account in `status.json` gains:

```json
"windows": [
  {"kind": "5h", "label": "5h", "used_pct": 41.0,
   "reset_at_epoch": 1784786399, "severity": "normal",
   "is_active": true, "source": "api", "as_of_epoch": 1784782000}
]
```

- `kind`: `5h | daily | weekly | monthly | model_weekly` (Fable and
  Antigravity per-group windows use `model_weekly` with `label`).
- Every poller's existing primary/secondary/fable/usage_windows output is
  normalized into `windows[]` at export time; existing formatted-string
  fields stay for one release (the Swift app switches to `windows[]`
  in this same change, but external readers of status.json keep working).
- All timestamps are exported both formatted (KST string, existing fields)
  and as `*_epoch` numbers.
- Binding-window selection (pure function, backend): among active windows
  across all accounts, pick by severity rank (non-normal first), then
  highest used_pct, then soonest reset. Exported as top-level
  `headline: {account_id, kind, label, used_pct, reset_at_epoch, severity}`.

### 4. Burn rate (`status.py`)

For each window, compute `Δused_pct / Δt` over the trailing 60 minutes of
`limit_snapshots` (ignoring error rows and resets, i.e. drops in used_pct).
If the projection at current pace reaches 100% before `reset_at`, export
`projected_exhaust_epoch` on that window. The Swift app renders
"이 속도면 리셋 ~Nm 전 소진" in the submenu and escalates the menu-bar color
one level. Pure function + unit tests; no new tables (limit_snapshots
already holds the history).

### 5. Swift UI (`app/TokenStatusBar.swift`)

- **Menu bar title**: renders `headline` as `41% · 1h12m` next to the icon.
  Color: green < 50%, yellow 50–80%, red > 80% or severity ≠ normal or
  projected exhaustion. Countdown recomputed locally from
  `reset_at_epoch` on the existing 30s reload timer.
- **Account rows**: replace text lines with gauge rows — per window a
  `[label | bar | pct · time-left]` grid drawn in the existing custom row
  view. Copilot's many accounts stay grouped by provider with the worst
  window summarized on the group row (current submenu drill-down kept).
- **Live ticker**: one row at the dropdown bottom showing the most recent
  local event ("Codex 세션 +100.7k tok · 컨텍스트 39%"); hidden when no
  local data is fresh (< 10 min).
- Existing submenu detail lines (plan, billing, heartbeat, reset coupons)
  are unchanged.

### 6. Testing

- Fixtures captured from real responses: oauth/usage JSON, a Codex
  `token_count` JSONL line, a Claude `usage` JSONL line.
- Unit tests: oauth/usage parser (incl. Fable scoped window, severity,
  extra_usage/spend), local-sync tail parser + attribution (match /
  no-match), window normalization, binding-window selection, burn-rate
  projection (incl. reset-drop handling).
- Swift: `build.sh` app + dmg build must pass before PR (AGENTS.md);
  relaunch the app and verify menu bar title, gauges, ticker manually.

## Implementation order

1. Claude endpoint swap + parser tests (immediate quota win).
2. `windows[]` + epoch export + binding-window selection.
3. `local_sync.py` + poller-loop integration + dirty-check export.
4. Swift UI: menu-bar headline, gauge rows, live ticker, countdown tick.
5. Burn-rate projection + color escalation.
6. Build, dmg, relaunch, manual verification.

## Out of scope

- Devin per-session ACU history, Copilot overage-cost estimation,
  Antigravity full per-model list UI (submenu keeps worst-window summary).
- Web dashboard changes (`dashboard.py` untouched).
- Push/webhook-style provider APIs (none exist for these providers).
