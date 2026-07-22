"""Cheap per-account keep-alive: send "just answer: hi" on a 5h cadence.

Providers:
  codex        — all accounts, gpt-5.4-mini @ low (isolated CODEX_HOME)
  claude       — all accounts, haiku-4.5 via Messages API (pool OAuth token)
  antigravity  — all accounts, Gemini 3.5 Flash (Low) via `agy -p`

Usage:
  python3 backend/pool.py heartbeat        # one cycle
  python3 backend/pool.py heartbeat-loop   # every 5 hours
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import store
import work_queue

PROMPT = "Reply with exactly the single lowercase word: hi. Do not add punctuation, markdown, or any other text."
INTERVAL_S = 5 * 60 * 60  # 5 hours
PROVIDERS = ("codex", "claude", "antigravity")

CODEX_MODEL = "gpt-5.4-mini"
CODEX_EFFORT = "low"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
AGY_MODEL = "Gemini 3.5 Flash (Low)"

CODEX_TIMEOUT_S = 180
AGY_TIMEOUT_S = 180
CLAUDE_TIMEOUT_S = 30


def _ts() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def _chatgpt_account_id(account, token) -> str | None:
    """Prefer store.account_id; fall back to id_token claim."""
    if account.get("account_id"):
        return account["account_id"]
    id_token = token.get("id_token") or ""
    if not id_token or id_token.count(".") < 2:
        return None
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        auth = claims.get("https://api.openai.com/auth") or {}
        return auth.get("chatgpt_account_id")
    except Exception:
        return None


def _refresh_if_needed(conn, account, token):
    """Reuse poller's refresh helper when available."""
    try:
        from poller import _refresh_if_needed as refresh
        return refresh(conn, account, token)
    except Exception:
        return token


def _which(name: str) -> str | None:
    return shutil.which(name)


# ─── codex ─────────────────────────────────────────────────────────────────
def _heartbeat_codex(account, token) -> str:
    codex = _which("codex")
    if not codex:
        raise RuntimeError("codex CLI not found on PATH")

    account_id = _chatgpt_account_id(account, token)
    if not account_id:
        raise RuntimeError("missing chatgpt account_id")

    home = Path(tempfile.mkdtemp(prefix=f"codex-hb-{account['id']}-"))
    try:
        auth = {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "access_token": token["access_token"],
                "refresh_token": token.get("refresh_token"),
                "id_token": token.get("id_token"),
                "account_id": account_id,
            },
            "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        (home / "auth.json").write_text(json.dumps(auth))

        env = os.environ.copy()
        env["CODEX_HOME"] = str(home)
        # Prefer project python/node bins; keep user PATH.
        cmd = [
            codex, "exec",
            "--ephemeral",
            "-m", CODEX_MODEL,
            "-c", f'model_reasoning_effort="{CODEX_EFFORT}"',
            "--skip-git-repo-check",
            PROMPT,
        ]
        p = subprocess.run(
            cmd,
            cwd="/tmp",
            env=env,
            capture_output=True,
            text=True,
            timeout=CODEX_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
        if p.returncode != 0:
            err = (p.stderr or p.stdout or "").strip()
            raise RuntimeError(err[-500:] or f"exit {p.returncode}")
        out = (p.stdout or "").strip()
        return out.splitlines()[-1] if out else "ok"
    finally:
        shutil.rmtree(home, ignore_errors=True)


# ─── claude ────────────────────────────────────────────────────────────────
def _heartbeat_claude(account, token) -> str:
    body = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": PROMPT}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token['access_token']}")
    req.add_header("anthropic-version", "2023-06-01")
    try:
        with urllib.request.urlopen(req, timeout=CLAUDE_TIMEOUT_S) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"HTTP {e.code}: {msg}") from e

    parts = data.get("content") or []
    texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"]
    return ("".join(texts).strip() or "ok")


