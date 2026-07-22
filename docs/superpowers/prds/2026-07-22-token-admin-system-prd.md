# PRD: Token operating admin system v2

Date: 2026-07-22
Status: approved
Spec: `docs/superpowers/specs/2026-07-22-token-admin-system-design.md`
Plan: `docs/superpowers/plans/2026-07-22-token-admin-system.md`

## Problem

The menu-bar app monitors token quotas across 7+ AI-agent accounts,
but the user still has to *interpret* it:

- Window data goes stale — a 5h window that reset an hour ago still
  shows 87% used. Subscription facts (paid, expired, renewal date)
  are fetched but never surfaced as one clear state.
- The dropdown groups by provider, not by "what can I use right now",
  so answering the daily question requires opening submenus.
- When the active account for an agent CLI (e.g. Codex) burns through
  all its quota, the user must notice, pick another account, and
  re-login by hand — mid-work interruption, often at the worst time.

## Users

Single power user (repo owner) running multiple paid accounts per
agent CLI across Codex, Claude Code, Copilot, xAI, Antigravity, Devin.

## Goals

| # | Goal | Metric |
|---|------|--------|
| G1 | Every poll classifies each account exactly: subscription (paid / expired / renews-soon / free) and quota (ok / warning / exhausted), with window resets applied the moment they pass | 0 stale-window rows in UI; state truth-table fully unit-tested |
| G2 | Dropdown answers "which account do I use now?" at a glance | New usable-first layout ships beside classic; keeper chosen after ~1 week of live A/B use |
| G3 | Zero-touch continuity: exhausting the active Codex account auto-swaps CLI credentials to the best same-provider account | Swap completes < 1 poll cycle after exhaustion; user notified, never interrupted mid-session |
| G4 | Auditability: every lifecycle change (reset, paid, expired, swap) is recorded | `lifecycle_events` table queryable; notifications derive from it |

## Requirements

### P0 — exact state, every poll
- Per-account `state` export: auth (ok / token_expired / error),
  subscription (paid / free / expired / renews_soon / unknown) with
  renewal/expiry dates, quota (ok / warning / exhausted / unknown),
  and a single `usable` boolean.
- Windows past their reset time show 0% used immediately (roll forward
  when cadence known); data older than 3× poll interval is flagged
  stale and never drives decisions.

### P0 — auto account swap (Codex)
- Trigger: active account's quota `exhausted` on all fresh windows AND
  a usable same-provider candidate exists.
- Fully automatic: rewrite `~/.codex/auth.json` atomically, back up
  the old file, refresh target token first if near expiry.
- Guardrails: no swap on stale data, 30-min per-provider cooldown, no
  swap while an agent session was live in the last 120s, per-provider
  kill-switch (default: codex on, others off), macOS notification on
  every swap, manual "Swap to this account" always available.
- Claude swap ships only after a keychain spike proves Claude Code
  re-reads credentials without relogin; flag off by default.

### P1 — dropdown UX experiment
- Two layouts switchable live from the menu (persisted): `classic`
  (today) and `usable-first` (USE NOW / LIMITED with reset countdowns /
  BLOCKED with explicit reasons).
- Each row states *why* it is in its section; countdown reaching zero
  moves the row without waiting for the next poll.
- Account submenus gain a subscription line (plan · paid · renews …).

### P2 — carried over from v1
- Pricing catalog (`catalog.json`), effective-cost score, best-account
  export, notification settings — re-scoped to later milestones.

## Non-goals

- Cross-provider routing (swap is same-provider only).
- Kimi / Gemini / Z.ai pollers; Cursor entirely.
- Auto-updating catalog prices; web dashboard changes.
- Claude auto-swap on by default.

## Risks

| Risk | Mitigation |
|------|------------|
| Swapping credentials mid-turn breaks a running agent | 120s live-session guard + cooldown |
| Bad/expired token written to auth.json | Pre-swap refresh + atomic write + backup file |
| Wrong exhaustion call on stale data | Stale flag blocks swap decisions (fail-safe) |
| Claude keychain rewrite unsupported | Spike first; feature-flagged off |

## Release criteria

- Full backend suite (110+ existing + new state/lifecycle/swap tests)
  green.
- App + dmg build passes (AGENTS.md); manual pass of both layouts,
  countdown section-flips, one forced swap end-to-end.
