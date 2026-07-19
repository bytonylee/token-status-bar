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
