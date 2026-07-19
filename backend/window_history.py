"""Closed quota-window detection for window history.

detect_closed_windows() is a pure function shared verbatim by the live poll
hook and the one-time backfill: it compares two consecutive successful
snapshots of one account and returns the quota windows that closed between
them. Error snapshots never participate, so a connection failure can never
fake or corrupt a reset.
"""
from __future__ import annotations
import csv, datetime, json, os, time, zoneinfo
from dataclasses import dataclass, field
from pathlib import Path
import store

HISTORY_DIR = Path(os.environ.get("AGENT_POOL_HISTORY_DIR",
                                  str(Path.home() / "solo/token-status-bar" / "history")))
KST = zoneinfo.ZoneInfo("Asia/Seoul")
CSV_FIELDS = ("email", "label", "window_kind", "window_start", "window_end",
              "final_used_pct", "reset_cause", "final_snapshot_ts", "staleness_s")

RESET_TOLERANCE_S = 120     # reset_at must move forward by more than this
EARLY_RESET_S = 600         # roll observed >10 min before the old boundary = early
DROP_THRESHOLD_PCT = 10.0   # used-pct drop that closes a timestamp-less window
BOUNDARY_MATCH_S = 3600     # drop within 1h of a known boundary = natural
SUCCESS_STATUSES = ("active", "rate_limited")


@dataclass
class ClosedWindow:
    window_kind: str            # 5h|weekly|weekly_fable|daily|monthly|monthly_premium|monthly_chat
    window_start: float | None  # unix; reset_at - window_s when known
    window_end: float           # boundary the window closed at
    final_used_pct: float       # last successful reading before close
    final_snapshot_ts: float    # ts of the snapshot supplying it
    reset_cause: str            # natural|coupon|provider_reset|unknown
    details: dict = field(default_factory=dict)


def _parse_iso_ts(s) -> float | None:
    if not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def _fable(snap) -> dict | None:
    """Claude's model-scoped weekly window from raw_json (best-effort)."""
    try:
        rj = json.loads(snap.get("raw_json") or "{}")
    except Exception:
        return None
    f = rj.get("fable") if isinstance(rj, dict) else None
    if isinstance(f, dict) and f.get("used_pct") is not None and f.get("reset_at"):
        return f
    return None


def timed_windows(provider, snap) -> list[dict]:
    """Snapshot windows that carry a reset timestamp (detection rule 2)."""
    out: list[dict] = []

    def add(kind, used, reset_at, window_s, start=None):
        if used is None or not reset_at:
            return
        try:
            used_pct, reset_ts = float(used), float(reset_at)
        except (TypeError, ValueError):
            return  # a malformed reading skips its window, never aborts the sweep
        out.append({"kind": kind, "used_pct": used_pct, "reset_at": reset_ts,
                    "window_s": window_s, "start": start})

    if provider in ("codex", "claude", "antigravity"):
        add("5h", snap.get("primary_used_pct"), snap.get("primary_reset_at"),
            snap.get("primary_window_s"))
        add("weekly", snap.get("secondary_used_pct"), snap.get("secondary_reset_at"),
            snap.get("secondary_window_s"))
        if provider == "claude":
            f = _fable(snap)
            if f:
                add("weekly_fable", f.get("used_pct"), f.get("reset_at"), 604800)
    elif provider == "copilot":
        add("monthly_premium", snap.get("primary_used_pct"), snap.get("primary_reset_at"), None)
    elif provider == "xai":
        add("monthly", snap.get("monthly_used_pct"),
            _parse_iso_ts(snap.get("monthly_period_end")), None,
            start=_parse_iso_ts(snap.get("monthly_period_start")))
    return out


def drop_windows(provider, snap) -> list[dict]:
    """Snapshot windows without a reset timestamp (detection rule 3).

    boundary is a unix ts hint or "midnight" for devin's daily window.
    """
    out: list[dict] = []
    if provider == "copilot" and snap.get("secondary_used_pct") is not None:
        out.append({"kind": "monthly_chat", "used_pct": float(snap["secondary_used_pct"]),
                    "boundary": snap.get("primary_reset_at")})
    elif provider == "devin":
        if snap.get("daily_quota_remaining_percent") is not None:
            out.append({"kind": "daily",
                        "used_pct": 100.0 - float(snap["daily_quota_remaining_percent"]),
                        "boundary": "midnight"})
        if snap.get("weekly_quota_remaining_percent") is not None:
            out.append({"kind": "weekly",
                        "used_pct": 100.0 - float(snap["weekly_quota_remaining_percent"]),
                        "boundary": snap.get("plan_reset_unix")})
    return out


