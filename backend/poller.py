"""Poller — hits limit endpoints for every account (adaptive: 300s base,
60s hot / 180s hot-claude, pre-reset capture near known boundaries).

Limit sources:
  codex:        chatgpt.com/backend-api/wham/usage + /wham/rate-limit-reset-credits
  claude:       api.anthropic.com/api/oauth/usage (five_hour/seven_day/limits[])
  xai:          cli-chat-proxy.grok.com/v1/billing (monthly credits) + api.x.ai chat headers (daily)
  antigravity:  cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels (per-model remainingFraction)
  copilot:      api.github.com/copilot_internal/user (quota_snapshots.premium_interactions)
  devin:        server.codeium.com GetUserStatus protobuf (daily/weekly quota %)
"""
from __future__ import annotations
import json, os, re, sys, time, datetime, urllib.request, urllib.error
import store, oauth, window_history, work_queue

WHAM = "https://chatgpt.com/backend-api"
POLL_INTERVAL = int(os.environ.get("AGENT_POOL_POLL_INTERVAL", "300"))  # 5 min
LOCAL_SYNC_INTERVAL_S = int(os.environ.get("LOCAL_SYNC_INTERVAL_S", "15"))

# Adaptive cadence: hot accounts (any window >= HOT_THRESHOLD_PCT used) poll
# faster — an early reset only destroys meaningful data when usage is high —
# and accounts within PRERESET_LEAD_S of a known reset get a fresh capture
# with retries.
HOT_THRESHOLD_PCT = float(os.environ.get("HOT_THRESHOLD_PCT", "70"))
HOT_INTERVAL_S = int(os.environ.get("HOT_INTERVAL_S", "60"))
PRERESET_LEAD_S = int(os.environ.get("PRERESET_LEAD_S", "300"))
PRERESET_RETRY_S = int(os.environ.get("PRERESET_RETRY_S", "60"))
PRERESET_FINAL_GAP_S = 30

# IDE headers required for copilot_internal/user to return quota_snapshots.
COPILOT_IDE_HEADERS = {
    "Accept-Encoding": "identity",
    "Editor-Version": "vscode/1.107.0",
    "Editor-Plugin-Version": "copilot-chat/0.35.0",
    "User-Agent": "GitHubCopilotChat/0.35.0",
    "X-Github-Api-Version": "2025-04-01",
}


RETRYABLE_STATUSES = (429, 500, 502, 503, 504)
HTTP_RETRIES = 2  # extra attempts after the first; backoff 1s then 2s


def _http_error_body(e):
    """Read an HTTPError body once; JSON when possible, text otherwise."""
    raw = e.read()
    try:
        return json.loads(raw)
    except Exception:
        return raw.decode(errors="replace")


def _send(req, timeout):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = raw.decode(errors="replace")
            return r.status, body, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, _http_error_body(e), dict(e.headers)
    except urllib.error.URLError as e:
        return 0, str(e.reason), {}


def _send_with_retry(req, timeout):
    """Bounded retry on 429/5xx and network errors (honors integer Retry-After)."""
    st, body, hdrs = 0, "", {}
    delay = 1.0
    for attempt in range(HTTP_RETRIES + 1):
        st, body, hdrs = _send(req, timeout)
        if st != 0 and st not in RETRYABLE_STATUSES:
            return st, body, hdrs
        if attempt == HTTP_RETRIES:
            break
        wait = delay
        retry_after = (hdrs or {}).get("Retry-After")
        if retry_after is not None:
            try:
                # Cap so total added latency stays bounded.
                wait = max(0.0, min(float(int(retry_after)), 10.0))
            except (TypeError, ValueError):
                pass
        time.sleep(wait)
        delay *= 2
    return st, body, hdrs


def _get(url, headers, timeout=15):
    req = urllib.request.Request(url, method="GET")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return _send_with_retry(req, timeout)


def _post(url, data, headers, timeout=15):
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return _send_with_retry(req, timeout)


def _shape_error(source, resp):
    """Error snapshot for a response that isn't the expected JSON object."""
    return {"status": "error",
            "status_message": f"{source}: unexpected response shape: {str(resp)[:120]}"}


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
    if not isinstance(usage, dict):
        snap = _shape_error("wham/usage", usage)
        store.save_snapshot(conn, account["id"], snap)
        store.log_event(conn, account["id"], "limit_poll", False, snap["status_message"])
        return
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
        credit_list = credits.get("credits") or []
        store.replace_reset_credits(conn, account["id"], credit_list)
        store.upsert_credit_history(conn, account["id"], credit_list)
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


