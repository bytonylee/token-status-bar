# Real-time Token Sync + Unified Window Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Claude's quota-consuming probes with the free `oauth/usage` endpoint, add second-level local sync from CLI session logs, normalize all providers into a `windows[]` model, and redesign the menu bar around the riskiest active window.

**Architecture:** The Python backend (poller daemon) gains a new data source layer (local session-log scanning) and a normalization layer in `status.py` that exports `windows[]` + epoch timestamps + a top-level `headline`. The Swift menu-bar app renders the headline in the status item title, gauge bars per account, and a live ticker.

**Tech Stack:** Python 3.9+ stdlib only (no new dependencies), Swift/AppKit single-file app, SQLite (`pool.db`), launchd daemon.

**Spec:** `docs/superpowers/specs/2026-07-16-realtime-token-sync-design.md`

## Global Constraints

- Python 3.9 compatible: every new module starts with `from __future__ import annotations`; stdlib only.
- macOS 14+ / `swiftc` via `build.sh`; app targets `LSMinimumSystemVersion` 14.0.
- All user-facing times formatted in KST via existing `status.ts_fmt` / `iso_fmt`; epochs exported as numbers alongside.
- `status.json` stays backward compatible: existing fields keep their exact names/formats for one release; new data is additive (`windows`, `headline`, `live`, `source`).
- AGENTS.md rules: after editing `poller.py` run `launchctl kickstart -k "gui/$(id -u)/com.tonye.agentpool-poller"`; before any PR run `./build.sh` and build the `.dmg`; after building, quit and reopen the app.
- Backend tests run with: `cd /Users/tonylee/solo/token-status-bar/backend && python3 -m unittest <module> -v`.
- Never keep the old Claude probe as a fallback — a fallback that silently spends quota is worse than an error snapshot.
- Commit after every task (backend commits must not include `pool.db`, `status.json`, logs — they are git-ignored already).

---

### Task 1: Claude poller swap to `GET /api/oauth/usage`

**Files:**
- Modify: `backend/poller.py` (replace `poll_claude`, delete `_claude_snap` and `_claude_fable_window`, add `_claude_usage_snap`; remove `HOT_INTERVAL_CLAUDE_S` special-casing in `compute_next_due` and `run_loop` banner)
- Modify: `backend/status.py` (`claude_extra` learns the new `raw_json["usage_api"]` shape, keeps legacy `ratelimit` branch for old rows)
- Modify: `backend/test_window_history.py` (two cadence tests that assert the Claude floor)
- Create: `backend/test_claude_usage.py`

**Interfaces:**
- Consumes: `_get(url, headers)` (existing, returns `(status, body, headers)`), `oauth.refresh_claude(refresh_token) -> dict`, `store.save_token`, `window_history._parse_iso_ts(s) -> float | None`.
- Produces: `_claude_usage_snap(body: dict, profile: dict | None) -> dict` — snapshot dict with `primary_*` (5h), `secondary_*` (7d), and `raw_json` containing `{"usage_api": <full body>, "profile": ..., "fable": {"label", "used_pct", "reset_at", "status"}}`. The `fable` shape is identical to today's, so `window_history._fable()` and `status.claude_extra` fable handling keep working.

- [ ] **Step 1: Write the failing tests**

Create `backend/test_claude_usage.py`:

```python
"""Tests for the Claude oauth/usage snapshot parser."""
from __future__ import annotations
import datetime, json, os, sys, tempfile, time, unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tsb-test-")
os.environ["AGENT_POOL_DB"] = os.path.join(_TMP, "pool.db")
os.environ["AGENT_POOL_STATUS_JSON"] = os.path.join(_TMP, "status.json")
os.environ["AGENT_POOL_HISTORY_DIR"] = os.path.join(_TMP, "history")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import poller  # noqa: E402
import status  # noqa: E402
import store  # noqa: E402

FIXTURE = {
    "five_hour": {"utilization": 41.0, "resets_at": "2026-07-16T13:59:59.819914+00:00",
                  "limit_dollars": None, "used_dollars": None, "remaining_dollars": None},
    "seven_day": {"utilization": 5.0, "resets_at": "2026-07-17T01:59:59.819931+00:00",
                  "limit_dollars": None, "used_dollars": None, "remaining_dollars": None},
    "limits": [
        {"kind": "session", "group": "session", "percent": 41, "severity": "normal",
         "resets_at": "2026-07-16T13:59:59.819914+00:00", "scope": None, "is_active": True},
        {"kind": "weekly_all", "group": "weekly", "percent": 5, "severity": "normal",
         "resets_at": "2026-07-17T01:59:59.819931+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 9, "severity": "normal",
         "resets_at": "2026-07-17T01:59:59.820183+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
         "is_active": False},
    ],
    "extra_usage": {"is_enabled": False, "monthly_limit": None, "used_credits": None,
                    "utilization": None},
    "spend": {"used": {"amount_minor": 0, "currency": "USD", "exponent": 2},
              "limit": None, "percent": 0, "severity": "normal", "enabled": False},
}


class ClaudeUsageSnapTest(unittest.TestCase):
    def test_primary_secondary_windows(self):
        snap = poller._claude_usage_snap(FIXTURE, None)
        self.assertEqual(snap["status"], "active")
        self.assertEqual(snap["primary_used_pct"], 41.0)
        self.assertEqual(snap["primary_window_s"], 18000)
        expected = datetime.datetime.fromisoformat(
            "2026-07-16T13:59:59.819914+00:00").timestamp()
        self.assertAlmostEqual(snap["primary_reset_at"], expected, places=3)
        self.assertEqual(snap["secondary_used_pct"], 5.0)
        self.assertEqual(snap["secondary_window_s"], 604800)

    def test_fable_window_from_weekly_scoped(self):
        snap = poller._claude_usage_snap(FIXTURE, None)
        rj = json.loads(snap["raw_json"])
        self.assertEqual(rj["fable"]["label"], "Fable")
        self.assertEqual(rj["fable"]["used_pct"], 9.0)
        self.assertEqual(rj["fable"]["status"], "normal")
        self.assertIsNotNone(rj["fable"]["reset_at"])

    def test_raw_json_keeps_full_body_and_profile(self):
        snap = poller._claude_usage_snap(FIXTURE, {"plan": "Claude Max"})
        rj = json.loads(snap["raw_json"])
        self.assertEqual(rj["usage_api"]["five_hour"]["utilization"], 41.0)
        self.assertEqual(rj["profile"]["plan"], "Claude Max")
        self.assertEqual(snap["plan"], "Claude Max")

    def test_binding_and_severity_fields(self):
        snap = poller._claude_usage_snap(FIXTURE, None)
        self.assertEqual(snap["rate_limit_remaining"], "normal")
        self.assertEqual(snap["rate_limit_limit"], "unified")

    def test_missing_windows_do_not_crash(self):
        snap = poller._claude_usage_snap({"limits": []}, None)
        self.assertEqual(snap["status"], "active")
        self.assertNotIn("primary_used_pct", snap)


class ClaudeExtraUsageApiTest(unittest.TestCase):
    def test_claude_extra_reads_usage_api(self):
        snap = poller._claude_usage_snap(FIXTURE, {"plan": "Claude Max",
                                                   "subscription_status": "active"})
        out = status.claude_extra(snap)
        self.assertEqual(out["fable_label"], "Fable")
        self.assertEqual(out["fable_used_pct"], 9.0)
        self.assertEqual(out["primary_status"], "normal")
        self.assertEqual(out["secondary_status"], "normal")
        self.assertEqual(out["binding_window"], "5h")


class ClaudePoll401RefreshTest(unittest.TestCase):
    """poll_claude's 401 → refresh → retry branch (auth-critical DB side effects)."""

    def setUp(self):
        self.conn = store.connect()
        # wipe between tests: the temp DB is shared module-wide
        for t in ("refresh_log", "limit_snapshots", "tokens", "accounts"):
            self.conn.execute(f"DELETE FROM {t}")
        self.conn.commit()
        aid = store.upsert_account(self.conn, "claude", "claude-401@example.com",
                                   label="claude-401-test")
        store.save_token(self.conn, aid, "old-access", "refresh-tok", "",
                         time.time() + 3600)
        self.account = store.get_account(self.conn, aid)

    def tearDown(self):
        self.conn.close()

    def _events(self, kind):
        return [e for e in store.recent_events(self.conn, self.account["id"])
                if e["kind"] == kind]

    def test_401_refresh_retry_succeeds(self):
        # 401 on the stale token, 200 on the refreshed one; profile is
        # best-effort so a non-200 there must not fail the poll.
        def fake_get(url, headers, timeout=15):
            if url != poller.CLAUDE_USAGE_URL:
                return 404, "", {}
            if headers["Authorization"] == "Bearer old-access":
                return 401, {"error": "token expired"}, {}
            self.assertEqual(headers["Authorization"], "Bearer new-access")
            return 200, FIXTURE, {}

        refreshed = {"access_token": "new-access", "refresh_token": "new-refresh",
                     "id_token": "", "expires_at": time.time() + 3600, "raw": {}}
        token = store.get_token(self.conn, self.account["id"])
        with mock.patch.object(poller, "_get", side_effect=fake_get), \
             mock.patch.object(poller.oauth, "refresh_claude",
                               return_value=refreshed) as refresh:
            poller.poll_claude(self.conn, self.account, token)
        refresh.assert_called_once_with("refresh-tok")
        snap = store.latest_snapshot(self.conn, self.account["id"])
        self.assertEqual(snap["status"], "active")
        self.assertEqual(snap["primary_used_pct"], 41.0)
        saved = store.get_token(self.conn, self.account["id"])
        self.assertEqual(saved["access_token"], "new-access")
        self.assertEqual(saved["refresh_token"], "new-refresh")
        refreshes = self._events("token_refresh")
        self.assertEqual(len(refreshes), 1)
        self.assertTrue(refreshes[0]["success"])
        polls = self._events("limit_poll")
        self.assertEqual(len(polls), 1)
        self.assertTrue(polls[0]["success"])

    def test_401_refresh_failure_writes_error_snapshot(self):
        # 401 everywhere and a refresh that blows up: the poll must still land
        # an error snapshot with the HTTP status, plus a logged refresh failure.
        def fake_get(url, headers, timeout=15):
            return 401, {"error": "token expired"}, {}

        token = store.get_token(self.conn, self.account["id"])
        with mock.patch.object(poller, "_get", side_effect=fake_get), \
             mock.patch.object(poller.oauth, "refresh_claude",
                               side_effect=RuntimeError("refresh boom")):
            poller.poll_claude(self.conn, self.account, token)
        snap = store.latest_snapshot(self.conn, self.account["id"])
        self.assertEqual(snap["status"], "error")
        self.assertIn("HTTP 401", snap["status_message"])
        self.assertEqual(store.get_token(self.conn, self.account["id"])["access_token"],
                         "old-access")
        refreshes = self._events("token_refresh")
        self.assertEqual(len(refreshes), 1)
        self.assertFalse(refreshes[0]["success"])
        self.assertIn("refresh boom", refreshes[0]["message"])
        polls = self._events("limit_poll")
        self.assertEqual(len(polls), 1)
        self.assertFalse(polls[0]["success"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/tonylee/solo/token-status-bar/backend && python3 -m unittest test_claude_usage -v`
