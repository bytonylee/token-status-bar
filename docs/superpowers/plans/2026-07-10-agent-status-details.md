# Agent Status Details Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Antigravity submenu shows the IDE's 5h/weekly quota split; every agent gets a uniform 5-row Status section; heartbeat shows last success and gains a manual "Run heartbeat now" action.

**Architecture:** A new `backend/agy_usage.py` pty-drives the `agy` CLI's `/usage` panel and parses it into structured windows that `poller.py` stores and `status.py` exports. `status.py` also derives missing plan dates (Claude anniversary, Copilot start) and exports heartbeat last-success. The Swift menu app (`app/TokenStatusBar.swift`) renders the new fields and shells out to `pool.py heartbeat [--account N]` for the manual trigger.

**Tech Stack:** Python 3.12 stdlib only (pty, sqlite3, urllib), Swift/Cocoa (single-file app, no test target), unittest for backend tests.

**Spec:** `docs/superpowers/specs/2026-07-10-agent-status-details-design.md`

## Global Constraints

- Backend is Python stdlib only — no new dependencies.
- Swift app is one file (`app/TokenStatusBar.swift`); no test target exists — Swift verification is `swiftc` compile via `build.sh` plus visual check.
- Every new user-facing string gets entries in all 4 L10n tables (en, ko, zh, ja).
- agy failures must never break the antigravity poll: `fetch_usage()` returns `None` on any error, and the menu falls back to today's per-model view.
- Backend tests run as plain scripts: `python3 backend/test_<name>.py` (unittest style, like `test_onboarding.py`). They must not touch the real DB — set `AGENT_POOL_DB`/`AGENT_POOL_STATUS_JSON` env vars to temp paths BEFORE importing backend modules.
- The poller runs under LaunchAgent `com.tonye.agentpool-poller`: after backend edits, `pkill` the running poller so it respawns with new code. Never start a second poller by hand.
- Per AGENTS.md: build the `.dmg` (`bash build.sh --dmg`) and confirm success before any PR; after building, quit the old app and reopen `/Applications/TokenStatusBar.app`.
- Commit after each task with a Conventional-style message ending in `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: agy `/usage` panel parser (`backend/agy_usage.py` — parse half)

**Files:**
- Create: `backend/agy_usage.py`
- Test: `backend/test_agy_usage.py`

**Interfaces:**
- Produces: `parse_usage_panel(text: str, now: float | None = None) -> list[dict] | None` where each dict is `{"group": "gemini"|"other", "window": "5h"|"weekly", "remaining_pct": float, "reset_at": float | None}`. Returns `None` when nothing parses. Task 2 adds `fetch_usage()` to the same module; Task 3 consumes the dict shape.

- [ ] **Step 1: Write the failing test**

Create `backend/test_agy_usage.py`. The fixture is real ANSI-stripped output captured from `agy` `/usage` on 2026-07-10:

```python
#!/usr/bin/env python3
"""Tests for the agy /usage panel parser."""
from __future__ import annotations
import os, sys, tempfile, unittest

_TMP = tempfile.mkdtemp(prefix="tsb-test-")
os.environ["AGENT_POOL_DB"] = os.path.join(_TMP, "pool.db")
os.environ["AGENT_POOL_STATUS_JSON"] = os.path.join(_TMP, "status.json")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agy_usage  # noqa: E402

PANEL = """\
└ Models & Quota
  Account: user@example.com
GEMINI MODELS
  Models within this group: Gemini Flash, Gemini Pro
  Weekly Limit
    [██████████████████████████████████████████████████] 99.63%
    100% remaining · Refreshes in 151h 36m
  Five Hour Limit
    [██████████████████████████████████████████████████] 99.45%
    99% remaining · Refreshes in 48m
CLAUDE AND GPT MODELS
  Models within this group: Claude Opus, Claude Sonnet, GPT-OSS
  Weekly Limit
    [██████████████████████████████████████████████████] 100.00%
    Quota available
  Five Hour Limit
    [██████████████████████████████████████████████████] 100.00%
    Quota available
  │Within each group, models share a weekly limit and a 5-hour limit.
"""