# ─── Claude oauth/usage ────────────────────────────────────────────────────
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


def poll_claude(conn, account, token):
    """Poll Claude via the quota-free oauth/usage endpoint (no probes)."""
    def _fetch(access_token):
        return _get(CLAUDE_USAGE_URL, {
            "Authorization": f"Bearer {access_token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
        })

    st, body, _ = _fetch(token["access_token"])
    if st == 401 and token.get("refresh_token"):
        # One refresh + retry; a second 401 becomes an error snapshot.
        # The lock serializes rotating-refresh-token use across processes.
        try:
            with work_queue.exclusive("token_refresh"):
                latest = store.get_token(conn, account["id"]) or token
                if latest["access_token"] != token["access_token"]:
                    # Another process already refreshed while we waited.
                    token = latest
                else:
                    result = oauth.refresh_claude(token["refresh_token"])
                    store.save_token(conn, account["id"], result["access_token"],
                                     result.get("refresh_token"), result.get("id_token"),
                                     result.get("expires_at"), result.get("raw"))
                    store.log_event(conn, account["id"], "token_refresh", True, "")
                    token = store.get_token(conn, account["id"])
            st, body, _ = _fetch(token["access_token"])
        except Exception as e:
            store.log_event(conn, account["id"], "token_refresh", False, str(e))

    if st != 200 or not isinstance(body, dict):
        snap = {"status": "error",
                "status_message": f"oauth/usage HTTP {st}: {str(body)[:120]}"}
    else:
        snap = _claude_usage_snap(body, _claude_profile(token))
    store.save_snapshot(conn, account["id"], snap)
    store.log_event(conn, account["id"], "limit_poll", snap["status"] == "active",
                    snap.get("status_message", ""))