Expected: FAIL/ERROR with `AttributeError: module 'poller' has no attribute '_claude_usage_snap'`

- [ ] **Step 3: Implement `_claude_usage_snap` and the new `poll_claude`**

In `backend/poller.py`, replace the whole Claude section (`poll_claude`, `_claude_snap`, `_claude_fable_window` — keep `_claude_profile` unchanged) with:

```python
# ─── Claude oauth/usage ────────────────────────────────────────────────────
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


def poll_claude(conn, account, token):
    """Poll Claude via the quota-free oauth/usage endpoint (no probes)."""
    def _fetch(access_token):
        return _get(CLAUDE_USAGE_URL, {
            "Authorization": f"Bearer {access_token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
        })

    st, body, _ = _fetch(token["access_token"])
    if st == 401 and token.get("refresh_token"):
        # One refresh + retry; a second 401 becomes an error snapshot.
        try:
            result = oauth.refresh_claude(token["refresh_token"])
            store.save_token(conn, account["id"], result["access_token"],
                             result.get("refresh_token"), result.get("id_token"),
                             result.get("expires_at"), result.get("raw"))
            store.log_event(conn, account["id"], "token_refresh", True, "")
            token = store.get_token(conn, account["id"])
            st, body, _ = _fetch(token["access_token"])
        except Exception as e:
            store.log_event(conn, account["id"], "token_refresh", False, str(e))

    if st != 200 or not isinstance(body, dict):
        snap = {"status": "error",
                "status_message": f"oauth/usage HTTP {st}: {str(body)[:120]}"}
    else:
        snap = _claude_usage_snap(body, _claude_profile(token))
    store.save_snapshot(conn, account["id"], snap)
    store.log_event(conn, account["id"], "limit_poll", snap["status"] == "active",
                    snap.get("status_message", ""))


def _claude_usage_snap(body, profile):
    """Build a snapshot from the oauth/usage response body (pure function)."""
    snap = {"status": "active", "status_message": ""}
    rj = {"usage_api": body}
    if profile:
        rj["profile"] = profile
        if profile.get("plan"):
            snap["plan"] = profile["plan"]

    fh = body.get("five_hour") or {}
    if fh.get("utilization") is not None:
        snap["primary_used_pct"] = float(fh["utilization"])
        snap["primary_window_s"] = 18000
        reset = window_history._parse_iso_ts(fh.get("resets_at"))
        if reset:
            snap["primary_reset_at"] = reset
    sd = body.get("seven_day") or {}
    if sd.get("utilization") is not None:
        snap["secondary_used_pct"] = float(sd["utilization"])
        snap["secondary_window_s"] = 604800
        reset = window_history._parse_iso_ts(sd.get("resets_at"))
        if reset:
            snap["secondary_reset_at"] = reset

    limits = [l for l in (body.get("limits") or []) if isinstance(l, dict)]
    for lim in limits:
        if lim.get("kind") != "weekly_scoped":
            continue
        scope_model = ((lim.get("scope") or {}).get("model") or {})
        rj["fable"] = {
            "label": scope_model.get("display_name") or "scoped",
            "used_pct": float(lim["percent"]) if lim.get("percent") is not None else None,
            "reset_at": window_history._parse_iso_ts(lim.get("resets_at")),
            "status": lim.get("severity"),
        }
        break

    active = next((l for l in limits if l.get("is_active")), None)
    snap["rate_limit_remaining"] = (active or {}).get("severity") or "normal"
    snap["rate_limit_limit"] = "unified"
    if snap.get("primary_reset_at"):
        snap["rate_limit_reset"] = str(snap["primary_reset_at"])
    snap["raw_json"] = json.dumps(rj)
    return snap
```

Also in `backend/poller.py`:
- Delete the `HOT_INTERVAL_CLAUDE_S` constant (line ~26) and its comment.
- In `compute_next_due`, change `interval = HOT_INTERVAL_CLAUDE_S if provider == "claude" else HOT_INTERVAL_S` to `interval = HOT_INTERVAL_S`, and delete the claude-specific block:

```python
            # (delete these lines)
            if provider == "claude":
                candidate = max(candidate, last_poll_ts + HOT_INTERVAL_CLAUDE_S)
```

- In `run_loop`'s startup print, drop `(claude {HOT_INTERVAL_CLAUDE_S}s)`.
- Update the module docstring line for claude to: `claude: api.anthropic.com/api/oauth/usage (five_hour/seven_day/limits[])`.

- [ ] **Step 4: Update `claude_extra` in `backend/status.py`**

Replace the body of `claude_extra` so it prefers `usage_api` and falls back to the legacy `ratelimit` headers for pre-migration rows. Replace the current `if rl:` block (keep everything above it — fable and profile handling stay exactly as-is):

```python
    usage = rj.get("usage_api") or {}
    limits = [l for l in (usage.get("limits") or []) if isinstance(l, dict)]
    if limits:
        by_kind = {l.get("kind"): l for l in limits}
        if by_kind.get("session"):
            out["primary_status"] = by_kind["session"].get("severity")
        if by_kind.get("weekly_all"):
            out["secondary_status"] = by_kind["weekly_all"].get("severity")
        active = next((l for l in limits if l.get("is_active")), None)
        if active:
            label = {"session": "5h", "weekly_all": "weekly"}.get(active.get("kind"))
            if label is None:
                scope_model = ((active.get("scope") or {}).get("model") or {})
                label = scope_model.get("display_name") or active.get("kind")
            out["binding_window"] = label
        extra = usage.get("extra_usage") or {}
        if extra.get("is_enabled"):
            out["extra_usage_enabled"] = True
            if extra.get("utilization") is not None:
                out["extra_usage_used_pct"] = float(extra["utilization"])
    elif rl:
        out["primary_status"] = rl.get("anthropic-ratelimit-unified-5h-status")
        out["secondary_status"] = rl.get("anthropic-ratelimit-unified-7d-status")
        fallback_pct = rl.get("anthropic-ratelimit-unified-fallback-percentage")
        if fallback_pct is not None:
            try:
                out["fallback_used_pct"] = float(fallback_pct) * 100
            except (TypeError, ValueError):
                pass
        claim = rl.get("anthropic-ratelimit-unified-representative-claim")
        out["binding_window"] = {"five_hour": "5h", "seven_day": "weekly"}.get(claim, claim)
        out["overage_status"] = rl.get("anthropic-ratelimit-unified-overage-status")
    return {k: v for k, v in out.items() if v is not None}
```

- [ ] **Step 5: Fix the two cadence tests**

In `backend/test_window_history.py`, `test_hot_claude_capped` (~line 479) and `test_prereset_claude_capped` (~line 512) assert the old Claude floor. Rewrite both to assert Claude now matches the standard cadence:

```python
    def test_hot_claude_uses_standard_interval(self):
        due = poller.compute_next_due(1000.0, provider="claude", last_poll_ts=900.0,
                                      last_success_ts=900.0, hot=True, reset_at=None)
        self.assertEqual(due, 900.0 + poller.HOT_INTERVAL_S)

    def test_prereset_claude_matches_codex(self):
        now = 10_000.0
        last_poll = now - 5
        reset_at = now + 200
        claude_due = poller.compute_next_due(now, provider="claude", last_poll_ts=last_poll,
                                             last_success_ts=None, hot=False, reset_at=reset_at)
        codex_due = poller.compute_next_due(now, provider="codex", last_poll_ts=last_poll,
                                            last_success_ts=None, hot=False, reset_at=reset_at)
        self.assertEqual(claude_due, codex_due)
```

