"""Tests for the refresh_windows() post-pass (phase / effective pct / stale)."""
from __future__ import annotations
import os, sys, tempfile, time, unittest

_TMP = tempfile.mkdtemp(prefix="tsb-test-")
os.environ["AGENT_POOL_DB"] = os.path.join(_TMP, "pool.db")
os.environ["AGENT_POOL_STATUS_JSON"] = os.path.join(_TMP, "status.json")
os.environ["AGENT_POOL_HISTORY_DIR"] = os.path.join(_TMP, "history")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import status  # noqa: E402

NOW = 1_000_000.0
H5 = 5 * 3600
WEEK = 604800


def win(**kw):
    base = {"kind": "5h", "label": None, "used_pct": 50.0,
            "reset_at_epoch": NOW + 100, "severity": "normal",
            "is_active": None, "source": "api", "as_of_epoch": NOW - 60}
    base.update(kw)
    return base


class RefreshWindowsTest(unittest.TestCase):
    def test_live_window_untouched(self):
        w = status.refresh_windows([win()], NOW)[0]
        self.assertEqual(w["phase"], "live")
        self.assertEqual(w["used_pct_effective"], 50.0)
        self.assertEqual(w["reset_at_epoch"], NOW + 100)
        self.assertFalse(w["stale"])

    def test_past_reset_zeroed(self):
        w = status.refresh_windows([win(used_pct=87.0,
                                        reset_at_epoch=NOW - 100)], NOW)[0]
        self.assertEqual(w["phase"], "reset")
        self.assertEqual(w["used_pct_effective"], 0.0)
        self.assertEqual(w["used_pct"], 87.0)  # raw reading preserved

    def test_cadence_roll_forward_single_step(self):
        w = status.refresh_windows([win(reset_at_epoch=NOW - 100)], NOW)[0]
        self.assertEqual(w["reset_at_epoch"], NOW - 100 + H5)
        self.assertFalse(w["stale"])

    def test_cadence_roll_forward_whole_multiples(self):
        # 2.5 cadences in the past → 3 whole steps to clear now.
        reset = NOW - 2.5 * H5
        w = status.refresh_windows([win(reset_at_epoch=reset)], NOW)[0]
        self.assertEqual(w["reset_at_epoch"], reset + 3 * H5)
        self.assertGreater(w["reset_at_epoch"], NOW)

    def test_cadence_roll_forward_exact_multiple_is_strictly_future(self):
        # Exactly one cadence ago: reset + 1 step == now, so take 2 steps.
        reset = NOW - H5
        w = status.refresh_windows([win(reset_at_epoch=reset)], NOW)[0]
        self.assertEqual(w["reset_at_epoch"], NOW + H5)

    def test_weekly_cadence(self):
        reset = NOW - 100
        w = status.refresh_windows([win(kind="weekly",
                                        reset_at_epoch=reset)], NOW)[0]
        self.assertEqual(w["reset_at_epoch"], reset + WEEK)

    def test_unknown_cadence_keeps_reset_and_flags_stale(self):
        w = status.refresh_windows([win(kind="monthly",
                                        reset_at_epoch=NOW - 100)], NOW)[0]
        self.assertEqual(w["phase"], "reset")
        self.assertEqual(w["used_pct_effective"], 0.0)
        self.assertEqual(w["reset_at_epoch"], NOW - 100)
        self.assertTrue(w["stale"])

    def test_as_of_staleness_boundary(self):
        limit = status.STALE_AFTER_S
        stale = status.refresh_windows([win(as_of_epoch=NOW - limit - 1)], NOW)[0]
        fresh = status.refresh_windows([win(as_of_epoch=NOW - limit)], NOW)[0]
        self.assertTrue(stale["stale"])
        self.assertFalse(fresh["stale"])

    def test_missing_as_of_not_stale(self):
        w = status.refresh_windows([win(as_of_epoch=None)], NOW)[0]
        self.assertFalse(w["stale"])

    def test_no_reset_at_is_live(self):
        w = status.refresh_windows([win(reset_at_epoch=None)], NOW)[0]
        self.assertEqual(w["phase"], "live")
        self.assertEqual(w["used_pct_effective"], 50.0)

    def test_idempotent(self):
        ws = [win(reset_at_epoch=NOW - 100, used_pct=87.0),
              win(kind="monthly", reset_at_epoch=NOW - 100),
              win()]
        once = [dict(w) for w in status.refresh_windows(ws, NOW)]
        twice = status.refresh_windows(ws, NOW)
        self.assertEqual(once, twice)

    def test_empty_and_none(self):
        self.assertEqual(status.refresh_windows([], NOW), [])
        self.assertEqual(status.refresh_windows(None, NOW), None)


class HeadlineUsesEffectiveTest(unittest.TestCase):
    def _item(self, aid, windows):
        return {"id": aid, "provider": "codex", "email": f"a{aid}@x.com",
                "windows": windows}

    def test_headline_reads_effective_pct(self):
        now = time.time()
        items = [
            self._item(1, [{"kind": "5h", "used_pct": 99.0,
                            "reset_at_epoch": now - 10, "severity": "normal"}]),
            self._item(2, [{"kind": "weekly", "used_pct": 30.0,
                            "reset_at_epoch": now + 100, "severity": "normal"}]),
        ]
        h = status.select_headline(items)
        self.assertEqual(h["account_id"], 2)  # live 30% beats reset-to-0%
        self.assertEqual(h["used_pct"], 30.0)

    def test_reset_window_severity_neutralized(self):
        # Pre-reset "rate_limited" severity must not outrank live windows.
        now = time.time()
        items = [
            self._item(1, [{"kind": "5h", "used_pct": 100.0,
                            "reset_at_epoch": now - 10,
                            "severity": "rate_limited"}]),
            self._item(2, [{"kind": "5h", "used_pct": 10.0,
                            "reset_at_epoch": now + 100, "severity": "normal"}]),
        ]
        self.assertEqual(status.select_headline(items)["account_id"], 2)


if __name__ == "__main__":
    unittest.main()