def _claude_usage_snap(body, profile):
    """Build a snapshot from the oauth/usage response body (pure function)."""
    snap = {"status": "active", "status_message": ""}
    rj = {"usage_api": body}
    if profile:
        rj["profile"] = profile
        if profile.get("plan"):
            snap["plan"] = profile["plan"]

    fh = body.get("five_hour") or {}
    if fh.get("utilization") is not None:
        snap["primary_used_pct"] = float(fh["utilization"])
        snap["primary_window_s"] = 18000
        reset = window_history._parse_iso_ts(fh.get("resets_at"))
        if reset:
            snap["primary_reset_at"] = reset
    sd = body.get("seven_day") or {}
    if sd.get("utilization") is not None:
        snap["secondary_used_pct"] = float(sd["utilization"])
        snap["secondary_window_s"] = 604800
        reset = window_history._parse_iso_ts(sd.get("resets_at"))
        if reset:
            snap["secondary_reset_at"] = reset

    limits = [l for l in (body.get("limits") or []) if isinstance(l, dict)]
    for lim in limits:
        if lim.get("kind") != "weekly_scoped":
            continue
        scope_model = ((lim.get("scope") or {}).get("model") or {})
        rj["fable"] = {
            "label": scope_model.get("display_name") or "scoped",
            "used_pct": float(lim["percent"]) if lim.get("percent") is not None else None,
            "reset_at": window_history._parse_iso_ts(lim.get("resets_at")),
            "status": lim.get("severity"),
        }
        break

    active = next((l for l in limits if l.get("is_active")), None)
    snap["rate_limit_remaining"] = (active or {}).get("severity") or "normal"
    snap["rate_limit_limit"] = "unified"
    if snap.get("primary_reset_at"):
        snap["rate_limit_reset"] = str(snap["primary_reset_at"])
    snap["raw_json"] = json.dumps(rj)
    return snap


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
        if not isinstance(resp, dict):
            snap = _shape_error("billing", resp)
        else:
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
    except json.JSONDecodeError as e:
        snap = _shape_error("billing", f"invalid JSON: {e}")

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
                if daily.get("primary_reset_at"):
                    snap["secondary_reset_at"] = daily["primary_reset_at"]
            if "rate_limit_remaining" in daily:
                snap["daily_remaining"] = daily["rate_limit_remaining"]
    except urllib.error.HTTPError as e:
        hdrs = dict(e.headers)
        msg = e.read().decode(errors="replace")[:120]
        if e.code == 429:
            daily = _xai_snap(hdrs, "rate_limited", f"429: {msg}")
            snap["secondary_used_pct"] = daily.get("primary_used_pct", 100.0)
            snap["secondary_window_s"] = 86400
            if daily.get("primary_reset_at"):
                snap["secondary_reset_at"] = daily["primary_reset_at"]
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
    except json.JSONDecodeError as e:
        snap = _shape_error("loadCodeAssist", f"invalid JSON: {e}")
        store.save_snapshot(conn, account["id"], snap)
        store.log_event(conn, account["id"], "limit_poll", False, snap["status_message"])
        return
    if not isinstance(resp, dict):
        snap = _shape_error("loadCodeAssist", resp)
        store.save_snapshot(conn, account["id"], snap)
        store.log_event(conn, account["id"], "limit_poll", False, snap["status_message"])
        return
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
        if isinstance(resp, dict):
            snap.update(_antigravity_quota(resp.get("models") or {}, plan))
        else:
            snap["status"] = "error"
            snap["status_message"] = f"fetchAvailableModels: unexpected response shape: {str(resp)[:120]}"
    except urllib.error.HTTPError as e:
        body_resp = e.read().decode(errors="replace")[:120]
        snap["status"] = "error"
        snap["status_message"] = f"fetchAvailableModels HTTP {e.code}: {body_resp}"
    except urllib.error.URLError as e:
        snap["status"] = "error"
        snap["status_message"] = str(e.reason)
    except json.JSONDecodeError as e:
        snap["status"] = "error"
        snap["status_message"] = f"fetchAvailableModels: invalid JSON: {e}"

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
        if isinstance(user, dict):
            snap = _copilot_quota_snap(user, sku)
        else:
            snap = _shape_error("copilot_internal/user", user)
            snap["sku"] = sku
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")[:120]
        snap = {"status": "error" if e.code != 429 else "rate_limited",
                "status_message": f"user HTTP {e.code}: {msg}", "sku": sku}
    except urllib.error.URLError as e:
        snap = {"status": "error", "status_message": str(e.reason), "sku": sku}
    except json.JSONDecodeError as e:
        snap = _shape_error("copilot_internal/user", f"invalid JSON: {e}")
        snap["sku"] = sku

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
            dt = datetime.datetime.fromisoformat(reset_date)
            # Naive timestamps are UTC; explicit offsets must be preserved.
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            snap["primary_reset_at"] = dt.astimezone(datetime.timezone.utc).timestamp()
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


# Client identity constants sent in the GetUserStatus protobuf request.
DEVIN_CLIENT_NAME = "chisel"
DEVIN_CLIENT_VERSION = "2026.8.18"
DEVIN_CLIENT_PLATFORM = "mac"
DEVIN_CLIENT_LOCALE = "en"
DEVIN_CLIENT_FINGERPRINT = "080d03eeaa0cd7a10d0e0c84c26cb9a1c533e2675c14a85c3a971248f6521a710e4c02372539fc56c8b6a0454553533dc7f9e54fa3c16a2b141c87d0fb43a8b6a7e32b15a2267290298a6c6382e7b0096e06b41012d46d998f947b53a35b84c55ab589c683c6a3727aa5bf90c18e349fba8ac069b4121f5298fbacca590c903f169850ec072da539ed40d46f212aea973c725221098fcea6fb6fde32a7ef324003cb070e8a603c8c7a1d6743cfadd9e86f53797b32bb88b11abe8bcc98ec38473496bd9aaf482c6ab2c5def224bb7f554687cd78202159112e1cdee29b1c44b38cd407629d59c9dc0e2eab891ccacca859f358d7641acedc7fed0ba64a4d3e3a4827ac433bae69f7e48917ff2a6df24e2adf4fb9ed88ed8266228b99604a7cba3356fc28cb304de708958d188143f12eff3d52178a680c86073b21bc1efbf45a44b09887b60e9fe13ae9e5256e640b7159a595dcd5ecb2b470a290cf30357403e4a820dfb0ce990517e2cd64"


