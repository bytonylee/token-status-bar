#!/usr/bin/env python3
"""Tests for the automatic account-swap engine (spec §3, backend/swap.py).

All filesystem side effects go to tmp dirs: env paths are pinned before the
module imports, and CODEX_AUTH is patched per test. Token values are fakes.
"""
from __future__ import annotations
import json, os, stat, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tsb-test-swap-")
os.environ["AGENT_POOL_DB"] = os.path.join(_TMP, "pool.db")
os.environ["AGENT_POOL_STATUS_JSON"] = os.path.join(_TMP, "status.json")
os.environ["AGENT_POOL_HISTORY_DIR"] = os.path.join(_TMP, "history")
os.environ["AGENT_POOL_SETTINGS"] = os.path.join(_TMP, "settings.json")
os.environ["AGENT_POOL_SWAP_BACKUPS"] = os.path.join(_TMP, "swap_backups")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import local_sync  # noqa: E402
import status  # noqa: E402
import store  # noqa: E402
import swap  # noqa: E402

NOW = time.time()
ON = {"auto_swap": {"codex": True}}


def _win(used=100.0, phase="live", stale=False, reset=NOW + 3600,
         kind="5h", as_of=NOW - 60, severity="normal"):
    return {"kind": kind, "label": None, "used_pct": used,
            "used_pct_effective": 0.0 if phase == "reset" else used,
            "reset_at_epoch": reset, "phase": phase, "stale": stale,
            "severity": severity, "as_of_epoch": as_of}


def _item(aid=1, provider="codex", upstream="acct-1", email=None,
          usable=True, quota="ok", windows=None, binding=None,
          last_poll_epoch=NOW - 60):
    return {"id": aid, "provider": provider, "account_id": upstream,
            "email": email or f"a{aid}@example.com",
            "label": f"{provider} #{aid}",
            "windows": windows if windows is not None else [],
            "last_poll_epoch": last_poll_epoch,
            "state": {"auth": "ok", "subscription": "paid",
                      "sub_renews_at": None, "sub_expires_at": None,
                      "quota": quota, "usable": usable,
                      "binding_window": binding}}


def _exhausted_item(aid=1, upstream="acct-1", **kw):
    return _item(aid=aid, upstream=upstream, quota="exhausted", usable=False,
                 windows=[_win(used=100.0), _win(used=100.0, kind="weekly",
                                                 reset=NOW + 86400)], **kw)


class CandidateTests(unittest.TestCase):
    def test_ranking_by_headroom_desc(self):
        low = _item(aid=2, upstream="acct-2",
                    binding=_win(used=80.0, kind="weekly"))
        high = _item(aid=3, upstream="acct-3",
                     binding=_win(used=20.0, kind="weekly"))
        out = swap.swap_candidates([low, high], "codex", "acct-1")
        self.assertEqual([it["id"] for it in out], [3, 2])

    def test_tie_break_earlier_reset_first(self):
        late = _item(aid=2, upstream="acct-2",
                     binding=_win(used=50.0, reset=NOW + 7200))
        early = _item(aid=3, upstream="acct-3",
                      binding=_win(used=50.0, reset=NOW + 3600))
        out = swap.swap_candidates([late, early], "codex", "acct-1")
        self.assertEqual([it["id"] for it in out], [3, 2])

    def test_exclusions(self):
        active = _item(aid=1, upstream="acct-1")
        other_provider = _item(aid=2, upstream="acct-2", provider="claude")
        not_usable = _item(aid=3, upstream="acct-3", usable=False)
        no_upstream = _item(aid=4, upstream=None)
        ok = _item(aid=5, upstream="acct-5")
        out = swap.swap_candidates(
            [active, other_provider, not_usable, no_upstream, ok],
            "codex", "acct-1")
        self.assertEqual([it["id"] for it in out], [5])

    def test_unknown_headroom_ranks_last(self):
        unknown = _item(aid=2, upstream="acct-2", binding=None)
        known = _item(aid=3, upstream="acct-3", binding=_win(used=90.0))
        out = swap.swap_candidates([unknown, known], "codex", "acct-1")
        self.assertEqual([it["id"] for it in out], [3, 2])


