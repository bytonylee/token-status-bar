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


if __name__ == "__main__":
    unittest.main(verbosity=2)
