"""Tests for burn-rate projection."""
from __future__ import annotations
import os, sys, tempfile, unittest

_TMP = tempfile.mkdtemp(prefix="tsb-test-")
os.environ["AGENT_POOL_DB"] = os.path.join(_TMP, "pool.db")
os.environ["AGENT_POOL_STATUS_JSON"] = os.path.join(_TMP, "status.json")
os.environ["AGENT_POOL_HISTORY_DIR"] = os.path.join(_TMP, "history")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import status  # noqa: E402


class ProjectExhaustTest(unittest.TestCase):
    def test_projects_when_pace_beats_reset(self):
        # 10% per hour starting at 40%: 100% reached in 6h; reset in 8h.
        pts = [(0.0, 40.0), (1800.0, 45.0), (3600.0, 50.0)]
        out = status.project_exhaust(pts, reset_at=3600.0 + 8 * 3600, now=3600.0)
        self.assertIsNotNone(out)
        self.assertAlmostEqual(out, 3600.0 + (50.0 / (10.0 / 3600.0)), delta=60)

    def test_none_when_reset_comes_first(self):
        pts = [(0.0, 40.0), (1800.0, 41.0), (3600.0, 42.0)]
        self.assertIsNone(status.project_exhaust(pts, reset_at=3600.0 + 3600, now=3600.0))

    def test_none_when_flat_or_decreasing(self):
        self.assertIsNone(status.project_exhaust(
            [(0.0, 40.0), (1800.0, 40.0), (3600.0, 40.0)], reset_at=99999.0, now=3600.0))

    def test_reset_drop_trims_series(self):
        # A reset mid-series: only the post-reset segment counts (2 pts < min 3).
        pts = [(0.0, 90.0), (1800.0, 95.0), (3600.0, 5.0), (5400.0, 10.0)]
        self.assertIsNone(status.project_exhaust(pts, reset_at=99999.0, now=5400.0))

    def test_too_few_points(self):
        self.assertIsNone(status.project_exhaust([(0.0, 40.0)], reset_at=9999.0, now=0.0))

    def test_no_reset_at_projects_nothing(self):
        pts = [(0.0, 40.0), (1800.0, 45.0), (3600.0, 50.0)]
        self.assertIsNone(status.project_exhaust(pts, reset_at=None, now=3600.0))


if __name__ == "__main__":
    unittest.main()
