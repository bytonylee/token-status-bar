"""Automatic same-provider account swap (spec §3, Codex only for now).

When the locally-active Codex account exhausts its quota, rewrite
~/.codex/auth.json with the best pool account's tokens so the CLI keeps
working. Every automatic swap must pass ALL safety rails in should_swap();
perform_swap() does the side effects (pre-refresh, backup, atomic write,
lifecycle event, notification). The daemon calls auto_swap_tick() from
poller.export_status(); the Swift menu's manual action goes through
cmd_swap() (pool.py swap).

SAFETY: this module handles real OAuth tokens — never log or print token
values, only emails/labels/account ids.
"""
from __future__ import annotations
import datetime, json, os, subprocess, time
from pathlib import Path
import store

# Mirrors poller.POLL_INTERVAL / status.POLL_INTERVAL_S (no import cycle).
POLL_INTERVAL_S = int(os.environ.get("AGENT_POOL_POLL_INTERVAL", "300"))
SNAPSHOT_MAX_AGE_S = 3 * POLL_INTERVAL_S   # stale-data refusal (spec §3.2)
COOLDOWN_S = 30 * 60                       # per-provider swap cooldown
LIVE_GUARD_S = 120                         # recent live agent session → refuse
PREREFRESH_LEAD_S = 600                    # refresh target token if it expires sooner

_SECRETS_DIR = Path.home() / "solo/token-status-bar" / "secrets"
SETTINGS_PATH = Path(os.environ.get("AGENT_POOL_SETTINGS",
                                    str(_SECRETS_DIR / "settings.json")))
BACKUP_DIR = Path(os.environ.get("AGENT_POOL_SWAP_BACKUPS",
                                 str(_SECRETS_DIR / "swap_backups")))

DEFAULT_SETTINGS = {"auto_swap": {"codex": True}}


