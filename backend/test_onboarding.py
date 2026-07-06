#!/usr/bin/env python3
"""Verify that every newly-onboarded user gets every subscription data on
initial onboarding — not deferred to the next 5-minute poll cycle.

For each provider this asserts two things:
  1. `pool.cmd_add` / `pool.cmd_add_devin` invokes `poller.poll_account`
     immediately after saving the token (the onboarding→poll wiring).
  2. The provider's poller, fed canned HTTP responses, saves a snapshot
     containing the full set of subscription fields that provider surfaces.

Run:  python test_onboarding.py
"""
from __future__ import annotations
import json, os, sys, tempfile, time, unittest
import urllib.request
from pathlib import Path
from unittest import mock

# Point the store + status export at a temp DB / file BEFORE importing the
# modules that read these at import time.
_TMP = tempfile.mkdtemp(prefix="tsb-test-")
os.environ["AGENT_POOL_DB"] = str(Path(_TMP) / "pool.db")
os.environ["AGENT_POOL_STATUS_JSON"] = str(Path(_TMP) / "status.json")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store, oauth, poller, pool  # noqa: E402


# ─── fake HTTP ──────────────────────────────────────────────────────────────
class _FakeResp:
    def __init__(self, status, body, headers):
        self.status = status
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = headers

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(req, timeout=None):
    url = req.full_url
    method = req.get_method()
    # Dispatch keyed on URL substring; each branch returns canned bytes.
    # ── codex ──
    if "wham/usage" in url:
        return _FakeResp(200, {
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {"used_percent": 42.0, "reset_after_seconds": 3600,
                                   "limit_window_seconds": 7200},
                "secondary_window": {"used_percent": 10.0, "reset_after_seconds": 86400,
                                     "limit_window_seconds": 172800},
            },
            "credits": {"balance": 12.5},
            "rate_limit_reset_credits": {"available_count": 2},
        }, {})
    if "wham/rate-limit-reset-credits" in url and "consume" not in url:
        return _FakeResp(200, {"credits": [
            {"id": "rc1", "title": "Reset", "status": "available",
             "expires_at": "2026-12-31T00:00:00Z", "granted_at": "2026-01-01T00:00:00Z",
             "description": "banked"},
        ]}, {})
    # ── claude ──
    if "api.anthropic.com/v1/messages" in url:
        return _FakeResp(200, {}, {
            "anthropic-ratelimit-unified-5h-utilization": "0.07",
            "anthropic-ratelimit-unified-5h-reset": str(time.time() + 18000),
            "anthropic-ratelimit-unified-7d-utilization": "0.20",
            "anthropic-ratelimit-unified-7d-reset": str(time.time() + 604800),
            "anthropic-ratelimit-unified-status": "ok",
        })
    if "api.anthropic.com/api/oauth/profile" in url:
        return _FakeResp(200, {
            "account": {"has_claude_max": True, "display_name": "Test",
                        "full_name": "Test User", "created_at": "2024-01-01T00:00:00Z"},
            "organization": {"subscription_status": "active", "billing_type": "stripe",
                             "rate_limit_tier": "tier_2",
                             "has_extra_usage_enabled": True,
                             "subscription_created_at": "2024-01-01T00:00:00Z",
                             "organization_type": "individual",
                             "name": "Personal"},
        }, {})
    # ── xai ──
    if "cli-chat-proxy.grok.com/v1/billing" in url:
        return _FakeResp(200, {"config": {
            "used": {"val": 30}, "monthlyLimit": {"val": 100},
            "billingPeriodStart": "2026-01-01T00:00:00Z",
            "billingPeriodEnd": "2026-02-01T00:00:00Z",
            "onDemandCap": {"val": 50},
        }}, {})
    if "api.x.ai/v1/chat/completions" in url:
        return _FakeResp(200, {}, {
            "x-ratelimit-limit-requests": "100",
            "x-ratelimit-remaining-requests": "80",
            "x-ratelimit-limit-tokens": "10000",
            "x-ratelimit-remaining-tokens": "9000",
            "x-ratelimit-reset-requests": "23h59m",
        })
    # ── antigravity ──
    if "loadCodeAssist" in url:
        return _FakeResp(200, {
            "currentTier": {"id": "free-tier", "name": "Free"},
            "paidTier": {"id": "g1-pro-tier", "name": "Google AI Pro",
                         "description": "Pro tier"},
            "cloudaicompanionProject": "proj-123",
        }, {})
    if "fetchAvailableModels" in url:
        return _FakeResp(200, {"models": {
            "gemini-3-pro": {"displayName": "Gemini 3 Pro",
                             "quotaInfo": {"remainingFraction": 0.75,
                                           "resetTime": "2026-01-02T00:00:00Z"}},
        }}, {})
    # ── copilot ──
    if "copilot_internal/v2/token" in url:
        return _FakeResp(200, {"token": "cp-tok", "expires_at": time.time() + 7200,
                               "sku": "individual_pro", "limited_user_quotas": None,
                               "limited_user_reset_date": None}, {})
    if "copilot_internal/user" in url:
        return _FakeResp(200, {
            "copilot_plan": "individual_pro",
            "quota_reset_date": "2026-02-01",
            "quota_snapshots": {
                "premium_interactions": {"percent_remaining": 60.0, "unlimited": False,
                                         "entitlement": "pro", "overage_count": 0},
                "chat": {"percent_remaining": 90.0, "unlimited": False},
                "completions": {"unlimited": True},
            },
            "access_type_sku": "individual_pro",
            "can_upgrade_plan": False,
            "organization_login_list": ["my-org"],
        }, {})
    if "api.github.com/user" in url:
        return _FakeResp(200, {"login": "testuser", "id": 1234,
                               "email": "test@example.com", "name": "Test User"}, {})
    # ── devin ──
    if "GetUserStatus" in url:
        # Minimal protobuf: field 2 (plan_info) with field 2 = "Pro",
        # field 1 (user_status) with field 13 (quota) containing varints 14..18.
        def _varint(v):
            out = b""
            while v > 0x7f:
                out += bytes([0x80 | (v & 0x7f)]); v >>= 7
            return out + bytes([v & 0x7f])
        def _bytes_field(fn, data):
            return _varint((fn << 3) | 2) + _varint(len(data)) + data
        def _varint_field(fn, v):
            return _varint((fn << 3) | 0) + _varint(v)
        plan_start = int(time.time()) - 30 * 86400
        plan_reset = int(time.time()) + 86400
        quota = (_bytes_field(2, _varint_field(1, plan_start))
                 + _bytes_field(3, _varint_field(1, plan_reset))
                 + _varint_field(14, 70) + _varint_field(15, 40)
                 + _varint_field(16, 5_000_000) + _varint_field(17, int(time.time()) + 86400)
                 + _varint_field(18, int(time.time()) + 604800))
        user_status = _bytes_field(7, b"test@example.com") + _bytes_field(13, quota)
        plan_info = _bytes_field(2, b"Pro")
        body = _bytes_field(1, user_status) + _bytes_field(2, plan_info)
        return _FakeResp(200, body, {})
    raise AssertionError(f"unexpected urlopen: {method} {url}")


