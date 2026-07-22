"""Tests for local session-log sync (pure helpers)."""
from __future__ import annotations
import json, os, sys, tempfile, unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="tsb-test-")
os.environ["AGENT_POOL_DB"] = os.path.join(_TMP, "pool.db")
os.environ["AGENT_POOL_STATUS_JSON"] = os.path.join(_TMP, "status.json")
os.environ["AGENT_POOL_HISTORY_DIR"] = os.path.join(_TMP, "history")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import local_sync  # noqa: E402

CODEX_EVENT = {
    "timestamp": "2026-07-16T13:45:00.000Z",
    "type": "event_msg",
    "payload": {
        "type": "token_count",
        "info": {
            "total_token_usage": {"input_tokens": 43961548, "cached_input_tokens": 40634112,
                                  "output_tokens": 107495, "reasoning_output_tokens": 27705,
                                  "total_tokens": 44069043},
            "last_token_usage": {"input_tokens": 98745, "cached_input_tokens": 97024,
                                 "output_tokens": 1968, "reasoning_output_tokens": 76,
                                 "total_tokens": 100713},
            "model_context_window": 258400,
        },
        "rate_limits": {
            "limit_id": "codex", "limit_name": None,
            "primary": {"used_percent": 6.0, "window_minutes": 10080,
                        "resets_at": 1784781194},
            "secondary": None,
            "credits": {"has_credits": True, "unlimited": False, "balance": "2500"},
            "plan_type": "pro",
        },
    },
}


class ExtractTokenCountTest(unittest.TestCase):
    def test_wrapped_payload(self):
        out = local_sync.extract_token_count(CODEX_EVENT)
        self.assertIsNotNone(out)
        self.assertEqual(out["rate_limits"]["primary"]["used_percent"], 6.0)

    def test_bare_payload(self):
        out = local_sync.extract_token_count(CODEX_EVENT["payload"])
        self.assertIsNotNone(out)

    def test_other_event_returns_none(self):
        self.assertIsNone(local_sync.extract_token_count({"type": "session_meta"}))


class CodexSnapTest(unittest.TestCase):
    def test_snapshot_fields(self):
        snap = local_sync.codex_snap(CODEX_EVENT["payload"]["rate_limits"])
        self.assertEqual(snap["source"], "local")
        self.assertEqual(snap["status"], "active")
        self.assertEqual(snap["primary_used_pct"], 6.0)
        self.assertEqual(snap["primary_window_s"], 10080 * 60)
        self.assertEqual(snap["primary_reset_at"], 1784781194.0)
        self.assertEqual(snap["credits_balance"], 2500.0)
        self.assertEqual(snap["plan"], "pro")
        self.assertNotIn("secondary_used_pct", snap)


class MatchAccountTest(unittest.TestCase):
    def test_match_and_miss(self):
        accounts = [{"id": 1, "provider": "codex", "account_id": "abc"},
                    {"id": 2, "provider": "claude", "account_id": "abc"},
                    {"id": 3, "provider": "codex", "account_id": "xyz"}]
        self.assertEqual(local_sync.match_codex_account(accounts, "xyz")["id"], 3)
        self.assertIsNone(local_sync.match_codex_account(accounts, "nope"))
        self.assertIsNone(local_sync.match_codex_account(accounts, None))


class TailLinesTest(unittest.TestCase):
    def test_tail_drops_partial_first_line(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.jsonl"
            lines = [json.dumps({"i": i, "pad": "x" * 100}) for i in range(100)]
            p.write_text("\n".join(lines) + "\n")
            out = local_sync.tail_lines(p, 1024)
            self.assertGreater(len(out), 2)
            json.loads(out[0])
            self.assertEqual(json.loads(out[-1])["i"], 99)


class ClaudeUsageTotalsTest(unittest.TestCase):
    def test_sums_recent_usage(self):
        mk = lambda ts, inp, out_t: json.dumps({
            "type": "assistant", "timestamp": ts,
            "message": {"usage": {"input_tokens": inp, "output_tokens": out_t,
                                  "cache_creation_input_tokens": 10,
                                  "cache_read_input_tokens": 999}}})
        lines = [mk("2026-07-16T13:00:00.000Z", 100, 50),
                 mk("2026-07-16T13:30:00.000Z", 200, 60),
                 "not json", json.dumps({"type": "user"})]
        since = local_sync._iso_epoch("2026-07-16T13:10:00.000Z")
        totals = local_sync.claude_usage_totals(lines, since)
        self.assertEqual(totals["tokens_60m"], 200 + 60 + 10)
        self.assertAlmostEqual(totals["last_event_epoch"],
                               local_sync._iso_epoch("2026-07-16T13:30:00.000Z"))

    def test_skips_nonnumeric_usage_without_crashing(self):
        lines = [json.dumps({
            "type": "assistant", "timestamp": "2026-07-16T13:30:00.000Z",
            "message": {"usage": {"input_tokens": "lots", "output_tokens": None,
                                  "cache_creation_input_tokens": {"oops": 1}}}}),
                 json.dumps({
            "type": "assistant", "timestamp": "2026-07-16T13:31:00.000Z",
            "message": {"usage": {"input_tokens": 100, "output_tokens": 50}}})]
        totals = local_sync.claude_usage_totals(lines, 0.0)
        self.assertEqual(totals["tokens_60m"], 150)  # malformed record contributes 0


class ContextUsedPctTests(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(local_sync.context_used_pct(200000, 50000), 25.0)

    def test_zero_window_returns_none(self):
        self.assertIsNone(local_sync.context_used_pct(0, 50000))

    def test_negative_or_nonnumeric_returns_none(self):
        self.assertIsNone(local_sync.context_used_pct(-1, 50000))
        self.assertIsNone(local_sync.context_used_pct("big", 50000))
        self.assertIsNone(local_sync.context_used_pct(200000, None))

    def test_caps_at_100(self):
        self.assertEqual(local_sync.context_used_pct(1000, 5000), 100.0)


if __name__ == "__main__":
    unittest.main()
