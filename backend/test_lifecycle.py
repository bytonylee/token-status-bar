#!/usr/bin/env python3
"""Tests for lifecycle transition events: detection, store, daemon wiring."""
from __future__ import annotations
import copy, json, os, sys, tempfile, unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tsb-test-")
os.environ["AGENT_POOL_DB"] = os.path.join(_TMP, "pool.db")
os.environ["AGENT_POOL_STATUS_JSON"] = os.path.join(_TMP, "status.json")
os.environ["AGENT_POOL_HISTORY_DIR"] = os.path.join(_TMP, "history")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lifecycle  # noqa: E402
import store  # noqa: E402
import status  # noqa: E402
import poller  # noqa: E402

BASE = 1_000_000.0


def _w(kind="5h", label=None, phase="live", used=50.0, reset=BASE + 3600):
    return {"kind": kind, "label": label, "phase": phase,
            "used_pct": used,
            "used_pct_effective": 0.0 if phase == "reset" else used,
            "reset_at_epoch": reset, "severity": "normal", "stale": False}


def _acct(aid=1, sub="paid", quota="ok", renews=None, windows=None):
    return {"id": aid, "provider": "codex", "email": f"a{aid}@example.com",
            "windows": list(windows) if windows is not None else [],
            "state": {"auth": "ok", "subscription": sub,
                      "sub_renews_at": renews, "sub_expires_at": None,
                      "quota": quota, "usable": True, "binding_window": None}}


def _payload(*accounts):
    return {"generated_at": "2026-07-22T17:00:00",
            "account_count": len(accounts), "accounts": list(accounts)}


class WindowResetTests(unittest.TestCase):
    def test_phase_flip_live_to_reset(self):
        prev = _payload(_acct(windows=[_w(used=87.0)]))
        cur = _payload(_acct(windows=[_w(phase="reset", used=87.0,
                                          reset=BASE + 3600 + 18000)]))
        out = lifecycle.detect_transitions(prev, cur)
        self.assertEqual(len(out), 1)
        ev = out[0]
        self.assertEqual(ev["account_id"], 1)
        self.assertEqual(ev["event"], "window_reset")
        self.assertEqual(ev["detail"]["kind"], "5h")
        self.assertIsNone(ev["detail"]["label"])
        self.assertEqual(ev["detail"]["old_used_pct"], 87.0)

    def test_reset_at_rolled_forward_while_live(self):
        # Provider re-anchored the window between polls: still "live" but
        # reset_at_epoch jumped past prev's value → the old window closed.
        prev = _payload(_acct(windows=[_w(used=91.0, reset=BASE + 300)]))
        cur = _payload(_acct(windows=[_w(used=2.0, reset=BASE + 300 + 18000)]))
        out = lifecycle.detect_transitions(prev, cur)
        self.assertEqual([e["event"] for e in out], ["window_reset"])
        self.assertEqual(out[0]["detail"]["old_used_pct"], 91.0)

    def test_windows_matched_by_kind_and_label(self):
        prev = _payload(_acct(windows=[_w(kind="monthly", label="premium"),
                                       _w(kind="monthly", label="chat", used=10.0)]))
        cur = _payload(_acct(windows=[_w(kind="monthly", label="premium"),
                                      _w(kind="monthly", label="chat", used=10.0,
                                         phase="reset")]))
        out = lifecycle.detect_transitions(prev, cur)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["detail"], {"kind": "monthly", "label": "chat",
                                            "old_used_pct": 10.0})

    def test_no_event_when_prev_already_reset(self):
        prev = _payload(_acct(windows=[_w(phase="reset")]))
        cur = _payload(_acct(windows=[_w(phase="reset",
                                          reset=BASE + 3600 + 18000)]))
        self.assertEqual(lifecycle.detect_transitions(prev, cur), [])

    def test_no_event_for_brand_new_window(self):
        prev = _payload(_acct(windows=[]))
        cur = _payload(_acct(windows=[_w(phase="reset")]))
        self.assertEqual(lifecycle.detect_transitions(prev, cur), [])


