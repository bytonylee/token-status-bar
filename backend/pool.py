#!/usr/bin/env python3
"""agent-pool CLI — onboarding, status, and poller control.

Usage:
  pool.py add <provider> [label] [--incognito]  Onboard a new account (opens OAuth browser)
                                       provider: codex|claude|xai|antigravity|copilot|devin
                                       --incognito: open a private browser window (use when
                                                    adding a 2nd account on the same provider)
  pool.py add-devin <api_key> [label]  Add Devin account by API key
  pool.py list                         List all accounts
  pool.py remove <account_id>          Remove an account
  pool.py status                       Show all accounts + latest limit status
  pool.py poll                         Run one poll cycle (hit all limit endpoints)
  pool.py poll-loop                    Run poller daemon (5-min interval)
  pool.py refresh <account_id>         Refresh token for one account
  pool.py refresh-all                  Refresh all expiring tokens
  pool.py reset <account_id>           Redeem a Codex banked reset credit
  pool.py set-tier <account_id> <tier> Set manual tier override (e.g. 5x, 20x)
  pool.py export-status                Write status JSON for the menu bar app
"""
from __future__ import annotations
import json, os, sys, time, datetime
from pathlib import Path
import store, oauth, work_queue

DB = store.connect()


from status import ts_fmt


def human_secs(s):
    if s is None: return "n/a"
    s = int(s)
    if s < 60: return f"{s}s"
    if s < 3600: return f"{s//60}m"
    if s < 86400: return f"{s/3600:.1f}h"
    return f"{s/86400:.1f}d"


# ─── add ───────────────────────────────────────────────────────────────────
def cmd_add(provider, label=None, incognito=False):
    if provider == "devin":
        print("Devin uses API keys, not OAuth. Use: pool.py add-devin <api_key> [label]")
        return 1
    if provider not in oauth.LOGIN_FUNCS:
        print(f"Unknown provider: {provider}. Choose: {', '.join(oauth.PROVIDERS)}")
        return 1
    if label is None:
        existing = len([a for a in store.list_accounts(DB) if a["provider"] == provider])
        label = f"{provider} #{existing + 1}"
    print(f"\n=== Onboarding {label} ({provider}) ===")
    try:
        result = oauth.LOGIN_FUNCS[provider](incognito=incognito)
    except Exception as e:
        print(f"OAuth failed: {e}")
        store.log_event(DB, None, "onboard", False, str(e))
        return 1
    email = result.get("email") or "unknown"
    acct_id = store.upsert_account(DB, provider, email, label, result.get("plan"), result.get("account_id"))
    store.save_token(DB, acct_id, result["access_token"], result.get("refresh_token"),
                     result.get("id_token"), result.get("expires_at"), result.get("raw"))
    store.log_event(DB, acct_id, "onboard", True, f"{provider} {email}")
    print(f"✓ Saved: {provider} / {email} (account #{acct_id})")
    # Poll immediately so the new account has subscription data right away
    # instead of waiting for the next 5-minute poll cycle.
    import poller
    account = store.get_account(DB, acct_id)
    print("Fetching initial subscription data...")
    poller.poll_account(DB, account)
    return 0


def cmd_add_devin(api_key, label=None):
    label = label or "devin #1"
    print(f"\n=== Onboarding {label} (devin) ===")
    try:
        result = oauth.login_devin(api_key)
    except Exception as e:
        print(f"Devin key validation failed: {e}")
        store.log_event(DB, None, "onboard", False, str(e))
        return 1
    email = result.get("email") or "devin-user"
    acct_id = store.upsert_account(DB, "devin", email, label, result.get("plan"), result.get("account_id"))
    store.save_token(DB, acct_id, result["access_token"], result.get("refresh_token"),
                     result.get("id_token"), result.get("expires_at"), result.get("raw"))
    store.log_event(DB, acct_id, "onboard", True, f"devin {email}")
    print(f"✓ Saved: devin / {email} (account #{acct_id})")
    # Poll immediately so the new account has subscription data right away
    # instead of waiting for the next 5-minute poll cycle.
    import poller
    account = store.get_account(DB, acct_id)
    print("Fetching initial subscription data...")
    poller.poll_account(DB, account)
    return 0


# ─── list ──────────────────────────────────────────────────────────────────
def cmd_list():
    accounts = store.list_accounts(DB)
    if not accounts:
        print("(no accounts yet)")
        return 0
    print(f"{'ID':>3} {'PROVIDER':12} {'EMAIL':34} {'LABEL':20} {'PLAN':6} {'TOKEN_EXPIRES':18}")
    print("-" * 100)
    for a in accounts:
        tok = store.get_token(DB, a["id"])
        exp = ts_fmt(tok["expires_at"]) if tok else "no token"
        print(f"{a['id']:>3} {a['provider']:12} {a['email'] or '?':34} {a['label'] or '':20} {a['plan'] or '?':6} {exp}")
    return 0