def _banked_decreased(prev_snap, new_snap) -> bool:
    b0, b1 = prev_snap.get("banked_resets"), new_snap.get("banked_resets")
    return b0 is not None and b1 is not None and b1 < b0


def _near_boundary(prev_ts, new_ts, boundary_ts) -> bool:
    return (boundary_ts is not None
            and prev_ts - BOUNDARY_MATCH_S <= float(boundary_ts) <= new_ts + BOUNDARY_MATCH_S)


def _near_local_midnight(prev_ts, new_ts) -> bool:
    day = datetime.datetime.fromtimestamp(new_ts).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return any(_near_boundary(prev_ts, new_ts, (day + datetime.timedelta(days=n)).timestamp())
               for n in (0, 1))


def detect_closed_windows(provider, prev_snap, new_snap, *, coupon_hint=False) -> list[ClosedWindow]:
    """Windows that closed between two consecutive successful snapshots.

    Rule 1: only successful snapshots participate (status active/rate_limited).
    Rule 2: a window with a reset timestamp closed when reset_at moved forward
            by more than RESET_TOLERANCE_S. Natural when the roll was observed
            near or after the old boundary (poll gaps spanning it stay natural,
            with details.staleness_s recording how stale the final reading is);
            when observed more than EARLY_RESET_S before the old boundary it is
            coupon (with evidence: banked_resets decreased, or coupon_hint from
            a reset_credits flip) else provider_reset, and window_end is the
            midpoint of the two snapshot timestamps.
    Rule 3: a window without a reset timestamp closed when used_pct dropped by
            more than DROP_THRESHOLD_PCT. Natural when a related boundary
            (copilot quota_reset_date, devin plan_reset_unix, local midnight
            for daily) falls within BOUNDARY_MATCH_S of the pair, else unknown.
    Zero-usage windows (final_used_pct <= 0) are never reported.
    """
    if not prev_snap or not new_snap:
        return []
    if prev_snap.get("status") not in SUCCESS_STATUSES:
        return []
    if new_snap.get("status") not in SUCCESS_STATUSES:
        return []
    prev_ts, new_ts = float(prev_snap["ts"]), float(new_snap["ts"])
    if new_ts <= prev_ts:
        return []
    closed: list[ClosedWindow] = []
    coupon_evidence = coupon_hint or _banked_decreased(prev_snap, new_snap)

    new_timed = {w["kind"]: w for w in timed_windows(provider, new_snap)}
    for w in timed_windows(provider, prev_snap):
        nw = new_timed.get(w["kind"])
        if nw is None or w["used_pct"] <= 0:
            continue
        r_old, r_new = w["reset_at"], nw["reset_at"]
        if r_new - r_old <= RESET_TOLERANCE_S:
            continue
        # A reset_at that merely tracks "now" (an idle account, or a sleep/outage
        # gap while the boundary slides) moves forward by ≈ the poll gap. A real
        # reset jumps the boundary by the elapsed part of the window — far more
        # than the gap — so require that for both natural and early closes. The
        # rare true single roll seen only across a gap >= window_s is suppressed
        # too; its final reading was already maximally stale.
        if r_new - r_old <= new_ts - prev_ts + RESET_TOLERANCE_S:
            continue
        if new_ts < r_old - EARLY_RESET_S:
            # An early reset also demands a usage drop; a sliding boundary shows
            # none, so a flat/climbing reading within one gap is not a close.
            if nw["used_pct"] >= w["used_pct"]:
                continue
            window_end = (prev_ts + new_ts) / 2
            cause = "coupon" if coupon_evidence else "provider_reset"
        else:
            window_end = r_old
            cause = "natural"
        start = w["start"]
        if start is None and w.get("window_s"):
            start = r_old - w["window_s"]
        closed.append(ClosedWindow(
            w["kind"], start, window_end, w["used_pct"], prev_ts, cause,
            {"staleness_s": round(max(0.0, window_end - prev_ts), 1),
             "prev_ts": prev_ts, "new_ts": new_ts,
             "old_reset_at": r_old, "new_reset_at": r_new}))

    new_drop = {w["kind"]: w for w in drop_windows(provider, new_snap)}
    for w in drop_windows(provider, prev_snap):
        nw = new_drop.get(w["kind"])
        if nw is None or w["used_pct"] <= 0:
            continue
        if w["used_pct"] - nw["used_pct"] < DROP_THRESHOLD_PCT:
            continue
        boundary = w["boundary"]
        if boundary == "midnight":
            natural = _near_local_midnight(prev_ts, new_ts)
        else:
            natural = _near_boundary(prev_ts, new_ts, boundary)
        closed.append(ClosedWindow(
            w["kind"], None, new_ts, w["used_pct"], prev_ts,
            "natural" if natural else "unknown",
            {"staleness_s": round(new_ts - prev_ts, 1),
             "prev_ts": prev_ts, "new_ts": new_ts,
             "prev_used_pct": w["used_pct"], "new_used_pct": nw["used_pct"]}))
    return closed


