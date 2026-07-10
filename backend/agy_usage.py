"""Antigravity 5h/weekly quota windows, scraped from the `agy` CLI.

The cloudcode retrieveUserQuotaSummary API returns only a collapsed
per-model view for our OAuth client; the Weekly/Five-Hour breakdown per
model group is rendered only by the CLI's /usage panel. This module drives
the interactive CLI in a pseudo-terminal, sends /usage, captures the panel
text, and parses it. Every failure path returns None so the poller can
fall back to the API view.
"""
from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import shutil
import struct
import termios
import time

PANEL_TIMEOUT_S = 60

_ANSI_RE = re.compile(r"\x1b\[[0-9;?$ ]*[A-Za-z]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[=>]")
_GROUP_RE = re.compile(r"^\s*([A-Z][A-Z &/]* MODELS)\s*$")
_WINDOW_RE = re.compile(r"^\s*(Weekly|Five Hour) Limit\s*$")
_BAR_PCT_RE = re.compile(r"\]\s*([0-9]+(?:\.[0-9]+)?)%")
_REFRESH_RE = re.compile(r"Refreshes in\s+(?:(\d+)h)?\s*(?:(\d+)m)?")


def parse_usage_panel(text: str, now: float | None = None):
    """Parse the /usage panel text into quota windows.

    Returns [{"group": "gemini"|"other", "window": "5h"|"weekly",
              "remaining_pct": float, "reset_at": epoch_or_None}]
    or None when no windows were found.
    """
    now = time.time() if now is None else now
    windows = []
    group = None
    window = None
    bar_pct = None
    for raw in text.splitlines():
        line = raw.rstrip()
        m = _GROUP_RE.match(line)
        if m:
            group = "gemini" if "GEMINI" in m.group(1) else "other"
            window = None
            continue
        if group is None:
            continue
        m = _WINDOW_RE.match(line)
        if m:
            window = "weekly" if m.group(1) == "Weekly" else "5h"
            bar_pct = None
            continue
        if window is None:
            continue
        m = _BAR_PCT_RE.search(line)
        if m:
            bar_pct = float(m.group(1))
            continue
        if "Quota available" in line:
            windows.append({"group": group, "window": window,
                            "remaining_pct": bar_pct if bar_pct is not None else 100.0,
                            "reset_at": None})
            window = None
            continue
        m = _REFRESH_RE.search(line)
        if m and (m.group(1) or m.group(2)):
            secs = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60
            windows.append({"group": group, "window": window,
                            "remaining_pct": bar_pct if bar_pct is not None else 100.0,
                            "reset_at": now + secs})
            window = None
    return windows or None


def _agy_path():
    return shutil.which("agy") or os.path.expanduser("~/.local/bin/agy")


def _read_for(fd, secs, stop: bytes | None = None) -> bytes:
    end = time.time() + secs
    chunks: list[bytes] = []
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.5)
        if not r:
            continue
        try:
            d = os.read(fd, 65536)
        except OSError:
            break
        if not d:
            break
        chunks.append(d)
        if stop and stop in b"".join(chunks[-3:]):
            break
    return b"".join(chunks)


def fetch_usage(timeout_s: int = PANEL_TIMEOUT_S):
    """Drive `agy` /usage in a pty and return parsed windows, or None."""
    agy = _agy_path()
    if not agy or not os.path.exists(agy):
        return None
    try:
        return _drive(agy, timeout_s)
    except Exception:
        return None


def _drive(agy: str, timeout_s: int):
    pid, fd = pty.fork()
    if pid == 0:  # child: exec agy with a sane TERM, outside any repo
        try:
            os.environ["TERM"] = "xterm-256color"
            try:
                os.chdir("/tmp")
            except OSError:
                pass
            os.execv(agy, [agy])
        finally:
            os._exit(1)
    buf = b""
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 160, 0, 0))
        # Wait for the interactive prompt ("? for shortcuts" footer).
        buf += _read_for(fd, min(15, timeout_s), stop=b"shortcuts")
        for ch in "/usage":
            os.write(fd, ch.encode())
            time.sleep(0.15)
        time.sleep(1.0)
        os.write(fd, b"\r")
        # The panel ends with an explainer starting "Within each group".
        buf += _read_for(fd, min(20, timeout_s), stop=b"Within each group")
    finally:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
    text = _ANSI_RE.sub("", buf.decode(errors="replace"))
    return parse_usage_panel(text)