- [ ] **Step 6: Run the full backend test suite**

Run: `cd /Users/tonylee/solo/token-status-bar/backend && python3 -m unittest test_claude_usage test_window_history test_status_dates test_onboarding -v`
Expected: all PASS (the `test_claude_fable_window_from_raw_json` test keeps passing because the `fable` raw_json shape is unchanged).

- [ ] **Step 7: Live smoke test**

Run: `cd /Users/tonylee/solo/token-status-bar/backend && python3 pool.py poll && python3 -c "import json; d=json.load(open('/Users/tonylee/solo/token-status-bar/secrets/status.json')); c=[a for a in d['accounts'] if a['provider']=='claude'][0]; print(c['status'], c['primary_used_pct'], c.get('fable_label'), c.get('binding_window'))"`
Expected: `active <pct> Fable 5h` (or whichever window is active) — and the poll log shows `✓ claude` without any `/v1/messages` call.

- [ ] **Step 8: Restart the daemon and commit**

```bash
launchctl kickstart -k "gui/$(id -u)/com.tonye.agentpool-poller"
cd /Users/tonylee/solo/token-status-bar
git add backend/poller.py backend/status.py backend/test_claude_usage.py backend/test_window_history.py
git commit -m "feat(claude): Poll quota-free oauth/usage endpoint instead of probes"
```

---

### Task 2: `windows[]` normalization + epoch export + headline

**Files:**
- Modify: `backend/status.py` (add `_kind_from_window_s`, `_win`, `normalize_windows`, `select_headline`; wire into `cmd_export`)
- Create: `backend/test_status_windows.py`

**Interfaces:**
- Consumes: snapshot dicts as stored by `store.save_snapshot` / read by `store.latest_snapshot` (keys: `ts`, `primary_used_pct`, `primary_reset_at`, `primary_window_s`, `secondary_*`, `monthly_*`, `daily_quota_remaining_percent`, `weekly_quota_remaining_percent`, `plan_reset_unix`, `raw_json`, optional `source`).
- Produces:
  - `normalize_windows(provider: str, snap: dict) -> list[dict]` — entries `{"kind": "5h"|"daily"|"weekly"|"monthly"|"model_weekly", "label": str|None, "used_pct": float, "reset_at_epoch": float|None, "severity": str, "is_active": bool|None, "source": "api"|"local", "as_of_epoch": float|None}`.
  - `select_headline(items: list[dict]) -> dict | None` — `{"account_id", "provider", "email", "kind", "label", "used_pct", "reset_at_epoch", "severity"}` picked by: non-normal severity first, then `projected_exhaust_epoch` present, then highest used_pct, then soonest reset. Windows whose `reset_at_epoch` is in the past are skipped.
  - `cmd_export` adds per-account `"windows"` and top-level `"headline"` to `status.json`.

- [ ] **Step 1: Write the failing tests**

Create `backend/test_status_windows.py`:

```python
"""Tests for windows[] normalization and headline selection."""
from __future__ import annotations
import json, os, sys, tempfile, time, unittest

_TMP = tempfile.mkdtemp(prefix="tsb-test-")
os.environ["AGENT_POOL_DB"] = os.path.join(_TMP, "pool.db")
os.environ["AGENT_POOL_STATUS_JSON"] = os.path.join(_TMP, "status.json")
os.environ["AGENT_POOL_HISTORY_DIR"] = os.path.join(_TMP, "history")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import status  # noqa: E402


def snap(**kw):
    base = {"ts": 1000.0, "status": "active"}
    base.update(kw)
    return base


class NormalizeWindowsTest(unittest.TestCase):
    def test_codex_weekly_primary(self):
        s = snap(primary_used_pct=6.0, primary_reset_at=2000.0,
                 primary_window_s=604800)
        w = status.normalize_windows("codex", s)
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0]["kind"], "weekly")
        self.assertEqual(w[0]["used_pct"], 6.0)
        self.assertEqual(w[0]["reset_at_epoch"], 2000.0)
        self.assertEqual(w[0]["source"], "api")
        self.assertEqual(w[0]["as_of_epoch"], 1000.0)

    def test_claude_with_usage_api_severity_and_fable(self):
        rj = json.dumps({
            "usage_api": {"limits": [
                {"kind": "session", "percent": 41, "severity": "normal", "is_active": True},
                {"kind": "weekly_all", "percent": 5, "severity": "normal", "is_active": False},
            ]},
            "fable": {"label": "Fable", "used_pct": 9.0, "reset_at": 3000.0,
                      "status": "normal"},
        })
        s = snap(primary_used_pct=41.0, primary_reset_at=2000.0, primary_window_s=18000,
                 secondary_used_pct=5.0, secondary_reset_at=3000.0,
                 secondary_window_s=604800, raw_json=rj)
        w = {x["kind"]: x for x in status.normalize_windows("claude", s)}
        self.assertEqual(w["5h"]["is_active"], True)
        self.assertEqual(w["weekly"]["is_active"], False)
        self.assertEqual(w["model_weekly"]["label"], "Fable")
        self.assertEqual(w["model_weekly"]["used_pct"], 9.0)

    def test_xai_monthly_and_daily(self):
        s = snap(monthly_used_pct=3.17, monthly_period_end="2026-08-01T00:00:00+00:00",
                 secondary_used_pct=0.0, secondary_window_s=86400)
        w = {x["kind"]: x for x in status.normalize_windows("xai", s)}
        self.assertIn("monthly", w)
        self.assertIn("daily", w)
        self.assertIsNotNone(w["monthly"]["reset_at_epoch"])

    def test_copilot_premium_monthly(self):
        s = snap(primary_used_pct=12.1, primary_reset_at=2000.0)
        w = status.normalize_windows("copilot", s)
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0]["kind"], "monthly")
        self.assertEqual(w[0]["label"], "premium")

    def test_devin_daily_weekly(self):
        s = snap(daily_quota_remaining_percent=90.0,
                 weekly_quota_remaining_percent=80.0,
                 primary_reset_at=2000.0, secondary_reset_at=3000.0)
        w = {x["kind"]: x for x in status.normalize_windows("devin", s)}
        self.assertEqual(w["daily"]["used_pct"], 10.0)
        self.assertEqual(w["weekly"]["used_pct"], 20.0)

    def test_antigravity_usage_windows(self):
        rj = json.dumps({"extra": {"usage_windows": [
            {"group": "gemini", "window": "5h", "remaining_pct": 100.0, "reset_at": None},
            {"group": "gemini", "window": "weekly", "remaining_pct": 92.0, "reset_at": 4000.0},
        ]}})
        s = snap(primary_used_pct=0.0, raw_json=rj,
                 rate_limit_remaining="100% left (Gemini 3 Pro)")
        w = status.normalize_windows("antigravity", s)
        kinds = [x["kind"] for x in w]
        self.assertIn("model_weekly", kinds)
        self.assertIn("5h", kinds)
        self.assertIn("weekly", kinds)
        weekly = next(x for x in w if x["kind"] == "weekly")
        self.assertEqual(weekly["used_pct"], 8.0)
        self.assertEqual(weekly["label"], "gemini")

    def test_antigravity_malformed_usage_window_skipped(self):
        rj = json.dumps({"extra": {"usage_windows": [
            {"group": "gemini", "window": "5h", "remaining_pct": "oops", "reset_at": None},
            {"group": "gemini", "window": "weekly", "remaining_pct": 92.0, "reset_at": 4000.0},
        ]}})
        s = snap(primary_used_pct=0.0, raw_json=rj)
        w = status.normalize_windows("antigravity", s)
        kinds = [x["kind"] for x in w]
        self.assertNotIn("5h", kinds)
        self.assertIn("weekly", kinds)
        weekly = next(x for x in w if x["kind"] == "weekly")
        self.assertEqual(weekly["used_pct"], 8.0)

    def test_devin_non_numeric_daily_skipped(self):
        s = snap(daily_quota_remaining_percent="n/a",
                 weekly_quota_remaining_percent=80.0,
                 primary_reset_at=2000.0, secondary_reset_at=3000.0)
        w = {x["kind"]: x for x in status.normalize_windows("devin", s)}
        self.assertNotIn("daily", w)
        self.assertEqual(w["weekly"]["used_pct"], 20.0)

    def test_error_snapshot_yields_nothing(self):
        self.assertEqual(status.normalize_windows("codex", snap(status="error")), [])
        self.assertEqual(status.normalize_windows("codex", None), [])


class HeadlineTest(unittest.TestCase):
    def _item(self, aid, windows):
        return {"id": aid, "provider": "codex", "email": f"a{aid}@x.com",
                "windows": windows}

    def test_highest_used_wins(self):
        now = time.time()
        items = [
            self._item(1, [{"kind": "5h", "label": None, "used_pct": 41.0,
                            "reset_at_epoch": now + 100, "severity": "normal"}]),
            self._item(2, [{"kind": "weekly", "label": None, "used_pct": 12.0,
                            "reset_at_epoch": now + 100, "severity": "normal"}]),
        ]
        h = status.select_headline(items)
        self.assertEqual(h["account_id"], 1)
        self.assertEqual(h["used_pct"], 41.0)

    def test_non_normal_severity_beats_higher_pct(self):
        now = time.time()
        items = [
            self._item(1, [{"kind": "5h", "label": None, "used_pct": 90.0,
                            "reset_at_epoch": now + 100, "severity": "normal"}]),
            self._item(2, [{"kind": "5h", "label": None, "used_pct": 50.0,
                            "reset_at_epoch": now + 100, "severity": "warning"}]),
        ]
        self.assertEqual(status.select_headline(items)["account_id"], 2)

    def test_past_reset_skipped(self):
        now = time.time()
        items = [self._item(1, [{"kind": "5h", "label": None, "used_pct": 99.0,
                                 "reset_at_epoch": now - 10, "severity": "normal"}])]
        self.assertIsNone(status.select_headline(items))

    def test_empty(self):
        self.assertIsNone(status.select_headline([]))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/tonylee/solo/token-status-bar/backend && python3 -m unittest test_status_windows -v`