class ParseUsagePanelTests(unittest.TestCase):
    def test_parses_four_windows(self):
        now = 1_000_000.0
        windows = agy_usage.parse_usage_panel(PANEL, now=now)
        self.assertEqual(len(windows), 4)
        by_key = {(w["group"], w["window"]): w for w in windows}
        self.assertEqual(set(by_key), {("gemini", "weekly"), ("gemini", "5h"),
                                       ("other", "weekly"), ("other", "5h")})

        gw = by_key[("gemini", "weekly")]
        self.assertAlmostEqual(gw["remaining_pct"], 99.63)
        self.assertEqual(gw["reset_at"], now + 151 * 3600 + 36 * 60)

        g5 = by_key[("gemini", "5h")]
        self.assertAlmostEqual(g5["remaining_pct"], 99.45)
        self.assertEqual(g5["reset_at"], now + 48 * 60)

        for key in (("other", "weekly"), ("other", "5h")):
            self.assertAlmostEqual(by_key[key]["remaining_pct"], 100.0)
            self.assertIsNone(by_key[key]["reset_at"])

    def test_garbage_returns_none(self):
        self.assertIsNone(agy_usage.parse_usage_panel("no quota panel here"))
        self.assertIsNone(agy_usage.parse_usage_panel(""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 backend/test_agy_usage.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'agy_usage'`

- [ ] **Step 3: Write the parser**

Create `backend/agy_usage.py`:

```python
"""Antigravity 5h/weekly quota windows, scraped from the `agy` CLI.

The cloudcode retrieveUserQuotaSummary API returns only a collapsed
per-model view for our OAuth client; the Weekly/Five-Hour breakdown per
model group is rendered only by the CLI's /usage panel. This module drives
the interactive CLI in a pseudo-terminal, sends /usage, captures the panel
text, and parses it. Every failure path returns None so the poller can
fall back to the API view.
"""
from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import shutil
import struct
import termios
import time

PANEL_TIMEOUT_S = 60

_ANSI_RE = re.compile(r"\x1b\[[0-9;?$ ]*[A-Za-z]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[=>]")
_GROUP_RE = re.compile(r"^\s*([A-Z][A-Z &/]* MODELS)\s*$")
_WINDOW_RE = re.compile(r"^\s*(Weekly|Five Hour) Limit\s*$")
_BAR_PCT_RE = re.compile(r"\]\s*([0-9]+(?:\.[0-9]+)?)%")
_REFRESH_RE = re.compile(r"Refreshes in\s+(?:(\d+)h)?\s*(?:(\d+)m)?")


def parse_usage_panel(text: str, now: float | None = None):
    """Parse the /usage panel text into quota windows.

    Returns [{"group": "gemini"|"other", "window": "5h"|"weekly",
              "remaining_pct": float, "reset_at": epoch_or_None}]
    or None when no windows were found.
    """
    now = time.time() if now is None else now
    windows = []
    group = None
    window = None
    bar_pct = None
    for raw in text.splitlines():
        line = raw.rstrip()
        m = _GROUP_RE.match(line)
        if m:
            group = "gemini" if "GEMINI" in m.group(1) else "other"
            window = None
            continue
        if group is None:
            continue
        m = _WINDOW_RE.match(line)
        if m:
            window = "weekly" if m.group(1) == "Weekly" else "5h"
            bar_pct = None
            continue
        if window is None:
            continue
        m = _BAR_PCT_RE.search(line)
        if m:
            bar_pct = float(m.group(1))
            continue
        if "Quota available" in line:
            windows.append({"group": group, "window": window,
                            "remaining_pct": bar_pct if bar_pct is not None else 100.0,
                            "reset_at": None})
            window = None
            continue
        m = _REFRESH_RE.search(line)
        if m and (m.group(1) or m.group(2)):
            secs = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60
            windows.append({"group": group, "window": window,
                            "remaining_pct": bar_pct if bar_pct is not None else 100.0,
                            "reset_at": now + secs})
            window = None
    return windows or None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 backend/test_agy_usage.py`
Expected: `Ran 2 tests ... OK`

- [ ] **Step 5: Commit**

```bash
git add backend/agy_usage.py backend/test_agy_usage.py
git commit -m "feat(antigravity): Parse agy /usage quota panel

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: pty driver `fetch_usage()` + poller/status wiring

**Files:**
- Modify: `backend/agy_usage.py` (append driver functions)
- Modify: `backend/poller.py:543-547` (`poll_antigravity` raw_json block)
- Modify: `backend/status.py:274-277` (`provider_extra` antigravity branch)
- Test: `backend/test_agy_usage.py` (one export-shape test), manual pty run

**Interfaces:**
- Consumes: `parse_usage_panel` from Task 1.
- Produces: `agy_usage.fetch_usage(timeout_s: int = 60) -> list[dict] | None` (same dict shape as Task 1). status.json antigravity accounts gain `"usage_windows": [{"group": "gemini"|"other", "window": "5h"|"weekly", "used_pct": float, "reset": "YYYY-MM-DD HH:MM" | None}]` — Task 6 (Swift) decodes exactly this.

- [ ] **Step 1: Append the pty driver to `backend/agy_usage.py`**

No unit test for the driver itself (it needs a live logged-in `agy`); it is verified manually in Step 4 and hardened by returning `None` on every failure.

```python
def _agy_path():
    return shutil.which("agy") or os.path.expanduser("~/.local/bin/agy")


def _read_for(fd, secs, stop: bytes | None = None) -> bytes:
    end = time.time() + secs
    chunks: list[bytes] = []
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.5)
        if not r:
            continue
        try:
            d = os.read(fd, 65536)
        except OSError:
            break
        if not d:
            break
        chunks.append(d)
        if stop and stop in b"".join(chunks[-3:]):
            break
    return b"".join(chunks)


def fetch_usage(timeout_s: int = PANEL_TIMEOUT_S):
    """Drive `agy` /usage in a pty and return parsed windows, or None."""
    agy = _agy_path()
    if not agy or not os.path.exists(agy):
        return None
    try:
        return _drive(agy, timeout_s)
    except Exception:
        return None


def _drive(agy: str, timeout_s: int):
    pid, fd = pty.fork()
    if pid == 0:  # child: exec agy with a sane TERM, outside any repo
        os.environ["TERM"] = "xterm-256color"
        try:
            os.chdir("/tmp")
        except OSError:
            pass
        os.execv(agy, [agy])
    buf = b""
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 160, 0, 0))
        # Wait for the interactive prompt ("? for shortcuts" footer).
        buf += _read_for(fd, min(15, timeout_s), stop=b"shortcuts")
        for ch in "/usage":
            os.write(fd, ch.encode())
            time.sleep(0.15)
        time.sleep(1.0)
        os.write(fd, b"\r")
        # The panel ends with an explainer starting "Within each group".
        buf += _read_for(fd, min(20, timeout_s), stop=b"Within each group")
    finally:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
    text = _ANSI_RE.sub("", buf.decode(errors="replace"))
    return parse_usage_panel(text)
```

- [ ] **Step 2: Wire into `poll_antigravity`**

In `backend/poller.py`, replace the `snap["raw_json"] = ...` block at the end of `poll_antigravity` (currently lines 543-547):

```python
    try:
        import agy_usage
        usage_windows = agy_usage.fetch_usage()
    except Exception:
        usage_windows = None

    snap["raw_json"] = json.dumps({"extra": {
        "tier_id": tier_id,
        "tier_description": tier_desc,
        "active_tier": active_tier_id if active_tier_id and active_tier_id != tier_id else None,
        "usage_windows": usage_windows,
    }})
```

- [ ] **Step 3: Export from `status.py`**

In `backend/status.py` `provider_extra`, replace the antigravity branch (currently lines 274-277):

```python
    elif provider == "antigravity":
        out["tier_id"] = extra.get("tier_id")
        out["tier_description"] = extra.get("tier_description")
        out["active_tier"] = extra.get("active_tier")
        exported = []
        for w in extra.get("usage_windows") or []:
            if not isinstance(w, dict) or w.get("remaining_pct") is None:
                continue
            used = 100.0 - float(w["remaining_pct"])
            exported.append({
                "group": w.get("group"),
                "window": w.get("window"),
                "used_pct": round(max(0.0, min(100.0, used)), 2),
                "reset": ts_fmt(w["reset_at"]) if w.get("reset_at") else None,
            })
        if exported:
            out["usage_windows"] = exported
```

- [ ] **Step 4: Add an export-shape test and verify the driver live**

Append to `backend/test_agy_usage.py` (before the `if __name__` block):

```python
import json
import status  # noqa: E402


class ExportShapeTests(unittest.TestCase):
    def test_provider_extra_exports_usage_windows(self):
        snap = {"raw_json": json.dumps({"extra": {
            "tier_id": "g1-pro-tier",
            "usage_windows": [
                {"group": "gemini", "window": "5h",
                 "remaining_pct": 99.45, "reset_at": 1_000_000.0},
                {"group": "other", "window": "weekly",
                 "remaining_pct": 100.0, "reset_at": None},
            ],
        }})}
        out = status.provider_extra("antigravity", snap)
        self.assertEqual(len(out["usage_windows"]), 2)
        g5 = out["usage_windows"][0]
        self.assertEqual(g5["group"], "gemini")
        self.assertEqual(g5["window"], "5h")
        self.assertAlmostEqual(g5["used_pct"], 0.55)
        self.assertIsNotNone(g5["reset"])
        ow = out["usage_windows"][1]
        self.assertEqual(ow["used_pct"], 0.0)
        self.assertIsNone(ow["reset"])
```

Run: `python3 backend/test_agy_usage.py`
Expected: `Ran 3 tests ... OK`

Then the live driver check (needs the logged-in `agy` on this machine, takes ~35s):

Run: `python3 -c "import sys; sys.path.insert(0,'backend'); import agy_usage, json; print(json.dumps(agy_usage.fetch_usage(), indent=1))"`
Expected: JSON list with 4 entries (gemini/other × 5h/weekly), non-null `reset_at` on any window below 100%. If it prints `null`, debug before proceeding (check `agy` login state).

- [ ] **Step 5: Commit**

```bash
git add backend/agy_usage.py backend/test_agy_usage.py backend/poller.py backend/status.py
git commit -m "feat(antigravity): Export 5h/weekly usage windows

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Plan-date derivations (Claude anniversary, Copilot start)

**Files:**
- Modify: `backend/status.py` (imports; new helpers after `human_secs`; `claude_extra` ~line 236; `provider_extra` copilot branch ~line 288)
- Test: `backend/test_status_dates.py`

**Interfaces:**
- Produces: `status.next_monthly_anniversary(iso_start, now=None) -> datetime | None` (tz-aware, first monthly recurrence strictly after `now`); `status.previous_month(iso_date) -> datetime | None`. status.json: Claude accounts gain `plan_reset`, Copilot accounts gain `plan_start`. The Swift side (Task 5) reads these via the existing `plan_reset`/`plan_start` fields — no new Swift decoding.

- [ ] **Step 1: Write the failing test**

Create `backend/test_status_dates.py`:

```python
#!/usr/bin/env python3
"""Tests for derived plan dates (Claude anniversary, Copilot start)."""
from __future__ import annotations
import datetime, json, os, sys, tempfile, unittest

_TMP = tempfile.mkdtemp(prefix="tsb-test-")
os.environ["AGENT_POOL_DB"] = os.path.join(_TMP, "pool.db")
os.environ["AGENT_POOL_STATUS_JSON"] = os.path.join(_TMP, "status.json")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import status  # noqa: E402

UTC = datetime.timezone.utc


class AnniversaryTests(unittest.TestCase):
    def test_next_anniversary_same_month(self):
        now = datetime.datetime(2026, 7, 10, tzinfo=UTC)
        got = status.next_monthly_anniversary("2025-03-15T09:30:00Z", now=now)
        self.assertEqual(got, datetime.datetime(2026, 7, 15, 9, 30, tzinfo=UTC))

    def test_next_anniversary_rolls_to_next_month(self):
        now = datetime.datetime(2026, 7, 20, tzinfo=UTC)
        got = status.next_monthly_anniversary("2025-03-15T09:30:00Z", now=now)
        self.assertEqual(got, datetime.datetime(2026, 8, 15, 9, 30, tzinfo=UTC))

    def test_month_end_clamps(self):
        # Started Jan 31 → February anniversary clamps to Feb 28.
        now = datetime.datetime(2026, 2, 1, tzinfo=UTC)
        got = status.next_monthly_anniversary("2026-01-31T00:00:00Z", now=now)
        self.assertEqual(got, datetime.datetime(2026, 2, 28, tzinfo=UTC))

    def test_invalid_input_returns_none(self):
        self.assertIsNone(status.next_monthly_anniversary(None))
        self.assertIsNone(status.next_monthly_anniversary("not-a-date"))


class PreviousMonthTests(unittest.TestCase):
    def test_simple_shift(self):
        got = status.previous_month("2026-08-01")
        self.assertEqual((got.year, got.month, got.day), (2026, 7, 1))

    def test_clamp(self):
        # Mar 31 minus one month clamps to Feb 28.
        got = status.previous_month("2026-03-31")
        self.assertEqual((got.year, got.month, got.day), (2026, 2, 28))

    def test_invalid_returns_none(self):
        self.assertIsNone(status.previous_month(None))
        self.assertIsNone(status.previous_month("garbage"))


class WiringTests(unittest.TestCase):
    def test_claude_extra_derives_plan_reset(self):
        snap = {"raw_json": json.dumps({"profile": {
            "subscription_created_at": "2025-03-15T09:30:00Z",
        }})}
        out = status.claude_extra(snap)
        self.assertIn("plan_reset", out)
        # Formatted as "YYYY-MM-DD HH:MM" KST-local, day 15.
        self.assertRegex(out["plan_reset"], r"^\d{4}-\d{2}-15 \d{2}:\d{2}$")

    def test_copilot_extra_derives_plan_start(self):
        snap = {"raw_json": json.dumps({
            "extra": {"access_sku": "copilot_pro"},
            "reset": "2026-08-01",
        })}
        out = status.provider_extra("copilot", snap)
        self.assertIn("plan_reset", out)
        self.assertEqual(out["plan_start"], "2026-07-01")


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 backend/test_status_dates.py`
Expected: FAIL with `AttributeError: module 'status' has no attribute 'next_monthly_anniversary'`

- [ ] **Step 3: Implement helpers and wiring in `backend/status.py`**

Add `calendar` to the imports line (line 3):

```python
import calendar, json, os, re, sys, time, datetime, zoneinfo
```

Add after `human_secs` (line 89):

```python
def _shift_months(dt: datetime.datetime, months: int) -> datetime.datetime:
    month_index = dt.year * 12 + (dt.month - 1) + months
    year, month = divmod(month_index, 12)
    month += 1
    last_day = calendar.monthrange(year, month)[1]
    return dt.replace(year=year, month=month, day=min(dt.day, last_day))


def _parse_iso(s) -> datetime.datetime | None:
    if not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def next_monthly_anniversary(iso_start, now=None) -> datetime.datetime | None:
    """First monthly recurrence of iso_start strictly after now (UTC)."""
    start = _parse_iso(iso_start)
    if start is None:
        return None
    now_dt = now or datetime.datetime.now(datetime.timezone.utc)
    candidate = start
    while candidate <= now_dt:
        candidate = _shift_months(candidate, 1)
    return candidate


def previous_month(iso_date) -> datetime.datetime | None:
    """iso_date shifted back one month, day clamped to month length."""
    dt = _parse_iso(iso_date)
    if dt is None:
        return None
    return _shift_months(dt, -1)
```

In `claude_extra`, after the line `out["plan_start"] = iso_fmt(prof.get("subscription_created_at"))` (line 236), add:

```python
        anniversary = next_monthly_anniversary(prof.get("subscription_created_at"))
        if anniversary:
            out["plan_reset"] = anniversary.astimezone(KST).strftime("%Y-%m-%d %H:%M")
```

In `provider_extra`, in the copilot branch, replace:

```python
        # plan_reset from the top-level "reset" field (quota_reset_date)
        reset_date = rj.get("reset")
        if reset_date:
            out["plan_reset"] = iso_fmt(reset_date) or reset_date
```

with:

```python
        # plan_reset from the top-level "reset" field (quota_reset_date);
        # plan_start derived as one month before the reset.
        reset_date = rj.get("reset")
        if reset_date:
            out["plan_reset"] = iso_fmt(reset_date) or reset_date
            start_dt = previous_month(reset_date)
            if start_dt:
                out["plan_start"] = start_dt.strftime("%Y-%m-%d")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 backend/test_status_dates.py`
Expected: `Ran 9 tests ... OK`

Run: `python3 backend/test_onboarding.py`
Expected: OK (regression check — `claude_extra`/`provider_extra` are exercised there).

- [ ] **Step 5: Commit**

```bash
git add backend/status.py backend/test_status_dates.py
git commit -m "feat(status): Derive Claude plan reset and Copilot plan start

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Heartbeat backend — last success export + `--account` flag

**Files:**
- Modify: `backend/status.py:92-116` (`heartbeat_meta`)
- Modify: `backend/heartbeat.py:191` (`run_once` signature + account filter)
- Modify: `backend/pool.py:17` (docstring) and `backend/pool.py:295-297` (heartbeat command)
- Test: `backend/test_status_dates.py` (append one heartbeat_meta test)

**Interfaces:**
- Consumes: `store.log_event(conn, account_id, kind, success, message)` and the `refresh_log` table (existing).
- Produces: status.json heartbeat accounts gain `"heartbeat_last_success": "YYYY-MM-DD HH:MM" | None`; CLI `pool.py heartbeat --account <id>` runs one account; `heartbeat.run_once(conn, account_id=None)`. Task 7 (Swift) decodes `heartbeat_last_success` and shells `heartbeat --account <id>`.

- [ ] **Step 1: Write the failing test**

Append to `backend/test_status_dates.py` (before `if __name__`):

```python
import store  # noqa: E402


class HeartbeatMetaTests(unittest.TestCase):
    def test_last_success_survives_later_failure(self):
        conn = store.connect()
        acct_id = store.upsert_account(conn, "claude", "hb@example.com", "hb", None, None)
        store.log_event(conn, acct_id, "heartbeat", True, "hi")
        store.log_event(conn, acct_id, "heartbeat", False, "boom")
        meta = status.heartbeat_meta(conn, acct_id)
        self.assertEqual(meta["heartbeat_status"], "fail")
        self.assertIsNotNone(meta["heartbeat_last_success"])
        self.assertIsNotNone(meta["heartbeat_last"])

    def test_no_rows_reports_none(self):
        conn = store.connect()
        acct_id = store.upsert_account(conn, "codex", "hb2@example.com", "hb2", None, None)
        meta = status.heartbeat_meta(conn, acct_id)
        self.assertIsNone(meta["heartbeat_last_success"])
```

(`store.upsert_account(conn, provider, email, label=None, plan=None, account_id=None)` and `store.log_event(conn, account_id, kind, success, message="")` are the verified signatures in `backend/store.py:165,322`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 backend/test_status_dates.py`
Expected: FAIL with `KeyError: 'heartbeat_last_success'`

- [ ] **Step 3: Implement**

In `backend/status.py` `heartbeat_meta`, add `heartbeat_last_success` to both return dicts:

```python
    if not latest and not latest_success:
        return {
            "heartbeat_status": "unknown",
            "heartbeat_last": None,
            "heartbeat_last_success": None,
            "heartbeat_next": "due now",
            "heartbeat_message": None,
            "heartbeat_next_ts": time.time(),
        }
    next_ts = (latest_success["ts"] + HEARTBEAT_INTERVAL_S) if latest_success else time.time()
    return {
        "heartbeat_status": "success" if latest and latest["success"] else "fail",
        "heartbeat_last": ts_fmt(latest["ts"]) if latest else None,
        "heartbeat_last_success": ts_fmt(latest_success["ts"]) if latest_success else None,
        "heartbeat_next": ts_fmt(next_ts),
        "heartbeat_message": latest["message"] if latest else None,
        "heartbeat_next_ts": next_ts,
    }
```

In `backend/heartbeat.py`, change `run_once` to accept an account filter:

```python
def run_once(conn, account_id: int | None = None) -> int:
```

and extend the accounts list comprehension:

```python
        accounts = [
            a for a in store.list_accounts(conn)
            if a["provider"] in PROVIDERS and not a.get("disabled")
            and (account_id is None or a["id"] == account_id)
        ]
```

In `backend/pool.py`, update the docstring line 17 to:

```
  pool.py heartbeat [--account <id>]   One keep-alive cycle (codex/claude/agy → "hi")
```

and replace the heartbeat command handler:

```python
    if cmd == "heartbeat":
        import heartbeat
        args = argv[1:]
        account_id = None
        if "--account" in args:
            i = args.index("--account")
            if i + 1 >= len(args):
                print("usage: pool.py heartbeat [--account <id>]")
                return 1
            account_id = int(args[i + 1])
        return heartbeat.run_once(DB, account_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 backend/test_status_dates.py && python3 backend/test_agy_usage.py && python3 backend/test_onboarding.py`
Expected: all OK.

Quick CLI smoke (hits real providers — one account only, cheap):
Run: `python3 backend/pool.py heartbeat --account 19`
Expected: `Heartbeat 1 accounts ...` then `✓ antigravity ...` (or a logged failure — either proves the filter works).

- [ ] **Step 5: Commit**

```bash
git add backend/status.py backend/heartbeat.py backend/pool.py backend/test_status_dates.py
git commit -m "feat(heartbeat): Export last success and add --account flag

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Swift — uniform 5-row Status section

**Files:**
- Modify: `app/TokenStatusBar.swift` — L10n tables (~lines 603, 716, 822, 928), `statusGroup` (~line 1597), `buildCodexSubmenu` (~line 1525), `buildClaudeSubmenu` (~line 1574)

**Interfaces:**
- Consumes: existing `planText`, `planStartText`, `planResetText`, `infoItem`, `groupHeaderItem`, `L10n.label`, `t(_:)`.
- Produces: `statusGroup(_:acct:width:extra:)` renders exactly Plan / Plan started / Plan resets / [extra rows] / Token expires / Last poll, with localized `n/a` for missing values. All six provider builders use it.

- [ ] **Step 1: Add the `na` L10n key to all four language tables**

In each table's "── Messages ──" section (en ~line 665, ko ~line 771, zh ~line 877, ja ~line 983 — locate by searching for `"no_details"`), add one line:

```swift
            // en table:
            "na": "n/a",
            // ko table:
            "na": "정보 없음",
            // zh table:
            "na": "暂无",
            // ja table:
            "na": "情報なし",
```

- [ ] **Step 2: Rewrite `statusGroup` to always render 5 rows (+ extras)**

Replace the whole `statusGroup` function (lines 1597-1614):

```swift
    private func statusGroup(_ submenu: NSMenu, acct: Account, width: CGFloat, extra: [(String, String)] = []) {
        submenu.addItem(groupHeaderItem(t("status"), width: width))
        let na = t("na")
        if let line = planText(acct) {
            submenu.addItem(infoItem(line, width: width))
        } else {
            submenu.addItem(infoItem(L10n.label("plan", na), width: width))
        }
        submenu.addItem(infoItem(planStartText(acct) ?? L10n.label("plan_started", na), width: width))
        submenu.addItem(infoItem(planResetText(acct) ?? L10n.label("plan_resets", na), width: width))
        for (key, value) in extra {
            submenu.addItem(infoItem(L10n.label(key, value), width: width))
        }
        submenu.addItem(infoItem(L10n.label("token_expires", acct.token_expires ?? na), width: width))
        submenu.addItem(infoItem(L10n.label("last_poll", acct.last_poll ?? na), width: width))
    }
```

(Note the header changes from the hardcoded `"Status"` to `t("status")` — the key already exists in all tables.)

- [ ] **Step 3: Switch Codex and Claude builders to the shared group**

Replace the "─── Status group ───" block in `buildCodexSubmenu` (lines 1526-1548, everything before "─── Limit session group ───") with:

```swift
        // ─── Status group ───
        var extra: [(String, String)] = []
        if let created = acct.account_created, !created.isEmpty {
            extra.append(("account_created", created))
        }
        if let history = acct.payment_history, !history.isEmpty {
            extra.append(("payment_history", history))
        }
        statusGroup(submenu, acct: acct, width: width, extra: extra)
```

Replace the "─── Status group ───" block in `buildClaudeSubmenu` (lines 1575-1591) with:

```swift
        // ─── Status group ───
        statusGroup(submenu, acct: acct, width: width)
```

- [ ] **Step 4: Compile**

Run: `bash build.sh`
Expected: "Building TokenStatusBar..." then bundling with no swiftc errors.

- [ ] **Step 5: Commit**

```bash
git add app/TokenStatusBar.swift
git commit -m "feat(status-bar): Uniform 5-row status section for all agents

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Swift — Antigravity 5h/weekly rendering

**Files:**
- Modify: `app/TokenStatusBar.swift` — `Account` struct (~line 100), new `UsageWindow` struct (after `ResetCredit`, ~line 109), L10n tables, `buildAntigravitySubmenu` (~line 1668)

**Interfaces:**
- Consumes: status.json `usage_windows` array from Task 2 (`group`: "gemini"|"other", `window`: "5h"|"weekly", `used_pct`: Double, `reset`: String?); existing `limitSessionGroup`, `L10n.usedLine`.
- Produces: Antigravity submenu "Limit session" group with up to 4 window rows; falls back to `limitSessionGroup(primaryLabel: "tier_usage")` when `usage_windows` is absent.

- [ ] **Step 1: Add the model types**

In the `Account` struct, after `var tier_override: String?` (line 100), add:

```swift
    var heartbeat_last_success: String?
    var usage_windows: [UsageWindow]?
```

(`heartbeat_last_success` is added here so the struct changes land once; Task 7 renders it.)

After the `ResetCredit` struct (line 109), add:

```swift
struct UsageWindow: Codable {
    var group: String?
    var window: String?
    var used_pct: Double?
    var reset: String?
}
```

- [ ] **Step 2: Add L10n keys to all four tables**

In each table's "── Limit names ──" section (search for `"tier_usage"`), add:

```swift
            // en:
            "ag_group_gemini": "Gemini models",
            "ag_group_other": "Claude & GPT models",
            // ko:
            "ag_group_gemini": "Gemini 모델",
            "ag_group_other": "Claude & GPT 모델",
            // zh:
            "ag_group_gemini": "Gemini 模型",
            "ag_group_other": "Claude & GPT 模型",
            // ja:
            "ag_group_gemini": "Gemini モデル",
            "ag_group_other": "Claude & GPT モデル",
```

- [ ] **Step 3: Render the windows**

Replace `buildAntigravitySubmenu` (lines 1668-1673):

```swift
    func buildAntigravitySubmenu(_ submenu: NSMenu, acct: Account, width: CGFloat) {
        statusGroup(submenu, acct: acct, width: width)

        // ─── Limit session group ───
        if let windows = acct.usage_windows, !windows.isEmpty {
            submenu.addItem(separatorRow(width: width))
            submenu.addItem(groupHeaderItem(t("limit_session"), width: width))
            for w in windows {
                let groupLabel = w.group == "gemini" ? t("ag_group_gemini") : t("ag_group_other")
                let windowKey = w.window == "weekly" ? "weekly_limit" : "5h_limit"
                let pct = w.used_pct ?? 0
                let line = "\(groupLabel) · " + L10n.usedLine(windowKey, String(format: "%.1f", pct), reset: w.reset)
                submenu.addItem(infoItem(line, width: width, accentPercent: true, warnPercent: pct > 80))
            }
        } else {
            limitSessionGroup(submenu, acct: acct, width: width, primaryLabel: "tier_usage")
        }
    }
```

- [ ] **Step 4: Compile**

Run: `bash build.sh`
Expected: no swiftc errors.

- [ ] **Step 5: Commit**

```bash
git add app/TokenStatusBar.swift
git commit -m "feat(antigravity): Show 5h/weekly windows in submenu

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Swift — heartbeat last success + Run Heartbeat Now

**Files:**
- Modify: `app/TokenStatusBar.swift` — L10n tables, `StatusLoader` (add `runHeartbeat`, after `runPoll` ~line 219), `heartbeatItem` (~line 1211), `addHeartbeatStatus` (~line 1236)

**Interfaces:**
- Consumes: `heartbeat_last_success` field (Task 6 struct change), `pool.py heartbeat [--account <id>]` (Task 4), existing `poolProcess`, `actionItem`, `infoItem`.
- Produces: menu shows heartbeat status/last/last-success/next per account; "Run Heartbeat Now" at the bottom of the global Heartbeat submenu (all accounts) and inside each heartbeat-capable agent submenu (single account).

- [ ] **Step 1: Add L10n keys to all four tables**

Next to the existing `"heartbeat_last"` key in each table:

```swift
            // en:
            "heartbeat_last_success": "Last success",
            "run_heartbeat_now": "Run Heartbeat Now",
            // ko:
            "heartbeat_last_success": "마지막 성공",
            "run_heartbeat_now": "지금 하트비트 실행",
            // zh:
            "heartbeat_last_success": "上次成功",
            "run_heartbeat_now": "立即运行 Heartbeat",
            // ja:
            "heartbeat_last_success": "最終成功",
            "run_heartbeat_now": "今すぐハートビート実行",
```

- [ ] **Step 2: Add `runHeartbeat` to `StatusLoader`**

After `runPoll()` (line 219), add:

```swift
    func runHeartbeat(accountId: Int? = nil) {
        DispatchQueue.global(qos: .userInitiated).async {
            var args = ["heartbeat"]
            if let id = accountId {
                args += ["--account", "\(id)"]
            }
            let task = self.poolProcess(args)
            try? task.run()
            task.waitUntilExit()
            // heartbeat writes refresh_log only; export so the menu sees it.
            let export = self.poolProcess(["export-status"])
            try? export.run()
            export.waitUntilExit()
            DispatchQueue.main.async {
                self.reload()
            }
        }
    }
```

- [ ] **Step 3: Render last success + global action in `heartbeatItem`**

Inside the per-account loop, after the `if let last = acct.heartbeat_last { ... }` block (line 1222), add:

```swift
            if let lastOk = acct.heartbeat_last_success {
                submenu.addItem(infoItem("\(t("heartbeat_last_success")): \(lastOk)", width: width))
            }
```

After the loop (after line 1227's closing brace, before `let failed = ...`), add:

```swift
        submenu.addItem(actionItem(t("run_heartbeat_now"), width: width) { [weak self] in
            self?.loader.runHeartbeat()
        })
```

- [ ] **Step 4: Render last success + per-agent action in `addHeartbeatStatus`**

Replace the whole function (lines 1236-1246):

```swift
    private func addHeartbeatStatus(_ submenu: NSMenu, acct: Account, width: CGFloat) {
        guard acct.heartbeat_status != nil || acct.heartbeat_next != nil || acct.heartbeat_last != nil else { return }
        submenu.addItem(groupHeaderItem(t("heartbeat"), width: width))
        submenu.addItem(infoItem(heartbeatLine(status: acct.heartbeat_status, next: acct.heartbeat_next), width: width))
        if let last = acct.heartbeat_last {
            submenu.addItem(infoItem("\(t("heartbeat_last")): \(last)", width: width))
        }
        if let lastOk = acct.heartbeat_last_success {
            submenu.addItem(infoItem("\(t("heartbeat_last_success")): \(lastOk)", width: width))
        }
        if let msg = acct.heartbeat_message, !msg.isEmpty {
            submenu.addItem(infoItem(msg, width: width))
        }
        submenu.addItem(actionItem(t("run_heartbeat_now"), width: width) { [weak self] in
            self?.loader.runHeartbeat(accountId: acct.id)
        })
    }
```

- [ ] **Step 5: Compile and commit**

Run: `bash build.sh`
Expected: no swiftc errors.

```bash
git add app/TokenStatusBar.swift
git commit -m "feat(heartbeat): Show last success and manual run action

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: End-to-end verification, poller restart, build + relaunch

**Files:**
- No source changes expected (fixes go back to the owning task's files).

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Run the full backend test suite**

Run: `python3 backend/test_agy_usage.py && python3 backend/test_status_dates.py && python3 backend/test_onboarding.py`
Expected: all OK.

- [ ] **Step 2: Restart the poller with the new code**

The LaunchAgent `com.tonye.agentpool-poller` respawns the poller automatically after a kill. Do NOT start one by hand.

Run: `pkill -f "pool.py poll-loop" || true; pkill -f "pool.py heartbeat-loop" || true; sleep 5; pgrep -fl "pool.py"`
Expected: fresh `pool.py poll-loop` (and heartbeat-loop if configured) processes with new PIDs.

- [ ] **Step 3: Produce a fresh status.json and inspect it**

Run: `python3 backend/pool.py poll && python3 backend/pool.py export-status` (the antigravity poll now takes ~35s longer — that's the agy pty call)

Then: `python3 -c "import json; d=json.load(open('secrets/status.json')); ag=[a for a in d['accounts'] if a['provider']=='antigravity'][0]; cl=[a for a in d['accounts'] if a['provider']=='claude'][0]; print('windows:', json.dumps(ag.get('usage_windows'), indent=1)); print('claude plan_reset:', cl.get('plan_reset')); print('hb last success:', cl.get('heartbeat_last_success'))"`

Expected: `usage_windows` has 4 entries; Claude has a `plan_reset` date on its subscription anniversary day; `heartbeat_last_success` is a timestamp (or None if no heartbeat has succeeded yet — then run `python3 backend/pool.py heartbeat` once and re-export).

- [ ] **Step 4: Build the .dmg and relaunch the app (AGENTS.md requirement)**

Run: `bash build.sh --dmg`
Expected: `.app` installed to /Applications and `build/TokenStatusBar.dmg` created without error. Do not proceed to any PR if this fails.

Run: `osascript -e 'tell application "TokenStatusBar" to quit'; sleep 2; open /Applications/TokenStatusBar.app`

- [ ] **Step 5: Visual verification in the menu bar**

Check each of these by opening the menu:
1. Every agent submenu's Status section shows exactly: Plan, Plan started, Plan resets, Token expires, Last poll — `n/a` (or the localized equivalent) where unknown; Antigravity shows `n/a` for both plan dates; Codex additionally shows account created / previous payments.
2. Antigravity submenu → Limit session shows 4 rows: Gemini models 5h + weekly, Claude & GPT models 5h + weekly, with used % and reset times.
3. Heartbeat submenu (top of menu): per-account status, Last, Last success, Next; "Run Heartbeat Now" at the bottom. Click it → after the run finishes, Last/Last success update.
4. A codex/claude/antigravity agent submenu shows its own Heartbeat group with "Run Heartbeat Now"; click it and verify only that account's heartbeat timestamp changes.
5. Switch language (e.g. 한국어) and confirm the new labels translate.

- [ ] **Step 6: Final commit (if any fixes were made) and wrap up**

```bash
git status
```

If fixes were needed during verification, commit them in the style of the owning task. Then use the superpowers:finishing-a-development-branch skill flow (note: work is on `main` with pre-existing dirty files — do not commit unrelated dirty changes).