def _devin_build_request(at):
    """Build the GetUserStatusRequest protobuf."""
    inner = (
        _devin_encode_str(1, DEVIN_CLIENT_NAME) +
        _devin_encode_str(2, DEVIN_CLIENT_VERSION) +
        _devin_encode_str(3, at) +
        _devin_encode_str(4, DEVIN_CLIENT_LOCALE) +
        _devin_encode_str(5, DEVIN_CLIENT_PLATFORM) +
        _devin_encode_str(7, DEVIN_CLIENT_VERSION) +
        _devin_encode_str(12, DEVIN_CLIENT_NAME) +
        _devin_encode_str(31, DEVIN_CLIENT_FINGERPRINT)
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
                except Exception as e:
                    print(f"  devin: plan name decode failed: {e}")

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
                except Exception as e:
                    print(f"  devin: email decode failed: {e}")
            if sfn == 3 and swt == 'bytes':
                try:
                    snap["name"] = sval.decode('utf-8')
                except Exception as e:
                    print(f"  devin: name decode failed: {e}")

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
            # Serialize refreshes across processes: refresh tokens rotate, so
            # concurrent refreshes (daemon + on-demand poll) can invalidate
            # each other. Inside the lock re-read the token and skip when it
            # was already refreshed while we waited.
            with work_queue.exclusive("token_refresh"):
                token = store.get_token(conn, account["id"]) or token
                if not (token.get("expires_at") and token["expires_at"] - time.time() < 3600):
                    return token
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


def _poll_one(conn, account) -> bool:
    """Poll one account. Returns True when the provider poll succeeded.

    Wraps the provider poller with closed-window detection: the previous
    successful snapshot is captured before the poll and compared with the
    freshly saved one right after, archiving any windows that closed in
    between. Detection failures never fail the poll.
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
    # Capturing the baseline for detection must never fail the poll; on any
    # error skip detection (prev=None, no coupon hint) and poll anyway.
    try:
        prev = store.latest_successful_snapshot(conn, account["id"])
        prev_credits = ({c["credit_id"]: c["status"] for c in store.list_reset_credits(conn, account["id"])}
                        if account["provider"] == "codex" else {})
    except Exception as e:
        print(f"  pre-poll capture failed {account['provider']} #{account['id']}: {e}")
        prev, prev_credits = None, {}
    try:
        poller(conn, account, token)
        print(f"  ✓ {account['provider']:12} {account['email'] or account['label']}")
    except Exception as e:
        store.save_snapshot(conn, account["id"], {"status": "error", "status_message": str(e)[:200]})
        store.log_event(conn, account["id"], "limit_poll", False, str(e))
        print(f"  ✗ {account['provider']:12} {account['email'] or account['label']}: {e}")
        return False
    _archive_closed_windows(conn, account, prev, prev_credits)
    return True


def _archive_closed_windows(conn, account, prev, prev_credits):
    """Detect + archive windows closed since the previous successful snapshot.

    Coupon hint: a reset_credits row that was "available" before the poll and
    isn't afterwards (consumed or gone) marks a redeem from another device.
    """
    try:
        new = store.latest_snapshot(conn, account["id"])
        if not new or (prev and new["id"] == prev["id"]):
            return
        coupon_hint = False
        if prev_credits:
            cur = {c["credit_id"]: c["status"] for c in store.list_reset_credits(conn, account["id"])}
            coupon_hint = any(st == "available" and cur.get(cid) != "available"
                              for cid, st in prev_credits.items())
            _archive_credit_disappearances(conn, account, prev_credits, cur, coupon_hint)
        n = window_history.record_closed_windows(conn, account, prev, new, coupon_hint=coupon_hint)
        if n:
            print(f"  ⤷ archived {n} closed window(s): {account['provider']} #{account['id']}")
            try:
                import dashboard
                dashboard.generate(conn)
            except Exception as e:
                print(f"  dashboard generation failed: {e}")
    except Exception as e:
        print(f"  window-history detection failed: {e}")


def _archive_credit_disappearances(conn, account, prev_credits, cur, coupon_hint):
    """Mark credits that vanished since the previous poll in the ledger.

    Discriminator is the credit's expires_at: a past expiry means it expired
    unused; a future expiry means it was consumed (redeem from another device
    when coupon_hint is set) or provider-removed (gone). Our own redeems are
    marked earlier in redeem_reset, so those rows stay 'redeemed'.
    """
    import datetime
    disappeared = [cid for cid, st in prev_credits.items()
                   if st == "available" and cur.get(cid) is None]
    if not disappeared:
        return
    now_ts = time.time()
    hist = {r["credit_id"]: r for r in store.list_credit_history(conn, account["provider"])
            if r["account_id"] == account["id"]}
    changed = False
    for cid in disappeared:
        row = hist.get(cid)
        if not row or row.get("final_state") == "redeemed":
            continue
        exp = row.get("expires_at")
        exp_ts = None
        if exp:
            try:
                dt = datetime.datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
                exp_ts = dt.timestamp()
            except (ValueError, TypeError):
                exp_ts = None
        if exp_ts is not None and exp_ts < now_ts:
            state = "expired_unused"
        elif coupon_hint:
            state = "redeemed"
        else:
            state = "gone"
        store.mark_credit_final(conn, account["id"], cid, state)
        changed = True
    if changed:
        try:
            import dashboard
            dashboard.generate(conn)
        except Exception as e:
            print(f"  dashboard generation failed: {e}")


def poll_some(conn, accounts) -> None:
    """Poll the given accounts and export status.json once."""
    for a in accounts:
        # One account's unexpected failure must not abort the rest or the export.
        try:
            _poll_one(conn, a)
        except Exception as e:
            print(f"  ✗ poll failed {a['provider']} #{a['id']}: {e}")
    try:
        export_status(conn)
    except Exception as e:
        print(f"  export-status failed: {e}")


# Previous export payload, kept in memory so consecutive exports within this
# process (daemon loop, local-sync ticks) can be diffed for lifecycle events.
# First export after startup has no baseline → detect_transitions emits [].
_prev_export_payload: dict | None = None


def export_status(conn) -> None:
    """Export status.json, then detect + persist lifecycle transitions (§1.4)."""
    global _prev_export_payload
    import status
    payload = status.build_payload(conn)
    status.write_status(payload)
    try:
        import lifecycle
        ts = time.time()
        for ev in lifecycle.detect_transitions(_prev_export_payload, payload):
            store.save_lifecycle_event(conn, ts, ev["account_id"],
                                       ev["event"], ev["detail"])
    except Exception as e:
        # Event persistence must never break the export path the app reads.
        print(f"  lifecycle events failed: {e}")
    _prev_export_payload = payload


def poll_account(conn, account) -> bool:
    """Poll a single account and refresh status.json. Returns True on success.

    Used by onboarding so a freshly-added account immediately has subscription
    data instead of waiting for the next 5-minute poll cycle.
    """
    ok = _poll_one(conn, account)
    try:
        export_status(conn)
    except Exception as e:
        print(f"  export-status failed: {e}")
    return ok


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
        poll_some(conn, accounts)
    return 0


def max_used_pct(provider, snap) -> float:
    """Highest used% across a snapshot's tracked windows (hotness signal)."""
    vals = [w["used_pct"] for w in window_history.timed_windows(provider, snap)]
    vals += [w["used_pct"] for w in window_history.drop_windows(provider, snap)]
    return max(vals, default=0.0)


def next_reset_at(provider, snap, now) -> float | None:
    """Earliest future reset timestamp among the snapshot's timed windows."""
    future = [w["reset_at"] for w in window_history.timed_windows(provider, snap)
              if w["reset_at"] > now]
    return min(future) if future else None


def compute_next_due(now, *, provider, last_poll_ts, last_success_ts, hot, reset_at) -> float:
    """Pure next-poll-time computation for one account.

    Base cadence POLL_INTERVAL; hot accounts use HOT_INTERVAL_S. When
    reset_at is within PRERESET_LEAD_S and no success has landed inside that
    lead window yet, wake at the lead start and retry every PRERESET_RETRY_S,
    last attempt no later than reset_at - PRERESET_FINAL_GAP_S. First success
    wins.
    """
    interval = HOT_INTERVAL_S if hot else POLL_INTERVAL
    due = last_poll_ts + interval
    if reset_at:
        lead_start = reset_at - PRERESET_LEAD_S
        deadline = reset_at - PRERESET_FINAL_GAP_S
        captured = last_success_ts is not None and last_success_ts >= lead_start
        if not captured and now <= deadline:
            if now < lead_start:
                candidate = lead_start
            else:
                candidate = min(max(last_poll_ts + PRERESET_RETRY_S, now), deadline)
            if candidate <= deadline:
                due = min(due, candidate)
    return due


_next_local_scan = 0.0


def _local_sync_tick(conn):
    """Scan local CLI logs at most every LOCAL_SYNC_INTERVAL_S; export on change."""
    global _next_local_scan
    if time.time() < _next_local_scan:
        return
    _next_local_scan = time.time() + LOCAL_SYNC_INTERVAL_S
    try:
        import local_sync
        if local_sync.scan(conn):
            export_status(conn)
    except Exception as e:
        print(f"  local sync failed: {e}")


def run_loop(conn) -> int:
    print(f"Poller daemon started. Base interval: {POLL_INTERVAL}s, hot: {HOT_INTERVAL_S}s, "
          f"pre-reset lead: {PRERESET_LEAD_S}s. Ctrl+C to stop.")
    # In-memory attempt times: copilot's hold-last-good path saves no snapshot
    # on a transient failure, so DB timestamps alone would re-poll it instantly.
    last_attempt: dict[int, float] = {}
    next_prune = 0.0  # prune retention once at startup, then daily
    while True:
        try:
            now = time.time()
            if now >= next_prune:
                next_prune = now + 86400
                try:
                    store.prune_old_rows(conn)
                except Exception as e:
                    print(f"  retention prune failed: {e}")
            accounts = store.list_accounts(conn)
            _local_sync_tick(conn)
            if not accounts:
                time.sleep(POLL_INTERVAL)
                continue
            due, wake = [], now + POLL_INTERVAL
            for a in accounts:
                snap = store.latest_snapshot(conn, a["id"])
                good = store.latest_successful_snapshot(conn, a["id"])
                last_poll_ts = max(float(snap["ts"]) if snap else 0.0,
                                   last_attempt.get(a["id"], 0.0))
                t_due = compute_next_due(
                    now,
                    provider=a["provider"],
                    last_poll_ts=last_poll_ts,
                    last_success_ts=float(good["ts"]) if good else None,
                    hot=bool(good) and max_used_pct(a["provider"], good) >= HOT_THRESHOLD_PCT,
                    reset_at=next_reset_at(a["provider"], good, now) if good else None,
                )
                if t_due <= now:
                    due.append(a)
                else:
                    wake = min(wake, t_due)
            if due:
                with work_queue.single_worker("poll") as acquired:
                    if acquired:
                        stamp = datetime.datetime.now().strftime("%H:%M:%S")
                        print(f"[{stamp}] Polling {len(due)}/{len(accounts)} due accounts...")
                        for a in due:
                            last_attempt[a["id"]] = time.time()
                        poll_some(conn, due)
                        continue
                print("poll already running; waiting")
                time.sleep(5)
                continue
            time.sleep(max(1.0, min(wake - time.time(), LOCAL_SYNC_INTERVAL_S)))
        except KeyboardInterrupt:
            print("\nPoller stopped.")
            return 0
        except Exception as e:
            # Never let a single bad cycle kill the daemon (LaunchAgent would
            # crash-loop). Log, pause briefly so a persistent bug can't spin.
            print(f"poll cycle error: {e}")
            time.sleep(5)


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
    if not (0 <= idx < len(available)):
        print(f"Invalid selection: {idx} (choose 0-{len(available) - 1})")
        return 1
    credit = available[idx]
    import uuid
    # Hold the poll lock through consume + ledger + re-poll so the daemon
    # can't poll (and rewrite reset_credits) mid-redemption.
    with work_queue.exclusive("poll"):
        st, resp, _ = _post(f"{WHAM}/wham/rate-limit-reset-credits/consume",
                         {"credit_id": credit["credit_id"], "redeem_request_id": str(uuid.uuid4())},
                         {"Authorization": f"Bearer {token['access_token']}",
                          "ChatGPT-Account-Id": a["account_id"], "User-Agent": "agent-pool/1.0"})
        if st == 200:
            print(f"✓ Redeemed: {credit['title']}")
            # Mark the credit as redeemed in the ledger before the re-poll wipes it
            # from reset_credits, so it is never misclassified as expired_unused.
            store.mark_credit_redeemed(conn, account_id, credit["credit_id"])
            # Archive the closing windows now — before the confirmation re-poll —
            # so the coupon row exists even if that re-poll fails.
            try:
                n = window_history.archive_coupon_redeem(conn, a, credit["credit_id"])
                if n:
                    print(f"  ⤷ archived {n} window(s) as coupon reset")
            except Exception as e:
                print(f"  window-history archive failed: {e}")
            # Re-poll to show updated state
            poll_codex(conn, a, token)
            return 0
        else:
            print(f"Redeem failed: HTTP {st} {resp}")
            return 1