class ShouldSwapTests(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect()
        self.conn.execute("DELETE FROM lifecycle_events")
        self.conn.execute("DELETE FROM live_activity")
        self.conn.execute("DELETE FROM accounts")
        self.conn.commit()
        self.cand = [_item(aid=2, upstream="acct-2",
                           binding=_win(used=20.0, kind="weekly"))]

    def tearDown(self):
        self.conn.close()

    def test_happy_path(self):
        chosen = swap.should_swap(_exhausted_item(), self.cand, NOW, ON, self.conn)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["id"], 2)

    def test_kill_switch_off(self):
        for settings in ({"auto_swap": {"codex": False}}, {"auto_swap": {}}, {}):
            self.assertIsNone(swap.should_swap(
                _exhausted_item(), self.cand, NOW, settings, self.conn))

    def test_not_exhausted(self):
        it = _item(quota="warning", windows=[_win(used=85.0)])
        self.assertIsNone(swap.should_swap(it, self.cand, NOW, ON, self.conn))

    def test_all_windows_stale(self):
        it = _exhausted_item()
        for w in it["windows"]:
            w["stale"] = True
        self.assertIsNone(swap.should_swap(it, self.cand, NOW, ON, self.conn))

    def test_no_live_window(self):
        it = _exhausted_item()
        it["windows"] = [_win(used=100.0, phase="reset")]
        self.assertIsNone(swap.should_swap(it, self.cand, NOW, ON, self.conn))

    def test_live_window_with_room_refuses(self):
        it = _exhausted_item()
        it["windows"].append(_win(used=97.0, kind="weekly", reset=NOW + 86400))
        self.assertIsNone(swap.should_swap(it, self.cand, NOW, ON, self.conn))

    def test_live_window_unknown_usage_refuses(self):
        it = _exhausted_item()
        it["windows"][0]["used_pct_effective"] = None
        self.assertIsNone(swap.should_swap(it, self.cand, NOW, ON, self.conn))

    def test_stale_snapshot_refuses(self):
        old = NOW - swap.SNAPSHOT_MAX_AGE_S - 60
        it = _exhausted_item(last_poll_epoch=old)
        for w in it["windows"]:
            w["as_of_epoch"] = old
        self.assertIsNone(swap.should_swap(it, self.cand, NOW, ON, self.conn))

    def test_live_session_guard(self):
        aid = store.upsert_account(self.conn, "codex", "a1@example.com",
                                   "codex #1", "pro", "acct-1")
        store.upsert_live_activity(self.conn, aid, {"provider": "codex"})
        it = _exhausted_item(aid=aid)
        self.assertIsNone(swap.should_swap(it, self.cand, time.time(), ON, self.conn))

    def test_cooldown_active(self):
        store.save_lifecycle_event(self.conn, NOW - 100, 2, "account_swapped",
                                   {"provider": "codex"})
        self.assertIsNone(swap.should_swap(
            _exhausted_item(), self.cand, NOW, ON, self.conn))

    def test_cooldown_other_provider_does_not_block(self):
        store.save_lifecycle_event(self.conn, NOW - 100, 2, "account_swapped",
                                   {"provider": "claude"})
        self.assertIsNotNone(swap.should_swap(
            _exhausted_item(), self.cand, NOW, ON, self.conn))

    def test_cooldown_expired_allows(self):
        store.save_lifecycle_event(self.conn, NOW - swap.COOLDOWN_S - 60, 2,
                                   "account_swapped", {"provider": "codex"})
        self.assertIsNotNone(swap.should_swap(
            _exhausted_item(), self.cand, NOW, ON, self.conn))

    def test_no_candidates(self):
        self.assertIsNone(swap.should_swap(_exhausted_item(), [], NOW, ON, self.conn))


class LoadSettingsTests(unittest.TestCase):
    def test_creates_default_file_0600(self):
        path = Path(_TMP) / "fresh-settings" / "settings.json"
        with mock.patch.object(swap, "SETTINGS_PATH", path):
            settings = swap.load_settings()
        self.assertEqual(settings, {"auto_swap": {"codex": True}})
        self.assertTrue(path.exists())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        # Second call reads the file back.
        with mock.patch.object(swap, "SETTINGS_PATH", path):
            self.assertEqual(swap.load_settings(), settings)

    def test_corrupt_settings_fail_safe(self):
        path = Path(_TMP) / "corrupt-settings.json"
        path.write_text("{not json")
        with mock.patch.object(swap, "SETTINGS_PATH", path):
            self.assertEqual(swap.load_settings(), {"auto_swap": {}})


