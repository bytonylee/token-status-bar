"""Tests for account_state() — per-account lifecycle classification."""
from __future__ import annotations
import datetime, os, sys, tempfile, unittest

_TMP = tempfile.mkdtemp(prefix="tsb-test-")
os.environ["AGENT_POOL_DB"] = os.path.join(_TMP, "pool.db")
os.environ["AGENT_POOL_STATUS_JSON"] = os.path.join(_TMP, "status.json")
os.environ["AGENT_POOL_HISTORY_DIR"] = os.path.join(_TMP, "history")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import status  # noqa: E402

NOW = 1_753_000_000.0
UTC = datetime.timezone.utc


def iso(offset_s: float) -> str:
    return datetime.datetime.fromtimestamp(NOW + offset_s, tz=UTC).isoformat()


def item(provider="codex", snap_status="active", windows=None, **kw):
    base = {"id": 1, "provider": provider, "email": "a@x.com",
            "status": snap_status, "token_expired": False,
            "windows": windows if windows is not None else []}
    base.update(kw)
    return base


def win(**kw):
    base = {"kind": "5h", "label": None, "used_pct": 50.0,
            "reset_at_epoch": NOW + 100, "severity": "normal",
            "as_of_epoch": NOW - 60}
    base.update(kw)
    return base