def _write_private(path: Path, text: str) -> None:
    """Atomic 0600 write: tmp file in the same dir + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_settings() -> dict:
    """secrets/settings.json, creating it with the defaults when missing.

    Per-provider kill-switch lives under "auto_swap" (spec §3.2: default on
    for codex only). Unreadable/corrupt settings fail safe: swaps disabled.
    """
    try:
        return json.loads(SETTINGS_PATH.read_text())
    except FileNotFoundError:
        try:
            _write_private(SETTINGS_PATH, json.dumps(DEFAULT_SETTINGS, indent=2))
        except OSError:
            pass
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    except (OSError, ValueError):
        return {"auto_swap": {}}


# ─── candidate selection ────────────────────────────────────────────────────
def _headroom_key(item) -> tuple:
    """Sort key: most headroom on the binding window first, then earlier
    next reset. Unknown headroom ranks last (fail-safe)."""
    bw = (item.get("state") or {}).get("binding_window") or {}
    used = bw.get("used_pct_effective")
    if used is None:
        used = bw.get("used_pct")
    headroom = (100.0 - float(used)) if used is not None else 0.0
    reset = bw.get("reset_at_epoch")
    return (-headroom, reset if reset is not None else float("inf"))


def swap_candidates(items, provider, active_upstream_id) -> list:
    """Usable pool accounts of `provider`, excluding the locally-active one
    (matched by upstream account_id), best candidate first (spec §3.2)."""
    out = []
    for it in items or []:
        if it.get("provider") != provider:
            continue
        if not it.get("account_id"):
            continue  # can't write auth.json without the upstream id
        if active_upstream_id and it["account_id"] == active_upstream_id:
            continue
        if not ((it.get("state") or {}).get("usable")):
            continue
        out.append(it)
    out.sort(key=_headroom_key)
    return out


# ─── trigger decision ───────────────────────────────────────────────────────
def _cooldown_active(conn, provider, now) -> bool:
    """True when an account_swapped event for this provider landed within
    COOLDOWN_S. Unparseable detail counts as a match (fail-safe)."""
    for ev in store.list_lifecycle_events(conn, since=now - COOLDOWN_S):
        if ev.get("event") != "account_swapped":
            continue
        try:
            detail = json.loads(ev.get("detail") or "{}")
        except ValueError:
            return True
        if detail.get("provider") == provider:
            return True
    return False


def should_swap(active_item, candidates, now, settings, conn) -> dict | None:
    """The chosen swap target, or None. ALL rails must pass (spec §3.2):

    kill-switch on → active genuinely exhausted on fresh live windows →
    snapshot fresh (≤ 3× poll interval) → no live agent session in the last
    120s → provider cooldown clear → at least one usable candidate.
    """
    provider = active_item.get("provider")
    if not (settings.get("auto_swap") or {}).get(provider, False):
        return None  # kill-switch off

    state = active_item.get("state") or {}
    if state.get("quota") != "exhausted":
        return None

    # Re-verify exhaustion on the windows themselves — never trust a stale
    # or partial classification for a credential rewrite (fail-safe).
    windows = active_item.get("windows") or []
    fresh = [w for w in windows if not w.get("stale")]
    if not fresh:
        return None  # all windows stale → refuse
    live = [w for w in fresh if w.get("phase") != "reset"]
    if not live:
        return None  # no live evidence of exhaustion
    for w in live:
        used = w.get("used_pct_effective")
        if used is None or used < 100.0:
            return None  # a live window still has room (or is unknown)

    # Snapshot freshness: the newest reading backing this item must be
    # younger than 3× the poll interval.
    stamps = [active_item.get("last_poll_epoch")]
    stamps += [w.get("as_of_epoch") for w in windows]
    stamps = [float(s) for s in stamps if s is not None]
    if not stamps or now - max(stamps) > SNAPSHOT_MAX_AGE_S:
        return None

    # Live-session guard: don't yank credentials mid-turn.
    la = store.get_live_activity(conn, active_item.get("id"))
    if la and now - float(la.get("ts") or 0) < LIVE_GUARD_S:
        return None

    if _cooldown_active(conn, provider, now):
        return None

    return candidates[0] if candidates else None


# ─── side effects ───────────────────────────────────────────────────────────
def _notify(message: str) -> None:
    """macOS notification via osascript; failures never fail the swap."""
    try:
        safe = message.replace("\\", "").replace('"', "'")
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{safe}" with title "TokenStatusBar"'],
            capture_output=True, timeout=10)
    except Exception:
        pass


def _pre_refresh(conn, account, token_row, now, refresh_cb=None):
    """Refresh the target token when it expires within PREREFRESH_LEAD_S so
    the CLI never receives a nearly-dead token (spec §3.3)."""
    exp = token_row.get("expires_at")
    if not exp or exp - now >= PREREFRESH_LEAD_S:
        return token_row
    if not token_row.get("refresh_token"):
        return token_row
    if refresh_cb is None:
        import oauth
        refresh_cb = oauth.REFRESH_FUNCS.get(account["provider"])
    if refresh_cb is None:
        return token_row
    import work_queue
    # Refresh tokens rotate: serialize with the daemon's refresh path and
    # re-read inside the lock in case another process already refreshed.
    with work_queue.exclusive("token_refresh"):
        token_row = store.get_token(conn, account["id"]) or token_row
        exp = token_row.get("expires_at")
        if exp and exp - now >= PREREFRESH_LEAD_S:
            return token_row
        result = refresh_cb(token_row["refresh_token"])
        store.save_token(conn, account["id"], result["access_token"],
                         result.get("refresh_token"), result.get("id_token"),
                         result.get("expires_at"), result.get("raw"))
        store.log_event(conn, account["id"], "token_refresh", True, "")
    return store.get_token(conn, account["id"]) or token_row


def _codex_auth_path() -> Path:
    import local_sync
    return local_sync.CODEX_AUTH


def perform_swap(conn, provider, target_account_row, token_row,
                 refresh_cb=None, headroom_note=None) -> dict:
    """Swap the local CLI onto target_account_row's credentials (codex only).

    Pre-refresh near-expiry token → back up the current auth.json →
    atomically write the new one (preserving extra top-level keys like
    OPENAI_API_KEY/auth_mode) → account_swapped lifecycle event →
    macOS notification. Returns a summary dict (no token values).
    """
    if provider != "codex":
        raise ValueError(f"swap not supported for provider: {provider}")
    if not target_account_row.get("account_id"):
        raise ValueError("target account has no upstream account_id")
    if not token_row or not token_row.get("access_token"):
        raise ValueError("target account has no token")
    now = time.time()

    token_row = _pre_refresh(conn, target_account_row, token_row, now,
                             refresh_cb=refresh_cb)

    auth_path = _codex_auth_path()
    existing, backup_path = {}, None
    try:
        raw = auth_path.read_bytes()
        existing = json.loads(raw)
        if not isinstance(existing, dict):
            existing = {}
    except FileNotFoundError:
        raw = None
    except (OSError, ValueError):
        raw = None
        print(f"  swap: could not parse existing {auth_path.name}; not preserving extras")
    if raw is not None:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(BACKUP_DIR, 0o700)
        except OSError:
            pass
        backup_path = BACKUP_DIR / f"{provider}-{int(now)}.json"
        fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)

    from_account_id = ((existing.get("tokens") or {}).get("account_id")
                       if isinstance(existing.get("tokens"), dict) else None)
    from_row = None
    if from_account_id:
        r = conn.execute("SELECT * FROM accounts WHERE provider=? AND account_id=?",
                         (provider, from_account_id)).fetchone()
        from_row = dict(r) if r else None

    # Exact shape Codex CLI expects (verified keys-only against a real
    # ~/.codex/auth.json): top-level OPENAI_API_KEY / tokens / last_refresh,
    # plus whatever extras (e.g. auth_mode) the existing file carries.
    new_auth = dict(existing)
    new_auth.setdefault("OPENAI_API_KEY", None)
    new_auth["tokens"] = {
        "id_token": token_row.get("id_token"),
        "access_token": token_row.get("access_token"),
        "refresh_token": token_row.get("refresh_token"),
        "account_id": target_account_row["account_id"],
    }
    new_auth["last_refresh"] = (datetime.datetime.now(datetime.timezone.utc)
                                .isoformat().replace("+00:00", "Z"))
    _write_private(auth_path, json.dumps(new_auth, indent=2))

    from_email = from_row.get("email") if from_row else None
    to_email = target_account_row.get("email")
    detail = {"provider": provider,
              "from_email": from_email,
              "to_email": to_email,
              "from_account_id": from_account_id,
              "to_account_id": target_account_row["account_id"]}
    store.save_lifecycle_event(conn, now, target_account_row["id"],
                               "account_swapped", detail)

    def _short(s):
        s = s or "?"
        return s.split("@", 1)[0] + "@" if "@" in s else s
    msg = f"Codex swapped: {_short(from_email)} → {_short(to_email)}"
    if headroom_note:
        msg += f" ({headroom_note})"
    try:
        _notify(msg)
    except Exception:
        pass  # notification failure must never fail a completed swap

    print(f"  ⇄ swapped {provider}: {from_email or from_account_id or '?'}"
          f" → {to_email} (#{target_account_row['id']})")
    return {"provider": provider, "from_email": from_email, "to_email": to_email,
            "from_account_id": from_account_id,
            "to_account_id": target_account_row["account_id"],
            "backup": str(backup_path) if backup_path else None,
            "at_epoch": now}


def _headroom_note(candidate) -> str | None:
    bw = ((candidate.get("state") or {}).get("binding_window")) or {}
    used = bw.get("used_pct_effective")
    if used is None:
        used = bw.get("used_pct")
    if used is None:
        return None
    kind = bw.get("kind") or "window"
    return f"{100.0 - float(used):.0f}% {kind} left"


# ─── daemon + CLI entry points ──────────────────────────────────────────────
def auto_swap_tick(conn, payload, now=None) -> dict | None:
    """One automatic-swap evaluation over a freshly-built export payload.

    Called from poller.export_status() — the single choke point every
    poll/local-sync path funnels through. Returns the perform_swap summary
    when a swap happened, else None.
    """
    now = now if now is not None else time.time()
    items = (payload or {}).get("accounts") or []
    import local_sync
    active_upstream = local_sync.codex_active_account_id()
    if not active_upstream:
        return None
    active = next((it for it in items if it.get("provider") == "codex"
                   and it.get("account_id") == active_upstream), None)
    if active is None:
        return None  # locally-active account isn't in the pool
    candidates = swap_candidates(items, "codex", active_upstream)
    chosen = should_swap(active, candidates, now, load_settings(), conn)
    if chosen is None:
        return None
    target = store.get_account(conn, chosen["id"])
    token = store.get_token(conn, chosen["id"])
    if not target or not token:
        return None
    return perform_swap(conn, "codex", target, token,
                        headroom_note=_headroom_note(chosen))


def cmd_swap(conn, provider, account_db_id, force=False) -> int:
    """CLI: pool.py swap --provider codex --account-id N [--force].

    Without --force the full automatic rails apply (kill-switch, active
    exhausted, fresh data, live-session guard, cooldown) with the chosen
    account as the only candidate. --force is explicit user intent: it
    bypasses the rails but keeps backup + atomic write + event +
    notification. Swapping onto the already-active account is always a no-op.
    """
    if provider != "codex":
        print(f"swap only supports codex for now (got: {provider})")
        return 1
    target = store.get_account(conn, account_db_id)
    if not target or target.get("provider") != provider:
        print(f"No {provider} account with id {account_db_id}")
        return 1
    if not target.get("account_id"):
        print(f"Account {account_db_id} has no upstream account_id; reconnect it first")
        return 1
    token = store.get_token(conn, account_db_id)
    if not token or not token.get("access_token"):
        print(f"Account {account_db_id} has no token; reconnect it first")
        return 1

    import local_sync, status
    active_upstream = local_sync.codex_active_account_id()
    if active_upstream and active_upstream == target["account_id"]:
        print(f"{target.get('email') or account_db_id} is already the active codex account")
        return 0

    payload = status.build_payload(conn)
    items = payload.get("accounts") or []
    target_item = next((it for it in items if it.get("id") == account_db_id), None)

    if not force:
        active = next((it for it in items if it.get("provider") == "codex"
                       and it.get("account_id") == active_upstream), None)
        if active is None:
            print("Active codex account not found in pool; use --force to swap anyway")
            return 1
        cands = swap_candidates([target_item] if target_item else [],
                                "codex", active_upstream)
        if should_swap(active, cands, time.time(), load_settings(), conn) is None:
            print("Swap refused by safety rails (active not exhausted, stale data, "
                  "live session, cooldown, or target not usable). Use --force to override.")
            return 1

    note = _headroom_note(target_item) if target_item else None
    try:
        perform_swap(conn, provider, target, token, headroom_note=note)
    except Exception as e:
        print(f"Swap failed: {e}")
        return 1
    try:
        status.cmd_export(conn)
    except Exception as e:
        print(f"  export-status failed: {e}")
    return 0
