#!/usr/bin/env python3
"""Tests for window history: store accessors, detection, archiving, exports."""
from __future__ import annotations
import contextlib, csv, datetime, io, json, os, sys, tempfile, unittest

_TMP = tempfile.mkdtemp(prefix="tsb-test-")
os.environ["AGENT_POOL_DB"] = os.path.join(_TMP, "pool.db")
os.environ["AGENT_POOL_STATUS_JSON"] = os.path.join(_TMP, "status.json")
os.environ["AGENT_POOL_HISTORY_DIR"] = os.path.join(_TMP, "history")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402
import window_history  # noqa: E402
import dashboard  # noqa: E402
import poller  # noqa: E402

BASE = 1_000_000.0


def _insert_snap(conn, account_id, ts, **fields):
    """Insert a limit_snapshots row with an explicit ts (save_snapshot stamps now())."""
    cols = ["account_id", "ts"] + list(fields)
    conn.execute(
        f"INSERT INTO limit_snapshots({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
        [account_id, ts] + list(fields.values()),
    )
    conn.commit()


class StoreWindowHistoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect()
        self.acct = store.upsert_account(self.conn, "codex", f"wh-{id(self)}@example.com", "wh")

    def test_save_is_idempotent(self):
        kwargs = dict(window_kind="5h", window_start=900.0, window_end=1000.0,
                      final_used_pct=88.0, final_snapshot_ts=990.0,
                      reset_cause="natural", details={"staleness_s": 10.0})
        self.assertTrue(store.save_window_history(self.conn, self.acct, **kwargs))
        self.assertFalse(store.save_window_history(self.conn, self.acct, **kwargs))
        rows = store.list_window_history(self.conn, account_id=self.acct)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "codex")
        self.assertEqual(json.loads(rows[0]["details"]), {"staleness_s": 10.0})

    def test_latest_successful_skips_error_rows(self):
        _insert_snap(self.conn, self.acct, 100.0, status="active", primary_used_pct=40.0)
        _insert_snap(self.conn, self.acct, 200.0, status="error")
        snap = store.latest_successful_snapshot(self.conn, self.acct)
        self.assertEqual(snap["ts"], 100.0)
        self.assertEqual(store.latest_snapshot(self.conn, self.acct)["ts"], 200.0)

    def test_iter_snapshots_oldest_first(self):
        for ts in (300.0, 100.0, 200.0):
            _insert_snap(self.conn, self.acct, ts, status="active")
        self.assertEqual([s["ts"] for s in store.iter_snapshots(self.conn, self.acct)],
                         [100.0, 200.0, 300.0])

    def test_conflict_window_detection(self):
        store.save_window_history(self.conn, self.acct, "5h", None, 150.0, 90.0, 100.0,
                                  "coupon", None)
        self.assertTrue(store.window_history_conflict(self.conn, self.acct, "5h", 100.0, 200.0))
        # strictly-inside: a row ending exactly at a bound does not conflict
        self.assertFalse(store.window_history_conflict(self.conn, self.acct, "5h", 150.0, 200.0))
        self.assertFalse(store.window_history_conflict(self.conn, self.acct, "weekly", 100.0, 200.0))


def _snap(ts, status="active", **fields):
    d = {"ts": ts, "status": status}
    d.update(fields)
    return d


