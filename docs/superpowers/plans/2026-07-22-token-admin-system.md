# Token operating admin system v2 — implementation plan

Date: 2026-07-22
Spec: `docs/superpowers/specs/2026-07-22-token-admin-system-design.md`

## Milestones (commit per milestone; build app+dmg before any PR per AGENTS.md)

### M1: Exact window + lifecycle state (backend foundation)
- [ ] `status.py`: add `refresh_windows(windows, now)` post-pass —
      `phase` (live/reset), `used_pct_effective`, cadence roll-forward,
      `stale` flag (as_of > 3× poll interval)
- [ ] Migrate `select_headline` and gauge export to `used_pct_effective`;
      remove the ad-hoc past-reset clamp
- [ ] `status.py`: add `account_state(item, now)` pure function
      (auth / subscription / quota / usable / binding_window)
- [ ] Export `state` per account in `cmd_export`
- [ ] `test_refresh_windows.py` + `test_account_state.py`
- [ ] Run full suite, commit

### M2: Lifecycle transition events
- [ ] `backend/lifecycle.py`: `detect_transitions(prev, next)` —
      window_reset, sub_paid, sub_expired, sub_renewed,
      quota_exhausted, quota_recovered
- [ ] `store.py`: `lifecycle_events` table + migration + save/list helpers
- [ ] Daemon loop: keep previous export in memory, call after each export
- [ ] `test_lifecycle.py` (transitions, idempotency)
- [ ] Run tests, commit

### M3: Dropdown UI/UX experiment (Swift)
- [ ] Decode `state` in `Account` struct
- [ ] `menuLayout` UserDefaults toggle + footer "Layout" switcher
- [ ] `usable-first` layout: USE NOW / LIMITED (reset countdowns) /
      BLOCKED (reason text) sections driven by `state`
- [ ] Subscription line in each account submenu
      (`plan · paid · renews …` / `expired since …`)
- [ ] Countdown → phase flip moves rows between sections without repoll
- [ ] Last-swap row placeholder (hidden until M4 emits events)
- [ ] Build app + dmg, relaunch, verify both layouts, commit

### M4: Auto-swap engine — Codex
- [ ] `backend/swap.py`: `swap_candidates`, `should_swap`,
      `perform_swap` (auth.json atomic rewrite, backup to
      `secrets/swap_backups/`, 0600)
- [ ] Pre-swap token refresh when target expires < 10min
- [ ] Safety rails: stale-data refusal, 30-min cooldown, live-session
      (120s) refusal, per-provider kill-switch in `settings.json`
- [ ] Daemon integration post-export; `account_swapped` lifecycle event
      + macOS notification
- [ ] Manual "Swap to this account" action in Swift submenu
- [ ] `test_swap.py` (tmpdir CODEX_HOME; full trigger truth table)
- [ ] Live verify: force-exhaust scenario with a fixture export, confirm
      auth.json swap + backup; run tests, commit

### M5: Claude swap spike (flagged)
- [ ] Spike: rewrite keychain `Claude Code-credentials` +
      `~/.claude.json` oauthAccount; confirm Claude Code picks it up
      without relogin — document findings in spec
- [ ] If viable: enable behind `auto_swap.claude` flag (default off), tests
- [ ] Commit (spike notes land even if not viable)

### M6: Catalog + effective-cost + best-account (from v1)
- [ ] `backend/catalog.json` (9 providers + promos) + `catalog.py` +
      `test_catalog.py`
- [ ] Wire `catalog.get_plan_price` into `plan_label` (hardcoded fallback)
- [ ] `effective_cost_score()` + `test_effective_cost.py` + export field
- [ ] `best_account_now()` reusing `state` + swap ranking;
      `test_best_account.py`; export + effective-cost tie-break in swap
- [ ] Run tests, commit

### M7: Notifications
- [ ] `backend/notify.py`: fire via osascript, dedup on
      `lifecycle_events`, settings-gated, cooldowns
- [ ] Events: window_reset, swap, sub_expired/renews_soon, quota_exhausted
- [ ] Settings submenu toggles in Swift
- [ ] `test_notify.py`; run tests, commit

### M8: Final verification
- [ ] Full test suite
- [ ] Build app & dmg (AGENTS.md), relaunch
- [ ] Manual pass: both layouts, state sections, countdown flips,
      forced swap, notifications
- [ ] Commit fixes if any

## Backlog (explicitly deferred)
- Kimi poller + onboarding; Gemini/Z.ai catalog-only entries
- Catalog staleness checker (daily page-hash job)
- Promos section UI
- Copilot swap research