Expected: ERROR with `AttributeError: module 'status' has no attribute 'normalize_windows'`

- [ ] **Step 3: Implement normalization + headline in `backend/status.py`**

Add below `provider_extra` (before `codex_extra`):

```python
# ─── unified window model ───────────────────────────────────────────────────
def _kind_from_window_s(window_s, default):
    if not window_s:
        return default
    if 17000 <= window_s <= 19000:
        return "5h"
    if window_s == 86400:
        return "daily"
    if 500000 <= window_s < 1000000:
        return "weekly"
    if window_s >= 1000000:
        return "monthly"
    return default


def _win(kind, label, used, reset_epoch, *, severity=None, is_active=None,
         source="api", as_of=None):
    if used is None:
        return None
    try:
        used = float(used)
    except (TypeError, ValueError):
        return None
    try:
        reset_epoch = float(reset_epoch) if reset_epoch is not None else None
    except (TypeError, ValueError):
        reset_epoch = None
    return {"kind": kind, "label": label, "used_pct": round(used, 2),
            "reset_at_epoch": reset_epoch, "severity": severity or "normal",
            "is_active": is_active, "source": source, "as_of_epoch": as_of}


def _pct(v):
    """float(v), or None when the value is missing or non-numeric."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _snap_raw(snap) -> dict:
    try:
        rj = json.loads(snap.get("raw_json") or "{}")
    except Exception:
        return {}
    return rj if isinstance(rj, dict) else {}


def normalize_windows(provider, snap) -> list[dict]:
    """Reduce one snapshot to the unified windows[] model (pure function)."""
    if not snap or snap.get("status") not in ("active", "rate_limited"):
        return []
    as_of = float(snap["ts"]) if snap.get("ts") else None
    src = snap.get("source") or "api"
    rj = _snap_raw(snap)
    out = []

    def add(w):
        if w:
            out.append(w)

    if provider in ("codex", "claude"):
        sev_active = {}
        if provider == "claude":
            for lim in (rj.get("usage_api") or {}).get("limits") or []:
                if isinstance(lim, dict):
                    sev_active[lim.get("kind")] = (lim.get("severity"),
                                                   lim.get("is_active"))
        s5, a5 = sev_active.get("session", (None, None))
        sw, aw = sev_active.get("weekly_all", (None, None))
        add(_win(_kind_from_window_s(snap.get("primary_window_s"), "5h"), None,
                 snap.get("primary_used_pct"), snap.get("primary_reset_at"),
                 severity=s5, is_active=a5, source=src, as_of=as_of))
        add(_win(_kind_from_window_s(snap.get("secondary_window_s"), "weekly"), None,
                 snap.get("secondary_used_pct"), snap.get("secondary_reset_at"),
                 severity=sw, is_active=aw, source=src, as_of=as_of))
        fable = rj.get("fable") or {}
        if fable.get("used_pct") is not None:
            add(_win("model_weekly", fable.get("label"), fable["used_pct"],
                     fable.get("reset_at"), severity=fable.get("status"),
                     source=src, as_of=as_of))
    elif provider == "xai":
        reset = None
        end = snap.get("monthly_period_end")
        if end:
            dt = _parse_iso(end)
            reset = dt.timestamp() if dt else None
        add(_win("monthly", "credits", snap.get("monthly_used_pct"), reset,
                 source=src, as_of=as_of))
        if snap.get("secondary_window_s") == 86400:
            add(_win("daily", None, snap.get("secondary_used_pct"),
                     snap.get("secondary_reset_at"), source=src, as_of=as_of))
    elif provider == "copilot":
        add(_win("monthly", "premium", snap.get("primary_used_pct"),
                 snap.get("primary_reset_at"), source=src, as_of=as_of))
        add(_win("monthly", "chat", snap.get("secondary_used_pct"),
                 snap.get("primary_reset_at"), source=src, as_of=as_of))
    elif provider == "devin":
        daily_rem = _pct(snap.get("daily_quota_remaining_percent"))
        if daily_rem is not None:
            add(_win("daily", None, 100.0 - daily_rem,
                     snap.get("primary_reset_at"), source=src, as_of=as_of))
        weekly_rem = _pct(snap.get("weekly_quota_remaining_percent"))
        if weekly_rem is not None:
            add(_win("weekly", None, 100.0 - weekly_rem,
                     snap.get("secondary_reset_at"), source=src, as_of=as_of))
    elif provider == "antigravity":
        label = None
        rem = snap.get("rate_limit_remaining") or ""
        m = re.search(r"\(([^)]+)\)", rem)
        if m:
            label = m.group(1)
        add(_win("model_weekly", label, snap.get("primary_used_pct"),
                 snap.get("primary_reset_at"), source=src, as_of=as_of))
        for w in (rj.get("extra") or {}).get("usage_windows") or []:
            if not isinstance(w, dict):
                continue
            rem_pct = _pct(w.get("remaining_pct"))
            if rem_pct is None:
                continue
            kind = "weekly" if w.get("window") == "weekly" else "5h"
            add(_win(kind, w.get("group"),
                     max(0.0, min(100.0, 100.0 - rem_pct)),
                     w.get("reset_at"), source=src, as_of=as_of))
    return out


def select_headline(items) -> dict | None:
    """The single riskiest window across all accounts, for the menu bar title."""
    now_ts = time.time()
    best, best_key = None, None
    for it in items:
        for w in it.get("windows") or []:
            if w.get("used_pct") is None:
                continue
            reset = w.get("reset_at_epoch")
            if reset and reset < now_ts:
                continue
            sev = 0 if (w.get("severity") or "normal") == "normal" else 1
            proj = 1 if w.get("projected_exhaust_epoch") else 0
            key = (sev, proj, w["used_pct"], -(reset or 1e18))
            if best_key is None or key > best_key:
                best_key = key
                best = {"account_id": it["id"], "provider": it["provider"],
                        "email": it.get("email"), "kind": w["kind"],
                        "label": w.get("label"), "used_pct": w["used_pct"],
                        "reset_at_epoch": reset, "severity": w.get("severity") or "normal"}
    return best
```

Wire into `cmd_export`: inside the account loop, right after the `plan_label` lines, add:

```python
        items[-1]["windows"] = normalize_windows(a["provider"], snap)
```

and change the `payload = {...}` dict to include:

```python
        "headline": select_headline(items),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/tonylee/solo/token-status-bar/backend && python3 -m unittest test_status_windows test_claude_usage test_status_dates -v`
Expected: PASS

- [ ] **Step 5: Smoke-check export**

Run: `cd /Users/tonylee/solo/token-status-bar/backend && python3 pool.py export-status && python3 -c "import json; d=json.load(open('/Users/tonylee/solo/token-status-bar/secrets/status.json')); print(d['headline']); print([ (a['provider'], [w['kind'] for w in a['windows']]) for a in d['accounts'][:4] ])"`
Expected: a headline dict with `used_pct`/`reset_at_epoch`, and non-empty kind lists for accounts with data.

- [ ] **Step 6: Commit**

```bash
cd /Users/tonylee/solo/token-status-bar
git add backend/status.py backend/test_status_windows.py
git commit -m "feat(status): Export unified windows[] + headline with epoch timestamps"
```

---

### Task 3: Local sync from CLI session logs

**Files:**
- Create: `backend/local_sync.py`
- Modify: `backend/store.py` (add `source` column to `limit_snapshots` + `save_snapshot` fields; add `live_activity` table + `upsert_live_activity` / `get_live_activity`)
- Modify: `backend/poller.py` (`run_loop` gains a local-sync tick; sleep is sliced to `LOCAL_SYNC_INTERVAL_S`)
- Modify: `backend/status.py` (`cmd_export` exports fresh `live` blobs)
- Create: `backend/test_local_sync.py`

