# Token operating admin system — v2 (replan)

Date: 2026-07-22
Status: approved (replanned)
Supersedes: v1 of this document (catalog/Kimi-first plan)
PRD: `docs/superpowers/prds/2026-07-22-token-admin-system-prd.md`

## Goal

Four themes, in priority order:

1. **Exact state, every poll** — every poll cycle must classify each
   account's subscription lifecycle (paid / expired / renewing / reset)
   and every limit window's status (ok / warning / exhausted / reset)
   deterministically, with no stale carry-over between polls.
2. **Dropdown UI/UX experiment** — restructure the dropdown around
   "what can I use right now", A/B-able via a layout flag.
3. **Auto account swap** — when the active account for an agent CLI
   exhausts *all* its usable windows, automatically swap the CLI's
   credentials to the best same-provider account in the pool.
4. Retained from v1 (de-scoped to later milestones): pricing catalog,
   effective-cost score, notifications. Kimi poller is **deferred**.

## Locked decisions

| # | Decision | Choice |
|---|----------|--------|
| D1 | Swap mode | **Fully automatic** when all windows exhausted (per user) |
| D2 | Docs | Rewrite v1 spec/plan in place (uncommitted) |
| D3 | Lifecycle detection | Derived per-poll from provider data already fetched; no new provider APIs required for M1 |
| D4 | Dropdown experiment | Two layouts behind a `layout` toggle in the app (persisted in UserDefaults), switchable live |
| D5 | Swap scope | Codex first (file-based auth, mechanism verified); Claude behind a feature flag after keychain research task |

## 1. Exact lifecycle + limit state (backend)

### 1.1 Problems today

- `limit_snapshots.status` is only `active|error|expired|rate_limited`,
  set ad-hoc per poller; "expired subscription" vs "expired token" vs
  "quota exhausted" are conflated or missed.
- `normalize_windows()` returns windows whose `reset_at_epoch` is in
  the past with their old `used_pct` — the UI shows 87% used on a
  window that actually reset to 0% an hour ago. Only `select_headline`
  clamps this (`status.py:519`).
- Subscription paid/expired info exists per provider
  (`subscription_meta` for codex: `has_active_subscription`,
  `renews_at`, `expires_at`; claude `subscription_status` in raw_json;
  copilot sku; devin/xai `plan_start/plan_reset`) but is never
  normalized into one field the UI or a router can branch on.

### 1.2 `account_state()` — one pure function, computed at export

New in `status.py`:

```python
def account_state(item, now=None) -> dict:
    """Exact classification, recomputed on every export.

    Returns {
      "auth":  "ok" | "token_expired" | "error",
      "subscription": "paid" | "free" | "expired" | "renews_soon"
                      | "unknown",
      "sub_renews_at": iso | None,     # next billing anniversary
      "sub_expires_at": iso | None,    # hard end if cancelled
      "quota": "ok" | "warning" | "exhausted" | "unknown",
      "usable": bool,                  # auth ok AND sub not expired
                                       # AND quota != exhausted
      "binding_window": {...} | None,  # riskiest non-expired window
    }
    """
```

Rules (deterministic, unit-tested):

- `auth`: `token_expired` when the stored token's `expires_at < now`
  and last refresh failed; `error` when last snapshot status is
  `error`; else `ok`.
- `subscription`:
  - codex: from `subscription_meta.has_active_subscription` +
    `is_active_subscription_gratis` (`paid`/`free`), `expired` when
    `expires_at < now` and not active; `renews_soon` when `renews_at`
    within 72h.
  - claude: `raw_json.profile.subscription_status`.
  - copilot: sku → `free`/`paid`; xai/devin/antigravity: plan
    presence → `paid`|`unknown`.
- `quota` from **fresh** windows only (see 1.3): `exhausted` when
  every active window has `used_pct >= 100` or severity
  `rate_limited`/`exceeded`, or snapshot status is `rate_limited`;
  `warning` when the binding window `>= 80%`; else `ok`. `unknown`
  when there are no windows and no rate-limit signal.