class AuthTest(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(status.account_state(item(), NOW)["auth"], "ok")

    def test_token_expired(self):
        st = status.account_state(item(token_expired=True), NOW)
        self.assertEqual(st["auth"], "token_expired")
        self.assertFalse(st["usable"])

    def test_error_status(self):
        st = status.account_state(item(snap_status="error"), NOW)
        self.assertEqual(st["auth"], "error")
        self.assertFalse(st["usable"])

    def test_token_expired_wins_over_error(self):
        st = status.account_state(item(snap_status="error", token_expired=True), NOW)
        self.assertEqual(st["auth"], "token_expired")


class CodexSubscriptionTest(unittest.TestCase):
    def test_paid(self):
        st = status.account_state(item(has_active_subscription=True,
                                       renews_at=iso(30 * 86400)), NOW)
        self.assertEqual(st["subscription"], "paid")
        self.assertEqual(st["sub_renews_at"], iso(30 * 86400))

    def test_gratis_is_free(self):
        st = status.account_state(item(has_active_subscription=True,
                                       is_active_subscription_gratis=True), NOW)
        self.assertEqual(st["subscription"], "free")

    def test_renews_soon_inside_72h(self):
        st = status.account_state(item(has_active_subscription=True,
                                       renews_at=iso(71 * 3600)), NOW)
        self.assertEqual(st["subscription"], "renews_soon")

    def test_renews_soon_boundary(self):
        at_72h = status.account_state(item(has_active_subscription=True,
                                           renews_at=iso(72 * 3600)), NOW)
        past_72h = status.account_state(item(has_active_subscription=True,
                                             renews_at=iso(72 * 3600 + 60)), NOW)
        self.assertEqual(at_72h["subscription"], "renews_soon")
        self.assertEqual(past_72h["subscription"], "paid")

    def test_renews_at_in_past_not_renews_soon(self):
        st = status.account_state(item(has_active_subscription=True,
                                       renews_at=iso(-3600)), NOW)
        self.assertEqual(st["subscription"], "paid")

    def test_expired(self):
        st = status.account_state(item(has_active_subscription=False,
                                       expires_at=iso(-86400)), NOW)
        self.assertEqual(st["subscription"], "expired")
        self.assertFalse(st["usable"])
        self.assertEqual(st["sub_expires_at"], iso(-86400))

    def test_inactive_without_expiry_is_unknown(self):
        st = status.account_state(item(has_active_subscription=False), NOW)
        self.assertEqual(st["subscription"], "unknown")

    def test_no_meta_is_unknown(self):
        self.assertEqual(status.account_state(item(), NOW)["subscription"],
                         "unknown")

    def test_kst_export_format_parsed(self):
        # codex_extra renders renews_at as 'YYYY-MM-DD HH:MM' in KST.
        kst = datetime.datetime.fromtimestamp(NOW + 3600, tz=status.KST)
        st = status.account_state(item(has_active_subscription=True,
                                       renews_at=kst.strftime("%Y-%m-%d %H:%M")),
                                  NOW)
        self.assertEqual(st["subscription"], "renews_soon")


class OtherProvidersSubscriptionTest(unittest.TestCase):
    def test_claude_active_is_paid(self):
        st = status.account_state(item("claude", subscription_status="active"), NOW)
        self.assertEqual(st["subscription"], "paid")

    def test_claude_expired(self):
        st = status.account_state(item("claude", subscription_status="expired"), NOW)
        self.assertEqual(st["subscription"], "expired")
        self.assertFalse(st["usable"])

    def test_claude_missing_is_unknown(self):
        self.assertEqual(status.account_state(item("claude"), NOW)["subscription"],
                         "unknown")

    def test_copilot_free_sku(self):
        st = status.account_state(item("copilot", sku="free"), NOW)
        self.assertEqual(st["subscription"], "free")

    def test_copilot_paid_sku(self):
        st = status.account_state(item("copilot", sku="individual_pro"), NOW)
        self.assertEqual(st["subscription"], "paid")

    def test_copilot_no_sku_is_unknown(self):
        self.assertEqual(status.account_state(item("copilot"), NOW)["subscription"],
                         "unknown")

    def test_xai_plan_presence_is_paid(self):
        st = status.account_state(item("xai", plan="SuperGrok"), NOW)
        self.assertEqual(st["subscription"], "paid")

    def test_devin_plan_presence_is_paid(self):
        st = status.account_state(item("devin", plan="Core"), NOW)
        self.assertEqual(st["subscription"], "paid")

    def test_antigravity_free_plan(self):
        st = status.account_state(item("antigravity", plan="Free"), NOW)
        self.assertEqual(st["subscription"], "free")

    def test_antigravity_no_plan_is_unknown(self):
        self.assertEqual(status.account_state(item("antigravity"), NOW)["subscription"],
                         "unknown")


class QuotaTest(unittest.TestCase):
    def test_no_windows_unknown(self):
        self.assertEqual(status.account_state(item(), NOW)["quota"], "unknown")

    def test_rate_limited_status_is_exhausted_even_without_windows(self):
        st = status.account_state(item(snap_status="rate_limited"), NOW)
        self.assertEqual(st["quota"], "exhausted")
        self.assertFalse(st["usable"])

    def test_all_live_windows_at_100_exhausted(self):
        st = status.account_state(item(windows=[win(used_pct=100.0),
                                                win(kind="weekly", used_pct=100.0)]),
                                  NOW)
        self.assertEqual(st["quota"], "exhausted")
        self.assertFalse(st["usable"])

    def test_rate_limited_severity_exhausts(self):
        st = status.account_state(item(windows=[win(used_pct=97.0,
                                                    severity="rate_limited")]), NOW)
        self.assertEqual(st["quota"], "exhausted")

    def test_one_exhausted_one_live_is_warning_not_exhausted(self):
        st = status.account_state(item(windows=[win(used_pct=100.0),
                                                win(kind="weekly", used_pct=40.0)]),
                                  NOW)
        self.assertEqual(st["quota"], "warning")  # binding window at 100 >= 80
        self.assertTrue(st["usable"])

    def test_binding_at_80_is_warning(self):
        st = status.account_state(item(windows=[win(used_pct=80.0)]), NOW)
        self.assertEqual(st["quota"], "warning")

    def test_below_80_is_ok(self):
        st = status.account_state(item(windows=[win(used_pct=79.9)]), NOW)
        self.assertEqual(st["quota"], "ok")

    def test_stale_windows_do_not_exhaust(self):
        # Fail-safe: exhaustion from data older than 3× the poll interval
        # must not count — quota is unknown, account stays usable.
        old = NOW - status.STALE_AFTER_S - 1
        st = status.account_state(item(windows=[win(used_pct=100.0,
                                                    as_of_epoch=old)]), NOW)
        self.assertEqual(st["quota"], "unknown")
        self.assertTrue(st["usable"])

    def test_reset_windows_count_as_zero(self):
        st = status.account_state(item(windows=[win(used_pct=100.0,
                                                    reset_at_epoch=NOW - 10)]),
                                  NOW)
        self.assertEqual(st["quota"], "ok")
        self.assertEqual(st["binding_window"]["used_pct_effective"], 0.0)

    def test_binding_window_is_riskiest(self):
        st = status.account_state(item(windows=[win(used_pct=40.0),
                                                win(kind="weekly", used_pct=85.0)]),
                                  NOW)
        self.assertEqual(st["binding_window"]["kind"], "weekly")
        self.assertEqual(st["quota"], "warning")

    def test_non_normal_severity_binds_first(self):
        st = status.account_state(item(windows=[win(used_pct=90.0),
                                                win(kind="weekly", used_pct=50.0,
                                                    severity="warning")]), NOW)
        self.assertEqual(st["binding_window"]["kind"], "weekly")


class UsableTruthTableTest(unittest.TestCase):
    def test_all_good(self):
        st = status.account_state(item(has_active_subscription=True,
                                       windows=[win(used_pct=10.0)]), NOW)
        self.assertEqual((st["auth"], st["subscription"], st["quota"]),
                         ("ok", "paid", "ok"))
        self.assertTrue(st["usable"])

    def test_unknowns_are_usable(self):
        # unknown sub / unknown quota are not disqualifying by themselves.
        self.assertTrue(status.account_state(item(), NOW)["usable"])

    def test_each_blocker_disqualifies(self):
        blockers = [
            item(token_expired=True),
            item(snap_status="error"),
            item(has_active_subscription=False, expires_at=iso(-1)),
            item(snap_status="rate_limited"),
            item(windows=[win(used_pct=100.0)]),
        ]
        for it in blockers:
            with self.subTest(it=it):
                self.assertFalse(status.account_state(it, NOW)["usable"])

    def test_warning_is_still_usable(self):
        st = status.account_state(item(windows=[win(used_pct=90.0)]), NOW)
        self.assertTrue(st["usable"])


if __name__ == "__main__":
    unittest.main()