**Interfaces:**
- Consumes: `store.list_accounts`, `store.latest_snapshot`, `store.save_snapshot`, `store.upsert_live_activity(conn, account_id, payload: dict)`, `store.get_live_activity(conn, account_id) -> dict | None` (returns `{"ts": float, ...payload}`).
- Produces (in `local_sync.py`):
  - `scan(conn, now: float | None = None) -> bool` — True when anything changed (caller re-exports status.json).
  - Pure helpers used by tests: `extract_token_count(obj: dict) -> dict | None`, `codex_snap(rate_limits: dict) -> dict` (snapshot dict with `source="local"`), `match_codex_account(accounts: list[dict], upstream_id: str) -> dict | None`, `tail_lines(path, nbytes) -> list[str]`, `claude_usage_totals(lines: list[str], since_epoch: float) -> dict`.
- `status.json` accounts gain `"live": {"as_of_epoch": ..., "provider": ..., ...}` when fresh (< 600s).

- [ ] **Step 1: Write the failing tests**

Create `backend/test_local_sync.py`:

```python
"""Tests for local session-log sync (pure helpers)."""
from __future__ import annotations
import json, os, sys, tempfile, unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="tsb-test-")
os.environ["AGENT_POOL_DB"] = os.path.join(_TMP, "pool.db")
os.environ["AGENT_POOL_STATUS_JSON"] = os.path.join(_TMP, "status.json")
os.environ["AGENT_POOL_HISTORY_DIR"] = os.path.join(_TMP, "history")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import local_sync  # noqa: E402

CODEX_EVENT = {
    "timestamp": "2026-07-16T13:45:00.000Z",
    "type": "event_msg",
    "payload": {
        "type": "token_count",
        "info": {
            "total_token_usage": {"input_tokens": 43961548, "cached_input_tokens": 40634112,
                                  "output_tokens": 107495, "reasoning_output_tokens": 27705,
                                  "total_tokens": 44069043},
            "last_token_usage": {"input_tokens": 98745, "cached_input_tokens": 97024,
                                 "output_tokens": 1968, "reasoning_output_tokens": 76,
                                 "total_tokens": 100713},
            "model_context_window": 258400,
        },
        "rate_limits": {
            "limit_id": "codex", "limit_name": None,
            "primary": {"used_percent": 6.0, "window_minutes": 10080,
                        "resets_at": 1784781194},
            "secondary": None,
            "credits": {"has_credits": True, "unlimited": False, "balance": "2500"},
            "plan_type": "pro",
        },
    },
}


class ExtractTokenCountTest(unittest.TestCase):
    def test_wrapped_payload(self):
        out = local_sync.extract_token_count(CODEX_EVENT)
        self.assertIsNotNone(out)
        self.assertEqual(out["rate_limits"]["primary"]["used_percent"], 6.0)

    def test_bare_payload(self):
        out = local_sync.extract_token_count(CODEX_EVENT["payload"])
        self.assertIsNotNone(out)

    def test_other_event_returns_none(self):
        self.assertIsNone(local_sync.extract_token_count({"type": "session_meta"}))


class CodexSnapTest(unittest.TestCase):
    def test_snapshot_fields(self):
        snap = local_sync.codex_snap(CODEX_EVENT["payload"]["rate_limits"])
        self.assertEqual(snap["source"], "local")
        self.assertEqual(snap["status"], "active")
        self.assertEqual(snap["primary_used_pct"], 6.0)
        self.assertEqual(snap["primary_window_s"], 10080 * 60)
        self.assertEqual(snap["primary_reset_at"], 1784781194.0)
        self.assertEqual(snap["credits_balance"], 2500.0)
        self.assertEqual(snap["plan"], "pro")
        self.assertNotIn("secondary_used_pct", snap)


class MatchAccountTest(unittest.TestCase):
    def test_match_and_miss(self):
        accounts = [{"id": 1, "provider": "codex", "account_id": "abc"},
                    {"id": 2, "provider": "claude", "account_id": "abc"},
                    {"id": 3, "provider": "codex", "account_id": "xyz"}]
        self.assertEqual(local_sync.match_codex_account(accounts, "xyz")["id"], 3)
        self.assertIsNone(local_sync.match_codex_account(accounts, "nope"))
        self.assertIsNone(local_sync.match_codex_account(accounts, None))


class TailLinesTest(unittest.TestCase):
    def test_tail_drops_partial_first_line(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.jsonl"
            lines = [json.dumps({"i": i, "pad": "x" * 100}) for i in range(100)]
            p.write_text("\n".join(lines) + "\n")
            out = local_sync.tail_lines(p, 1024)
            self.assertGreater(len(out), 2)
            json.loads(out[0])
            self.assertEqual(json.loads(out[-1])["i"], 99)


class ClaudeUsageTotalsTest(unittest.TestCase):
    def test_sums_recent_usage(self):
        mk = lambda ts, inp, out_t: json.dumps({
            "type": "assistant", "timestamp": ts,
            "message": {"usage": {"input_tokens": inp, "output_tokens": out_t,
                                  "cache_creation_input_tokens": 10,
                                  "cache_read_input_tokens": 999}}})
        lines = [mk("2026-07-16T13:00:00.000Z", 100, 50),
                 mk("2026-07-16T13:30:00.000Z", 200, 60),
                 "not json", json.dumps({"type": "user"})]
        since = local_sync._iso_epoch("2026-07-16T13:10:00.000Z")
        totals = local_sync.claude_usage_totals(lines, since)
        self.assertEqual(totals["tokens_60m"], 200 + 60 + 10)
        self.assertAlmostEqual(totals["last_event_epoch"],
                               local_sync._iso_epoch("2026-07-16T13:30:00.000Z"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/tonylee/solo/token-status-bar/backend && python3 -m unittest test_local_sync -v`
Expected: ERROR with `ModuleNotFoundError: No module named 'local_sync'`

- [ ] **Step 3: Add store support (`source` column + `live_activity`)**

In `backend/store.py`:

1. Append to `SCHEMA` (before the closing `"""`):

```sql
CREATE TABLE IF NOT EXISTS live_activity (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    payload TEXT NOT NULL
);
```

2. In `connect()`, extend the `limit_snapshots` migration list with `("source", "TEXT")`:

```python
        ("monthly_period_end", "TEXT"),
        ("source", "TEXT"),
```

3. In `save_snapshot`, add `"source"` to the `fields` tuple (at the end, after `"monthly_period_end"`).

4. Add after `iter_snapshots`:

```python
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
```

- [ ] **Step 4: Implement `backend/local_sync.py`**