- Exported per-account as `"state": {...}` in `status.json`.

### 1.3 Window reset correctness ("for each time")

`normalize_windows()` gains a post-pass `refresh_windows(windows, now)`:

- If `reset_at_epoch < now`: the window **has reset**. Mark it
  `"phase": "reset"`, set `used_pct_effective = 0`, and roll
  `reset_at_epoch` forward by `window_s` when the cadence is known
  (5h/weekly), else keep it and flag `stale: true`.
- Else `phase` is `"live"`, `used_pct_effective = used_pct`.
- All consumers (headline, gauges, `account_state`, best-account,
  swap) read `used_pct_effective` — one clamp point instead of the
  current per-consumer special cases (removes the ad-hoc clamp in
  `select_headline`).
- Snapshot freshness: any window whose snapshot `as_of` is older than
  3× the poll interval is marked `stale: true`; stale windows never
  count as `exhausted` for swap decisions (fail-safe: don't swap on
  old data).

### 1.4 Lifecycle transition events

New pure function `detect_transitions(prev_export, next_export)` in a
new `backend/lifecycle.py`, called by the daemon after each export.
Emits events (stored in a new `lifecycle_events` table:
`ts, account_id, event, detail`):

- `window_reset` — a window's phase flipped live→reset
- `sub_paid` — subscription unknown/free/expired → paid
- `sub_expired` — paid → expired
- `sub_renewed` — `sub_renews_at` passed while still paid
- `quota_exhausted` / `quota_recovered`
- `account_swapped` (written by the swap engine, §3)

These power notifications later (M7) and give an audit trail now.

## 2. Dropdown UI/UX experiment (Swift)

### 2.1 Experiment mechanics

- `UserDefaults` key `menuLayout`: `"classic"` (today's provider-grouped
  list) or `"usable-first"` (new). Footer gets a "Layout: Usable-first /
  Classic" toggle; switch rebuilds the menu instantly. No rebuild/
  reinstall needed to compare — this is the experiment.

### 2.2 `usable-first` layout

```
  Agent Pool · 7 accounts · 5 usable
  Updated 17:12 · heartbeat ok
  ──────────────────────────────
  USE NOW
  ● Codex #2  tony@…   weekly 78% left        ← best per provider
  ● Claude    max@…    5h 100% left
  ──────────────────────────────
  LIMITED / COOLING DOWN
  ◐ Codex #1  main@…   5h 0% · resets 18:00   ← countdown, live phase
  ◐ Copilot   …        monthly 3% left
  ──────────────────────────────
  BLOCKED
  ○ xai       …        sub expired 07-19      ← why, exactly
  ○ devin     …        token expired · reconnect
  ──────────────────────────────
  ⇄ auto-swap: codex → #2 at 16:41            ← last swap event
  ⚡︎ live ticker (unchanged)
  Settings ▸   Layout ▸   Quit
```

- Sections come straight from `state.usable` / `state.quota` /
  `state.auth` — no UI-side heuristics.
- Every row shows the *reason* it's in its section (binding window +
  `used_pct_effective`, or `sub_expired`, or `token_expired`).
- Reset countdowns tick with the existing 1s ticker; when a countdown
  hits 0 the row moves to USE NOW on next rebuild (phase flip from
  §1.3, no waiting for the next poll).
- Status dot colors: green = usable, half = cooling down (quota
  exhausted but sub fine), hollow = blocked (auth/sub problem).
- Account submenus (detail views) unchanged in both layouts.
- Subscription line added to each submenu: `plan · paid · renews 08-01`
  or `expired since 07-19`, from `state`.

### 2.3 Success criteria for the experiment

Keep both layouts through at least a week of use; the keeper is the
one that answers "which account do I use now?" without opening a
submenu. Classic stays default until then.

## 3. Automatic account swap (same-provider multi-account)

### 3.1 Mechanism per provider