class PerformSwapTests(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect()
        for table in ("lifecycle_events", "live_activity", "tokens", "accounts"):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.commit()
        self.home = Path(tempfile.mkdtemp(prefix="tsb-codex-home-"))
        self.auth = self.home / "auth.json"
        self.backups = Path(tempfile.mkdtemp(prefix="tsb-backups-"))
        self.old_id = store.upsert_account(self.conn, "codex", "old@example.com",
                                           "codex #1", "pro", "acct-old")
        self.new_id = store.upsert_account(self.conn, "codex", "new@example.com",
                                           "codex #2", "plus", "acct-new")
        store.save_token(self.conn, self.new_id, "fake-access", "fake-refresh",
                         "fake-id", time.time() + 7200)
        self.auth.write_text(json.dumps({
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {"id_token": "old-id", "access_token": "old-access",
                       "refresh_token": "old-refresh", "account_id": "acct-old"},
            "last_refresh": "2026-07-20T00:00:00Z"}))
        self._patches = [
            mock.patch.object(local_sync, "CODEX_AUTH", self.auth),
            mock.patch.object(swap, "BACKUP_DIR", self.backups),
            mock.patch.object(swap, "_notify"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.conn.close()

    def _target(self):
        return store.get_account(self.conn, self.new_id)

    def _token(self):
        return store.get_token(self.conn, self.new_id)

    def test_writes_exact_auth_shape_and_preserves_extras(self):
        out = swap.perform_swap(self.conn, "codex", self._target(), self._token())
        data = json.loads(self.auth.read_text())
        self.assertEqual(set(data), {"auth_mode", "OPENAI_API_KEY",
                                     "tokens", "last_refresh"})
        self.assertEqual(data["auth_mode"], "chatgpt")   # extra key preserved
        self.assertIsNone(data["OPENAI_API_KEY"])
        self.assertEqual(set(data["tokens"]), {"id_token", "access_token",
                                               "refresh_token", "account_id"})
        self.assertEqual(data["tokens"]["account_id"], "acct-new")
        self.assertEqual(data["tokens"]["access_token"], "fake-access")
        self.assertNotEqual(data["last_refresh"], "2026-07-20T00:00:00Z")
        self.assertEqual(out["to_account_id"], "acct-new")
        self.assertEqual(out["from_email"], "old@example.com")

    def test_atomic_write_perms_no_partial(self):
        swap.perform_swap(self.conn, "codex", self._target(), self._token())
        self.assertEqual(stat.S_IMODE(self.auth.stat().st_mode), 0o600)
        self.assertFalse((self.home / "auth.json.tmp").exists())

    def test_backup_created_0600(self):
        original = self.auth.read_text()
        swap.perform_swap(self.conn, "codex", self._target(), self._token())
        backups = list(self.backups.glob("codex-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), original)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.backups.stat().st_mode), 0o700)

    def test_no_existing_auth_no_backup(self):
        self.auth.unlink()
        swap.perform_swap(self.conn, "codex", self._target(), self._token())
        self.assertEqual(list(self.backups.glob("*.json")), [])
        data = json.loads(self.auth.read_text())
        self.assertEqual(data["tokens"]["account_id"], "acct-new")
        self.assertIsNone(data["OPENAI_API_KEY"])  # default injected

    def test_lifecycle_event_saved(self):
        swap.perform_swap(self.conn, "codex", self._target(), self._token())
        ev = store.latest_lifecycle_event(self.conn, "account_swapped")
        self.assertIsNotNone(ev)
        self.assertEqual(ev["account_id"], self.new_id)
        detail = json.loads(ev["detail"])
        self.assertEqual(detail, {"provider": "codex",
                                  "from_email": "old@example.com",
                                  "to_email": "new@example.com",
                                  "from_account_id": "acct-old",
                                  "to_account_id": "acct-new"})

    def test_notification_fired_with_short_emails(self):
        swap.perform_swap(self.conn, "codex", self._target(), self._token(),
                          headroom_note="78% weekly left")
        swap._notify.assert_called_once()
        msg = swap._notify.call_args[0][0]
        self.assertIn("old@ → new@", msg)
        self.assertIn("(78% weekly left)", msg)
        self.assertNotIn("fake-access", msg)

    def test_pre_refresh_called_when_near_expiry(self):
        store.save_token(self.conn, self.new_id, "fake-access", "fake-refresh",
                         "fake-id", time.time() + 100)  # < 600s left
        refreshed = {"access_token": "refreshed-access",
                     "refresh_token": "refreshed-refresh",
                     "id_token": "refreshed-id",
                     "expires_at": time.time() + 3600, "raw": {}}
        cb = mock.Mock(return_value=refreshed)
        swap.perform_swap(self.conn, "codex", self._target(), self._token(),
                          refresh_cb=cb)
        cb.assert_called_once_with("fake-refresh")
        data = json.loads(self.auth.read_text())
        self.assertEqual(data["tokens"]["access_token"], "refreshed-access")
        self.assertEqual(store.get_token(self.conn, self.new_id)["access_token"],
                         "refreshed-access")

    def test_pre_refresh_skipped_when_fresh(self):
        cb = mock.Mock()
        swap.perform_swap(self.conn, "codex", self._target(), self._token(),
                          refresh_cb=cb)
        cb.assert_not_called()

    def test_unsupported_provider_raises(self):
        with self.assertRaises(ValueError):
            swap.perform_swap(self.conn, "claude", self._target(), self._token())

    def test_notification_failure_never_fails_swap(self):
        swap._notify.side_effect = RuntimeError("no UI")
        try:
            swap.perform_swap(self.conn, "codex", self._target(), self._token())
        except RuntimeError:
            self.fail("notification error must not propagate")


class BuildPayloadLastSwapTests(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect()
        for table in ("lifecycle_events", "live_activity", "tokens", "accounts"):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_no_events_no_last_swap(self):
        self.assertNotIn("last_swap", status.build_payload(self.conn))

    def test_latest_event_exported(self):
        store.save_lifecycle_event(self.conn, NOW - 4000, 1, "account_swapped",
                                   {"provider": "codex", "from_email": "x@a.com",
                                    "to_email": "y@a.com"})
        store.save_lifecycle_event(self.conn, NOW - 30, 2, "account_swapped",
                                   {"provider": "codex",
                                    "from_email": "old@example.com",
                                    "to_email": "new@example.com",
                                    "from_account_id": "acct-old",
                                    "to_account_id": "acct-new"})
        payload = status.build_payload(self.conn)
        ls = payload["last_swap"]
        self.assertEqual(ls["provider"], "codex")
        self.assertEqual(ls["from"], "old@")
        self.assertEqual(ls["to"], "new@")
        self.assertAlmostEqual(ls["at_epoch"], NOW - 30, delta=1)
        self.assertRegex(ls["at"], r"^\d{2}:\d{2}$")

    def test_exports_upstream_account_id_and_last_poll_epoch(self):
        aid = store.upsert_account(self.conn, "codex", "a@example.com",
                                   "codex #1", "pro", "acct-a")
        store.save_snapshot(self.conn, aid, {"status": "active",
                                             "primary_used_pct": 10.0})
        item = status.build_payload(self.conn)["accounts"][0]
        self.assertEqual(item["account_id"], "acct-a")
        self.assertAlmostEqual(item["last_poll_epoch"], time.time(), delta=10)


class AutoSwapTickTests(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect()
        for table in ("lifecycle_events", "live_activity", "tokens", "accounts"):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.commit()
        self.home = Path(tempfile.mkdtemp(prefix="tsb-codex-home-"))
        self.auth = self.home / "auth.json"
        self.backups = Path(tempfile.mkdtemp(prefix="tsb-backups-"))
        self.active_id = store.upsert_account(self.conn, "codex", "old@example.com",
                                              "codex #1", "pro", "acct-old")
        self.target_id = store.upsert_account(self.conn, "codex", "new@example.com",
                                              "codex #2", "plus", "acct-new")
        store.save_token(self.conn, self.target_id, "fake-access", "fake-refresh",
                         "fake-id", time.time() + 7200)
        self.auth.write_text(json.dumps({
            "OPENAI_API_KEY": None,
            "tokens": {"id_token": "o", "access_token": "o",
                       "refresh_token": "o", "account_id": "acct-old"},
            "last_refresh": "2026-07-20T00:00:00Z"}))
        self._patches = [
            mock.patch.object(local_sync, "CODEX_AUTH", self.auth),
            mock.patch.object(swap, "BACKUP_DIR", self.backups),
            mock.patch.object(swap, "_notify"),
            mock.patch.object(swap, "load_settings", return_value=ON),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.conn.close()

    def _payload(self, active, candidate):
        return {"generated_at": "x", "account_count": 2,
                "accounts": [active, candidate]}

    def test_happy_path_swaps(self):
        now = time.time()
        active = _exhausted_item(aid=self.active_id, upstream="acct-old",
                                 email="old@example.com", last_poll_epoch=now - 30)
        for w in active["windows"]:
            w["as_of_epoch"] = now - 30
        cand = _item(aid=self.target_id, upstream="acct-new",
                     email="new@example.com",
                     binding=_win(used=22.0, kind="weekly"))
        out = swap.auto_swap_tick(self.conn, self._payload(active, cand), now=now)
        self.assertIsNotNone(out)
        data = json.loads(self.auth.read_text())
        self.assertEqual(data["tokens"]["account_id"], "acct-new")
        ev = store.latest_lifecycle_event(self.conn, "account_swapped")
        self.assertIsNotNone(ev)
        msg = swap._notify.call_args[0][0]
        self.assertIn("78% weekly left", msg)

    def test_not_exhausted_no_swap(self):
        now = time.time()
        active = _item(aid=self.active_id, upstream="acct-old",
                       quota="ok", windows=[_win(used=40.0, as_of=now - 30)],
                       last_poll_epoch=now - 30)
        cand = _item(aid=self.target_id, upstream="acct-new",
                     binding=_win(used=22.0))
        out = swap.auto_swap_tick(self.conn, self._payload(active, cand), now=now)
        self.assertIsNone(out)
        data = json.loads(self.auth.read_text())
        self.assertEqual(data["tokens"]["account_id"], "acct-old")

    def test_active_not_in_pool_no_swap(self):
        self.auth.write_text(json.dumps(
            {"tokens": {"account_id": "acct-unknown"}}))
        now = time.time()
        active = _exhausted_item(aid=self.active_id, upstream="acct-old",
                                 last_poll_epoch=now - 30)
        cand = _item(aid=self.target_id, upstream="acct-new")
        self.assertIsNone(swap.auto_swap_tick(
            self.conn, self._payload(active, cand), now=now))


class CmdSwapTests(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect()
        for table in ("lifecycle_events", "live_activity", "tokens", "accounts"):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.commit()
        self.home = Path(tempfile.mkdtemp(prefix="tsb-codex-home-"))
        self.auth = self.home / "auth.json"
        self.backups = Path(tempfile.mkdtemp(prefix="tsb-backups-"))
        self.active_id = store.upsert_account(self.conn, "codex", "old@example.com",
                                              "codex #1", "pro", "acct-old")
        self.target_id = store.upsert_account(self.conn, "codex", "new@example.com",
                                              "codex #2", "plus", "acct-new")
        store.save_token(self.conn, self.target_id, "fake-access", "fake-refresh",
                         "fake-id", time.time() + 7200)
        self.auth.write_text(json.dumps({
            "OPENAI_API_KEY": None,
            "tokens": {"id_token": "o", "access_token": "o",
                       "refresh_token": "o", "account_id": "acct-old"},
            "last_refresh": "2026-07-20T00:00:00Z"}))
        self._patches = [
            mock.patch.object(local_sync, "CODEX_AUTH", self.auth),
            mock.patch.object(swap, "BACKUP_DIR", self.backups),
            mock.patch.object(swap, "_notify"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.conn.close()

    def test_force_swaps_and_exports(self):
        rc = swap.cmd_swap(self.conn, "codex", self.target_id, force=True)
        self.assertEqual(rc, 0)
        data = json.loads(self.auth.read_text())
        self.assertEqual(data["tokens"]["account_id"], "acct-new")
        self.assertEqual(data["tokens"]["access_token"], "fake-access")
        # backup + event + exported status.json with last_swap
        self.assertEqual(len(list(self.backups.glob("codex-*.json"))), 1)
        # status.STATUS_JSON was pinned at whichever test module imported
        # status first — read the real constant, not this module's env var.
        exported = json.loads(status.STATUS_JSON.read_text())
        self.assertEqual(exported["last_swap"]["to"], "new@")

    def test_swap_to_already_active_is_noop(self):
        store.save_token(self.conn, self.active_id, "fake-a", "fake-r",
                         "fake-i", time.time() + 7200)
        rc = swap.cmd_swap(self.conn, "codex", self.active_id, force=True)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(self.auth.read_text())["tokens"]["account_id"],
                         "acct-old")  # unchanged
        self.assertIsNone(store.latest_lifecycle_event(self.conn, "account_swapped"))

    def test_unknown_account_fails(self):
        self.assertEqual(swap.cmd_swap(self.conn, "codex", 999, force=True), 1)

    def test_unsupported_provider_fails(self):
        self.assertEqual(swap.cmd_swap(self.conn, "claude", self.target_id,
                                       force=True), 1)

    def test_non_force_refused_when_active_not_exhausted(self):
        # No snapshots → the freshly-built payload can't prove exhaustion,
        # so the rails must refuse a non-forced swap.
        with mock.patch.object(swap, "load_settings", return_value=ON):
            rc = swap.cmd_swap(self.conn, "codex", self.target_id, force=False)
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(self.auth.read_text())["tokens"]["account_id"],
                         "acct-old")


class CliParsingTests(unittest.TestCase):
    def test_pool_swap_requires_account_id(self):
        import pool
        self.assertEqual(pool.main(["swap", "--provider", "codex"]), 1)
        self.assertEqual(pool.main(["swap", "--account-id", "not-a-number"]), 1)


if __name__ == "__main__":
    unittest.main()
