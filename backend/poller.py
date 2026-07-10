"""Poller — hits limit endpoints for every account every 5 minutes.

Limit sources:
  codex:        chatgpt.com/backend-api/wham/usage + /wham/rate-limit-reset-credits
  claude:       console.anthropic.com/v1/messages probe (x-ratelimit-* headers)
  xai:          cli-chat-proxy.grok.com/v1/billing (monthly credits) + api.x.ai chat headers (daily)
  antigravity:  cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels (per-model remainingFraction)
  copilot:      api.github.com/copilot_internal/user (quota_snapshots.premium_interactions)
  devin:        server.codeium.com GetUserStatus protobuf (daily/weekly quota %)
"""
from __future__ import annotations
import json, os, re, sys, time, datetime, urllib.request, urllib.error
import store, oauth, work_queue

WHAM = "https://chatgpt.com/backend-api"
POLL_INTERVAL = int(os.environ.get("AGENT_POOL_POLL_INTERVAL", "300"))  # 5 min

# IDE headers required for copilot_internal/user to return quota_snapshots.
COPILOT_IDE_HEADERS = {
    "Accept-Encoding": "identity",
    "Editor-Version": "vscode/1.107.0",
    "Editor-Plugin-Version": "copilot-chat/0.35.0",
    "User-Agent": "GitHubCopilotChat/0.35.0",
    "X-Github-Api-Version": "2025-04-01",
}


def _get(url, headers, timeout=15):
    req = urllib.request.Request(url, method="GET")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = raw.decode(errors="replace")
            return r.status, body, dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = e.read().decode(errors="replace")
        return e.code, body, dict(e.headers)
    except urllib.error.URLError as e:
        return 0, str(e.reason), {}


def _post(url, data, headers, timeout=15):
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw), dict(r.headers)
            except json.JSONDecodeError:
                return r.status, raw.decode(errors="replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read()), dict(e.headers)
        except Exception:
            return e.code, e.read().decode(errors="replace"), dict(e.headers)
    except urllib.error.URLError as e:
        return 0, str(e.reason), {}


# ─── Codex wham ────────────────────────────────────────────────────────────
def poll_codex(conn, account, token):
    aid = account["account_id"] or ""
    headers = {"Authorization": f"Bearer {token['access_token']}",
               "ChatGPT-Account-Id": aid, "User-Agent": "agent-pool/1.0",
               "OAI-Product-Sku": "codex"}
    # usage
    st, usage, _ = _get(f"{WHAM}/wham/usage", headers)
    snap = {"status": "active", "status_message": "", "raw_json": json.dumps({"usage": usage}) if isinstance(usage, dict) else str(usage)}
    if st != 200:
        snap["status"] = "error"
        snap["status_message"] = f"wham/usage HTTP {st}: {str(usage)[:120]}"
        store.save_snapshot(conn, account["id"], snap)
        store.log_event(conn, account["id"], "limit_poll", False, snap["status_message"])
        return
    if isinstance(usage, dict):
        snap["plan"] = usage.get("plan_type")
        rl = usage.get("rate_limit") or {}
        pw = rl.get("primary_window") or {}
        sw = rl.get("secondary_window") or {}
        snap["primary_used_pct"] = pw.get("used_percent")
        snap["primary_reset_at"] = (time.time() + pw["reset_after_seconds"]) if pw.get("reset_after_seconds") is not None else None
        snap["primary_window_s"] = pw.get("limit_window_seconds")
        snap["secondary_used_pct"] = sw.get("used_percent")
        snap["secondary_reset_at"] = (time.time() + sw["reset_after_seconds"]) if sw.get("reset_after_seconds") is not None else None
        snap["secondary_window_s"] = sw.get("limit_window_seconds")
        snap["credits_balance"] = (usage.get("credits") or {}).get("balance")
        snap["banked_resets"] = (usage.get("rate_limit_reset_credits") or {}).get("available_count")
    store.save_snapshot(conn, account["id"], snap)

    # reset credits detail
    st2, credits, _ = _get(f"{WHAM}/wham/rate-limit-reset-credits", headers)
    if st2 == 200 and isinstance(credits, dict):
        store.replace_reset_credits(conn, account["id"], credits.get("credits") or [])
    _sync_codex_subscription_meta(conn, account, headers)
    store.log_event(conn, account["id"], "limit_poll", True, "")


def _sync_codex_subscription_meta(conn, account, headers):
    """Best-effort ChatGPT account metadata sync for Codex subscriptions."""
    aid = account.get("account_id") or ""
    if not aid:
        return
    existing = store.get_subscription_meta(conn, account["id"]) or {}
    account_created_at = existing.get("account_created_at")

    st, accounts_body, _ = _get(f"{WHAM}/accounts", headers)
    if st == 200 and isinstance(accounts_body, dict):
        for item in accounts_body.get("items") or []:
            if isinstance(item, dict) and item.get("id") == aid:
                account_created_at = item.get("created_time") or account_created_at
                break

    st, check_body, _ = _get(f"{WHAM}/accounts/check/v4-2023-04-27", headers)
    if st != 200 or not isinstance(check_body, dict):
        return
    entry = (check_body.get("accounts") or {}).get(aid) or {}
    acct = entry.get("account") or {}
    ent = entry.get("entitlement") or {}
    if not acct and not ent:
        return

    renews_at = ent.get("renews_at")
    expires_at = ent.get("expires_at")
    gratis = ent.get("is_active_subscription_gratis")
    plan = ent.get("subscription_plan")
    if gratis:
        note = f"active free promotion; expires_at={expires_at}" if expires_at else "active free promotion"
    elif ent.get("has_active_subscription"):
        note = f"active paid subscription; renews_at={renews_at}" if renews_at else "active paid subscription"
    else:
        note = "subscription metadata synced from accounts/check"

    store.upsert_subscription_meta(
        conn,
        account["id"],
        paid_since=existing.get("paid_since"),
        renews_at=renews_at,
        expires_at=expires_at,
        account_created_at=account_created_at,
        subscription_plan=plan,
        has_active_subscription=ent.get("has_active_subscription"),
        is_active_subscription_gratis=gratis,
        has_previously_paid_subscription=acct.get("has_previously_paid_subscription"),
        previous_paid_months=existing.get("previous_paid_months"),
        billing_note=note,
    )


# ─── Claude headers ────────────────────────────────────────────────────────
def poll_claude(conn, account, token):
    # Minimal 1-token messages call to read rate-limit headers
    # Claude returns: anthropic-ratelimit-unified-5h-utilization (0.07 = 7%)
    #                 anthropic-ratelimit-unified-5h-reset (epoch)
    #                 anthropic-ratelimit-unified-7d-utilization, -7d-reset
    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token['access_token']}")
    req.add_header("anthropic-version", "2023-06-01")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            hdrs = dict(r.headers)
            snap = _claude_snap(hdrs, "active", "")
    except urllib.error.HTTPError as e:
        hdrs = dict(e.headers)
        msg = e.read().decode(errors="replace")[:120]
        snap = _claude_snap(hdrs, "error" if e.code != 429 else "rate_limited",
                            f"HTTP {e.code}: {msg}")
    except urllib.error.URLError as e:
        snap = {"status": "error", "status_message": str(e.reason)}
    # Enrich with subscription profile (best-effort, never fails the poll)
    profile = _claude_profile(token)
    if profile:
        if profile.get("plan"):
            snap["plan"] = profile["plan"]
        try:
            rj = json.loads(snap.get("raw_json") or "{}")
        except Exception:
            rj = {}
        if not isinstance(rj, dict):
            rj = {}
        rj["profile"] = profile
        snap["raw_json"] = json.dumps(rj)
    # Step 2: probe claude-fable-5 to read its model-scoped weekly window
    # (anthropic-ratelimit-unified-7d_oi-*), the separate "Fable" weekly limit
    # shown in Claude's usage UI. These headers only appear on responses to
    # requests for that model tier, so an active probe is required. Best-effort:
    # a failed/missing probe leaves the fable window empty without failing poll.
    fable = _claude_fable_window(token)
    if fable:
        try:
            rj = json.loads(snap.get("raw_json") or "{}")
        except Exception:
            rj = {}
        if not isinstance(rj, dict):
            rj = {}
        rj["fable"] = fable
        snap["raw_json"] = json.dumps(rj)
    store.save_snapshot(conn, account["id"], snap)
    store.log_event(conn, account["id"], "limit_poll", snap["status"] == "active", snap.get("status_message", ""))