| Provider | Active-account location | Swap mechanism | Status |
|----------|------------------------|----------------|--------|
| codex | `~/.codex/auth.json` (`tokens.account_id`, already read by `local_sync.codex_active_account_id`) | Rewrite `auth.json` with pool account's `{id_token, access_token, refresh_token, account_id}`; back up prior file | **M4 — implement** |
| claude | macOS keychain item `Claude Code-credentials` (+ `~/.claude.json` `oauthAccount`) | `add-generic-password -U` via `security -i` stdin + patch `~/.claude.json`; spike findings in §3.4 | **M5 — implemented behind `auto_swap.claude` (default off), supervised trial pending** |
| copilot | `gh auth` / `~/.config/github-copilot/apps.json` | research only | backlog |
| others | n/a (single account or IDE-managed) | — | out of scope |

### 3.2 Swap engine (`backend/swap.py`)

```python
def swap_candidates(items, provider, active_upstream_id) -> list
def should_swap(active_item, candidates, now) -> dict | None
def perform_swap(conn, provider, target_account) -> dict   # side effects
```

- **Trigger** (daemon loop, after export): for each provider with >1
  pool account and a known active local account —
  `active.state.quota == "exhausted"` (all fresh windows at 100% /
  rate_limited) **and** at least one candidate with
  `state.usable == true`.
- **Candidate ranking**: most headroom on binding window, tie-break by
  earlier next reset. (Effective-cost tie-break arrives with M6.)
- **Safety rails** (all mandatory for "fully automatic"):
  - never swap on `stale` windows or when the active account's data
    is older than 3× poll interval;
  - 30-min cooldown per provider (no flapping);
  - refuse if a live agent session was active in the last 120s
    (`live_activity` freshness) — don't yank credentials mid-turn;
  - back up the replaced `auth.json` to
    `secrets/swap_backups/<provider>-<ts>.json` (0600);
  - atomic write (tmp + rename), 0600 perms;
  - write `account_swapped` lifecycle event + fire a macOS
    notification ("Codex swapped: main@ → tony@ (78% weekly left)");
  - kill-switch: `settings.json` `"auto_swap": {"codex": true,
    "claude": false}` — per-provider, default on for codex only.
- **Swap-back**: when the previously-active account's windows reset
  and it becomes `usable` again, it simply re-enters the candidate
  pool; no automatic swap-back unless the current one exhausts
  (avoids ping-pong).
- Manual override stays: dropdown row action "Swap to this account"
  on any usable candidate (works in both layouts).

### 3.3 Token freshness

`perform_swap` refreshes the target's OAuth token first if
`expires_at < now + 10min` (existing refresh paths in `poller.py`),
so the CLI never receives an expired token.

### 3.4 M5 spike findings — Claude Code credential swap

Investigated 2026-07-22 on a live macOS Claude Code install (read-only;
key names only, no secret values). **Verdict: viable-with-supervised-trial**
— implemented behind `auto_swap.claude` (default **off**).

**Keychain item shape.** Login keychain, class `genp`, service
`"Claude Code-credentials"`, account attribute `acct` = the macOS login
user name (e.g. `tonylee`). No alternate service names exist
(`"Claude Code"`, `"Claude Code credentials"`, `"claude-code"` all absent)
and the Linux fallback `~/.claude/.credentials.json` does not exist on
macOS. Password payload is JSON:

```
{"claudeAiOauth": {accessToken, refreshToken,
                   expiresAt (ms epoch), refreshTokenExpiresAt (ms epoch),
                   scopes (list of 5), subscriptionType, rateLimitTier}}
```

**`~/.claude.json`.** `oauthAccount` identifies the active account:
`{accountUuid, emailAddress, organizationUuid, organizationName, …}`
among ~85 unrelated top-level keys — the swap patches only the
`oauthAccount` identity fields and preserves everything else.

