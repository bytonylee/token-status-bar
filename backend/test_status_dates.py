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

    def test_month_end_does_not_drift(self):
        # Jan 31 start: February clamps to 28, but March must return to 31.
        now = datetime.datetime(2026, 3, 1, tzinfo=UTC)
        got = status.next_monthly_anniversary("2026-01-31T00:00:00Z", now=now)
        self.assertEqual(got, datetime.datetime(2026, 3, 31, tzinfo=UTC))

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