# ─── antigravity / agy ─────────────────────────────────────────────────────
def _heartbeat_antigravity(account, token) -> str:
    # `agy` uses the currently logged-in Google session. With a single account
    # in the pool this matches; run_once() skips antigravity entirely when the
    # pool holds more than one account. Multi-account would need isolated
    # state dirs.
    agy = _which("agy")
    if not agy:
        raise RuntimeError("agy CLI not found on PATH")

    cmd = [agy, "--model", AGY_MODEL, "--print", PROMPT]
    p = subprocess.run(
        cmd,
        cwd="/tmp",
        capture_output=True,
        text=True,
        timeout=AGY_TIMEOUT_S,
        stdin=subprocess.DEVNULL,
    )
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()
        raise RuntimeError(err[-500:] or f"exit {p.returncode}")
    out = (p.stdout or "").strip()
    # First non-empty line is enough for the log line.
    for line in out.splitlines():
        if line.strip():
            return line.strip()[:120]
    return "ok"


HANDLERS = {
    "codex": _heartbeat_codex,
    "claude": _heartbeat_claude,
    "antigravity": _heartbeat_antigravity,
}


def run_once(conn, account_id: int | None = None) -> int:
    with work_queue.single_worker("heartbeat") as acquired:
        if not acquired:
            print("heartbeat already running; skipped")
            return 0

        all_accounts = store.list_accounts(conn)
        agy_count = sum(1 for a in all_accounts
                        if a["provider"] == "antigravity" and not a.get("disabled"))
        accounts = [
            a for a in all_accounts
            if a["provider"] in PROVIDERS and not a.get("disabled")
            and (account_id is None or a["id"] == account_id)
        ]
        if not accounts:
            print("(no codex/claude/antigravity accounts)")
            return 0

        print(
            f"[{_ts()}] Heartbeat {len(accounts)} accounts "
            f"(codex={CODEX_MODEL}/{CODEX_EFFORT}, "
            f"claude={CLAUDE_MODEL}, agy={AGY_MODEL})..."
        )
        ok = fail = 0
        for a in accounts:
            label = f"{a['provider']:12} {a['email'] or a['label']}"
            if a["provider"] == "antigravity" and agy_count > 1:
                # `agy` always uses the globally logged-in Google session, so
                # with several pool accounts the heartbeat could burn quota on
                # the wrong one. Skip until per-account isolation exists.
                msg = (f"skipped: {agy_count} antigravity accounts in pool; "
                       "agy CLI uses the global login (wrong-account burn risk)")
                store.log_event(conn, a["id"], "heartbeat", False, msg)
                print(f"  ⚠ {label}: {msg}")
                continue
            token = store.get_token(conn, a["id"])
            if not token or not token.get("access_token"):
                store.log_event(conn, a["id"], "heartbeat", False, "no token")
                print(f"  ✗ {label}: no token")
                fail += 1
                continue
            token = _refresh_if_needed(conn, a, token)
            handler = HANDLERS.get(a["provider"])
            if not handler:
                store.log_event(conn, a["id"], "heartbeat", False, "no handler")
                print(f"  ✗ {label}: no handler")
                fail += 1
                continue
            try:
                reply = handler(a, token)
                store.log_event(conn, a["id"], "heartbeat", True, reply[:200])
                print(f"  ✓ {label}: {reply[:80]}")
                ok += 1
            except Exception as e:
                msg = str(e)[:200]
                store.log_event(conn, a["id"], "heartbeat", False, msg)
                print(f"  ✗ {label}: {msg}")
                fail += 1
        print(f"[{_ts()}] Heartbeat done: {ok} ok, {fail} failed")
        return 0 if fail == 0 else 1


def run_loop(conn) -> int:
    print(f"Heartbeat daemon started. Interval: {INTERVAL_S}s ({INTERVAL_S // 3600}h). Ctrl+C to stop.")
    while True:
        try:
            run_once(conn)
        except KeyboardInterrupt:
            print("\nHeartbeat daemon stopped.")
            return 0
        except Exception as e:
            print(f"[{_ts()}] Heartbeat cycle error: {e}")
        try:
            time.sleep(INTERVAL_S)
        except KeyboardInterrupt:
            print("\nHeartbeat daemon stopped.")
            return 0
