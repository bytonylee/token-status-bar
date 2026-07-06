"""Single-worker guards for poll and refresh jobs."""
from __future__ import annotations
from contextlib import contextmanager
import fcntl
from pathlib import Path
import store


@contextmanager
def single_worker(name: str):
    """Yield True only when this process owns the named worker lock."""
    store.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    path = store.DB_PATH.parent / f"{name}.lock"
    with open(path, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