class DetectTimedWindowTests(unittest.TestCase):
    def test_natural_rollover_codex_5h(self):
        prev = _snap(BASE, primary_used_pct=85.0, primary_reset_at=BASE + 300,
                     primary_window_s=18000)
        new = _snap(BASE + 600, primary_used_pct=3.0,
                    primary_reset_at=BASE + 300 + 18000, primary_window_s=18000)
        out = window_history.detect_closed_windows("codex", prev, new)
        self.assertEqual(len(out), 1)
        cw = out[0]
        self.assertEqual(cw.window_kind, "5h")
        self.assertEqual(cw.reset_cause, "natural")
        self.assertEqual(cw.window_end, BASE + 300)
        self.assertEqual(cw.window_start, BASE + 300 - 18000)
        self.assertEqual(cw.final_used_pct, 85.0)
        self.assertEqual(cw.final_snapshot_ts, BASE)
        self.assertEqual(cw.details["staleness_s"], 300.0)

    def test_sleep_gap_spanning_boundary_is_natural(self):
        prev = _snap(BASE, primary_used_pct=85.0, primary_reset_at=BASE + 300,
                     primary_window_s=18000)
        new = _snap(BASE + 30000, primary_used_pct=3.0,  # 8h gap (laptop asleep)
                    primary_reset_at=BASE + 30000 + 12000, primary_window_s=18000)
        out = window_history.detect_closed_windows("codex", prev, new)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].reset_cause, "natural")
        self.assertEqual(out[0].window_end, BASE + 300)
        self.assertEqual(out[0].details["staleness_s"], 300.0)

    def test_early_reset_with_coupon_evidence(self):
        prev = _snap(BASE, primary_used_pct=90.0, primary_reset_at=BASE + 10000,
                     primary_window_s=18000, banked_resets=2)
        new = _snap(BASE + 300, primary_used_pct=0.5,
                    primary_reset_at=BASE + 300 + 18000, primary_window_s=18000,
                    banked_resets=1)
        out = window_history.detect_closed_windows("codex", prev, new)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].reset_cause, "coupon")
        self.assertEqual(out[0].window_end, BASE + 150)  # midpoint of the two ts

    def test_early_reset_without_evidence_is_provider_reset(self):
        prev = _snap(BASE, primary_used_pct=90.0, primary_reset_at=BASE + 10000,
                     primary_window_s=18000, banked_resets=2)
        new = _snap(BASE + 300, primary_used_pct=0.5,
                    primary_reset_at=BASE + 300 + 18000, primary_window_s=18000,
                    banked_resets=2)
        out = window_history.detect_closed_windows("codex", prev, new)
        self.assertEqual(out[0].reset_cause, "provider_reset")

    def test_coupon_hint_forces_coupon(self):
        prev = _snap(BASE, primary_used_pct=90.0, primary_reset_at=BASE + 10000,
                     primary_window_s=18000)
        new = _snap(BASE + 300, primary_used_pct=0.5,
                    primary_reset_at=BASE + 300 + 18000, primary_window_s=18000)
        out = window_history.detect_closed_windows("codex", prev, new, coupon_hint=True)
        self.assertEqual(out[0].reset_cause, "coupon")

    def test_sliding_reset_idle_account_emits_nothing(self):
        # Idle codex: used_pct pinned ~1.0 and primary_reset_at tracks "now"
        # (ts + window_s) on every poll — a sliding boundary, not a real reset.
        # The forward jump equals the poll gap and usage never drops.
        prev = _snap(BASE, primary_used_pct=1.0, primary_reset_at=BASE + 18000,
                     primary_window_s=18000)
        new = _snap(BASE + 300, primary_used_pct=1.0,
                    primary_reset_at=BASE + 300 + 18000, primary_window_s=18000)
        self.assertEqual(window_history.detect_closed_windows("codex", prev, new), [])

    def test_genuine_midwindow_coupon_still_detected(self):
        # A real coupon redeem mid-window: fixed old boundary, reset_at jumps
        # forward by ~half a window (≫ the poll gap) and usage collapses.
        prev = _snap(BASE, primary_used_pct=80.0, primary_reset_at=BASE + 9000,
                     primary_window_s=18000, banked_resets=2)
        new = _snap(BASE + 300, primary_used_pct=2.0,
                    primary_reset_at=BASE + 300 + 18000, primary_window_s=18000,
                    banked_resets=1)
        out = window_history.detect_closed_windows("codex", prev, new)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].reset_cause, "coupon")

    def test_guard_large_jump_but_usage_climbed_emits_nothing(self):
        # Isolates the usage-fell condition: the boundary jumps far more than
        # the poll gap (jump 9300 >> gap+120), but used_pct rose 5 -> 7, so no
        # window closed. Fails if the usage-fell check is removed from the guard.
        prev = _snap(BASE, primary_used_pct=5.0, primary_reset_at=BASE + 9000,
                     primary_window_s=18000)
        new = _snap(BASE + 300, primary_used_pct=7.0,
                    primary_reset_at=BASE + 300 + 18000, primary_window_s=18000)
        self.assertEqual(window_history.detect_closed_windows("codex", prev, new), [])

    def test_guard_usage_fell_but_jump_within_gap_emits_nothing(self):
        # Isolates the jump condition: usage collapses 80 -> 2 but the boundary
        # only slides by the poll gap (jump 300 <= gap+120), indistinguishable
        # from a rolling reset_at. Fails if the jump check is removed.
        prev = _snap(BASE, primary_used_pct=80.0, primary_reset_at=BASE + 18000,
                     primary_window_s=18000)
        new = _snap(BASE + 300, primary_used_pct=2.0,
                    primary_reset_at=BASE + 300 + 18000, primary_window_s=18000)
        self.assertEqual(window_history.detect_closed_windows("codex", prev, new), [])

    def test_natural_roll_across_long_gap_is_suppressed(self):
        # A single natural roll seen only across a poll gap >= window_s: reset_at
        # slides by exactly the gap (jump == gap), indistinguishable from a
        # rolling boundary, so the guard suppresses it (accepted trade-off).
        prev = _snap(BASE, primary_used_pct=50.0, primary_reset_at=BASE + 18000,
                     primary_window_s=18000)
        new = _snap(BASE + 18000, primary_used_pct=1.0,
                    primary_reset_at=BASE + 18000 + 18000, primary_window_s=18000)
        self.assertEqual(window_history.detect_closed_windows("codex", prev, new), [])

    def test_copilot_monthly_premium_timed_close(self):
        r_old = datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc).timestamp()
        prev = _snap(r_old - 200, primary_used_pct=70.0, primary_reset_at=r_old)
        new = _snap(r_old + 300, primary_used_pct=2.0, primary_reset_at=r_old + 2592000)
        out = window_history.detect_closed_windows("copilot", prev, new)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].window_kind, "monthly_premium")
        self.assertEqual(out[0].reset_cause, "natural")
        self.assertEqual(out[0].window_end, r_old)
        self.assertEqual(out[0].final_used_pct, 70.0)

    def test_antigravity_primary_window_close(self):
        prev = _snap(BASE, primary_used_pct=60.0, primary_reset_at=BASE + 400,
                     primary_window_s=18000)
        new = _snap(BASE + 700, primary_used_pct=3.0,
                    primary_reset_at=BASE + 400 + 18000, primary_window_s=18000)
        out = window_history.detect_closed_windows("antigravity", prev, new)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].window_kind, "5h")
        self.assertEqual(out[0].reset_cause, "natural")
        self.assertEqual(out[0].window_end, BASE + 400)

    def test_nonnumeric_reset_at_skips_window_not_crash(self):
        # A malformed reset_at (an unparsed date string) must skip that window
        # rather than raise inside float() and abort the whole account's sweep.
        prev = _snap(BASE, primary_used_pct=70.0, primary_reset_at="2026-08-01T00:00:00Z")
        new = _snap(BASE + 300, primary_used_pct=2.0, primary_reset_at="2026-09-01T00:00:00Z")
        self.assertEqual(window_history.detect_closed_windows("copilot", prev, new), [])

    def test_error_snapshots_never_participate(self):
        good = _snap(BASE, primary_used_pct=85.0, primary_reset_at=BASE + 300,
                     primary_window_s=18000)
        bad = _snap(BASE + 600, status="error")
        self.assertEqual(window_history.detect_closed_windows("codex", good, bad), [])
        self.assertEqual(window_history.detect_closed_windows("codex", bad, good), [])
        self.assertEqual(window_history.detect_closed_windows("codex", None, good), [])

    def test_tolerance_jitter_is_ignored(self):
        prev = _snap(BASE, primary_used_pct=85.0, primary_reset_at=BASE + 300,
                     primary_window_s=18000)
        new = _snap(BASE + 300, primary_used_pct=86.0,
                    primary_reset_at=BASE + 360, primary_window_s=18000)  # +60s < 120s
        self.assertEqual(window_history.detect_closed_windows("codex", prev, new), [])

    def test_zero_usage_window_is_skipped(self):
        prev = _snap(BASE, primary_used_pct=0.0, primary_reset_at=BASE + 300,
                     primary_window_s=18000)
        new = _snap(BASE + 600, primary_used_pct=0.0,
                    primary_reset_at=BASE + 300 + 18000, primary_window_s=18000)
        self.assertEqual(window_history.detect_closed_windows("codex", prev, new), [])

    def test_claude_fable_window_from_raw_json(self):
        prev = _snap(BASE, raw_json=json.dumps(
            {"fable": {"label": "7d_oi", "used_pct": 42.0, "reset_at": BASE + 400}}))
        new = _snap(BASE + 600, raw_json=json.dumps(
            {"fable": {"label": "7d_oi", "used_pct": 1.0, "reset_at": BASE + 400 + 604800}}))
        out = window_history.detect_closed_windows("claude", prev, new)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].window_kind, "weekly_fable")
        self.assertEqual(out[0].reset_cause, "natural")
        self.assertEqual(out[0].window_end, BASE + 400)
        # fable missing on one side → skipped without crashing
        self.assertEqual(window_history.detect_closed_windows("claude", prev, _snap(BASE + 600)), [])

    def test_xai_monthly_uses_period_fields(self):
        r_old = datetime.datetime(2026, 7, 10, tzinfo=datetime.timezone.utc).timestamp()
        start_old = datetime.datetime(2026, 6, 10, tzinfo=datetime.timezone.utc).timestamp()
        prev = _snap(r_old - 200, monthly_used_pct=60.0,
                     monthly_period_start="2026-06-10T00:00:00Z",
                     monthly_period_end="2026-07-10T00:00:00Z")
        new = _snap(r_old + 300, monthly_used_pct=0.5,
                    monthly_period_start="2026-07-10T00:00:00Z",
                    monthly_period_end="2026-08-10T00:00:00Z")
        out = window_history.detect_closed_windows("xai", prev, new)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].window_kind, "monthly")
        self.assertEqual(out[0].reset_cause, "natural")
        self.assertEqual(out[0].window_end, r_old)
        self.assertEqual(out[0].window_start, start_old)


