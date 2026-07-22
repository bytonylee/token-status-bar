"""Automatic same-provider account swap (spec §3: codex + flagged claude).

When the locally-active account exhausts its quota, rewrite the local CLI's
credentials with the best pool account's tokens so the CLI keeps working.
Codex: ~/.codex/auth.json. Claude (behind auto_swap.claude, default OFF —
spec §3.4 spike): macOS keychain item "Claude Code-credentials" via
`security -i` + ~/.claude.json oauthAccount patch.
Every automatic swap must pass ALL safety rails in should_swap();
perform_swap() does the side effects (pre-refresh, backup, atomic write,
lifecycle event, notification). The daemon calls auto_swap_tick() from
poller.export_status(); the Swift menu's manual action goes through
cmd_swap() (pool.py swap).

SAFETY: this module handles real OAuth tokens — never log or print token
values, only emails/labels/account ids.
"""
from __future__ import annotations
import datetime, getpass, json, os, re, subprocess, time
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

DEFAULT_SETTINGS = {"auto_swap": {"codex": True, "claude": False}}
SWAP_PROVIDERS = ("codex", "claude")

# Claude Code credential store (spec §3.4 spike findings).
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
DEFAULT_CLAUDE_SCOPES = ["user:file_upload", "user:inference",
                         "user:mcp_servers", "user:profile",
                         "user:sessions:claude_code"]


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
    for codex only; claude default OFF pending the §3.4 supervised trial).
    New provider keys are merge-updated into an existing file without
    clobbering the user's choices. Unreadable/corrupt settings fail safe:
    swaps disabled.
    """
    try:
        settings = json.loads(SETTINGS_PATH.read_text())
    except FileNotFoundError:
        try:
            _write_private(SETTINGS_PATH, json.dumps(DEFAULT_SETTINGS, indent=2))
        except OSError:
            pass
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    except (OSError, ValueError):
        return {"auto_swap": {}}
    if not isinstance(settings, dict):
        return {"auto_swap": {}}
    auto = settings.setdefault("auto_swap", {})
    if isinstance(auto, dict):
        missing = {k: v for k, v in DEFAULT_SETTINGS["auto_swap"].items()
                   if k not in auto}
        if missing:
            auto.update(missing)
            try:
                _write_private(SETTINGS_PATH, json.dumps(settings, indent=2))
            except OSError:
                pass
    return settings


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


def swap_candidates(items, provider, active_upstream_id, active_email=None) -> list:
    """Usable pool accounts of `provider`, excluding the locally-active one,
    best candidate first (spec §3.2). Codex identity is the upstream
    account_id (written into auth.json); claude identity is the account
    email (matched against ~/.claude.json oauthAccount.emailAddress —
    claude pool rows may have no upstream account_id)."""
    active_email = (active_email or "").lower()
    out = []
    for it in items or []:
        if it.get("provider") != provider:
            continue
        if provider == "claude":
            email = (it.get("email") or "").lower()
            if not email:
                continue  # can't identify/patch oauthAccount without an email
            if active_email and email == active_email:
                continue
        else:
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


def _claude_config_path() -> Path:
    import local_sync
    return local_sync.CLAUDE_CONFIG


def _short_email(s) -> str:
    s = s or "?"
    return s.split("@", 1)[0] + "@" if "@" in s else s


# ─── claude keychain plumbing (spec §3.4) ───────────────────────────────────
def _security_escape(s: str) -> str:
    """Escape for security(1)'s interactive command parser (double-quoted
    argument): backslash and double-quote. Verified to round-trip JSON
    payloads byte-identically (spec §3.4 spike)."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _keychain_read(service: str) -> str | None:
    """Password payload of the generic-password item, or None when absent."""
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