**Pool-token compatibility (the real viability question).** Our
`oauth.py` claude flow uses the *same* OAuth client as Claude Code
(client_id `9d1c250a-e61b-44d9-88ed-5944d1962f5e`) and the identical
5-scope set (`user:profile user:inference user:sessions:claude_code
user:mcp_servers user:file_upload`); tokens have the same `sk-ant-o…`
shape/length, and `tokens.raw_json` carries `account.uuid`,
`account.email_address`, `organization.{uuid,name}`,
`refresh_token_expires_in` — everything needed to rebuild both the
keychain payload and the `oauthAccount` patch. **Shape-compatible.**
Note: claude pool rows may have `accounts.account_id = NULL`, so claude
swap identity is the account *email* (matched against
`oauthAccount.emailAddress`), not an upstream id.

**Write mechanism chosen.** `add-generic-password -U` (update-in-place,
reusing the existing item's `acct` attribute) fed through `security -i`
*stdin* so the token JSON never appears on argv (ps-visible). Escaping
(`\` → `\\`, `"` → `\"`) verified to round-trip a JSON payload
byte-identically on a throwaway service name (`TSB-spike-test`:
add → read-back-identical → -U update → delete, all rc 0). The real
item was never written during the spike.

**Residual risks (why supervised trial is required before enabling):**

1. **Keychain ACL**: the existing item's ACL is held by the Claude Code
   binary; rewriting it via `security`(1) creates a new ACL owned by
   `security`, so Claude Code will likely show a one-time keychain
   prompt ("Claude Code wants to use…" → *Always Allow*) on next access.
   Not destructive, but needs a human at the screen.
2. **Re-read behavior**: unconfirmed whether a *running* Claude Code
   session re-reads swapped credentials without `/login`; new sessions
   are expected to pick them up. Only a live trial answers this.

**Supervised live trial (manual, run while watching the screen):**

```
python3 backend/pool.py accounts                 # find claude account db id N
python3 backend/pool.py swap --provider claude --account-id N --force
```

Then (1) approve the keychain prompt with "Always Allow" if it appears,
(2) start a fresh `claude` session and confirm it works and shows the
swapped account (`/status`), (3) check an already-running session keeps
working. Rollback: the pre-swap keychain JSON is in
`secrets/swap_backups/claude-<ts>.json`; restore with the same
`security -i` mechanism or re-`/login`. If the trial passes, flip
`"auto_swap": {"claude": true}` in `secrets/settings.json`.

## 4. Retained v1 features (re-scoped)

- **Catalog** (`catalog.json`/`catalog.py`): unchanged design, moves to
  M6; still feeds `plan_label` and later effective-cost.
- **Effective-cost + best-account export**: M6, unchanged design;
  `best_account_now` reuses §1's `state` and §3's candidate ranking.
- **Notifications** (`notify.py` + dedup table): M7; window-reset and
  swap events come free from `lifecycle_events`.
- **Kimi poller, staleness checker, promos UI**: deferred to backlog.

## 5. Testing

- `test_account_state.py`: every lifecycle branch per provider
  (paid/expired/renews_soon/free/unknown; token_expired; quota
  ok/warning/exhausted/unknown; usable truth table).
- `test_refresh_windows.py`: past-reset zeroing, cadence roll-forward,
  stale flag, `used_pct_effective` consumption by headline.
- `test_lifecycle.py`: transition detection across two exports, event
  rows, idempotency (same export twice = no events).
- `test_swap.py`: trigger truth table, ranking, cooldown, stale-data
  refusal, live-session refusal, auth.json write (tmpdir + fake
  `CODEX_HOME`), backup creation, kill-switch off.
- Swift: manual verification of both layouts (AGENTS.md build ritual).
- Existing suite (110+) must pass.

## Out of scope

- Kimi/Gemini/Z.ai pollers; Cursor entirely.
- Claude auto-swap enabled-by-default (needs spike result first).
- Auto-updating catalog prices; web dashboard changes.
- Cross-provider routing ("use claude instead of codex") — swap is
  same-provider only.
