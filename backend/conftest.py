"""Pytest bootstrap: isolate backend modules from the real DB/exports.

Backend modules resolve AGENT_POOL_* paths at import time, so whichever test
module pytest collects first decides the paths for the whole run. Each test
module sets these env vars itself for standalone `python3 test_x.py` runs;
this conftest only fills in what is still unset (defensive setdefault) so
collection order can never leak the real pool.db / status.json / history dir
into a test run.
"""
from __future__ import annotations
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TMP = tempfile.mkdtemp(prefix="tsb-conftest-")
for _key, _default in (
    ("AGENT_POOL_DB", os.path.join(_TMP, "pool.db")),
    ("AGENT_POOL_STATUS_JSON", os.path.join(_TMP, "status.json")),
    ("AGENT_POOL_HISTORY_DIR", os.path.join(_TMP, "history")),
    ("AGENT_POOL_SETTINGS", os.path.join(_TMP, "settings.json")),
    ("AGENT_POOL_SWAP_BACKUPS", os.path.join(_TMP, "swap_backups")),
):
    os.environ.setdefault(_key, _default)