def _keychain_account_attr(service: str) -> str:
    """The existing item's "acct" attribute (reused on rewrite so we replace
    the same item), falling back to the login user name — which is what
    Claude Code uses (spec §3.4 spike)."""
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", service],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            m = re.search(r'"acct"<blob>="([^"]*)"', r.stdout + r.stderr)
            if m:
                return m.group(1)
    except Exception:
        pass
    return getpass.getuser()


def _keychain_write(service: str, account: str, secret: str) -> None:
    """add-generic-password -U (update-in-place) fed through `security -i`
    stdin so the secret never appears on argv (ps-visible). Spec §3.4."""
    cmd = (f'add-generic-password -U'
           f' -s "{_security_escape(service)}"'
           f' -a "{_security_escape(account)}"'
           f' -w "{_security_escape(secret)}"\n')
    r = subprocess.run(["security", "-i"], input=cmd,
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(
            f"keychain write failed (rc={r.returncode}): {(r.stderr or '').strip()[:200]}")


def _claude_subscription_type(plan) -> str | None:
    """Keychain subscriptionType from our accounts.plan label
    ("Claude Max" → "max"), or None to keep the existing value."""
    p = (plan or "").lower()
    for kind in ("max", "pro", "team", "enterprise", "free"):
        if kind in p:
            return kind
    return None


def _token_raw(token_row) -> dict:
    try:
        raw = json.loads(token_row.get("raw_json") or "{}")
        return raw if isinstance(raw, dict) else {}
    except ValueError:
        return {}


def _claude_keychain_payload(existing, target_row, token_row) -> dict:
    """New "Claude Code-credentials" JSON: the existing item with only the
    claudeAiOauth token fields replaced (shape verified keys-only against
    the real item, spec §3.4): accessToken, refreshToken, expiresAt(ms),
    refreshTokenExpiresAt(ms), scopes, subscriptionType; extras like
    rateLimitTier are preserved from the existing item."""
    existing = existing if isinstance(existing, dict) else {}
    old = existing.get("claudeAiOauth")
    oauth = dict(old) if isinstance(old, dict) else {}
    oauth["accessToken"] = token_row["access_token"]
    oauth["refreshToken"] = token_row.get("refresh_token")
    exp = token_row.get("expires_at")
    if exp:
        oauth["expiresAt"] = int(float(exp) * 1000)
    raw = _token_raw(token_row)
    scope = raw.get("scope")
    if scope:
        oauth["scopes"] = scope.split()
    elif "scopes" not in oauth:
        oauth["scopes"] = list(DEFAULT_CLAUDE_SCOPES)
    rte = raw.get("refresh_token_expires_in")
    last_refresh = token_row.get("last_refresh")
    if rte and last_refresh:
        oauth["refreshTokenExpiresAt"] = int((float(last_refresh) + float(rte)) * 1000)
    sub = _claude_subscription_type(target_row.get("plan"))
    if sub:
        oauth["subscriptionType"] = sub
    out = dict(existing)
    out["claudeAiOauth"] = oauth
    return out


def _patch_claude_config(target_row, raw_token) -> dict | None:
    """Point ~/.claude.json oauthAccount at the swapped-in account, atomically
    preserving every other key (and unrelated oauthAccount fields). Returns
    the previous oauthAccount dict (None when unknown). An unparseable
    config is left untouched — never destroy the user's ~/.claude.json."""
    path = _claude_config_path()
    try:
        cfg = json.loads(path.read_text())
        if not isinstance(cfg, dict):
            raise ValueError("not a JSON object")
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        print(f"  swap: could not parse {path.name}; oauthAccount not patched")
        return None
    prev = cfg.get("oauthAccount") if isinstance(cfg.get("oauthAccount"), dict) else None
    oa = dict(prev or {})
    oa["emailAddress"] = target_row.get("email")
    acct = raw_token.get("account") or {}
    if acct.get("uuid"):
        oa["accountUuid"] = acct["uuid"]
    org = raw_token.get("organization") or {}
    if org.get("uuid"):
        oa["organizationUuid"] = org["uuid"]
    if org.get("name"):
        oa["organizationName"] = org["name"]
    cfg["oauthAccount"] = oa
    _write_private(path, json.dumps(cfg, indent=2))
    return prev


def _perform_swap_claude(conn, target_account_row, token_row,
                         refresh_cb=None, headroom_note=None) -> dict:
    """Claude Code credential swap (spec §3.4, behind auto_swap.claude):
    pre-refresh → back up the current keychain JSON → rewrite the keychain
    item via `security -i` (secret on stdin, not argv) → patch
    ~/.claude.json oauthAccount → account_swapped event → notification."""
    if not target_account_row.get("email"):
        raise ValueError("target claude account has no email")
    if not token_row or not token_row.get("access_token"):
        raise ValueError("target account has no token")
    now = time.time()

    token_row = _pre_refresh(conn, target_account_row, token_row, now,
                             refresh_cb=refresh_cb)

    existing_raw = _keychain_read(CLAUDE_KEYCHAIN_SERVICE)
    existing, backup_path = {}, None
    if existing_raw is not None:
        try:
            existing = json.loads(existing_raw)
            if not isinstance(existing, dict):
                existing = {}
        except ValueError:
            existing = {}
            print("  swap: existing keychain item is not JSON; not preserving extras")
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(BACKUP_DIR, 0o700)
        except OSError:
            pass
        backup_path = BACKUP_DIR / f"claude-{int(now)}.json"
        fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(existing_raw)

    acct_attr = _keychain_account_attr(CLAUDE_KEYCHAIN_SERVICE)
    payload = _claude_keychain_payload(existing, target_account_row, token_row)
    _keychain_write(CLAUDE_KEYCHAIN_SERVICE, acct_attr, json.dumps(payload))

    raw = _token_raw(token_row)
    prev_oa = _patch_claude_config(target_account_row, raw)
    from_email = (prev_oa or {}).get("emailAddress")
    from_uuid = (prev_oa or {}).get("accountUuid")
    to_email = target_account_row.get("email")
    to_uuid = ((raw.get("account") or {}).get("uuid")
               or target_account_row.get("account_id"))

    detail = {"provider": "claude",
              "from_email": from_email,
              "to_email": to_email,
              "from_account_id": from_uuid,
              "to_account_id": to_uuid}
    store.save_lifecycle_event(conn, now, target_account_row["id"],
                               "account_swapped", detail)

    msg = f"Claude swapped: {_short_email(from_email)} → {_short_email(to_email)}"
    if headroom_note:
        msg += f" ({headroom_note})"
    try:
        _notify(msg)
    except Exception:
        pass  # notification failure must never fail a completed swap

    print(f"  ⇄ swapped claude: {from_email or '?'} → {to_email}"
          f" (#{target_account_row['id']})")
    return {"provider": "claude", "from_email": from_email, "to_email": to_email,
            "from_account_id": from_uuid, "to_account_id": to_uuid,
            "backup": str(backup_path) if backup_path else None,
            "at_epoch": now}


def perform_swap(conn, provider, target_account_row, token_row,
                 refresh_cb=None, headroom_note=None) -> dict:
    """Swap the local CLI onto target_account_row's credentials.

    codex: pre-refresh near-expiry token → back up the current auth.json →
    atomically write the new one (preserving extra top-level keys like
    OPENAI_API_KEY/auth_mode) → account_swapped lifecycle event →
    macOS notification. claude: see _perform_swap_claude (spec §3.4).
    Returns a summary dict (no token values).
    """
    if provider == "claude":
        return _perform_swap_claude(conn, target_account_row, token_row,
                                    refresh_cb=refresh_cb,
                                    headroom_note=headroom_note)
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

    msg = f"Codex swapped: {_short_email(from_email)} → {_short_email(to_email)}"
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


def _find_active_item(items, provider, active_upstream, active_email):
    """The payload item for the locally-active account of `provider`, or
    None. Codex matches by upstream account_id, claude by email."""
    if provider == "claude":
        if not active_email:
            return None
        return next((it for it in items if it.get("provider") == "claude"
                     and (it.get("email") or "").lower() == active_email), None)
    if not active_upstream:
        return None
    return next((it for it in items if it.get("provider") == provider
                 and it.get("account_id") == active_upstream), None)


# ─── daemon + CLI entry points ──────────────────────────────────────────────
def auto_swap_tick(conn, payload, now=None) -> dict | None:
    """One automatic-swap evaluation over a freshly-built export payload.

    Called from poller.export_status() — the single choke point every
    poll/local-sync path funnels through. Iterates the providers whose
    auto_swap kill-switch is on (codex default-on, claude default-off) and
    performs at most one swap per tick. Returns the perform_swap summary
    when a swap happened, else None.
    """
    now = now if now is not None else time.time()
    items = (payload or {}).get("accounts") or []
    settings = load_settings()
    import local_sync
    for provider in SWAP_PROVIDERS:
        if not (settings.get("auto_swap") or {}).get(provider, False):
            continue  # kill-switch off — don't even read local state
        if provider == "claude":
            active_upstream = None
            active_email = local_sync.claude_active_email()
        else:
            active_upstream = local_sync.codex_active_account_id()
            active_email = None
        active = _find_active_item(items, provider, active_upstream, active_email)
        if active is None:
            continue  # locally-active account unknown or not in the pool
        candidates = swap_candidates(items, provider, active_upstream,
                                     active_email=active_email)
        chosen = should_swap(active, candidates, now, settings, conn)
        if chosen is None:
            continue
        target = store.get_account(conn, chosen["id"])
        token = store.get_token(conn, chosen["id"])
        if not target or not token:
            continue
        return perform_swap(conn, provider, target, token,
                            headroom_note=_headroom_note(chosen))
    return None


def cmd_swap(conn, provider, account_db_id, force=False) -> int:
    """CLI: pool.py swap --provider codex|claude --account-id N [--force].

    Without --force the full automatic rails apply (kill-switch, active
    exhausted, fresh data, live-session guard, cooldown) with the chosen
    account as the only candidate. --force is explicit user intent: it
    bypasses the rails but keeps backup + atomic write + event +
    notification. Swapping onto the already-active account is always a no-op.
    """
    if provider not in SWAP_PROVIDERS:
        print(f"swap supports {'/'.join(SWAP_PROVIDERS)} (got: {provider})")
        return 1
    target = store.get_account(conn, account_db_id)
    if not target or target.get("provider") != provider:
        print(f"No {provider} account with id {account_db_id}")
        return 1
    if provider == "codex" and not target.get("account_id"):
        print(f"Account {account_db_id} has no upstream account_id; reconnect it first")
        return 1
    if provider == "claude" and not target.get("email"):
        print(f"Account {account_db_id} has no email; reconnect it first")
        return 1
    token = store.get_token(conn, account_db_id)
    if not token or not token.get("access_token"):
        print(f"Account {account_db_id} has no token; reconnect it first")
        return 1

    import local_sync, status
    if provider == "claude":
        active_upstream = None
        active_email = local_sync.claude_active_email()
        already_active = bool(active_email) and \
            active_email == (target.get("email") or "").lower()
    else:
        active_upstream = local_sync.codex_active_account_id()
        active_email = None
        already_active = bool(active_upstream) and \
            active_upstream == target["account_id"]
    if already_active:
        print(f"{target.get('email') or account_db_id} is already the active {provider} account")
        return 0

    payload = status.build_payload(conn)
    items = payload.get("accounts") or []
    target_item = next((it for it in items if it.get("id") == account_db_id), None)

    if not force:
        active = _find_active_item(items, provider, active_upstream, active_email)
        if active is None:
            print(f"Active {provider} account not found in pool; use --force to swap anyway")
            return 1
        cands = swap_candidates([target_item] if target_item else [],
                                provider, active_upstream,
                                active_email=active_email)
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