def fmt_local(ts) -> str:
    if ts is None:
        return ""
    return datetime.datetime.fromtimestamp(float(ts), tz=KST).strftime("%Y-%m-%d %H:%M:%S")


def archive(conn, account, closed) -> list[ClosedWindow]:
    """INSERT OR IGNORE each closed window; returns the ones actually added.

    Early-cause rows (coupon/provider_reset) are additionally skipped when a
    row for the same window kind already ends inside the same snapshot pair:
    the redeem-time direct archive and the next poll's detection describe the
    same close with slightly different window_end values.
    """
    inserted = []
    for cw in closed:
        if cw.reset_cause in ("coupon", "provider_reset"):
            lo, hi = cw.details.get("prev_ts"), cw.details.get("new_ts")
            if lo is not None and hi is not None and \
                    store.window_history_conflict(conn, account["id"], cw.window_kind, lo, hi):
                continue
        if store.save_window_history(conn, account["id"], cw.window_kind, cw.window_start,
                                     cw.window_end, cw.final_used_pct, cw.final_snapshot_ts,
                                     cw.reset_cause, cw.details or None):
            inserted.append(cw)
    return inserted


def record_closed_windows(conn, account, prev_snap, new_snap, *, coupon_hint=False) -> int:
    """Live hook: detect + archive + refresh exports for one poll step."""
    closed = detect_closed_windows(account["provider"], prev_snap, new_snap,
                                   coupon_hint=coupon_hint)
    inserted = archive(conn, account, closed)
    if inserted:
        append_jsonl(account, inserted)
        write_provider_csv(conn, account["provider"])
    return len(inserted)


def archive_coupon_redeem(conn, account, credit_id, now_ts=None) -> int:
    """Archive at coupon-redeem time (HTTP 200), before the confirmation
    re-poll, so the coupon row exists even if that re-poll fails."""
    now_ts = now_ts or time.time()
    snap = store.latest_successful_snapshot(conn, account["id"])
    if not snap:
        return 0
    closed = []
    for w in timed_windows(account["provider"], snap):
        # NOTE: archives every timed window (5h AND weekly) as coupon-reset.
        # Whether a Codex banked-reset credit actually rolls the weekly window
        # too is an unverified assumption — confirm on the first real redeem.
        if w["used_pct"] <= 0:
            continue
        start = w["start"]
        if start is None and w.get("window_s"):
            start = w["reset_at"] - w["window_s"]
        closed.append(ClosedWindow(
            w["kind"], start, now_ts, w["used_pct"], float(snap["ts"]), "coupon",
            {"credit_id": credit_id,
             "staleness_s": round(max(0.0, now_ts - float(snap["ts"])), 1),
             "old_reset_at": w["reset_at"]}))
    inserted = archive(conn, account, closed)
    if inserted:
        append_jsonl(account, inserted)
        write_provider_csv(conn, account["provider"])
    return len(inserted)


def _row_fields(cw: ClosedWindow, email, label) -> dict:
    return {
        "email": email or "",
        "label": label or "",
        "window_kind": cw.window_kind,
        "window_start": fmt_local(cw.window_start),
        "window_end": fmt_local(cw.window_end),
        "final_used_pct": cw.final_used_pct,
        "reset_cause": cw.reset_cause,
        "final_snapshot_ts": fmt_local(cw.final_snapshot_ts),
        "staleness_s": cw.details.get("staleness_s", ""),
    }


