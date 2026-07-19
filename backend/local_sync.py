"""Local real-time sync from CLI session logs (Codex, Claude Code).

Codex CLI writes a token_count event (rate_limits + real token counts) to
~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl after every turn; Claude Code
writes per-message usage to ~/.claude/projects/**/*.jsonl. scan() tails the
recently-modified files and turns them into local snapshots (source="local")
and live_activity blobs — zero network, zero quota. Attribution is strict:
codex events map via ~/.codex/auth.json tokens.account_id; no match, no save.
"""
from __future__ import annotations
import datetime, json, os, time
from pathlib import Path
import store

CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
CODEX_SESSIONS = CODEX_HOME / "sessions"
CODEX_AUTH = CODEX_HOME / "auth.json"
CLAUDE_PROJECTS = Path(os.environ.get("CLAUDE_CONFIG_DIR",
                                      str(Path.home() / ".claude"))) / "projects"
CLAUDE_CONFIG = Path.home() / ".claude.json"

RECENT_S = 600           # only files touched in the last 10 min matter
CODEX_TAIL_BYTES = 65536
CLAUDE_TAIL_BYTES = 262144

# Last handled event timestamp per account id — suppresses no-op re-saves.
_last_event: dict = {}


def _iso_epoch(s) -> float | None:
    if not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def tail_lines(path: Path, nbytes: int) -> list[str]:
    """Last complete lines of a file, reading at most nbytes."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - nbytes))
            data = f.read()
    except OSError:
        return []
    lines = data.decode(errors="replace").splitlines()
    if size > nbytes and lines:
        lines = lines[1:]  # first line is almost certainly cut mid-record
    return lines


def _codex_recent_files(now: float) -> list[Path]:
    """Today's + yesterday's session files touched within RECENT_S, newest first."""
    out = []
    for off in (0, 1):
        d = datetime.date.fromtimestamp(now - off * 86400)
        day_dir = CODEX_SESSIONS / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}"
        if not day_dir.is_dir():
            continue
        for p in day_dir.glob("*.jsonl"):
            try:
                if now - p.stat().st_mtime <= RECENT_S:
                    out.append(p)
            except OSError:
                continue
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out


def extract_token_count(obj) -> dict | None:
    """token_count payload from a session-log record (bare or event_msg-wrapped)."""
    if not isinstance(obj, dict):
        return None
    if obj.get("type") == "token_count":
        return obj
    p = obj.get("payload")
    if isinstance(p, dict) and p.get("type") == "token_count":
        return p
    return None


def codex_active_account_id() -> str | None:
    try:
        d = json.loads(CODEX_AUTH.read_text())
    except (OSError, ValueError):
        return None
    tokens = d.get("tokens")
    return tokens.get("account_id") if isinstance(tokens, dict) else None


def match_codex_account(accounts, upstream_id) -> dict | None:
    if not upstream_id:
        return None
    for a in accounts:
        if a.get("provider") == "codex" and a.get("account_id") == upstream_id:
            return a
    return None


def codex_snap(rl) -> dict:
    """Snapshot dict from a token_count rate_limits block (pure function)."""
    snap = {"status": "active", "status_message": "", "source": "local"}
    for side, prefix in (("primary", "primary"), ("secondary", "secondary")):
        w = rl.get(side) or {}
        if w.get("used_percent") is None:
            continue
        snap[f"{prefix}_used_pct"] = float(w["used_percent"])
        if w.get("resets_at") is not None:
            snap[f"{prefix}_reset_at"] = float(w["resets_at"])
        if w.get("window_minutes"):
            snap[f"{prefix}_window_s"] = int(w["window_minutes"]) * 60
    credits = rl.get("credits") or {}
    if credits.get("balance") is not None:
        try:
            snap["credits_balance"] = float(credits["balance"])
        except (TypeError, ValueError):
            pass
    if rl.get("plan_type"):
        snap["plan"] = rl["plan_type"]
    return snap


def _scan_codex(conn, accounts, now: float) -> bool:
    account = match_codex_account(accounts, codex_active_account_id())
    if not account:
        return False
    for path in _codex_recent_files(now):
        event = None
        ev_iso = None
        for line in reversed(tail_lines(path, CODEX_TAIL_BYTES)):
            if '"token_count"' not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            payload = extract_token_count(obj)
            if payload:
                event = payload
                ev_iso = obj.get("timestamp") or payload.get("timestamp")
                break
        if not event:
            continue
        key = ("codex", account["id"])
        if _last_event.get(key) == ev_iso:
            return False
        ev_epoch = _iso_epoch(ev_iso) or now
        latest = store.latest_snapshot(conn, account["id"])
        changed = False
        # Never let a stale local event shadow a newer API reading.
        if not latest or float(latest["ts"]) < ev_epoch:
            rl = event.get("rate_limits") or {}
            if rl.get("primary") or rl.get("secondary"):
                snap = codex_snap(rl)
                snap["raw_json"] = json.dumps({"local_event_ts": ev_iso})
                store.save_snapshot(conn, account["id"], snap)
                changed = True
        info = event.get("info") or {}
        last = info.get("last_token_usage") or {}
        cw = info.get("model_context_window")
        live = {"provider": "codex", "event_epoch": ev_epoch,
                "last_total_tokens": last.get("total_tokens"),
                "last_cached_tokens": last.get("cached_input_tokens"),
                "last_output_tokens": last.get("output_tokens")}
        if cw and last.get("total_tokens"):
            live["context_used_pct"] = round(
                min(100.0, 100.0 * last["total_tokens"] / cw), 1)
        store.upsert_live_activity(conn, account["id"], live)
        _last_event[key] = ev_iso
        return True
    return False


def claude_usage_totals(lines, since_epoch: float) -> dict:
    """Sum new tokens (input+output+cache_creation) from events after since_epoch."""
    total = 0
    last_epoch = None
    for line in lines:
        if '"usage"' not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        usage = ((obj.get("message") or {}).get("usage")
                 if isinstance(obj.get("message"), dict) else None)
        if not isinstance(usage, dict):
            continue
        ts = _iso_epoch(obj.get("timestamp"))
        if ts is None:
            continue
        if last_epoch is None or ts > last_epoch:
            last_epoch = ts
        if ts < since_epoch:
            continue
        total += int(usage.get("input_tokens") or 0)
        total += int(usage.get("output_tokens") or 0)
        total += int(usage.get("cache_creation_input_tokens") or 0)
    return {"tokens_60m": total, "last_event_epoch": last_epoch}


def _claude_account(accounts) -> dict | None:
    claudes = [a for a in accounts if a.get("provider") == "claude"]
    if len(claudes) == 1:
        return claudes[0]
    try:
        cfg = json.loads(CLAUDE_CONFIG.read_text())
        email = ((cfg.get("oauthAccount") or {}).get("emailAddress") or "").lower()
    except (OSError, ValueError):
        return None
    for a in claudes:
        if (a.get("email") or "").lower() == email:
            return a
    return None


def _scan_claude(conn, accounts, now: float) -> bool:
    account = _claude_account(accounts)
    if not account or not CLAUDE_PROJECTS.is_dir():
        return False
    recent = []
    for p in CLAUDE_PROJECTS.rglob("*.jsonl"):
        try:
            if now - p.stat().st_mtime <= RECENT_S:
                recent.append(p)
        except OSError:
            continue
    if not recent:
        return False
    total = 0
    last_epoch = None
    for p in recent:
        t = claude_usage_totals(tail_lines(p, CLAUDE_TAIL_BYTES), now - 3600)
        total += t["tokens_60m"]
        if t["last_event_epoch"] and (last_epoch is None or t["last_event_epoch"] > last_epoch):
            last_epoch = t["last_event_epoch"]
    key = ("claude", account["id"])
    marker = (last_epoch, total)
    if _last_event.get(key) == marker:
        return False
    store.upsert_live_activity(conn, account["id"], {
        "provider": "claude", "event_epoch": last_epoch, "tokens_60m": total})
    _last_event[key] = marker
    return True


def scan(conn, now=None) -> bool:
    """One local-sync pass over both CLI log trees. True when anything changed."""
    now = now or time.time()
    accounts = store.list_accounts(conn)
    changed = False
    try:
        changed = _scan_codex(conn, accounts, now) or changed
    except Exception as e:
        print(f"  local sync (codex) failed: {e}")
    try:
        changed = _scan_claude(conn, accounts, now) or changed
    except Exception as e:
        print(f"  local sync (claude) failed: {e}")
    return changed
