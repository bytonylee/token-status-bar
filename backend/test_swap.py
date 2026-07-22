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
        self.assertEqual(settings, {"auto_swap": {"codex": True, "claude": False}})
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

    def test_merge_adds_new_provider_keys_without_clobbering(self):
        # An M4-era settings.json (codex only, flipped off by the user)
        # gains the claude key with its OFF default; codex stays untouched.
        path = Path(_TMP) / "m4-settings.json"
        path.write_text(json.dumps({"auto_swap": {"codex": False},
                                    "unrelated": 7}))
        with mock.patch.object(swap, "SETTINGS_PATH", path):
            settings = swap.load_settings()
        self.assertEqual(settings["auto_swap"], {"codex": False, "claude": False})
        self.assertEqual(settings["unrelated"], 7)
        on_disk = json.loads(path.read_text())
        self.assertEqual(on_disk["auto_swap"], {"codex": False, "claude": False})

    def test_merge_does_not_override_existing_claude_choice(self):
        path = Path(_TMP) / "claude-on-settings.json"
        path.write_text(json.dumps({"auto_swap": {"codex": True, "claude": True}}))
        with mock.patch.object(swap, "SETTINGS_PATH", path):
            self.assertTrue(swap.load_settings()["auto_swap"]["claude"])


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
            swap.perform_swap(self.conn, "xai", self._target(), self._token())

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
        self.assertEqual(swap.cmd_swap(self.conn, "xai", self.target_id,
                                       force=True), 1)

    def test_claude_provider_mismatch_fails(self):
        # target_id is a codex account: asking for a claude swap onto it
        # must refuse before touching anything.
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


# ─── M5: claude swap (spec §3.4, behind auto_swap.claude) ───────────────────
FAKE_KEYCHAIN_OLD = {
    "claudeAiOauth": {
        "accessToken": "fake-old-access",
        "refreshToken": "fake-old-refresh",
        "expiresAt": 1000,
        "refreshTokenExpiresAt": 2000,
        "scopes": ["user:profile"],
        "subscriptionType": "pro",
        "rateLimitTier": "default_claude_ai",
    }
}

FAKE_RAW_TOKEN = {
    "scope": "user:profile user:inference",
    "account": {"uuid": "uuid-new", "email_address": "new@example.com"},
    "organization": {"uuid": "org-new", "name": "New Org"},
    "refresh_token_expires_in": 1000000,
}


def _fake_claude_config():
    return {
        "numStartups": 42,
        "projects": {"/x": {"history": []}},
        "mcpServers": {"foo": {"command": "bar"}},
        "oauthAccount": {"emailAddress": "old@example.com",
                         "accountUuid": "uuid-old",
                         "billingType": "stripe"},
    }


class ClaudeActiveEmailTests(unittest.TestCase):
    def _with_config(self, text):
        path = Path(tempfile.mkdtemp(prefix="tsb-claude-cfg-")) / "claude.json"
        if text is not None:
            path.write_text(text)
        with mock.patch.object(local_sync, "CLAUDE_CONFIG", path):
            return local_sync.claude_active_email()

    def test_reads_and_normalizes_email(self):
        got = self._with_config(json.dumps(
            {"oauthAccount": {"emailAddress": " Old@Example.COM "}}))
        self.assertEqual(got, "old@example.com")

    def test_missing_file_none(self):
        self.assertIsNone(self._with_config(None))

    def test_missing_oauth_account_none(self):
        self.assertIsNone(self._with_config(json.dumps({"numStartups": 1})))

    def test_non_object_config_none(self):
        self.assertIsNone(self._with_config(json.dumps([1, 2])))


class ClaudeCandidateTests(unittest.TestCase):
    def test_no_upstream_account_id_still_eligible(self):
        # Real claude pool rows can have account_id=None; identity is email.
        it = _item(aid=2, provider="claude", upstream=None,
                   email="b@example.com", binding=_win(used=10.0))
        out = swap.swap_candidates([it], "claude", None,
                                   active_email="a@example.com")
        self.assertEqual([x["id"] for x in out], [2])

    def test_active_excluded_by_email_case_insensitive(self):
        active = _item(aid=2, provider="claude", upstream=None,
                       email="Active@Example.com", binding=_win(used=10.0))
        other = _item(aid=3, provider="claude", upstream=None,
                      email="other@example.com", binding=_win(used=10.0))
        out = swap.swap_candidates([active, other], "claude", None,
                                   active_email="active@example.com")
        self.assertEqual([x["id"] for x in out], [3])

    def test_no_email_excluded(self):
        it = _item(aid=2, provider="claude", upstream=None, email="x")
        it["email"] = None
        self.assertEqual(swap.swap_candidates([it], "claude", None), [])