class DetectDropWindowTests(unittest.TestCase):
    def test_copilot_chat_drop_near_premium_reset_is_natural(self):
        prev = _snap(BASE, secondary_used_pct=45.0, primary_used_pct=10.0,
                     primary_reset_at=BASE + 500)
        new = _snap(BASE + 300, secondary_used_pct=2.0, primary_used_pct=10.0,
                    primary_reset_at=BASE + 500)
        out = window_history.detect_closed_windows("copilot", prev, new)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].window_kind, "monthly_chat")
        self.assertEqual(out[0].reset_cause, "natural")
        self.assertEqual(out[0].window_end, BASE + 300)  # new snapshot ts

    def test_copilot_chat_drop_far_from_boundary_is_unknown(self):
        prev = _snap(BASE, secondary_used_pct=45.0, primary_reset_at=BASE + 100000)
        new = _snap(BASE + 300, secondary_used_pct=2.0, primary_reset_at=BASE + 100000)
        out = window_history.detect_closed_windows("copilot", prev, new)
        self.assertEqual(out[0].reset_cause, "unknown")

    def test_small_drop_does_not_close(self):
        prev = _snap(BASE, secondary_used_pct=45.0)
        new = _snap(BASE + 300, secondary_used_pct=38.0)  # -7 < 10 points
        self.assertEqual(window_history.detect_closed_windows("copilot", prev, new), [])

    def test_devin_daily_drop_near_local_midnight_is_natural(self):
        mid = datetime.datetime(2026, 7, 10).timestamp()  # local midnight
        prev = _snap(mid - 200, daily_quota_remaining_percent=30.0,
                     weekly_quota_remaining_percent=50.0)
        new = _snap(mid + 200, daily_quota_remaining_percent=99.0,
                    weekly_quota_remaining_percent=50.0)
        out = window_history.detect_closed_windows("devin", prev, new)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].window_kind, "daily")
        self.assertEqual(out[0].reset_cause, "natural")
        self.assertEqual(out[0].final_used_pct, 70.0)  # 100 - 30 remaining

    def test_devin_weekly_uses_plan_reset_hint(self):
        prev = _snap(BASE, weekly_quota_remaining_percent=20.0,
                     plan_reset_unix=BASE + 400)
        new = _snap(BASE + 300, weekly_quota_remaining_percent=99.0,
                    plan_reset_unix=BASE + 400)
        out = window_history.detect_closed_windows("devin", prev, new)
        self.assertEqual(out[0].window_kind, "weekly")
        self.assertEqual(out[0].reset_cause, "natural")
        far = window_history.detect_closed_windows(
            "devin",
            _snap(BASE, weekly_quota_remaining_percent=20.0, plan_reset_unix=BASE + 900000),
            _snap(BASE + 300, weekly_quota_remaining_percent=99.0, plan_reset_unix=BASE + 900000))
        self.assertEqual(far[0].reset_cause, "unknown")