def _claude_profile(token):
    """Fetch Claude subscription/account profile via the OAuth profile endpoint."""
    try:
        st, body, _ = _get("https://api.anthropic.com/api/oauth/profile", {
            "Authorization": f"Bearer {token['access_token']}",
            "anthropic-version": "2023-06-01",
        })
    except Exception:
        return None
    if st != 200 or not isinstance(body, dict):
        return None
    acct = body.get("account") or {}
    org = body.get("organization") or {}
    if acct.get("has_claude_max"):
        plan = "Claude Max"
    elif acct.get("has_claude_pro"):
        plan = "Claude Pro"
    else:
        ot = org.get("organization_type") or ""
        plan = ot.replace("_", " ").title() or None
    return {
        "plan": plan,
        "subscription_status": org.get("subscription_status"),
        "billing_type": org.get("billing_type"),
        "rate_limit_tier": org.get("rate_limit_tier"),
        "extra_usage_enabled": org.get("has_extra_usage_enabled"),
        "subscription_created_at": org.get("subscription_created_at"),
        "organization_type": org.get("organization_type"),
        "display_name": acct.get("display_name"),
        "full_name": acct.get("full_name"),
        "org_name": org.get("name"),
        "member_since": acct.get("created_at"),
    }


def _claude_snap(hdrs, status, msg):
    raw = {k: v for k, v in hdrs.items() if "ratelimit" in k.lower()}
    snap = {"status": status, "status_message": msg, "raw_json": json.dumps({"ratelimit": raw})}
    # 5h window
    util_5h = hdrs.get("anthropic-ratelimit-unified-5h-utilization")
    reset_5h = hdrs.get("anthropic-ratelimit-unified-5h-reset")
    if util_5h is not None:
        snap["primary_used_pct"] = float(util_5h) * 100
    if reset_5h:
        snap["primary_reset_at"] = float(reset_5h)
        snap["primary_window_s"] = 18000  # 5h
    # 7d window
    util_7d = hdrs.get("anthropic-ratelimit-unified-7d-utilization")
    reset_7d = hdrs.get("anthropic-ratelimit-unified-7d-reset")
    if util_7d is not None:
        snap["secondary_used_pct"] = float(util_7d) * 100
    if reset_7d:
        snap["secondary_reset_at"] = float(reset_7d)
        snap["secondary_window_s"] = 604800  # 7d
    # Generic fields for display
    snap["rate_limit_remaining"] = hdrs.get("anthropic-ratelimit-unified-status")
    snap["rate_limit_reset"] = reset_5h
    snap["rate_limit_limit"] = "unified"
    return snap