class ClaudeKeychainPayloadTests(unittest.TestCase):
    def _token(self, raw=FAKE_RAW_TOKEN, last_refresh=NOW):
        return {"access_token": "fake-claude-access",
                "refresh_token": "fake-claude-refresh",
                "expires_at": NOW + 7200, "last_refresh": last_refresh,
                "raw_json": json.dumps(raw) if raw is not None else None}

    def test_maps_token_row_and_preserves_extras(self):
        target = {"email": "new@example.com", "plan": "Claude Max"}
        existing = dict(FAKE_KEYCHAIN_OLD, somethingElse={"keep": True})
        out = swap._claude_keychain_payload(existing, target, self._token())
        oauth = out["claudeAiOauth"]
        self.assertEqual(oauth["accessToken"], "fake-claude-access")
        self.assertEqual(oauth["refreshToken"], "fake-claude-refresh")
        self.assertEqual(oauth["expiresAt"], int((NOW + 7200) * 1000))
        self.assertEqual(oauth["scopes"], ["user:profile", "user:inference"])
        self.assertEqual(oauth["subscriptionType"], "max")  # from plan
        self.assertEqual(oauth["rateLimitTier"], "default_claude_ai")  # kept
        self.assertEqual(oauth["refreshTokenExpiresAt"],
                         int((NOW + 1000000) * 1000))
        self.assertEqual(out["somethingElse"], {"keep": True})  # top-level kept

    def test_defaults_when_raw_missing(self):
        target = {"email": "new@example.com", "plan": None}
        out = swap._claude_keychain_payload({}, target, self._token(raw=None))
        oauth = out["claudeAiOauth"]
        self.assertEqual(oauth["scopes"], swap.DEFAULT_CLAUDE_SCOPES)
        self.assertNotIn("subscriptionType", oauth)  # unknown plan → not set
        self.assertNotIn("refreshTokenExpiresAt", oauth)

    def test_unknown_plan_keeps_existing_subscription_type(self):
        target = {"email": "new@example.com", "plan": "Mystery Tier"}
        out = swap._claude_keychain_payload(FAKE_KEYCHAIN_OLD, target,
                                            self._token())
        self.assertEqual(out["claudeAiOauth"]["subscriptionType"], "pro")


class ClaudeKeychainWriteTests(unittest.TestCase):
    def test_secret_travels_via_stdin_not_argv(self):
        secret = json.dumps({"claudeAiOauth": {"accessToken": "fake-secret"}})
        ok = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(swap.subprocess, "run", return_value=ok) as run:
            swap._keychain_write("Claude Code-credentials", "tester", secret)
        run.assert_called_once()
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["security", "-i"])
        self.assertNotIn("fake-secret", " ".join(argv))
        stdin = run.call_args[1]["input"]
        self.assertIn("add-generic-password -U", stdin)
        self.assertIn('-s "Claude Code-credentials"', stdin)
        self.assertIn('-a "tester"', stdin)
        self.assertIn("fake-secret", stdin)
        # JSON quotes are escaped for security(1)'s parser.
        self.assertIn('\\"accessToken\\"', stdin)

    def test_nonzero_rc_raises(self):
        bad = mock.Mock(returncode=1, stdout="", stderr="boom")
        with mock.patch.object(swap.subprocess, "run", return_value=bad):
            with self.assertRaises(RuntimeError):
                swap._keychain_write("svc", "acct", "s")

    def test_escape_roundtrip_characters(self):
        self.assertEqual(swap._security_escape('a"b\\c'), 'a\\"b\\\\c')