def append_jsonl(account, closed) -> None:
    """O(1) per-account append: one JSON object per closed window."""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = HISTORY_DIR / f"{account['provider']}-{account['id']}.jsonl"
    with open(path, "a") as f:
        for cw in closed:
            obj = _row_fields(cw, account.get("email"), account.get("label"))
            obj["details"] = cw.details
            f.write(json.dumps(obj) + "\n")


def write_provider_csv(conn, provider):
    """Rewrite history/<provider>.csv from the window_history table.

    Called only when the table gains rows — files are KB-sized, a few
    writes per day."""
    rows = store.list_window_history(conn, provider=provider)
    if not rows:
        return None
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = HISTORY_DIR / f"{provider}.csv"
    with open(path, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(CSV_FIELDS))
        wr.writeheader()
        for r in rows:
            details = json.loads(r["details"]) if r["details"] else {}
            wr.writerow({
                "email": r["email"] or "",
                "label": r["label"] or "",
                "window_kind": r["window_kind"],
                "window_start": fmt_local(r["window_start"]),
                "window_end": fmt_local(r["window_end"]),
                "final_used_pct": r["final_used_pct"],
                "reset_cause": r["reset_cause"],
                "final_snapshot_ts": fmt_local(r["final_snapshot_ts"]),
                "staleness_s": details.get("staleness_s", ""),
            })
    return path


def backfill(conn) -> int:
    """One-time replay: stream each account's snapshots oldest-first and feed
    consecutive successful pairs through detect_closed_windows.

    Idempotent — the UNIQUE constraint makes re-runs safe. Coupon evidence
    comes from the banked_resets column, which exists historically.
    """
    total = 0
    accounts = store.list_accounts(conn)
    for a in accounts:
        inserted: list[ClosedWindow] = []
        prev = None
        for s in store.iter_snapshots(conn, a["id"]):
            if s.get("status") not in SUCCESS_STATUSES:
                continue
            if prev is not None:
                inserted += archive(conn, a, detect_closed_windows(a["provider"], prev, s))
            prev = s
        if inserted:
            append_jsonl(a, inserted)
        print(f"  {a['provider']:12} {a['email'] or a['label']}: {len(inserted)} windows")
        total += len(inserted)
    for provider in sorted({a["provider"] for a in accounts}):
        write_provider_csv(conn, provider)
    return total


def backfill_credit_history(conn) -> int:
    """Seed the coupon ledger from current reset_credits and reconstruct
    credits that disappeared before live tracking was wired in.

    banked_resets only stores a count, so disappeared credits get synthetic
    ids and final_state='expired_unused' — no coupon-cause window_history
    rows exist for codex, so every count drop is an expiry, not a redeem.
    Idempotent via the UNIQUE(account_id, credit_id) constraint.
    """
    total = 0
    for a in store.list_accounts(conn):
        if a["provider"] != "codex":
            continue
        current = store.list_reset_credits(conn, a["id"])
        if current:
            fetched = current[0]["fetched_at"] or time.time()
            store.upsert_credit_history(conn, a["id"], [
                {"id": c["credit_id"], "title": c["title"], "description": c["description"],
                 "granted_at": c["granted_at"], "expires_at": c["expires_at"]}
                for c in current], fetched_at=fetched)
        snaps = conn.execute(
            "SELECT ts, banked_resets FROM limit_snapshots "
            "WHERE account_id=? AND banked_resets IS NOT NULL ORDER BY ts",
            (a["id"],)).fetchall()
        acct_reconstructed = 0
        prev_count, prev_ts = None, None
        for s in snaps:
            c = s["banked_resets"]
            if prev_count is not None and c < prev_count:
                for i in range(prev_count - c):
                    cid = f"reconstructed_{a['id']}_{int(prev_ts)}_{i}"
                    if conn.execute(
                        "INSERT OR IGNORE INTO reset_credit_history("
                        "account_id,credit_id,first_seen_at,last_seen_at,final_state,final_seen_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (a["id"], cid, prev_ts, prev_ts, "expired_unused", prev_ts)).rowcount:
                        acct_reconstructed += 1
            prev_count, prev_ts = c, s["ts"]
        total += acct_reconstructed
        print(f"  codex         {a['email'] or a['label']}: {len(current)} current, "
              f"ledger seeded{(' + ' + str(acct_reconstructed) + ' reconstructed') if acct_reconstructed else ''}")
    conn.commit()
    return total
