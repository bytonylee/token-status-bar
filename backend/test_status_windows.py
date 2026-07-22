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

    def test_past_reset_shows_effective_zero(self):
        # A window whose reset passed has reset to 0% — the headline shows the
        # effective value (refresh_windows post-pass), not the stale used_pct.
        now = time.time()
        items = [self._item(1, [{"kind": "5h", "label": None, "used_pct": 99.0,
                                 "reset_at_epoch": now - 10, "severity": "normal"}])]
        h = status.select_headline(items)
        self.assertEqual(h["used_pct"], 0.0)
        self.assertGreater(h["reset_at_epoch"], now)  # rolled forward one 5h step

    def test_live_window_beats_reset_window(self):
        now = time.time()
        items = [
            self._item(1, [{"kind": "5h", "label": None, "used_pct": 99.0,
                            "reset_at_epoch": now - 10, "severity": "exceeded"}]),
            self._item(2, [{"kind": "weekly", "label": None, "used_pct": 40.0,
                            "reset_at_epoch": now + 100, "severity": "normal"}]),
        ]
        self.assertEqual(status.select_headline(items)["account_id"], 2)

    def test_empty(self):
        self.assertIsNone(status.select_headline([]))


if __name__ == "__main__":
    unittest.main()