class ClaudePerformSwapTests(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect()
        for table in ("lifecycle_events", "live_activity", "tokens", "accounts"):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.commit()
        self.backups = Path(tempfile.mkdtemp(prefix="tsb-backups-"))
        self.config = Path(tempfile.mkdtemp(prefix="tsb-claude-cfg-")) / "claude.json"
        self.config.write_text(json.dumps(_fake_claude_config()))
        self.old_id = store.upsert_account(self.conn, "claude", "old@example.com",
                                           "claude #1", "Claude Pro", None)
        self.new_id = store.upsert_account(self.conn, "claude", "new@example.com",
                                           "claude #2", "Claude Max", None)
        store.save_token(self.conn, self.new_id, "fake-claude-access",
                         "fake-claude-refresh", "", time.time() + 7200,
                         FAKE_RAW_TOKEN)
        self._patches = [
            mock.patch.object(local_sync, "CLAUDE_CONFIG", self.config),
            mock.patch.object(swap, "BACKUP_DIR", self.backups),
            mock.patch.object(swap, "_notify"),
            mock.patch.object(swap, "_keychain_read",
                              return_value=json.dumps(FAKE_KEYCHAIN_OLD)),
            mock.patch.object(swap, "_keychain_account_attr",
                              return_value="tester"),
            mock.patch.object(swap, "_keychain_write"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.conn.close()

    def _swap(self, **kw):
        return swap.perform_swap(self.conn, "claude",
                                 store.get_account(self.conn, self.new_id),
                                 store.get_token(self.conn, self.new_id), **kw)

    def test_keychain_written_with_mapped_payload(self):
        self._swap()
        swap._keychain_write.assert_called_once()
        service, acct, secret = swap._keychain_write.call_args[0]
        self.assertEqual(service, "Claude Code-credentials")
        self.assertEqual(acct, "tester")
        oauth = json.loads(secret)["claudeAiOauth"]
        self.assertEqual(oauth["accessToken"], "fake-claude-access")
        self.assertEqual(oauth["refreshToken"], "fake-claude-refresh")
        self.assertEqual(oauth["subscriptionType"], "max")
        self.assertEqual(oauth["rateLimitTier"], "default_claude_ai")

    def test_backup_of_keychain_json_0600(self):
        self._swap()
        backups = list(self.backups.glob("claude-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), FAKE_KEYCHAIN_OLD)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)

    def test_config_patch_preserves_unrelated_keys(self):
        self._swap()
        cfg = json.loads(self.config.read_text())
        self.assertEqual(cfg["numStartups"], 42)
        self.assertEqual(cfg["projects"], {"/x": {"history": []}})
        self.assertEqual(cfg["mcpServers"], {"foo": {"command": "bar"}})
        oa = cfg["oauthAccount"]
        self.assertEqual(oa["emailAddress"], "new@example.com")
        self.assertEqual(oa["accountUuid"], "uuid-new")
        self.assertEqual(oa["organizationUuid"], "org-new")
        self.assertEqual(oa["organizationName"], "New Org")
        self.assertEqual(oa["billingType"], "stripe")  # unrelated field kept

    def test_lifecycle_event_and_summary(self):
        out = self._swap()
        ev = store.latest_lifecycle_event(self.conn, "account_swapped")
        detail = json.loads(ev["detail"])
        self.assertEqual(detail, {"provider": "claude",
                                  "from_email": "old@example.com",
                                  "to_email": "new@example.com",
                                  "from_account_id": "uuid-old",
                                  "to_account_id": "uuid-new"})
        self.assertEqual(out["provider"], "claude")
        self.assertTrue(out["backup"].endswith(".json"))

    def test_notification_short_emails_no_tokens(self):
        self._swap(headroom_note="62% weekly left")
        msg = swap._notify.call_args[0][0]
        self.assertIn("Claude swapped: old@ → new@", msg)
        self.assertIn("(62% weekly left)", msg)
        self.assertNotIn("fake-claude-access", msg)

    def test_missing_keychain_item_no_backup_still_swaps(self):
        swap._keychain_read.return_value = None
        self._swap()
        self.assertEqual(list(self.backups.glob("*.json")), [])
        swap._keychain_write.assert_called_once()

    def test_unparseable_config_left_untouched(self):
        self.config.write_text("{not json")
        self._swap()  # must not raise
        self.assertEqual(self.config.read_text(), "{not json")

    def test_target_without_email_raises(self):
        target = {"id": 99, "provider": "claude", "email": None, "plan": None}
        token = {"access_token": "fake", "expires_at": time.time() + 7200}
        with self.assertRaises(ValueError):
            swap.perform_swap(self.conn, "claude", target, token)


class ClaudeAutoSwapTickTests(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect()
        for table in ("lifecycle_events", "live_activity", "tokens", "accounts"):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.commit()
        self.backups = Path(tempfile.mkdtemp(prefix="tsb-backups-"))
        tmp = Path(tempfile.mkdtemp(prefix="tsb-claude-tick-"))
        self.config = tmp / "claude.json"
        self.config.write_text(json.dumps(_fake_claude_config()))
        self.active_id = store.upsert_account(self.conn, "claude", "old@example.com",
                                              "claude #1", "Claude Pro", None)
        self.target_id = store.upsert_account(self.conn, "claude", "new@example.com",
                                              "claude #2", "Claude Max", None)
        store.save_token(self.conn, self.target_id, "fake-claude-access",
                         "fake-claude-refresh", "", time.time() + 7200,
                         FAKE_RAW_TOKEN)
        self._patches = [
            mock.patch.object(local_sync, "CLAUDE_CONFIG", self.config),
            mock.patch.object(local_sync, "CODEX_AUTH", tmp / "missing-auth.json"),
            mock.patch.object(swap, "BACKUP_DIR", self.backups),
            mock.patch.object(swap, "_notify"),
            mock.patch.object(swap, "_keychain_read",
                              return_value=json.dumps(FAKE_KEYCHAIN_OLD)),
            mock.patch.object(swap, "_keychain_account_attr",
                              return_value="tester"),
            mock.patch.object(swap, "_keychain_write"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.conn.close()

    def _payload(self, now):
        active = _exhausted_item(aid=self.active_id, upstream=None,
                                 email="old@example.com",
                                 last_poll_epoch=now - 30)
        for w in active["windows"]:
            w["as_of_epoch"] = now - 30
        active["provider"] = "claude"
        cand = _item(aid=self.target_id, provider="claude", upstream=None,
                     email="new@example.com",
                     binding=_win(used=38.0, kind="weekly"))
        return {"accounts": [active, cand]}

    def test_kill_switch_default_off_blocks_auto_path(self):
        # Fresh settings file → real defaults: claude must be OFF.
        settings_path = Path(tempfile.mkdtemp(prefix="tsb-set-")) / "settings.json"
        now = time.time()
        with mock.patch.object(swap, "SETTINGS_PATH", settings_path):
            out = swap.auto_swap_tick(self.conn, self._payload(now), now=now)
        self.assertIsNone(out)
        swap._keychain_write.assert_not_called()
        self.assertEqual(self.config.read_text(),
                         json.dumps(_fake_claude_config()))  # untouched

    def test_enabled_swaps_matching_active_by_email(self):
        now = time.time()
        with mock.patch.object(swap, "load_settings",
                               return_value={"auto_swap": {"claude": True}}):
            out = swap.auto_swap_tick(self.conn, self._payload(now), now=now)
        self.assertIsNotNone(out)
        self.assertEqual(out["provider"], "claude")
        self.assertEqual(out["to_email"], "new@example.com")
        swap._keychain_write.assert_called_once()
        cfg = json.loads(self.config.read_text())
        self.assertEqual(cfg["oauthAccount"]["emailAddress"], "new@example.com")
        msg = swap._notify.call_args[0][0]
        self.assertIn("62% weekly left", msg)

    def test_no_active_claude_email_no_swap(self):
        self.config.unlink()
        now = time.time()
        with mock.patch.object(swap, "load_settings",
                               return_value={"auto_swap": {"claude": True}}):
            out = swap.auto_swap_tick(self.conn, self._payload(now), now=now)
        self.assertIsNone(out)
        swap._keychain_write.assert_not_called()


class ClaudeCmdSwapTests(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect()
        for table in ("lifecycle_events", "live_activity", "tokens", "accounts"):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.commit()
        self.backups = Path(tempfile.mkdtemp(prefix="tsb-backups-"))
        self.config = Path(tempfile.mkdtemp(prefix="tsb-claude-cmd-")) / "claude.json"
        self.config.write_text(json.dumps(_fake_claude_config()))
        self.active_id = store.upsert_account(self.conn, "claude", "old@example.com",
                                              "claude #1", "Claude Pro", None)
        self.target_id = store.upsert_account(self.conn, "claude", "new@example.com",
                                              "claude #2", "Claude Max", None)
        store.save_token(self.conn, self.target_id, "fake-claude-access",
                         "fake-claude-refresh", "", time.time() + 7200,
                         FAKE_RAW_TOKEN)
        self._patches = [
            mock.patch.object(local_sync, "CLAUDE_CONFIG", self.config),
            mock.patch.object(swap, "BACKUP_DIR", self.backups),
            mock.patch.object(swap, "_notify"),
            mock.patch.object(swap, "_keychain_read",
                              return_value=json.dumps(FAKE_KEYCHAIN_OLD)),
            mock.patch.object(swap, "_keychain_account_attr",
                              return_value="tester"),
            mock.patch.object(swap, "_keychain_write"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.conn.close()

    def test_force_swaps(self):
        rc = swap.cmd_swap(self.conn, "claude", self.target_id, force=True)
        self.assertEqual(rc, 0)
        swap._keychain_write.assert_called_once()
        cfg = json.loads(self.config.read_text())
        self.assertEqual(cfg["oauthAccount"]["emailAddress"], "new@example.com")
        self.assertEqual(len(list(self.backups.glob("claude-*.json"))), 1)

    def test_swap_to_already_active_is_noop(self):
        store.save_token(self.conn, self.active_id, "fake-a", "fake-r", "",
                         time.time() + 7200, FAKE_RAW_TOKEN)
        rc = swap.cmd_swap(self.conn, "claude", self.active_id, force=True)
        self.assertEqual(rc, 0)
        swap._keychain_write.assert_not_called()
        self.assertIsNone(store.latest_lifecycle_event(self.conn, "account_swapped"))

    def test_non_force_refused_without_exhaustion_evidence(self):
        with mock.patch.object(swap, "load_settings",
                               return_value={"auto_swap": {"claude": True}}):
            rc = swap.cmd_swap(self.conn, "claude", self.target_id, force=False)
        self.assertEqual(rc, 1)
        swap._keychain_write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