class SubscriptionTests(unittest.TestCase):
    def test_sub_paid_from_each_non_paid_state(self):
        for frm in ("unknown", "free", "expired"):
            with self.subTest(frm=frm):
                out = lifecycle.detect_transitions(_payload(_acct(sub=frm)),
                                                   _payload(_acct(sub="paid")))
                self.assertEqual([e["event"] for e in out], ["sub_paid"])
                self.assertEqual(out[0]["detail"], {"from": frm, "to": "paid"})

    def test_sub_expired(self):
        out = lifecycle.detect_transitions(_payload(_acct(sub="paid")),
                                           _payload(_acct(sub="expired")))
        self.assertEqual([e["event"] for e in out], ["sub_expired"])

    def test_sub_renewed_when_anniversary_passes(self):
        prev = _payload(_acct(sub="paid", renews="2026-08-01T00:00:00+00:00"))
        cur = _payload(_acct(sub="paid", renews="2026-09-01T00:00:00+00:00"))
        out = lifecycle.detect_transitions(prev, cur)
        self.assertEqual([e["event"] for e in out], ["sub_renewed"])
        self.assertEqual(out[0]["detail"]["prev_renews_at"],
                         "2026-08-01T00:00:00+00:00")

    def test_renews_soon_counts_as_paid(self):
        # account_state flips paid → renews_soon near the anniversary; that
        # must not read as a subscription transition, and the roll back to
        # "paid" with a later renews_at is a renewal.
        prev = _payload(_acct(sub="paid", renews="2026-08-01T00:00:00+00:00"))
        soon = _payload(_acct(sub="renews_soon", renews="2026-08-01T00:00:00+00:00"))
        self.assertEqual(lifecycle.detect_transitions(prev, soon), [])
        cur = _payload(_acct(sub="paid", renews="2026-09-01T00:00:00+00:00"))
        out = lifecycle.detect_transitions(soon, cur)
        self.assertEqual([e["event"] for e in out], ["sub_renewed"])

    def test_no_event_paid_to_paid_same_renews(self):
        p = _payload(_acct(sub="paid", renews="2026-08-01T00:00:00+00:00"))
        self.assertEqual(lifecycle.detect_transitions(p, copy.deepcopy(p)), [])

    def test_expired_to_free_emits_nothing(self):
        self.assertEqual(
            lifecycle.detect_transitions(_payload(_acct(sub="expired")),
                                         _payload(_acct(sub="free"))), [])


class QuotaTests(unittest.TestCase):
    def test_quota_exhausted_from_any_other_value(self):
        for frm in ("ok", "warning", "unknown"):
            with self.subTest(frm=frm):
                out = lifecycle.detect_transitions(
                    _payload(_acct(quota=frm)),
                    _payload(_acct(quota="exhausted")))
                self.assertEqual([e["event"] for e in out], ["quota_exhausted"])
                self.assertEqual(out[0]["detail"], {"from": frm})

    def test_quota_recovered_to_ok_or_warning(self):
        for to in ("ok", "warning"):
            with self.subTest(to=to):
                out = lifecycle.detect_transitions(
                    _payload(_acct(quota="exhausted")),
                    _payload(_acct(quota=to)))
                self.assertEqual([e["event"] for e in out], ["quota_recovered"])
                self.assertEqual(out[0]["detail"], {"to": to})

    def test_exhausted_to_unknown_is_not_recovery(self):
        self.assertEqual(
            lifecycle.detect_transitions(_payload(_acct(quota="exhausted")),
                                         _payload(_acct(quota="unknown"))), [])


class GeneralTests(unittest.TestCase):
    def test_first_run_prev_none(self):
        self.assertEqual(
            lifecycle.detect_transitions(None, _payload(_acct(quota="exhausted"))),
            [])

    def test_idempotent_same_payload_twice(self):
        p = _payload(
            _acct(1, sub="paid", quota="warning",
                  renews="2026-08-01T00:00:00+00:00",
                  windows=[_w(used=85.0), _w(kind="weekly", phase="reset")]),
            _acct(2, sub="expired", quota="exhausted"))
        self.assertEqual(lifecycle.detect_transitions(p, copy.deepcopy(p)), [])

    def test_new_account_emits_nothing(self):
        prev = _payload(_acct(1))
        cur = _payload(_acct(1), _acct(2, quota="exhausted"))
        self.assertEqual(lifecycle.detect_transitions(prev, cur), [])

    def test_multiple_accounts_and_events(self):
        prev = _payload(_acct(1, quota="ok", windows=[_w(used=99.0)]),
                        _acct(2, sub="free"))
        cur = _payload(_acct(1, quota="exhausted",
                             windows=[_w(phase="reset", used=99.0)]),
                       _acct(2, sub="paid"))
        out = lifecycle.detect_transitions(prev, cur)
        self.assertEqual(sorted((e["account_id"], e["event"]) for e in out),
                         [(1, "quota_exhausted"), (1, "window_reset"),
                          (2, "sub_paid")])