def _claude_fable_window(token):
    """Probe claude-fable-5 to read its model-scoped weekly rate-limit window.

    Claude exposes a separate weekly limit for the top model tier ("Fable" in
    the usage UI) via headers shaped anthropic-ratelimit-unified-<window>-*
    where <window> is e.g. 7d_oi. These only appear on responses to requests
    for that model tier, so we send a 1-token Claude Code-shaped probe.
    Matched generically so a renamed or newly added 7d_<label> window is
    picked up as-is. Best-effort: returns None on any failure so the main poll
    stays green.
    """
    body = json.dumps({
        "model": "claude-fable-5",
        "max_tokens": 1,
        "system": "You are Claude Code, Anthropic's official CLI for Claude.",
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token['access_token']}")
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("anthropic-beta", "oauth-2025-04-20")
    hdrs = None
    http_code = None
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            hdrs = dict(r.headers)
    except urllib.error.HTTPError as e:
        # Rate-limit headers (incl. the 7d_oi Fable window) are often attached
        # to 429/error responses too — read them so a throttled probe still
        # reports the Fable weekly utilization.
        hdrs = dict(e.headers)
        http_code = e.code
    except urllib.error.URLError:
        return None
    win = None
    for key, val in hdrs.items():
        m = re.match(r"^anthropic-ratelimit-unified-(7d_[a-z0-9_]+)-(utilization|reset|status)$",
                     key, re.IGNORECASE)
        if not m:
            continue
        label, field = m.group(1), m.group(2)
        if win is None:
            win = {"label": label, "used_pct": None, "reset_at": None, "status": None}
        if field == "utilization":
            try:
                win["used_pct"] = float(val) * 100
            except (TypeError, ValueError):
                pass
        elif field == "reset":
            try:
                win["reset_at"] = float(val)
            except (TypeError, ValueError):
                pass
        else:
            win["status"] = val
    # No 7d_<label> headers but a 429 means the Fable tier is rate-limited (or
    # not yet provisioned) — record the state so the UI can surface a Fable row
    # instead of silently omitting it.
    if win is None and http_code == 429:
        win = {"label": None, "used_pct": None, "reset_at": None, "status": "rate_limited"}
    return win


# ─── xAI headers ───────────────────────────────────────────────────────────
def poll_xai(conn, account, token):
    # Step 1: Query the billing API for monthly credit usage
    # cli-chat-proxy.grok.com/v1/billing returns real-time monthly usage
    at = token["access_token"]
    snap = {}
    try:
        req = urllib.request.Request("https://cli-chat-proxy.grok.com/v1/billing", method="GET")
        req.add_header("Authorization", f"Bearer {at}")
        req.add_header("X-XAI-Token-Auth", "xai-grok-cli")
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
            config = resp.get("config", {})
            used = config.get("used", {}).get("val", 0)
            limit = config.get("monthlyLimit", {}).get("val", 0)
            period_end = config.get("billingPeriodEnd", "")
            period_start = config.get("billingPeriodStart", "")
            pct = (used / limit * 100) if limit > 0 else 0
            snap["monthly_used"] = used
            snap["monthly_limit"] = limit
            snap["monthly_used_pct"] = pct
            snap["monthly_period_start"] = period_start
            snap["monthly_period_end"] = period_end
            # Parse the period end into a reset timestamp
            import datetime as dt
            try:
                reset_dt = dt.datetime.fromisoformat(period_end.replace("Z", "+00:00"))
                snap["primary_reset_at"] = reset_dt.timestamp()
            except Exception:
                pass
            snap["primary_used_pct"] = pct
            snap["primary_window_s"] = 2592000  # ~30 days
            snap["rate_limit_remaining"] = f"{limit - used} credits"
            snap["rate_limit_limit"] = f"{limit} credits/month"
            snap["rate_limit_reset"] = period_end[:10] if period_end else "monthly"
            snap["status"] = "active"
            snap["status_message"] = ""
            snap["raw_json"] = json.dumps({"extra": {
                "credits_used": used,
                "credits_limit": limit,
                "on_demand_cap": config.get("onDemandCap", {}).get("val"),
                "period_start": period_start,
                "period_end": period_end,
            }})
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")[:120]
        snap = {"status": "error", "status_message": f"billing HTTP {e.code}: {msg}"}
    except urllib.error.URLError as e:
        snap = {"status": "error", "status_message": str(e.reason)}

    # Step 2: Also probe the chat API for daily rate-limit headers
    body = json.dumps({
        "model": "grok-4",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    req = urllib.request.Request("https://api.x.ai/v1/chat/completions", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {at}")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            hdrs = dict(r.headers)
            daily = _xai_snap(hdrs, "active", "")
            # Merge daily rate-limit data as secondary window
            if "primary_used_pct" in daily:
                snap["secondary_used_pct"] = daily["primary_used_pct"]
                snap["secondary_window_s"] = 86400
            if "rate_limit_remaining" in daily:
                snap["daily_remaining"] = daily["rate_limit_remaining"]
    except urllib.error.HTTPError as e:
        hdrs = dict(e.headers)
        msg = e.read().decode(errors="replace")[:120]
        if e.code == 429:
            daily = _xai_snap(hdrs, "rate_limited", f"429: {msg}")
            snap["secondary_used_pct"] = daily.get("primary_used_pct", 100.0)
            snap["secondary_window_s"] = 86400
            if snap["status"] == "active":
                snap["status"] = "rate_limited"
                snap["status_message"] = "daily rate limited"
        # Don't override error status from billing
    except urllib.error.URLError as e:
        pass  # Daily probe is best-effort

    store.save_snapshot(conn, account["id"], snap)
    store.log_event(conn, account["id"], "limit_poll", snap["status"] == "active", snap.get("status_message", ""))


def _xai_snap(hdrs, status, msg):
    raw = {k: v for k, v in hdrs.items() if "ratelimit" in k.lower()}
    snap = {"status": status, "status_message": msg, "raw_json": json.dumps(raw)}
    # xAI exposes per-day request + token limits via headers.
    # Monthly limits exist (tier-based spend) but are NOT exposed via API headers.
    # Request-based rate limit (per-day window)
    limit_req = hdrs.get("x-ratelimit-limit-requests")
    remaining_req = hdrs.get("x-ratelimit-remaining-requests")
    if limit_req and remaining_req:
        lim = float(limit_req)
        rem = float(remaining_req)
        used = lim - rem
        snap["primary_used_pct"] = (used / lim * 100) if lim > 0 else 0
        snap["primary_window_s"] = 86400  # 24h
        snap["rate_limit_limit"] = str(int(lim))
        snap["rate_limit_remaining"] = str(int(rem))
    # Token-based rate limit (per-day)
    limit_tok = hdrs.get("x-ratelimit-limit-tokens")
    remaining_tok = hdrs.get("x-ratelimit-remaining-tokens")
    if limit_tok and remaining_tok:
        lim = float(limit_tok)
        rem = float(remaining_tok)
        used = lim - rem
        snap["secondary_used_pct"] = (used / lim * 100) if lim > 0 else 0
        snap["secondary_window_s"] = 86400
    # Reset timestamp (xAI returns a human-readable string like "1h23m45s" or "1d")
    reset_req = hdrs.get("x-ratelimit-reset-requests")
    if reset_req:
        snap["rate_limit_reset"] = reset_req
        # Try to parse as duration for primary_reset_at
        snap["primary_reset_at"] = _xai_parse_reset(reset_req)
    else:
        snap["rate_limit_reset"] = "daily"
    return snap


def _xai_parse_reset(s):
    """Parse xAI reset string like '1h23m45s' or '23h59m' into epoch time."""
    import re
    total = 0
    m = re.match(r'(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?', s)
    if m:
        d, h, mi, se = m.groups()
        if d: total += int(d) * 86400
        if h: total += int(h) * 3600
        if mi: total += int(mi) * 60
        if se: total += int(se)
    return time.time() + total if total > 0 else None


# ─── Antigravity / Google quota ────────────────────────────────────────────
def poll_antigravity(conn, account, token):
    # Step 1: loadCodeAssist to get tier info + project ID
    url = "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
    body = json.dumps({"metadata": {}}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token['access_token']}")
    req.add_header("Accept", "*/*")
    req.add_header("User-Agent", "antigravity/cli/1.0.13 (aidev_client; os_type=darwin; arch=arm64)")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
            current = resp.get("currentTier", {})
            paid = resp.get("paidTier") or {}
            allowed = resp.get("allowedTiers") or []
            # The session's active Code Assist tier is currentTier (often free-tier),
            # but the user's actual entitlement lives in paidTier. Prefer paidTier.
            tier = paid if paid.get("id") else current
            if not tier.get("id") and allowed:
                tier = allowed[0]
            plan = tier.get("name", "Gemini Code Assist")
            tier_id = tier.get("id")
            tier_desc = tier.get("description")
            active_tier_id = current.get("id")
            project = resp.get("cloudaicompanionProject", "")
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")[:120]
        snap = {"status": "error", "status_message": f"loadCodeAssist HTTP {e.code}: {msg}"}
        store.save_snapshot(conn, account["id"], snap)
        store.log_event(conn, account["id"], "limit_poll", False, snap["status_message"])
        return
    except urllib.error.URLError as e:
        snap = {"status": "error", "status_message": str(e.reason)}
        store.save_snapshot(conn, account["id"], snap)
        store.log_event(conn, account["id"], "limit_poll", False, snap["status_message"])
        return

    # Step 2: fetch real-time per-model quota via fetchAvailableModels.
    # Response: models[<key>].quotaInfo.remainingFraction (0..1) + .resetTime (ISO8601).
    # This is a metadata call — it reports quota without consuming a request.
    models_url = "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels"
    payload = json.dumps({"project": project}).encode()
    req = urllib.request.Request(models_url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token['access_token']}")
    req.add_header("Accept", "*/*")
    req.add_header("User-Agent", "antigravity")

    snap = {"status": "active", "status_message": "", "plan": plan}
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read())
        snap.update(_antigravity_quota(resp.get("models", {}), plan))
    except urllib.error.HTTPError as e:
        body_resp = e.read().decode(errors="replace")[:120]
        snap["status"] = "error"
        snap["status_message"] = f"fetchAvailableModels HTTP {e.code}: {body_resp}"
    except urllib.error.URLError as e:
        snap["status"] = "error"
        snap["status_message"] = str(e.reason)

    try:
        import agy_usage
        usage_windows = agy_usage.fetch_usage()
    except Exception:
        usage_windows = None

    snap["raw_json"] = json.dumps({"extra": {
        "tier_id": tier_id,
        "tier_description": tier_desc,
        "active_tier": active_tier_id if active_tier_id and active_tier_id != tier_id else None,
        "usage_windows": usage_windows,
    }})
    store.save_snapshot(conn, account["id"], snap)
    store.log_event(conn, account["id"], "limit_poll", snap["status"] == "active", snap.get("status_message", ""))


def _antigravity_quota(models, plan):
    """Reduce per-model quotaInfo to a single most-constrained window.

    Skips internal/non-chat models (tab completion, chat_* internal ids,
    legacy gemini 2.5, and image models) and reports the model with the
    least remaining quota as the headline usage number.
    """
    worst = None  # (remainingFraction, resetTime, label)
    for key, info in models.items():
        qi = info.get("quotaInfo")
        if not qi:
            continue
        label = info.get("displayName") or key
        low = label.lower()
        if (low.startswith("chat_") or low.startswith("rev19")
                or low.startswith("tab_") or "gemini 2.5" in low or "image" in low):
            continue
        frac = qi.get("remainingFraction")
        if frac is None:
            continue
        if worst is None or frac < worst[0]:
            worst = (frac, qi.get("resetTime"), label)

    if worst is None:
        return {"primary_used_pct": 0.0, "rate_limit_remaining": "available",
                "rate_limit_limit": plan, "rate_limit_reset": "unknown"}

    frac, reset_iso, label = worst
    used = max(0.0, min(100.0, (1.0 - frac) * 100.0))
    out = {
        "primary_used_pct": used,
        "primary_window_s": 0,
        "rate_limit_limit": plan,
        "rate_limit_remaining": f"{frac * 100:.0f}% left ({label})",
    }
    if reset_iso:
        try:
            out["primary_reset_at"] = datetime.datetime.fromisoformat(
                reset_iso.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    else:
        out["rate_limit_reset"] = "rolling"
    if frac <= 0:
        out["status"] = "rate_limited"
        out["status_message"] = "quota exhausted"
    return out


# ─── Copilot token refresh + rate limit ────────────────────────────────────
def poll_copilot(conn, account, token):
    # Step 1: refresh the copilot token. The copilot_internal/v2/token endpoint
    # intermittently returns HTTP 403 "Resource not accessible by integration" —
    # a transient GitHub-side entitlement re-check that resolves on the next
    # poll. Retry once after a short backoff; if it still fails, hold the last
    # good snapshot (so the menu bar doesn't flip red for one bad poll) and only
    # write an error when there is no prior active snapshot to hold.
    raw = json.loads(token["raw_json"]) if token["raw_json"] else {}
    github_token = raw.get("github_token") or token["access_token"]
    token_url = "https://api.github.com/copilot_internal/v2/token"
    token_hdrs = {"Authorization": f"token {github_token}",
                  "User-Agent": "agent-pool/1.0",
                  "X-GitHub-Api-Version": "2025-04-01"}
    st, resp, hdrs = _get(token_url, token_hdrs)
    if st in (403, 500, 502, 503, 504):
        time.sleep(2)
        st, resp, hdrs = _get(token_url, token_hdrs)
    if st != 200 or not isinstance(resp, dict):
        msg = f"token refresh HTTP {st}: {str(resp)[:120]}"
        prior = store.latest_snapshot(conn, account["id"])
        if prior and prior.get("status") == "active":
            # Hold the last good snapshot; log the transient failure for audit.
            store.log_event(conn, account["id"], "limit_poll", False, msg)
            return
        snap = {"status": "error", "status_message": msg}
        store.save_snapshot(conn, account["id"], snap)
        store.log_event(conn, account["id"], "limit_poll", False, snap["status_message"])
        return

    copilot_token = resp.get("token", "")
    expires = resp.get("expires_at", 0)
    sku = resp.get("sku", "")
    limited_quota = resp.get("limited_user_quotas")
    limited_reset = resp.get("limited_user_reset_date")
    raw["copilot_token"] = copilot_token
    raw["copilot_expires_at"] = expires
    store.save_token(conn, account["id"], token["access_token"], None, "",
                     expires or (time.time() + 7200), raw)

    # Step 2: fetch real-time premium-request quota via copilot_internal/user.
    # quota_snapshots.premium_interactions.percent_remaining is the headline number;
    # the plain v2/token endpoint reports limited_user_quotas=null for most SKUs.
    req = urllib.request.Request("https://api.github.com/copilot_internal/user",
                                 headers={"Authorization": f"token {github_token}",
                                          **COPILOT_IDE_HEADERS})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            user = json.loads(r.read())
        snap = _copilot_quota_snap(user, sku)
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")[:120]
        snap = {"status": "error" if e.code != 429 else "rate_limited",
                "status_message": f"user HTTP {e.code}: {msg}", "sku": sku}
    except urllib.error.URLError as e:
        snap = {"status": "error", "status_message": str(e.reason), "sku": sku}

    # Quota fields from the token response (populated on limited/free SKUs)
    if limited_quota is not None:
        snap["limited_user_quotas"] = limited_quota
    if limited_reset is not None:
        snap["limited_user_reset_date"] = limited_reset

    # Best-effort: real email/name via the public user endpoint. Email is often
    # null (private profile / token lacks user:email scope); captured when present.
    if snap.get("raw_json"):
        _copilot_attach_identity(snap, github_token)

    store.save_snapshot(conn, account["id"], snap)
    store.log_event(conn, account["id"], "limit_poll", snap["status"] == "active", snap.get("status_message", ""))


def _copilot_attach_identity(snap, github_token):
    """Merge github_email / github_name into snap['raw_json'] extra when available."""
    try:
        req = urllib.request.Request(
            "https://api.github.com/user",
            headers={"Authorization": f"token {github_token}",
                     "User-Agent": "agent-pool/1.0",
                     "X-GitHub-Api-Version": "2022-11-28"})
        with urllib.request.urlopen(req, timeout=15) as r:
            u = json.loads(r.read())
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
        return
    payload = json.loads(snap["raw_json"]) if snap.get("raw_json") else {}
    extra = payload.setdefault("extra", {})
    extra["github_email"] = u.get("email")
    extra["github_name"] = u.get("name")
    snap["raw_json"] = json.dumps(payload)


def _copilot_quota_snap(user, sku):
    """Build a snapshot from copilot_internal/user quota_snapshots.

    premium_interactions → primary window (premium requests),
    chat → secondary window. unlimited plans report 0% used.
    """
    qs = user.get("quota_snapshots") or {}
    pi = qs.get("premium_interactions") or {}
    ch = qs.get("chat") or {}
    reset_date = user.get("quota_reset_date")
    snap = {"status": "active", "status_message": "",
            "plan": user.get("copilot_plan"), "sku": sku}

    prem_rem = pi.get("percent_remaining")
    if pi.get("unlimited"):
        snap["primary_used_pct"] = 0.0
        snap["rate_limit_remaining"] = "unlimited premium"
        snap["rate_limit_limit"] = "premium (unlimited)"
    elif prem_rem is not None:
        snap["primary_used_pct"] = max(0.0, min(100.0, 100.0 - prem_rem))
        snap["primary_window_s"] = 0
        snap["rate_limit_remaining"] = f"{prem_rem:.1f}% premium left"
        snap["rate_limit_limit"] = "premium requests"
        if prem_rem <= 0:
            snap["status"] = "rate_limited"
            snap["status_message"] = "premium quota exhausted"
    else:
        snap["primary_used_pct"] = 0.0
        snap["rate_limit_remaining"] = "ok"
        snap["rate_limit_limit"] = "copilot quota"

    chat_rem = ch.get("percent_remaining")
    if chat_rem is not None and not ch.get("unlimited"):
        snap["secondary_used_pct"] = max(0.0, min(100.0, 100.0 - chat_rem))
        snap["secondary_window_s"] = 0

    if reset_date:
        snap["rate_limit_reset"] = reset_date
        try:
            snap["primary_reset_at"] = datetime.datetime.fromisoformat(
                reset_date).replace(tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            pass
    else:
        snap["rate_limit_reset"] = "monthly"

    snap["raw_json"] = json.dumps({
        "extra": {
            "access_sku": user.get("access_type_sku"),
            "premium_entitlement": pi.get("entitlement"),
            "premium_overage": pi.get("overage_count"),
            "chat_unlimited": ch.get("unlimited"),
            "completions_unlimited": (qs.get("completions") or {}).get("unlimited"),
            "can_upgrade": user.get("can_upgrade_plan"),
            "organizations": ", ".join(user.get("organization_login_list") or []) or None,
        },
        "plan": user.get("copilot_plan"),
        "reset": reset_date,
    })
    return snap


# ─── Devin quota via GetUserStatus Connect-RPC ─────────────────────────────
def poll_devin(conn, account, token):
    # Devin exposes quota via a Connect-RPC protobuf endpoint:
    #   POST /exa.seat_management_pb.SeatManagementService/GetUserStatus
    #   Content-Type: application/proto
    #   Authorization: Basic <session_token>
    # The response protobuf contains (in field 1.13):
    #   .14 = daily_quota_remaining_percent (0-100)
    #   .15 = weekly_quota_remaining_percent (0-100)
    #   .16 = credit balance (micros)
    #   .17 = daily_quota_reset_at_unix
    #   .18 = weekly_quota_reset_at_unix
    #   .2.1 = plan_start_unix
    #   .3.1 = plan_reset_unix (billing cycle end)
    # Field 2 = plan_info with .2 = plan name (e.g. "Pro")
    at = token["access_token"]

    # Build the protobuf request body
    body = _devin_build_request(at)

    url = "https://server.codeium.com/exa.seat_management_pb.SeatManagementService/GetUserStatus"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/proto")
    req.add_header("Connect-Protocol-Version", "1")
    req.add_header("Authorization", f"Basic {at}")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            snap = _devin_parse_response(raw)
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")[:120]
        snap = {"status": "error", "status_message": f"GetUserStatus HTTP {e.code}: {msg}"}
    except urllib.error.URLError as e:
        snap = {"status": "error", "status_message": str(e.reason)}
    store.save_snapshot(conn, account["id"], snap)
    store.log_event(conn, account["id"], "limit_poll", snap["status"] == "active", snap.get("status_message", ""))


def _devin_encode_varint(val):
    result = b''
    while val > 0x7f:
        result += bytes([0x80 | (val & 0x7f)])
        val >>= 7
    result += bytes([val & 0x7f])
    return result


def _devin_encode_str(field_num, s):
    data = s.encode('utf-8') if isinstance(s, str) else s
    return _devin_encode_varint((field_num << 3) | 2) + _devin_encode_varint(len(data)) + data


def _devin_build_request(at):
    """Build the GetUserStatusRequest protobuf."""
    inner = (
        _devin_encode_str(1, "chisel") +
        _devin_encode_str(2, "2026.8.18") +
        _devin_encode_str(3, at) +
        _devin_encode_str(4, "en") +
        _devin_encode_str(5, "mac") +
        _devin_encode_str(7, "2026.8.18") +
        _devin_encode_str(12, "chisel") +
        _devin_encode_str(31, "080d03eeaa0cd7a10d0e0c84c26cb9a1c533e2675c14a85c3a971248f6521a710e4c02372539fc56c8b6a0454553533dc7f9e54fa3c16a2b141c87d0fb43a8b6a7e32b15a2267290298a6c6382e7b0096e06b41012d46d998f947b53a35b84c55ab589c683c6a3727aa5bf90c18e349fba8ac069b4121f5298fbacca590c903f169850ec072da539ed40d46f212aea973c725221098fcea6fb6fde32a7ef324003cb070e8a603c8c7a1d6743cfadd9e86f53797b32bb88b11abe8bcc98ec38473496bd9aaf482c6ab2c5def224bb7f554687cd78202159112e1cdee29b1c44b38cd407629d59c9dc0e2eab891ccacca859f358d7641acedc7fed0ba64a4d3e3a4827ac433bae69f7e48917ff2a6df24e2adf4fb9ed88ed8266228b99604a7cba3356fc28cb304de708958d188143f12eff3d52178a680c86073b21bc1efbf45a44b09887b60e9fe13ae9e5256e640b7159a595dcd5ecb2b470a290cf30357403e4a820dfb0ce990517e2cd64")
    )
    return _devin_encode_varint((1 << 3) | 2) + _devin_encode_varint(len(inner)) + inner


def _devin_parse_field(data, pos):
    """Parse one protobuf field. Returns (field_num, wire_type, value, new_pos)."""
    key = 0; shift = 0
    while pos < len(data):
        b = data[pos]; pos += 1
        key |= (b & 0x7f) << shift
        shift += 7
        if not (b & 0x80): break
    field_num = key >> 3
    wire_type = key & 0x7
    if wire_type == 0:
        val = 0; shift = 0
        while pos < len(data):
            b = data[pos]; pos += 1
            val |= (b & 0x7f) << shift
            shift += 7
            if not (b & 0x80): break
        return field_num, 'varint', val, pos
    elif wire_type == 2:
        length = 0; shift = 0
        while pos < len(data):
            b = data[pos]; pos += 1
            length |= (b & 0x7f) << shift
            shift += 7
            if not (b & 0x80): break
        val = data[pos:pos+length]
        pos += length
        return field_num, 'bytes', val, pos
    elif wire_type == 5:
        val = data[pos:pos+4]; pos += 4
        return field_num, '32bit', val, pos
    elif wire_type == 1:
        val = data[pos:pos+8]; pos += 8
        return field_num, '64bit', val, pos
    return None, None, None, pos


def _devin_parse_response(raw):
    """Parse the GetUserStatusResponse protobuf."""
    snap = {"status": "active", "status_message": ""}
    # Top level: field 1 = user_status, field 2 = plan_info
    pos = 0
    user_status = None
    plan_info = None
    while pos < len(raw):
        fn, wt, val, pos = _devin_parse_field(raw, pos)
        if fn is None: break
        if fn == 1: user_status = val
        elif fn == 2: plan_info = val

    # Parse plan_info (field 2) for plan name
    if plan_info:
        sub_pos = 0
        while sub_pos < len(plan_info):
            sfn, swt, sval, sub_pos = _devin_parse_field(plan_info, sub_pos)
            if sfn is None: break
            if sfn == 2 and swt == 'bytes':
                try:
                    snap["plan"] = sval.decode('utf-8')
                except:
                    pass

    # Parse user_status (field 1) for quota info in field 13
    if user_status:
        sub_pos = 0
        while sub_pos < len(user_status):
            sfn, swt, sval, sub_pos = _devin_parse_field(user_status, sub_pos)
            if sfn is None: break
            if sfn == 13 and swt == 'bytes':
                # Field 1.13 = quota section
                quota = _devin_parse_quota(sval)
                snap.update(quota)
            if sfn == 7 and swt == 'bytes':
                try:
                    snap["email"] = sval.decode('utf-8')
                except:
                    pass
            if sfn == 3 and swt == 'bytes':
                try:
                    snap["name"] = sval.decode('utf-8')
                except:
                    pass

    # Set display fields from quota
    daily_pct = snap.get("daily_quota_remaining_percent")
    weekly_pct = snap.get("weekly_quota_remaining_percent")
    if daily_pct is not None:
        snap["primary_used_pct"] = 100.0 - daily_pct
        snap["primary_window_s"] = 86400  # daily
    if weekly_pct is not None:
        snap["secondary_used_pct"] = 100.0 - weekly_pct
        snap["secondary_window_s"] = 604800  # weekly
    daily_reset = snap.get("daily_quota_reset_at_unix")
    weekly_reset = snap.get("weekly_quota_reset_at_unix")
    if daily_reset:
        snap["primary_reset_at"] = float(daily_reset)
    if weekly_reset:
        snap["secondary_reset_at"] = float(weekly_reset)
    snap["rate_limit_remaining"] = f"{daily_pct or '?'}% daily, {weekly_pct or '?'}% weekly"
    snap["rate_limit_reset"] = f"daily: {daily_reset or '?'}, weekly: {weekly_reset or '?'}"
    snap["rate_limit_limit"] = snap.get("plan", "Devin")
    extra = {}
    if snap.get("credit_balance_micros") is not None:
        extra["credit_balance"] = snap["credit_balance_micros"] / 1_000_000
    if snap.get("plan_start_unix"):
        extra["plan_start_unix"] = snap["plan_start_unix"]
    if snap.get("plan_reset_unix"):
        extra["plan_reset_unix"] = snap["plan_reset_unix"]
    snap["raw_json"] = json.dumps({"extra": extra})
    return snap


def _devin_parse_quota(data):
    """Parse field 1.13 (quota section) of GetUserStatusResponse."""
    result = {}
    pos = 0
    while pos < len(data):
        fn, wt, val, pos = _devin_parse_field(data, pos)
        if fn is None: break
        if wt == 'varint':
            if fn == 14:
                result["daily_quota_remaining_percent"] = val
            elif fn == 15:
                result["weekly_quota_remaining_percent"] = val
            elif fn == 16:
                result["credit_balance_micros"] = val
            elif fn == 17:
                result["daily_quota_reset_at_unix"] = val
            elif fn == 18:
                result["weekly_quota_reset_at_unix"] = val
        elif wt == 'bytes' and fn in [2, 3]:
            # Nested message with field 1 = timestamp
            inner_pos = 0
            while inner_pos < len(val):
                ifn, iwt, ival, inner_pos = _devin_parse_field(val, inner_pos)
                if ifn is None: break
                if ifn == 1 and iwt == 'varint':
                    if fn == 2:
                        result["plan_start_unix"] = ival
                    elif fn == 3:
                        result["plan_reset_unix"] = ival
    return result


# ─── dispatch ──────────────────────────────────────────────────────────────
POLLERS = {
    "codex": poll_codex,
    "claude": poll_claude,
    "xai": poll_xai,
    "antigravity": poll_antigravity,
    "copilot": poll_copilot,
    "devin": poll_devin,
}


def _refresh_if_needed(conn, account, token):
    """Auto-refresh tokens that expire within 1 hour."""
    if not token or not token.get("refresh_token"):
        return token
    provider = account["provider"]
    if provider not in oauth.REFRESH_FUNCS:
        return token
    if token.get("expires_at") and token["expires_at"] - time.time() < 3600:
        try:
            if provider == "antigravity":
                client_id, client_secret = oauth._load_antigravity_creds()
                oauth.ANTIGRAVITY["client_id"] = client_id
                oauth.ANTIGRAVITY["client_secret"] = client_secret
            result = oauth.REFRESH_FUNCS[provider](token["refresh_token"])
            store.save_token(conn, account["id"], result["access_token"],
                             result.get("refresh_token"), result.get("id_token"),
                             result.get("expires_at"), result.get("raw"))
            store.log_event(conn, account["id"], "token_refresh", True, "")
            return store.get_token(conn, account["id"])
        except Exception as e:
            store.log_event(conn, account["id"], "token_refresh", False, str(e))
    return token


def poll_account(conn, account) -> bool:
    """Poll a single account and refresh status.json. Returns True on success.

    Used by onboarding so a freshly-added account immediately has subscription
    data instead of waiting for the next 5-minute poll cycle.
    """
    token = store.get_token(conn, account["id"])
    if not token:
        store.save_snapshot(conn, account["id"], {"status": "error", "status_message": "no token"})
        return False
    token = _refresh_if_needed(conn, account, token)
    poller = POLLERS.get(account["provider"])
    if not poller:
        store.save_snapshot(conn, account["id"],
                            {"status": "error", "status_message": f"no poller for {account['provider']}"})
        return False
    try:
        poller(conn, account, token)
        print(f"  ✓ {account['provider']:12} {account['email'] or account['label']}")
    except Exception as e:
        store.save_snapshot(conn, account["id"], {"status": "error", "status_message": str(e)[:200]})
        store.log_event(conn, account["id"], "limit_poll", False, str(e))
        print(f"  ✗ {account['provider']:12} {account['email'] or account['label']}: {e}")
        return False
    # Export so the menu bar app reflects the new account immediately.
    try:
        import status
        status.cmd_export(conn)
    except Exception as e:
        print(f"  export-status failed: {e}")
    return True


def run_once(conn) -> int:
    with work_queue.single_worker("poll") as acquired:
        if not acquired:
            print("poll already running; queued worker skipped")
            return 0
        accounts = store.list_accounts(conn)
        if not accounts:
            print("(no accounts to poll)")
            return 0
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Polling {len(accounts)} accounts...")
        for a in accounts:
            token = store.get_token(conn, a["id"])
            if not token:
                store.save_snapshot(conn, a["id"], {"status": "error", "status_message": "no token"})
                continue
            token = _refresh_if_needed(conn, a, token)
            poller = POLLERS.get(a["provider"])
            if not poller:
                store.save_snapshot(conn, a["id"], {"status": "error", "status_message": f"no poller for {a['provider']}"})
                continue
            try:
                poller(conn, a, token)
                print(f"  ✓ {a['provider']:12} {a['email'] or a['label']}")
            except Exception as e:
                store.save_snapshot(conn, a["id"], {"status": "error", "status_message": str(e)[:200]})
                store.log_event(conn, a["id"], "limit_poll", False, str(e))
                print(f"  ✗ {a['provider']:12} {a['email'] or a['label']}: {e}")
        # Export status JSON for the menu bar app
        try:
            import status
            status.cmd_export(conn)
        except Exception as e:
            print(f"  export-status failed: {e}")
    return 0


def run_loop(conn) -> int:
    print(f"Poller daemon started. Interval: {POLL_INTERVAL}s. Ctrl+C to stop.")
    while True:
        try:
            run_once(conn)
        except KeyboardInterrupt:
            print("\nPoller stopped.")
            return 0
        time.sleep(POLL_INTERVAL)


# ─── redeem reset credit ───────────────────────────────────────────────────
def redeem_reset(conn, account_id) -> int:
    a = store.get_account(conn, account_id)
    if not a:
        print(f"Account {account_id} not found")
        return 1
    if a["provider"] != "codex":
        print("Reset credits only available for Codex accounts")
        return 1
    token = store.get_token(conn, account_id)
    if not token:
        print("No token for this account")
        return 1
    credits = store.list_reset_credits(conn, account_id)
    available = [c for c in credits if c["status"] == "available"]
    if not available:
        print("No available banked reset credits")
        return 1
    from status import iso_fmt_exact
    print(f"{len(available)} available reset credits:")
    for i, c in enumerate(available):
        exp = iso_fmt_exact(c["expires_at"]) or c["expires_at"]
        print(f"  [{i}] {c['title'] or c['credit_id']}  expires: {exp}")
    try:
        idx = int(input("Pick one to redeem (number): "))
    except (ValueError, EOFError, KeyboardInterrupt):
        print("Cancelled")
        return 1
    credit = available[idx]
    import uuid
    st, resp = _post(f"{WHAM}/wham/rate-limit-reset-credits/consume",
                     {"credit_id": credit["credit_id"], "redeem_request_id": str(uuid.uuid4())},
                     {"Authorization": f"Bearer {token['access_token']}",
                      "ChatGPT-Account-Id": a["account_id"], "User-Agent": "agent-pool/1.0"})
    if st == 200:
        print(f"✓ Redeemed: {credit['title']}")
        # Re-poll to show updated state
        poll_codex(conn, a, token)
        return 0
    else:
        print(f"Redeem failed: HTTP {st} {resp}")
        return 1