# ─── remove ────────────────────────────────────────────────────────────────
def cmd_remove(account_id):
    a = store.get_account(DB, int(account_id))
    if not a:
        print(f"Account {account_id} not found")
        return 1
    store.delete_account(DB, int(account_id))
    try:
        import status
        status.cmd_export(DB)
    except Exception as e:
        print(f"  export-status failed: {e}")
    print(f"Removed: {a['provider']} / {a['email']}")
    return 0


# ─── refresh ───────────────────────────────────────────────────────────────
def cmd_refresh(account_id):
    a = store.get_account(DB, int(account_id))
    if not a:
        print(f"Account {account_id} not found")
        return 1
    tok = store.get_token(DB, int(account_id))
    if not tok or not tok["refresh_token"]:
        print(f"No refresh token for account {account_id}")
        return 1
    provider = a["provider"]
    if provider not in oauth.REFRESH_FUNCS:
        print(f"No refresh function for {provider}")
        return 1
    try:
        if provider == "antigravity":
            client_id, client_secret = oauth._load_antigravity_creds()
            oauth.ANTIGRAVITY["client_id"] = client_id
            oauth.ANTIGRAVITY["client_secret"] = client_secret
        result = oauth.REFRESH_FUNCS[provider](tok["refresh_token"])
        store.save_token(DB, int(account_id), result["access_token"],
                         result.get("refresh_token"), result.get("id_token"),
                         result.get("expires_at"), result.get("raw"))
        store.log_event(DB, int(account_id), "token_refresh", True, "")
        print(f"✓ Refreshed {provider} / {a['email']}")
        return 0
    except Exception as e:
        store.log_event(DB, int(account_id), "token_refresh", False, str(e))
        print(f"Refresh failed: {e}")
        return 1


def cmd_refresh_all():
    with work_queue.single_worker("refresh") as acquired:
        if not acquired:
            print("refresh already running; queued worker skipped")
            return 0
        accounts = store.list_accounts(DB)
        queue = []
        for a in accounts:
            tok = store.get_token(DB, a["id"])
            if not tok or not tok["refresh_token"]:
                continue
            # Refresh if token expires within 1 hour.
            if tok["expires_at"] and tok["expires_at"] - time.time() < 3600:
                queue.append(a)
        refreshed = 0
        print(f"Queued {len(queue)} token refreshes.")
        for i, a in enumerate(queue, start=1):
            print(f"[{i}/{len(queue)}] Refreshing {a['provider']} / {a['email']}...")
            rc = cmd_refresh(str(a["id"]))
            if rc == 0:
                refreshed += 1
            time.sleep(1)
        print(f"\nRefreshed {refreshed} tokens.")
    return 0


# ─── main ──────────────────────────────────────────────────────────────────
def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "add":
        args = argv[1:]
        incognito = False
        if "--incognito" in args:
            incognito = True
            args.remove("--incognito")
        elif "-i" in args:
            incognito = True
            args.remove("-i")
        if not args:
            print("usage: pool.py add <provider> [label] [--incognito]")
            return 1
        return cmd_add(args[0], args[1] if len(args) > 1 else None, incognito)
    if cmd == "add-devin":
        if len(argv) < 2:
            print("usage: pool.py add-devin <api_key> [label]")
            return 1
        return cmd_add_devin(argv[1], argv[2] if len(argv) > 2 else None)
    if cmd == "list":
        return cmd_list()
    if cmd == "remove":
        if len(argv) < 2:
            print("usage: pool.py remove <account_id>")
            return 1
        return cmd_remove(argv[1])
    if cmd == "refresh":
        if len(argv) < 2:
            print("usage: pool.py refresh <account_id>")
            return 1
        return cmd_refresh(argv[1])
    if cmd == "refresh-all":
        return cmd_refresh_all()
    if cmd == "status":
        import status
        return status.cmd_status(DB)
    if cmd == "poll":
        import poller
        return poller.run_once(DB)
    if cmd == "poll-loop":
        import poller
        return poller.run_loop(DB)
    if cmd == "export-status":
        import status
        return status.cmd_export(DB)
    if cmd == "reset":
        import poller
        if len(argv) < 2:
            print("usage: pool.py reset <account_id>")
            return 1
        return poller.redeem_reset(DB, int(argv[1]))
    if cmd == "set-tier":
        if len(argv) < 3:
            print("usage: pool.py set-tier <account_id> <tier>")
            return 1
        a = store.get_account(DB, int(argv[1]))
        if not a:
            print(f"Account {argv[1]} not found")
            return 1
        store.set_tier(DB, int(argv[1]), argv[2])
        print(f"✓ Set {a['provider']}/{a['email']} tier override: {argv[2]}")
        return 0
    print(f"Unknown command: {cmd}\n{__doc__}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