class ArchiveExportTests(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect()
        self.acct_id = store.upsert_account(self.conn, "codex", f"exp-{id(self)}@example.com", "exp")
        self.account = store.get_account(self.conn, self.acct_id)

    def test_record_appends_jsonl_and_rewrites_csv(self):
        prev = _snap(BASE, primary_used_pct=85.0, primary_reset_at=BASE + 300,
                     primary_window_s=18000)
        new = _snap(BASE + 600, primary_used_pct=3.0,
                    primary_reset_at=BASE + 300 + 18000, primary_window_s=18000)
        self.assertEqual(window_history.record_closed_windows(self.conn, self.account, prev, new), 1)
        # re-running is idempotent: no new row, no duplicate JSONL line
        self.assertEqual(window_history.record_closed_windows(self.conn, self.account, prev, new), 0)

        jsonl = window_history.HISTORY_DIR / f"codex-{self.acct_id}.jsonl"
        lines = [json.loads(l) for l in jsonl.read_text().splitlines()]
        ours = [l for l in lines if l["email"] == self.account["email"]]
        self.assertEqual(len(ours), 1)
        self.assertEqual(ours[0]["window_kind"], "5h")
        self.assertEqual(ours[0]["reset_cause"], "natural")
        self.assertIn("staleness_s", ours[0]["details"])

        with open(window_history.HISTORY_DIR / "codex.csv") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(list(rows[0].keys()), list(window_history.CSV_FIELDS))
        ours_csv = [r for r in rows if r["email"] == self.account["email"]]
        self.assertEqual(len(ours_csv), 1)
        self.assertEqual(float(ours_csv[0]["final_used_pct"]), 85.0)

    def test_coupon_redeem_archives_from_last_good_snapshot(self):
        _insert_snap(self.conn, self.acct_id, BASE, status="active",
                     primary_used_pct=91.0, primary_reset_at=BASE + 9000, primary_window_s=18000,
                     secondary_used_pct=40.0, secondary_reset_at=BASE + 500000,
                     secondary_window_s=604800)
        n = window_history.archive_coupon_redeem(self.conn, self.account, "credit-abc",
                                                 now_ts=BASE + 100)
        self.assertEqual(n, 2)
        rows = store.list_window_history(self.conn, account_id=self.acct_id)
        self.assertEqual({r["window_kind"] for r in rows}, {"5h", "weekly"})
        for r in rows:
            self.assertEqual(r["reset_cause"], "coupon")
            self.assertEqual(r["window_end"], BASE + 100)
            self.assertEqual(json.loads(r["details"])["credit_id"], "credit-abc")

    def test_detection_after_redeem_does_not_duplicate(self):
        # Direct archive at redeem time (rule 4)...
        _insert_snap(self.conn, self.acct_id, BASE, status="active",
                     primary_used_pct=91.0, primary_reset_at=BASE + 9000, primary_window_s=18000)
        window_history.archive_coupon_redeem(self.conn, self.account, "credit-abc",
                                             now_ts=BASE + 100)
        # ...then the next poll pair sees the same early roll (confirm re-poll failed).
        prev = _snap(BASE, primary_used_pct=91.0, primary_reset_at=BASE + 9000,
                     primary_window_s=18000, banked_resets=2)
        new = _snap(BASE + 300, primary_used_pct=0.5,
                    primary_reset_at=BASE + 300 + 18000, primary_window_s=18000,
                    banked_resets=1)
        self.assertEqual(window_history.record_closed_windows(self.conn, self.account, prev, new), 0)
        rows = [r for r in store.list_window_history(self.conn, account_id=self.acct_id)
                if r["window_kind"] == "5h"]
        self.assertEqual(len(rows), 1)


class DashboardTests(unittest.TestCase):
    def test_generate_is_self_contained(self):
        conn = store.connect()
        acct_id = store.upsert_account(conn, "codex", f"dash-{id(self)}@example.com", "dash")
        store.save_window_history(conn, acct_id, "weekly", BASE - 604800, BASE, 66.0, BASE - 60,
                                  "coupon", {"staleness_s": 60.0, "credit_id": "c1"})
        path = dashboard.generate(conn)
        html = path.read_text()
        self.assertIn("dash-", html)             # embedded data blob
        self.assertIn("coupon", html)
        self.assertIn("const ROWS =", html)
        self.assertIn("<table", html)
        self.assertNotIn('src="http', html)      # no CDN / external requests
        self.assertNotIn("src='http", html)
        self.assertNotIn('href="http', html)
        self.assertNotIn("@import", html)

    def test_generate_excludes_5h_windows(self):
        conn = store.connect()
        acct_id = store.upsert_account(conn, "codex", f"dash5h-{id(self)}@example.com", "dash5h")
        store.save_window_history(conn, acct_id, "5h", BASE - 18000, BASE, 42.0, BASE - 60,
                                  "natural", {"staleness_s": 60.0})
        store.save_window_history(conn, acct_id, "weekly", BASE - 604800, BASE, 88.0, BASE - 60,
                                  "natural", {"staleness_s": 60.0})
        rows = dashboard.dashboard_data(conn)
        kinds = {r["window_kind"] for r in rows if r["account_id"] == acct_id}
        self.assertNotIn("5h", kinds)
        self.assertIn("weekly", kinds)


class PollerHookTests(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect()
        self.acct_id = store.upsert_account(self.conn, "claude", f"hook-{id(self)}@example.com", "hook")
        self.account = store.get_account(self.conn, self.acct_id)

    def test_archives_after_natural_roll(self):
        _insert_snap(self.conn, self.acct_id, BASE, status="active",
                     primary_used_pct=88.0, primary_reset_at=BASE + 200, primary_window_s=18000)
        prev = store.latest_successful_snapshot(self.conn, self.acct_id)
        _insert_snap(self.conn, self.acct_id, BASE + 500, status="active",
                     primary_used_pct=1.0, primary_reset_at=BASE + 200 + 18000,
                     primary_window_s=18000)
        poller._archive_closed_windows(self.conn, self.account, prev, {})
        rows = store.list_window_history(self.conn, account_id=self.acct_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reset_cause"], "natural")
        self.assertEqual(rows[0]["window_end"], BASE + 200)

    def test_error_snapshot_never_archives(self):
        _insert_snap(self.conn, self.acct_id, BASE, status="active",
                     primary_used_pct=88.0, primary_reset_at=BASE + 200, primary_window_s=18000)
        prev = store.latest_successful_snapshot(self.conn, self.acct_id)
        _insert_snap(self.conn, self.acct_id, BASE + 500, status="error")
        poller._archive_closed_windows(self.conn, self.account, prev, {})
        self.assertEqual(store.list_window_history(self.conn, account_id=self.acct_id), [])

    def test_unchanged_snapshot_is_a_noop(self):
        # copilot's hold-last-good path saves no new snapshot: prev is new.
        _insert_snap(self.conn, self.acct_id, BASE, status="active",
                     primary_used_pct=88.0, primary_reset_at=BASE + 200, primary_window_s=18000)
        prev = store.latest_successful_snapshot(self.conn, self.acct_id)
        poller._archive_closed_windows(self.conn, self.account, prev, {})
        self.assertEqual(store.list_window_history(self.conn, account_id=self.acct_id), [])

    def test_poll_survives_archive_exception(self):
        # A crash inside window-history archiving must not fail the poll: the
        # snapshot is still saved and _poll_one returns True (loop continues).
        store.save_token(self.conn, self.acct_id, "tok", None, None, 9_999_999_999.0, None)

        def fake_poll(conn, account, token):
            _insert_snap(conn, account["id"], BASE, status="active", primary_used_pct=10.0)

        def boom(*a, **k):
            raise RuntimeError("archive blew up")

        orig_pollers = poller.POLLERS
        orig_record = window_history.record_closed_windows
        poller.POLLERS = {**orig_pollers, "claude": fake_poll}
        window_history.record_closed_windows = boom
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                ok = poller._poll_one(self.conn, self.account)
        finally:
            poller.POLLERS = orig_pollers
            window_history.record_closed_windows = orig_record
        self.assertTrue(ok)
        self.assertIsNotNone(store.latest_snapshot(self.conn, self.acct_id))
        self.assertIn("detection failed", buf.getvalue())


class SchedulerTests(unittest.TestCase):
    def test_base_cadence(self):
        due = poller.compute_next_due(1000.0, provider="codex", last_poll_ts=900.0,
                                      last_success_ts=900.0, hot=False, reset_at=None)
        self.assertEqual(due, 900.0 + poller.POLL_INTERVAL)

    def test_hot_account_polls_at_hot_interval(self):
        due = poller.compute_next_due(1000.0, provider="codex", last_poll_ts=900.0,
                                      last_success_ts=900.0, hot=True, reset_at=None)
        self.assertEqual(due, 900.0 + poller.HOT_INTERVAL_S)

    def test_hot_claude_uses_standard_interval(self):
        due = poller.compute_next_due(1000.0, provider="claude", last_poll_ts=900.0,
                                      last_success_ts=900.0, hot=True, reset_at=None)
        self.assertEqual(due, 900.0 + poller.HOT_INTERVAL_S)

    def test_prereset_wakes_at_lead_start(self):
        reset = 2000.0
        due = poller.compute_next_due(1650.0, provider="codex", last_poll_ts=1600.0,
                                      last_success_ts=1600.0, hot=False, reset_at=reset)
        self.assertEqual(due, reset - poller.PRERESET_LEAD_S)  # 1700

    def test_prereset_retries_inside_window(self):
        reset = 2000.0
        now = reset - 100.0  # inside the lead window, no capture yet
        due = poller.compute_next_due(now, provider="codex", last_poll_ts=now - 10,
                                      last_success_ts=1000.0, hot=False, reset_at=reset)
        self.assertEqual(due, now - 10 + poller.PRERESET_RETRY_S)

    def test_prereset_first_success_wins(self):
        reset = 2000.0
        now = reset - 100.0
        due = poller.compute_next_due(now, provider="codex", last_poll_ts=now - 10,
                                      last_success_ts=now - 10,  # success inside lead window
                                      hot=False, reset_at=reset)
        self.assertEqual(due, now - 10 + poller.POLL_INTERVAL)

    def test_prereset_last_attempt_before_final_gap(self):
        reset = 2000.0
        now = reset - 20.0  # past deadline (reset - 30)
        due = poller.compute_next_due(now, provider="codex", last_poll_ts=now - 5,
                                      last_success_ts=1000.0, hot=False, reset_at=reset)
        self.assertEqual(due, now - 5 + poller.POLL_INTERVAL)

    def test_prereset_claude_matches_codex(self):
        now = 10_000.0
        last_poll = now - 5
        reset_at = now + 200
        claude_due = poller.compute_next_due(now, provider="claude", last_poll_ts=last_poll,
                                             last_success_ts=None, hot=False, reset_at=reset_at)
        codex_due = poller.compute_next_due(now, provider="codex", last_poll_ts=last_poll,
                                            last_success_ts=None, hot=False, reset_at=reset_at)
        self.assertEqual(claude_due, codex_due)

    def test_run_loop_survives_cycle_exception(self):
        # A failure escaping the wake body must be logged and the loop must
        # continue, not propagate (LaunchAgent would crash-loop the daemon).
        calls = {"n": 0}

        def boom(conn):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("cycle blew up")
            raise KeyboardInterrupt

        orig_list, orig_sleep = store.list_accounts, poller.time.sleep
        store.list_accounts = boom
        poller.time.sleep = lambda *a, **k: None
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = poller.run_loop(None)
        finally:
            store.list_accounts, poller.time.sleep = orig_list, orig_sleep
        self.assertEqual(rc, 0)            # clean shutdown via KeyboardInterrupt
        self.assertEqual(calls["n"], 2)    # survived the first bad cycle
        # Pin the OUTER run_loop handler: the failure must escape to
        # "poll cycle error", not be swallowed by a narrower catch.
        self.assertIn("poll cycle error: cycle blew up", buf.getvalue())

    def test_hotness_and_next_reset_helpers(self):
        s = {"status": "active", "ts": 1000.0,
             "primary_used_pct": 20.0, "primary_reset_at": 4000.0, "primary_window_s": 18000,
             "secondary_used_pct": 75.0, "secondary_reset_at": 9000.0,
             "secondary_window_s": 604800}
        self.assertEqual(poller.max_used_pct("codex", s), 75.0)
        self.assertEqual(poller.next_reset_at("codex", s, 1000.0), 4000.0)
        self.assertEqual(poller.next_reset_at("codex", s, 5000.0), 9000.0)
        self.assertIsNone(poller.next_reset_at("codex", s, 10000.0))


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect()
        self.acct_id = store.upsert_account(self.conn, "claude", f"bf-{id(self)}@example.com", "bf")

    def test_backfill_streams_pairs_and_is_idempotent(self):
        _insert_snap(self.conn, self.acct_id, BASE, status="active",
                     primary_used_pct=80.0, primary_reset_at=BASE + 600, primary_window_s=18000)
        _insert_snap(self.conn, self.acct_id, BASE + 300, status="error")  # invisible (rule 1)
        _insert_snap(self.conn, self.acct_id, BASE + 900, status="active",
                     primary_used_pct=5.0, primary_reset_at=BASE + 600 + 18000,
                     primary_window_s=18000)
        window_history.backfill(self.conn)
        rows = store.list_window_history(self.conn, account_id=self.acct_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["window_kind"], "5h")
        self.assertEqual(rows[0]["reset_cause"], "natural")
        self.assertEqual(rows[0]["window_end"], BASE + 600)
        window_history.backfill(self.conn)  # re-run: UNIQUE constraint dedupes
        self.assertEqual(len(store.list_window_history(self.conn, account_id=self.acct_id)), 1)

    def test_backfill_separates_accounts_and_groups_by_provider(self):
        cx = store.upsert_account(self.conn, "codex", f"bf-cx-{id(self)}@example.com", "cx")
        cl = store.upsert_account(self.conn, "claude", f"bf-cl-{id(self)}@example.com", "cl")
        for aid, reset in ((cx, BASE + 400), (cl, BASE + 500)):
            _insert_snap(self.conn, aid, BASE, status="active", primary_used_pct=80.0,
                         primary_reset_at=reset, primary_window_s=18000)
            _insert_snap(self.conn, aid, BASE + 700, status="active", primary_used_pct=2.0,
                         primary_reset_at=reset + 18000, primary_window_s=18000)
        window_history.backfill(self.conn)

        # Per-account JSONL: each account's rows land only in its own file.
        cx_lines = [json.loads(l) for l in
                    (window_history.HISTORY_DIR / f"codex-{cx}.jsonl").read_text().splitlines()]
        cl_lines = [json.loads(l) for l in
                    (window_history.HISTORY_DIR / f"claude-{cl}.jsonl").read_text().splitlines()]
        self.assertTrue(cx_lines and all(l["email"].startswith("bf-cx-") for l in cx_lines))
        self.assertTrue(cl_lines and all(l["email"].startswith("bf-cl-") for l in cl_lines))

        # Per-provider CSV: codex.csv groups codex accounts and excludes claude.
        with open(window_history.HISTORY_DIR / "codex.csv") as f:
            cx_csv = list(csv.DictReader(f))
        self.assertTrue(any(r["email"].startswith("bf-cx-") for r in cx_csv))
        self.assertFalse(any(r["email"].startswith("bf-cl-") for r in cx_csv))


if __name__ == "__main__":
    unittest.main(verbosity=2)