```python
"""Local real-time sync from CLI session logs (Codex, Claude Code).

Codex CLI writes a token_count event (rate_limits + real token counts) to
~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl after every turn; Claude Code
writes per-message usage to ~/.claude/projects/**/*.jsonl. scan() tails the
recently-modified files and turns them into local snapshots (source="local")
and live_activity blobs — zero network, zero quota. Attribution is strict:
codex events map via ~/.codex/auth.json tokens.account_id; no match, no save.
"""
from __future__ import annotations
import datetime, json, os, time
from pathlib import Path
import store

CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
CODEX_SESSIONS = CODEX_HOME / "sessions"
CODEX_AUTH = CODEX_HOME / "auth.json"
CLAUDE_PROJECTS = Path(os.environ.get("CLAUDE_CONFIG_DIR",
                                      str(Path.home() / ".claude"))) / "projects"
CLAUDE_CONFIG = Path.home() / ".claude.json"

RECENT_S = 600           # only files touched in the last 10 min matter
CODEX_TAIL_BYTES = 65536
CLAUDE_TAIL_BYTES = 262144

# Last handled event timestamp per account id — suppresses no-op re-saves.
_last_event: dict = {}


def _iso_epoch(s) -> float | None:
    if not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def tail_lines(path: Path, nbytes: int) -> list[str]:
    """Last complete lines of a file, reading at most nbytes."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - nbytes))
            data = f.read()
    except OSError:
        return []
    lines = data.decode(errors="replace").splitlines()
    if size > nbytes and lines:
        lines = lines[1:]  # first line is almost certainly cut mid-record
    return lines


def _codex_recent_files(now: float) -> list[Path]:
    """Today's + yesterday's session files touched within RECENT_S, newest first."""
    out = []
    for off in (0, 1):
        d = datetime.date.fromtimestamp(now - off * 86400)
        day_dir = CODEX_SESSIONS / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}"
        if not day_dir.is_dir():
            continue
        for p in day_dir.glob("*.jsonl"):
            try:
                if now - p.stat().st_mtime <= RECENT_S:
                    out.append(p)
            except OSError:
                continue
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out


def extract_token_count(obj) -> dict | None:
    """token_count payload from a session-log record (bare or event_msg-wrapped)."""
    if not isinstance(obj, dict):
        return None
    if obj.get("type") == "token_count":
        return obj
    p = obj.get("payload")
    if isinstance(p, dict) and p.get("type") == "token_count":
        return p
    return None


def codex_active_account_id() -> str | None:
    try:
        d = json.loads(CODEX_AUTH.read_text())
    except (OSError, ValueError):
        return None
    tokens = d.get("tokens")
    return tokens.get("account_id") if isinstance(tokens, dict) else None


def match_codex_account(accounts, upstream_id) -> dict | None:
    if not upstream_id:
        return None
    for a in accounts:
        if a.get("provider") == "codex" and a.get("account_id") == upstream_id:
            return a
    return None


def codex_snap(rl) -> dict:
    """Snapshot dict from a token_count rate_limits block (pure function)."""
    snap = {"status": "active", "status_message": "", "source": "local"}
    for side, prefix in (("primary", "primary"), ("secondary", "secondary")):
        w = rl.get(side) or {}
        if w.get("used_percent") is None:
            continue
        snap[f"{prefix}_used_pct"] = float(w["used_percent"])
        if w.get("resets_at") is not None:
            snap[f"{prefix}_reset_at"] = float(w["resets_at"])
        if w.get("window_minutes"):
            snap[f"{prefix}_window_s"] = int(w["window_minutes"]) * 60
    credits = rl.get("credits") or {}
    if credits.get("balance") is not None:
        try:
            snap["credits_balance"] = float(credits["balance"])
        except (TypeError, ValueError):
            pass
    if rl.get("plan_type"):
        snap["plan"] = rl["plan_type"]
    return snap


def _scan_codex(conn, accounts, now: float) -> bool:
    account = match_codex_account(accounts, codex_active_account_id())
    if not account:
        return False
    for path in _codex_recent_files(now):
        event = None
        ev_iso = None
        for line in reversed(tail_lines(path, CODEX_TAIL_BYTES)):
            if '"token_count"' not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            payload = extract_token_count(obj)
            if payload:
                event = payload
                ev_iso = obj.get("timestamp") or payload.get("timestamp")
                break
        if not event:
            continue
        key = ("codex", account["id"])
        if _last_event.get(key) == ev_iso:
            return False
        ev_epoch = _iso_epoch(ev_iso) or now
        latest = store.latest_snapshot(conn, account["id"])
        changed = False
        # Never let a stale local event shadow a newer API reading.
        if not latest or float(latest["ts"]) < ev_epoch:
            rl = event.get("rate_limits") or {}
            if rl.get("primary") or rl.get("secondary"):
                snap = codex_snap(rl)
                snap["raw_json"] = json.dumps({"local_event_ts": ev_iso})
                store.save_snapshot(conn, account["id"], snap)
                changed = True
        info = event.get("info") or {}
        last = info.get("last_token_usage") or {}
        cw = info.get("model_context_window")
        live = {"provider": "codex", "event_epoch": ev_epoch,
                "last_total_tokens": last.get("total_tokens"),
                "last_cached_tokens": last.get("cached_input_tokens"),
                "last_output_tokens": last.get("output_tokens")}
        if cw and last.get("total_tokens"):
            live["context_used_pct"] = round(
                min(100.0, 100.0 * last["total_tokens"] / cw), 1)
        store.upsert_live_activity(conn, account["id"], live)
        _last_event[key] = ev_iso
        return True
    return False


def claude_usage_totals(lines, since_epoch: float) -> dict:
    """Sum new tokens (input+output+cache_creation) from events after since_epoch."""
    total = 0
    last_epoch = None
    for line in lines:
        if '"usage"' not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        usage = ((obj.get("message") or {}).get("usage")
                 if isinstance(obj.get("message"), dict) else None)
        if not isinstance(usage, dict):
            continue
        ts = _iso_epoch(obj.get("timestamp"))
        if ts is None:
            continue
        if last_epoch is None or ts > last_epoch:
            last_epoch = ts
        if ts < since_epoch:
            continue
        total += int(usage.get("input_tokens") or 0)
        total += int(usage.get("output_tokens") or 0)
        total += int(usage.get("cache_creation_input_tokens") or 0)
    return {"tokens_60m": total, "last_event_epoch": last_epoch}


def _claude_account(accounts) -> dict | None:
    claudes = [a for a in accounts if a.get("provider") == "claude"]
    if len(claudes) == 1:
        return claudes[0]
    try:
        cfg = json.loads(CLAUDE_CONFIG.read_text())
        email = ((cfg.get("oauthAccount") or {}).get("emailAddress") or "").lower()
    except (OSError, ValueError):
        return None
    for a in claudes:
        if (a.get("email") or "").lower() == email:
            return a
    return None


def _scan_claude(conn, accounts, now: float) -> bool:
    account = _claude_account(accounts)
    if not account or not CLAUDE_PROJECTS.is_dir():
        return False
    recent = []
    for p in CLAUDE_PROJECTS.rglob("*.jsonl"):
        try:
            if now - p.stat().st_mtime <= RECENT_S:
                recent.append(p)
        except OSError:
            continue
    if not recent:
        return False
    total = 0
    last_epoch = None
    for p in recent:
        t = claude_usage_totals(tail_lines(p, CLAUDE_TAIL_BYTES), now - 3600)
        total += t["tokens_60m"]
        if t["last_event_epoch"] and (last_epoch is None or t["last_event_epoch"] > last_epoch):
            last_epoch = t["last_event_epoch"]
    key = ("claude", account["id"])
    marker = (last_epoch, total)
    if _last_event.get(key) == marker:
        return False
    store.upsert_live_activity(conn, account["id"], {
        "provider": "claude", "event_epoch": last_epoch, "tokens_60m": total})
    _last_event[key] = marker
    return True


def scan(conn, now=None) -> bool:
    """One local-sync pass over both CLI log trees. True when anything changed."""
    now = now or time.time()
    accounts = store.list_accounts(conn)
    changed = False
    try:
        changed = _scan_codex(conn, accounts, now) or changed
    except Exception as e:
        print(f"  local sync (codex) failed: {e}")
    try:
        changed = _scan_claude(conn, accounts, now) or changed
    except Exception as e:
        print(f"  local sync (claude) failed: {e}")
    return changed
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/tonylee/solo/token-status-bar/backend && python3 -m unittest test_local_sync -v`
Expected: PASS

- [ ] **Step 6: Wire the tick into the poller loop and export**

In `backend/poller.py`:

1. Add near `POLL_INTERVAL`:

```python
LOCAL_SYNC_INTERVAL_S = int(os.environ.get("LOCAL_SYNC_INTERVAL_S", "15"))
```

2. Add above `run_loop`:

```python
_next_local_scan = 0.0


def _local_sync_tick(conn):
    """Scan local CLI logs at most every LOCAL_SYNC_INTERVAL_S; export on change."""
    global _next_local_scan
    if time.time() < _next_local_scan:
        return
    _next_local_scan = time.time() + LOCAL_SYNC_INTERVAL_S
    try:
        import local_sync, status
        if local_sync.scan(conn):
            status.cmd_export(conn)
    except Exception as e:
        print(f"  local sync failed: {e}")
```

3. In `run_loop`, call the tick at the top of every iteration (right after `now = time.time()`):

```python
            now = time.time()
            _local_sync_tick(conn)
```

and change the final idle sleep so the loop wakes at local-sync cadence:

```python
            time.sleep(max(1.0, min(wake - time.time(), LOCAL_SYNC_INTERVAL_S)))
```

In `backend/status.py` `cmd_export`, inside the account loop after the `windows` line from Task 2, add:

```python
        live = store.get_live_activity(conn, a["id"])
        if live and (time.time() - float(live.get("ts", 0))) < 600:
            live["as_of_epoch"] = float(live.pop("ts"))
            items[-1]["live"] = live
```

- [ ] **Step 7: Run the full backend suite + live smoke test**

Run: `cd /Users/tonylee/solo/token-status-bar/backend && python3 -m unittest test_local_sync test_status_windows test_claude_usage test_window_history -v`
Expected: PASS

Run: `cd /Users/tonylee/solo/token-status-bar/backend && python3 -c "
import store, local_sync, json
conn = store.connect()
print('changed:', local_sync.scan(conn))
for a in store.list_accounts(conn):
    la = store.get_live_activity(conn, a['id'])
    if la: print(a['provider'], a['email'], la)
"`
Expected: `changed: True` on first run when a Codex/Claude session was active in the last 10 minutes (this Claude Code session counts), and a live blob printed. If no CLI was used recently, `changed: False` with no error is also correct.

- [ ] **Step 8: Restart daemon and commit**

```bash
launchctl kickstart -k "gui/$(id -u)/com.tonye.agentpool-poller"
cd /Users/tonylee/solo/token-status-bar
git add backend/local_sync.py backend/store.py backend/poller.py backend/status.py backend/test_local_sync.py
git commit -m "feat(local-sync): Real-time snapshots from Codex/Claude session logs"
```

---

### Task 4: Swift UI — headline title, gauge rows, live ticker

**Files:**
- Modify: `app/TokenStatusBar.swift`

**Interfaces:**
- Consumes from `status.json` (Task 2/3): per-account `windows: [WindowInfo]`, `live: LiveActivity?`, top-level `headline: Headline?`. `WindowInfo` fields: `kind, label, used_pct, reset_at_epoch, severity, is_active, source, as_of_epoch, projected_exhaust_epoch` (last one arrives in Task 5; optional now).
- Produces: menu-bar title `"41% · 1h12m"` colored by risk; per-account gauge rows in the dropdown; live ticker row above the footer.

- [ ] **Step 1: Add the Codable models**

In `app/TokenStatusBar.swift`, add to `StatusPayload`:

```swift
    var headline: Headline?
```

and below the `UsageWindow` struct add:

