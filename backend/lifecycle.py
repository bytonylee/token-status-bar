#!/usr/bin/env python3
"""Lifecycle transition events — diff two consecutive export payloads (spec §1.4).

detect_transitions() is pure (no I/O): the poller daemon keeps the previous
status.json payload in memory, calls this after every export, and persists
each emitted event via store.save_lifecycle_event(). The lifecycle_events
table is the audit trail now and powers notifications later (M7); the swap
engine (§3.2) appends its own "account_swapped" rows to the same table.
"""
from __future__ import annotations

# account_state() reports "renews_soon" for a paid subscription near its
# billing anniversary (status.py §1.2) — for transition purposes it is paid.
_PAID = ("paid", "renews_soon")
_SUB_PAID_FROM = ("unknown", "free", "expired")


def _sub(item) -> str:
    s = (item.get("state") or {}).get("subscription") or "unknown"
    return "paid" if s in _PAID else s


def _quota(item) -> str:
    return (item.get("state") or {}).get("quota") or "unknown"


def _windows_by_key(item) -> dict:
    """Windows keyed by (kind, label) — the stable identity across polls."""
    out = {}
    for w in item.get("windows") or []:
        out.setdefault((w.get("kind"), w.get("label")), w)
    return out


def _window_events(aid, prev, cur) -> list[dict]:
    out = []
    prev_wins = _windows_by_key(prev)
    for key, w in _windows_by_key(cur).items():
        pw = prev_wins.get(key)
        if pw is None or pw.get("phase") != "live":
            continue  # brand-new window, or one that had already reset
        flipped = w.get("phase") == "reset"
        p_reset, c_reset = pw.get("reset_at_epoch"), w.get("reset_at_epoch")
        rolled = p_reset is not None and c_reset is not None and c_reset > p_reset
        if flipped or rolled:
            kind, label = key
            out.append({"account_id": aid, "event": "window_reset",
                        "detail": {"kind": kind, "label": label,
                                   "old_used_pct": pw.get(
                                       "used_pct_effective", pw.get("used_pct"))}})
    return out


def _subscription_events(aid, prev, cur) -> list[dict]:
    p, c = _sub(prev), _sub(cur)
    if p in _SUB_PAID_FROM and c == "paid":
        return [{"account_id": aid, "event": "sub_paid",
                 "detail": {"from": p, "to": "paid"}}]
    if p == "paid" and c == "expired":
        return [{"account_id": aid, "event": "sub_expired",
                 "detail": {"from": "paid", "to": "expired"}}]
    if p == "paid" and c == "paid":
        pr = (prev.get("state") or {}).get("sub_renews_at")
        cr = (cur.get("state") or {}).get("sub_renews_at")
        # account_state() emits both in one ISO format, so lexicographic
        # comparison is chronological; a later date means the anniversary
        # passed while the subscription stayed paid.
        if pr and cr and cr > pr:
            return [{"account_id": aid, "event": "sub_renewed",
                     "detail": {"renews_at": cr, "prev_renews_at": pr}}]
    return []


def _quota_events(aid, prev, cur) -> list[dict]:
    p, c = _quota(prev), _quota(cur)
    if c == "exhausted" and p != "exhausted":
        return [{"account_id": aid, "event": "quota_exhausted",
                 "detail": {"from": p}}]
    if p == "exhausted" and c in ("ok", "warning"):
        return [{"account_id": aid, "event": "quota_recovered",
                 "detail": {"to": c}}]
    return []


def detect_transitions(prev_payload, next_payload, now=None) -> list[dict]:
    """Lifecycle events between two consecutive export payloads.

    Both arguments are the dict cmd_export writes ({"generated_at",
    "accounts": [...], ...}); prev_payload is None on the daemon's first
    export (no baseline → no events). Returns
    [{"account_id", "event", "detail"}, ...] with detail a small dict.

    Emitted events: window_reset (a (kind, label) window flipped phase
    live→reset, or its reset_at_epoch rolled forward while prev was live),
    sub_paid, sub_expired, sub_renewed, quota_exhausted, quota_recovered.

    Pure and idempotent — detect_transitions(x, x) == []. Accounts are
    matched by id; unmatched (new) accounts emit nothing. `now` is accepted
    for signature stability; detection only compares the two payloads.
    """
    if not prev_payload:
        return []
    prev_by_id = {a.get("id"): a for a in prev_payload.get("accounts") or []}
    events = []
    for cur in (next_payload or {}).get("accounts") or []:
        prev = prev_by_id.get(cur.get("id"))
        if prev is None:
            continue
        aid = cur.get("id")
        events += _window_events(aid, prev, cur)
        events += _subscription_events(aid, prev, cur)
        events += _quota_events(aid, prev, cur)
    return events