class StoreLifecycleEventsTests(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect()
        self.acct = store.upsert_account(self.conn, "codex",
                                         f"lc-{id(self)}@example.com", "lc")

    def tearDown(self):
        self.conn.close()

    def test_save_list_roundtrip_newest_first(self):
        store.save_lifecycle_event(self.conn, BASE, self.acct, "sub_paid",
                                   {"from": "free", "to": "paid"})
        store.save_lifecycle_event(self.conn, BASE + 60, self.acct,
                                   "quota_exhausted", {"from": "ok"})
        rows = store.list_lifecycle_events(self.conn, account_id=self.acct)
        self.assertEqual([r["event"] for r in rows],
                         ["quota_exhausted", "sub_paid"])
        self.assertEqual(json.loads(rows[1]["detail"]),
                         {"from": "free", "to": "paid"})
        self.assertEqual(rows[0]["ts"], BASE + 60)

    def test_filters_and_limit(self):
        other = store.upsert_account(self.conn, "claude",
                                     f"lc2-{id(self)}@example.com", "lc2")
        for i in range(3):
            store.save_lifecycle_event(self.conn, BASE + i, self.acct,
                                       "window_reset", None)
        store.save_lifecycle_event(self.conn, BASE + 10, other, "sub_expired", None)
        self.assertEqual(len(store.list_lifecycle_events(self.conn,
                                                         account_id=self.acct)), 3)
        self.assertEqual([r["event"] for r in
                          store.list_lifecycle_events(self.conn, account_id=other)],
                         ["sub_expired"])
        self.assertEqual(len(store.list_lifecycle_events(self.conn,
                                                         account_id=self.acct,
                                                         since=BASE + 1)), 2)
        self.assertEqual(len(store.list_lifecycle_events(self.conn,
                                                         account_id=self.acct,
                                                         limit=1)), 1)

    def test_null_account_and_freeform_event(self):
        # §3.2: the swap engine writes "account_swapped" rows later — the
        # event column is free-form and account_id may be NULL.
        rid = store.save_lifecycle_event(self.conn, BASE, None,
                                         "account_swapped", "codex → #2")
        rows = [r for r in store.list_lifecycle_events(self.conn) if r["id"] == rid]
        self.assertEqual(rows[0]["account_id"], None)
        self.assertEqual(rows[0]["detail"], "codex → #2")


class DaemonWiringTests(unittest.TestCase):
    """poller.export_status: write + diff against the in-memory prev payload."""

    def setUp(self):
        self.conn = store.connect()
        self.acct = store.upsert_account(self.conn, "codex",
                                         f"dw-{id(self)}@example.com", "dw")
        poller._prev_export_payload = None

    def tearDown(self):
        poller._prev_export_payload = None
        self.conn.close()

    def test_export_status_persists_transitions(self):
        p1 = _payload(_acct(self.acct, quota="ok"))
        p2 = _payload(_acct(self.acct, quota="exhausted"))
        with mock.patch.object(status, "build_payload", side_effect=[p1, p2]), \
             mock.patch.object(status, "write_status") as ws:
            poller.export_status(self.conn)  # first export: no baseline
            self.assertEqual(store.list_lifecycle_events(self.conn,
                                                         account_id=self.acct), [])
            poller.export_status(self.conn)
        rows = store.list_lifecycle_events(self.conn, account_id=self.acct)
        self.assertEqual([r["event"] for r in rows], ["quota_exhausted"])
        self.assertEqual(json.loads(rows[0]["detail"]), {"from": "ok"})
        self.assertEqual(ws.call_count, 2)
        self.assertIs(poller._prev_export_payload, p2)

    def test_event_persistence_failure_does_not_break_export(self):
        p1 = _payload(_acct(self.acct, quota="ok"))
        p2 = _payload(_acct(self.acct, quota="exhausted"))
        with mock.patch.object(status, "build_payload", side_effect=[p1, p2]), \
             mock.patch.object(status, "write_status"), \
             mock.patch.object(store, "save_lifecycle_event",
                               side_effect=RuntimeError("db locked")):
            poller.export_status(self.conn)
            poller.export_status(self.conn)  # must not raise
        self.assertIs(poller._prev_export_payload, p2)

    def test_cmd_export_end_to_end_writes_payload(self):
        # The refactor (build_payload + write_status) must keep cmd_export's
        # on-disk contract for the Swift app.
        self.assertEqual(status.cmd_export(self.conn), 0)
        # status.STATUS_JSON was resolved at first import (possibly by an
        # earlier test module's env) — read the path the module actually uses.
        with open(status.STATUS_JSON) as f:
            data = json.load(f)
        self.assertIn("generated_at", data)
        self.assertIn(self.acct, [a["id"] for a in data["accounts"]])
        self.assertEqual(data["account_count"], len(data["accounts"]))


if __name__ == "__main__":
    unittest.main()
