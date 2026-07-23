"""Tests for poll_copilot token-refresh failure handling."""
from __future__ import annotations
import os, sys, tempfile, unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tsb-test-")
os.environ["AGENT_POOL_DB"] = os.path.join(_TMP, "pool.db")
os.environ["AGENT_POOL_STATUS_JSON"] = os.path.join(_TMP, "status.json")
os.environ["AGENT_POOL_HISTORY_DIR"] = os.path.join(_TMP, "history")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import poller  # noqa: E402
import store  # noqa: E402

ENDED_BODY = {"error_details": {"message": (
    "Thank you for using GitHub Copilot. Your subscription has ended. "
    "You are currently logged in as someone.")}}
TOKEN = {"raw_json": None, "access_token": "gho_test"}


class CopilotTokenRefreshFailureTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect()
        self.acct_id = store.upsert_account(self.conn, "copilot", "someone", "someone")
        self.account = {"id": self.acct_id, "provider": "copilot"}
        store.save_snapshot(self.conn, self.acct_id,
                            {"status": "active", "primary_used_pct": 0.0})

    def tearDown(self):
        self.conn.execute("DELETE FROM limit_snapshots")
        self.conn.execute("DELETE FROM accounts")
        self.conn.commit()
        self.conn.close()

    def test_subscription_ended_403_writes_error_not_hold(self):
        # A "subscription has ended" 403 is permanent: the poller must record
        # an error snapshot instead of holding the stale active one forever.
        with mock.patch.object(poller, "_get", return_value=(403, ENDED_BODY, {})), \
             mock.patch.object(poller.time, "sleep"):
            poller.poll_copilot(self.conn, self.account, TOKEN)
        snap = store.latest_snapshot(self.conn, self.acct_id)
        self.assertEqual(snap["status"], "error")
        self.assertIn("subscription has ended", snap["status_message"].lower())

    def test_transient_403_holds_last_active_snapshot(self):
        # Any other 403 stays transient: hold the prior active snapshot.
        with mock.patch.object(poller, "_get",
                               return_value=(403, {"message": "Resource not accessible"}, {})), \
             mock.patch.object(poller.time, "sleep"):
            poller.poll_copilot(self.conn, self.account, TOKEN)
        snap = store.latest_snapshot(self.conn, self.acct_id)
        self.assertEqual(snap["status"], "active")


if __name__ == "__main__":
    unittest.main()