```swift
struct WindowInfo: Codable {
    var kind: String
    var label: String?
    var used_pct: Double?
    var reset_at_epoch: Double?
    var severity: String?
    var is_active: Bool?
    var source: String?
    var as_of_epoch: Double?
    var projected_exhaust_epoch: Double?
}

struct Headline: Codable {
    var account_id: Int
    var provider: String
    var email: String?
    var kind: String
    var label: String?
    var used_pct: Double
    var reset_at_epoch: Double?
    var severity: String
}

struct LiveActivity: Codable {
    var provider: String?
    var event_epoch: Double?
    var last_total_tokens: Int?
    var last_cached_tokens: Int?
    var last_output_tokens: Int?
    var context_used_pct: Double?
    var tokens_60m: Int?
    var as_of_epoch: Double?
}
```

Add to `Account`:

```swift
    var windows: [WindowInfo]?
    var live: LiveActivity?
```

- [ ] **Step 2: Build and confirm decoding still works**

Run: `cd /Users/tonylee/solo/token-status-bar && ./build.sh`
Expected: build succeeds (all new fields optional, so old payloads still decode).

- [ ] **Step 3: Render the headline in the status item**

In `AppDelegate`, add helpers and extend `updateStatusIcon()` (keep the existing icon drawing; only add the title):

```swift
    private func windowRiskColor(pct: Double, severity: String?, projected: Bool) -> NSColor {
        if projected || pct > 80 || (severity ?? "normal") != "normal" { return .systemRed }
        if pct >= 50 { return .systemYellow }
        return .systemGreen
    }

    static func timeLeft(_ epoch: Double?) -> String? {
        guard let epoch else { return nil }
        let s = Int(epoch - Date().timeIntervalSince1970)
        if s <= 0 { return nil }
        if s < 3600 { return "\(s / 60)m" }
        if s < 86400 { return "\(s / 3600)h\((s % 3600) / 60)m" }
        return String(format: "%.1fd", Double(s) / 86400.0)
    }

    private func headlineTitle() -> NSAttributedString? {
        guard let h = loader.payload?.headline else { return nil }
        var text = " \(Int(h.used_pct.rounded()))%"
        if let left = AppDelegate.timeLeft(h.reset_at_epoch) { text += " · \(left)" }
        let color = windowRiskColor(pct: h.used_pct, severity: h.severity, projected: false)
        return NSAttributedString(string: text, attributes: [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium),
            .foregroundColor: color,
            .baselineOffset: 0.5,
        ])
    }
```

At the end of `updateStatusIcon()` (after `button.image = image`), add:

```swift
        if let title = headlineTitle() {
            button.attributedTitle = title
            button.imagePosition = .imageLeft
        } else {
            button.attributedTitle = NSAttributedString(string: "")
            button.imagePosition = .imageOnly
        }
```

The title refreshes whenever the loader publishes (every 30s), so the countdown ticks at 30s granularity without extra timers.

- [ ] **Step 4: Add the gauge view and attach it to account rows**

Add below the `FixedMenuRowView` class:

```swift
/// One thin usage gauge: [label | bar | pct · time-left].
final class GaugeRowView: NSView {
    private let window: WindowInfo
    private let color: NSColor

    init(window: WindowInfo, color: NSColor, width: CGFloat) {
        self.window = window
        self.color = color
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: 16))
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }

    private func kindLabel() -> String {
        if window.kind == "model_weekly" { return window.label ?? "model" }
        if let label = window.label, window.kind == "monthly" { return label }
        return window.kind
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        let pct = max(0, min(100, window.used_pct ?? 0))
        let labelX: CGFloat = 28
        let labelW: CGFloat = 62
        let rightW: CGFloat = 96
        let barX = labelX + labelW + 6
        let barW = bounds.width - barX - rightW - 16
        let attrsLabel: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 10.5),
            .foregroundColor: NSColor.secondaryLabelColor,
        ]
        (kindLabel() as NSString).draw(
            at: NSPoint(x: labelX, y: 2), withAttributes: attrsLabel)

        let track = NSRect(x: barX, y: 5.5, width: barW, height: 5)
        NSColor.quaternaryLabelColor.setFill()
        NSBezierPath(roundedRect: track, xRadius: 2.5, yRadius: 2.5).fill()
        if pct > 0 {
            let fill = NSRect(x: barX, y: 5.5, width: barW * pct / 100.0, height: 5)
            color.setFill()
            NSBezierPath(roundedRect: fill, xRadius: 2.5, yRadius: 2.5).fill()
        }

        var right = "\(Int(pct.rounded()))%"
        if let left = AppDelegate.timeLeft(window.reset_at_epoch) { right += " · \(left)" }
        let attrsRight: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 10.5, weight: .regular),
            .foregroundColor: NSColor.secondaryLabelColor,
        ]
        let size = (right as NSString).size(withAttributes: attrsRight)
        (right as NSString).draw(
            at: NSPoint(x: bounds.width - 16 - size.width, y: 2),
            withAttributes: attrsRight)
    }
}

/// Account row + its gauge bars stacked into one menu-item view.
final class AccountRowWithGauges: NSView {
    init(row: FixedMenuRowView, gauges: [GaugeRowView], width: CGFloat) {
        let gaugeH: CGFloat = 16
        let pad: CGFloat = gauges.isEmpty ? 0 : 4
        let height = MenuRowLayout.standardHeight + CGFloat(gauges.count) * gaugeH + pad
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: height))
        row.setFrameOrigin(NSPoint(x: 0, y: height - MenuRowLayout.standardHeight))
        addSubview(row)
        for (i, g) in gauges.enumerated() {
            g.setFrameOrigin(NSPoint(x: 0, y: CGFloat(gauges.count - 1 - i) * gaugeH + 2))
            g.setFrameSize(NSSize(width: width, height: gaugeH))
            addSubview(g)
        }
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }
}
```

In `accountItem(_:)`, replace the final view assignment:

```swift
        let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        item.submenu = submenu
        let row = FixedMenuRowView(title: title, style: .submenu, submenu: submenu,
                                   dotColor: statusColor(acct.status), badge: endSoonBadge(acct))
        let gauges = (acct.windows ?? []).prefix(3).map { w in
            GaugeRowView(window: w,
                         color: windowRiskColor(pct: w.used_pct ?? 0,
                                                severity: w.severity,
                                                projected: w.projected_exhaust_epoch != nil),
                         width: MenuRowLayout.width)
        }
        item.view = AccountRowWithGauges(row: row, gauges: Array(gauges),
                                         width: MenuRowLayout.width)
        return item
```

- [ ] **Step 5: Add the live ticker row**

In `buildMenu(_:)`, just before `addFooter(menu)`, add:

```swift
        // Live ticker: freshest local session activity across accounts.
        let fresh = payload.accounts.compactMap { a -> (Account, LiveActivity, Double)? in
            guard let live = a.live, let ts = live.as_of_epoch ?? live.event_epoch,
                  Date().timeIntervalSince1970 - ts < 600 else { return nil }
            return (a, live, ts)
        }.max(by: { $0.2 < $1.2 })
        if let (acct, live, _) = fresh {
            var parts: [String] = [providerDisplayName(acct.provider)]
            if let tokens = live.last_total_tokens {
                parts.append("+\(tokens.formatted()) tok")
            }
            if let ctx = live.context_used_pct {
                parts.append("context \(Int(ctx.rounded()))%")
            }
            if let t60 = live.tokens_60m, live.last_total_tokens == nil {
                parts.append("\(t60.formatted()) tok/60m")
            }
            menu.addItem(infoItem("⚡︎ " + parts.joined(separator: " · ")))
            menu.addItem(separatorRow())
        }
```

- [ ] **Step 6: Build, install, and verify visually**

```bash
cd /Users/tonylee/solo/token-status-bar
./build.sh
osascript -e 'tell application "TokenStatusBar" to quit'
open /Applications/TokenStatusBar.app
```

Expected checks: (1) menu-bar shows `NN% · XhYm` next to the chart icon in the headline color; (2) opening the dropdown shows gauge bars under each account title; (3) a fresh Codex/Claude session (this one) produces the `⚡︎` ticker row; (4) accounts without windows render exactly as before.

- [ ] **Step 7: Commit**

```bash
cd /Users/tonylee/solo/token-status-bar
git add app/TokenStatusBar.swift
git commit -m "feat(app): Headline in menu bar, gauge rows, live ticker"
```

---

### Task 5: Burn-rate projection

**Files:**
- Modify: `backend/store.py` (add `snapshots_since`)
- Modify: `backend/status.py` (add `project_exhaust`, `attach_projections`; wire into `cmd_export`)
- Modify: `app/TokenStatusBar.swift` (projection line in account submenus)
- Create: `backend/test_burn_rate.py`

**Interfaces:**
- Consumes: `normalize_windows(provider, snap)` (Task 2), `store.snapshots_since(conn, account_id, since_ts) -> list[dict]` (oldest first, successful statuses included as stored).
- Produces: `project_exhaust(points: list[tuple[float, float]], reset_at: float | None, now: float) -> float | None` (pure); windows in `status.json` gain `projected_exhaust_epoch` when exhaustion lands before the reset. `select_headline` from Task 2 already prioritizes it.

- [ ] **Step 1: Write the failing tests**

Create `backend/test_burn_rate.py`:

```python
"""Tests for burn-rate projection."""
from __future__ import annotations
import os, sys, tempfile, unittest

_TMP = tempfile.mkdtemp(prefix="tsb-test-")
os.environ["AGENT_POOL_DB"] = os.path.join(_TMP, "pool.db")
os.environ["AGENT_POOL_STATUS_JSON"] = os.path.join(_TMP, "status.json")
os.environ["AGENT_POOL_HISTORY_DIR"] = os.path.join(_TMP, "history")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import status  # noqa: E402


class ProjectExhaustTest(unittest.TestCase):
    def test_projects_when_pace_beats_reset(self):
        # 10% per hour starting at 40%: 100% reached in 6h; reset in 8h.
        pts = [(0.0, 40.0), (1800.0, 45.0), (3600.0, 50.0)]
        out = status.project_exhaust(pts, reset_at=3600.0 + 8 * 3600, now=3600.0)
        self.assertIsNotNone(out)
        self.assertAlmostEqual(out, 3600.0 + (50.0 / (10.0 / 3600.0)), delta=60)

    def test_none_when_reset_comes_first(self):
        pts = [(0.0, 40.0), (1800.0, 41.0), (3600.0, 42.0)]
        self.assertIsNone(status.project_exhaust(pts, reset_at=3600.0 + 3600, now=3600.0))

    def test_none_when_flat_or_decreasing(self):
        self.assertIsNone(status.project_exhaust(
            [(0.0, 40.0), (1800.0, 40.0), (3600.0, 40.0)], reset_at=99999.0, now=3600.0))

    def test_reset_drop_trims_series(self):
        # A reset mid-series: only the post-reset segment counts (2 pts < min 3).
        pts = [(0.0, 90.0), (1800.0, 95.0), (3600.0, 5.0), (5400.0, 10.0)]
        self.assertIsNone(status.project_exhaust(pts, reset_at=99999.0, now=5400.0))

    def test_too_few_points(self):
        self.assertIsNone(status.project_exhaust([(0.0, 40.0)], reset_at=9999.0, now=0.0))

    def test_no_reset_at_projects_nothing(self):
        pts = [(0.0, 40.0), (1800.0, 45.0), (3600.0, 50.0)]
        self.assertIsNone(status.project_exhaust(pts, reset_at=None, now=3600.0))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/tonylee/solo/token-status-bar/backend && python3 -m unittest test_burn_rate -v`
Expected: ERROR with `AttributeError: module 'status' has no attribute 'project_exhaust'`

- [ ] **Step 3: Implement the projection**

In `backend/store.py` after `iter_snapshots`:

```python
def snapshots_since(conn, account_id, since_ts) -> list[dict]:
    """Successful snapshots newer than since_ts, oldest first."""
    rows = conn.execute(
        "SELECT * FROM limit_snapshots WHERE account_id=? AND ts>? "
        "AND status IN ('active','rate_limited') ORDER BY ts ASC",
        (account_id, since_ts),
    ).fetchall()
    return [dict(r) for r in rows]
```

In `backend/status.py` below `select_headline`:

```python
BURN_LOOKBACK_S = 3600
BURN_MIN_POINTS = 3
BURN_RESET_DROP_PCT = 5.0


def project_exhaust(points, reset_at, now) -> float | None:
    """Epoch when used% hits 100 at the trailing pace, if before reset_at.

    points: (ts, used_pct) oldest first. A drop > BURN_RESET_DROP_PCT marks a
    reset — only the segment after the most recent reset is used.
    """
    if not reset_at:
        return None
    segment = []
    for ts, used in points:
        if segment and used < segment[-1][1] - BURN_RESET_DROP_PCT:
            segment = []
        segment.append((ts, used))
    if len(segment) < BURN_MIN_POINTS:
        return None
    (t0, u0), (t1, u1) = segment[0], segment[-1]
    if t1 <= t0 or u1 <= u0:
        return None
    rate = (u1 - u0) / (t1 - t0)  # pct per second
    exhaust = t1 + (100.0 - u1) / rate
    return exhaust if exhaust < reset_at else None


def attach_projections(conn, account, windows, now=None):
    """Set projected_exhaust_epoch on windows exhausting before their reset."""
    now = now or time.time()
    future = [w for w in windows
              if w.get("reset_at_epoch") and w["reset_at_epoch"] > now]
    if not future:
        return
    history = store.snapshots_since(conn, account["id"], now - BURN_LOOKBACK_S)
    if len(history) < BURN_MIN_POINTS:
        return
    series: dict = {}
    for s in history:
        for w in normalize_windows(account["provider"], s):
            key = (w["kind"], w.get("label"))
            series.setdefault(key, []).append((float(s["ts"]), w["used_pct"]))
    for w in future:
        pts = series.get((w["kind"], w.get("label"))) or []
        exhaust = project_exhaust(pts, w["reset_at_epoch"], now)
        if exhaust:
            w["projected_exhaust_epoch"] = exhaust
```

Wire into `cmd_export`, right after the Task 2 `items[-1]["windows"] = ...` line:

```python
        attach_projections(conn, a, items[-1]["windows"])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/tonylee/solo/token-status-bar/backend && python3 -m unittest test_burn_rate test_status_windows -v`
Expected: PASS

- [ ] **Step 5: Show the projection in the Swift submenu**

In `app/TokenStatusBar.swift`, `accountItem(_:)`, right after the provider-specific submenu builder calls (before `submenu.addItem(separatorRow(width: detailWidth))`), add:

```swift
        for w in acct.windows ?? [] {
            guard let exhaust = w.projected_exhaust_epoch,
                  let left = AppDelegate.timeLeft(exhaust) else { continue }
            let name = w.kind == "model_weekly" ? (w.label ?? "model") : w.kind
            submenu.addItem(infoItem("⚠︎ \(name): exhausts in ~\(left) at current pace",
                                     width: detailWidth))
        }
```

(The gauge color escalation for projected windows already happens in Task 4's `windowRiskColor(projected:)`, and `select_headline` already prefers projected windows.)

- [ ] **Step 6: Build and verify**

```bash
cd /Users/tonylee/solo/token-status-bar
./build.sh
osascript -e 'tell application "TokenStatusBar" to quit'
open /Applications/TokenStatusBar.app
```

Expected: build passes; app runs. (A live projection needs an actively-burning window — verify the plumbing by checking `status.json` for `projected_exhaust_epoch` after heavy usage, or temporarily by unit tests only.)

- [ ] **Step 7: Commit**

```bash
cd /Users/tonylee/solo/token-status-bar
git add backend/store.py backend/status.py backend/test_burn_rate.py app/TokenStatusBar.swift
git commit -m "feat(burn-rate): Project window exhaustion before reset"
```

---

### Task 6: End-to-end verification + dmg build

**Files:**
- No new files; verification only (fixes go into the files above).

- [ ] **Step 1: Full backend test suite**

Run: `cd /Users/tonylee/solo/token-status-bar/backend && python3 -m unittest discover -p 'test_*.py' -v`
Expected: all PASS.

- [ ] **Step 2: Restart daemon, force a full poll, inspect status.json**

```bash
launchctl kickstart -k "gui/$(id -u)/com.tonye.agentpool-poller"
cd /Users/tonylee/solo/token-status-bar/backend && python3 pool.py poll
python3 - <<'EOF'
import json
d = json.load(open('/Users/tonylee/solo/token-status-bar/secrets/status.json'))
assert d.get("headline"), "headline missing"
assert d["headline"]["reset_at_epoch"], "headline epoch missing"
for a in d["accounts"]:
    assert "windows" in a, f"windows missing for {a['provider']}"
claude = [a for a in d["accounts"] if a["provider"] == "claude"][0]
kinds = [w["kind"] for w in claude["windows"]]
assert "5h" in kinds and "weekly" in kinds, kinds
print("OK", d["headline"])
EOF
```

Expected: `OK {...headline...}` with no assertion errors.

- [ ] **Step 3: Verify local sync freshness end-to-end**

Use this Claude Code session itself as the signal: within ~15–30s of the daemon running, `secrets/status.json` should refresh with a `live` blob. Check:

```bash
python3 -c "
import json, time, os
p = '/Users/tonylee/solo/token-status-bar/secrets/status.json'
d = json.load(open(p))
age = time.time() - os.path.getmtime(p)
lives = [(a['provider'], a.get('live')) for a in d['accounts'] if a.get('live')]
print(f'status.json age: {age:.0f}s, live blobs: {lives}')
"
```

Expected: age well under 300s while a CLI session is active, and at least one live blob (claude, from this session).

- [ ] **Step 4: Build app + dmg per AGENTS.md**

```bash
cd /Users/tonylee/solo/token-status-bar
./build.sh
```

Expected: `.app` and `.dmg` build succeed (build.sh produces the app; then produce the dmg from the bundled app as build.sh defines). Do not proceed to any PR if the dmg fails.

- [ ] **Step 5: Relaunch and manual visual check**

```bash
osascript -e 'tell application "TokenStatusBar" to quit'
open /Applications/TokenStatusBar.app
```

Checklist: headline `NN% · XhYm` visible and colored; dropdown gauges per account; Claude submenu shows Fable row and binding window; ticker row appears; Poll Now still works; no layout clipping at 420px detail width.

- [ ] **Step 6: Final commit**

```bash
cd /Users/tonylee/solo/token-status-bar
git add -A -- ':!secrets' ':!*.log' ':!history'
git commit -m "chore: Verify realtime token sync end-to-end"
```

(Skip the commit if verification produced no file changes.)