def _fake_login(provider):
    """Return a fake OAuth result for the given provider (no browser/network)."""
    return {
        "access_token": f"fake-{provider}-access",
        "refresh_token": f"fake-{provider}-refresh",
        "id_token": "",
        "expires_at": time.time() + 3600,
        "account_id": f"{provider}-acct-1",
        "email": f"{provider}@example.com",
        "plan": "",
        "raw": {"github_token": f"fake-{provider}-gh"} if provider == "copilot" else {},
    }


# ─── tests ──────────────────────────────────────────────────────────────────
class OnboardingPollTests(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect()
        # wipe between tests
        for t in ("reset_credits", "refresh_log", "limit_snapshots", "tokens", "accounts"):
            self.conn.execute(f"DELETE FROM {t}")
        self.conn.commit()
        self._urlopen_patch = mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen)
        self._urlopen_patch.start()

    def tearDown(self):
        self._urlopen_patch.stop()
        self.conn.close()

    # Helper: run onboarding for a provider and return the saved snapshot.
    def _onboard(self, provider):
        with mock.patch.dict(oauth.LOGIN_FUNCS, {provider: lambda incognito=False: _fake_login(provider)}):
            rc = pool.cmd_add(provider, f"{provider}-test")
        self.assertEqual(rc, 0, f"cmd_add({provider}) returned {rc}")
        acct = next(a for a in store.list_accounts(self.conn) if a["provider"] == provider)
        snap = store.latest_snapshot(self.conn, acct["id"])
        self.assertIsNotNone(snap, f"no snapshot saved for {provider}")
        return acct, snap

    def test_every_oauth_provider_onboarding_polls(self):
        """cmd_add must call poller.poll_account for every OAuth provider."""
        for provider in oauth.LOGIN_FUNCS:
            with self.subTest(provider=provider):
                _, snap = self._onboard(provider)
                self.assertEqual(snap["status"], "active",
                                 f"{provider} onboarding did not produce an active snapshot: "
                                 f"{snap.get('status_message')}")

    def test_devin_onboarding_polls(self):
        """cmd_add_devin must call poller.poll_account for devin."""
        with mock.patch.object(oauth, "login_devin", return_value={
            "access_token": "fake-devin-key", "refresh_token": None, "id_token": "",
            "expires_at": 0, "account_id": "devin-org-1",
            "email": "devin@example.com", "plan": "", "raw": {"api_key": "fake-devin-key"},
        }):
            rc = pool.cmd_add_devin("fake-devin-key", "devin-test")
        self.assertEqual(rc, 0)
        acct = next(a for a in store.list_accounts(self.conn) if a["provider"] == "devin")
        snap = store.latest_snapshot(self.conn, acct["id"])
        self.assertIsNotNone(snap, "no snapshot saved for devin")
        self.assertEqual(snap["status"], "active",
                         f"devin onboarding did not produce an active snapshot: "
                         f"{snap.get('status_message')}")

    # ── per-provider subscription field coverage ──
    def test_codex_subscription_data(self):
        _, snap = self._onboard("codex")
        for f in ("plan", "primary_used_pct", "primary_reset_at", "primary_window_s",
                  "secondary_used_pct", "secondary_reset_at", "secondary_window_s",
                  "credits_balance", "banked_resets"):
            self.assertIsNotNone(snap[f], f"codex snapshot missing {f}")
        # reset credits table must be populated
        acct = next(a for a in store.list_accounts(self.conn) if a["provider"] == "codex")
        credits = store.list_reset_credits(self.conn, acct["id"])
        self.assertEqual(len(credits), 1, "codex reset credits not populated")

    def test_claude_subscription_data(self):
        _, snap = self._onboard("claude")
        for f in ("plan", "primary_used_pct", "primary_reset_at", "primary_window_s",
                  "secondary_used_pct", "secondary_reset_at", "secondary_window_s",
                  "rate_limit_remaining", "rate_limit_limit"):
            self.assertIsNotNone(snap[f], f"claude snapshot missing {f}")
        # profile must be merged into raw_json
        rj = json.loads(snap["raw_json"])
        self.assertEqual(rj["profile"]["plan"], "Claude Max")

    def test_xai_subscription_data(self):
        _, snap = self._onboard("xai")
        for f in ("monthly_used", "monthly_limit", "monthly_used_pct",
                  "monthly_period_start", "monthly_period_end",
                  "primary_used_pct", "primary_reset_at", "primary_window_s",
                  "secondary_used_pct", "secondary_window_s", "rate_limit_remaining",
                  "rate_limit_limit"):
            self.assertIsNotNone(snap[f], f"xai snapshot missing {f}")

    def test_antigravity_subscription_data(self):
        _, snap = self._onboard("antigravity")
        for f in ("plan", "primary_used_pct", "primary_window_s",
                  "rate_limit_remaining", "rate_limit_limit"):
            self.assertIsNotNone(snap[f], f"antigravity snapshot missing {f}")

    def test_copilot_subscription_data(self):
        _, snap = self._onboard("copilot")
        for f in ("plan", "sku", "primary_used_pct", "rate_limit_remaining",
                  "rate_limit_limit", "rate_limit_reset"):
            self.assertIsNotNone(snap[f], f"copilot snapshot missing {f}")
        rj = json.loads(snap["raw_json"])
        self.assertEqual(rj["extra"]["github_email"], "test@example.com")

    def test_devin_subscription_data(self):
        with mock.patch.object(oauth, "login_devin", return_value={
            "access_token": "fake-devin-key", "refresh_token": None, "id_token": "",
            "expires_at": 0, "account_id": "devin-org-1",
            "email": "devin@example.com", "plan": "", "raw": {"api_key": "fake-devin-key"},
        }):
            rc = pool.cmd_add_devin("fake-devin-key", "devin-test")
        self.assertEqual(rc, 0)
        acct = next(a for a in store.list_accounts(self.conn) if a["provider"] == "devin")
        snap = store.latest_snapshot(self.conn, acct["id"])
        for f in ("plan", "primary_used_pct", "primary_window_s",
                  "secondary_used_pct", "secondary_window_s",
                  "rate_limit_remaining", "rate_limit_limit"):
            self.assertIsNotNone(snap[f], f"devin snapshot missing {f}")
        rj = json.loads(snap["raw_json"])
        self.assertEqual(rj["extra"]["credit_balance"], 5.0)
        self.assertIsNotNone(rj["extra"].get("plan_start_unix"))
        self.assertIsNotNone(rj["extra"].get("plan_reset_unix"))

    def test_export_includes_billing_period_fields(self):
        self._onboard("xai")
        with mock.patch.object(oauth, "login_devin", return_value={
            "access_token": "fake-devin-key", "refresh_token": None, "id_token": "",
            "expires_at": 0, "account_id": "devin-org-1",
            "email": "devin@example.com", "plan": "", "raw": {"api_key": "fake-devin-key"},
        }):
            self.assertEqual(pool.cmd_add_devin("fake-devin-key", "devin-test"), 0)

        payload = json.loads(Path(os.environ["AGENT_POOL_STATUS_JSON"]).read_text())
        by_provider = {a["provider"]: a for a in payload["accounts"]}
        xai = by_provider["xai"]
        self.assertEqual(xai["monthly_period_start"], "2026-01-01 09:00")
        self.assertEqual(xai["monthly_period_end"], "2026-02-01 09:00")
        self.assertEqual(xai["plan_start"], "2026-01-01 09:00")
        self.assertEqual(xai["plan_reset"], "2026-02-01 09:00")
        devin = by_provider["devin"]
        self.assertIsNotNone(devin["plan_start"])
        self.assertIsNotNone(devin["plan_reset"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
