"""Status display + JSON export for the menu bar app."""
from __future__ import annotations
import json, os, re, sys, time, datetime, zoneinfo
from pathlib import Path
import store

STATUS_JSON = Path(os.environ.get("AGENT_POOL_STATUS_JSON",
                                    str(Path.home() / "solo/token-status-bar" / "secrets" / "status.json")))
KST = zoneinfo.ZoneInfo("Asia/Seoul")
HEARTBEAT_INTERVAL_S = 5 * 60 * 60
HEARTBEAT_PROVIDERS = ("codex", "claude", "antigravity")


def ts_fmt(ts) -> str:
    if not ts:
        return "n/a"
    try:
        return datetime.datetime.fromtimestamp(float(ts), tz=KST).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


def iso_fmt(s, *, with_seconds: bool = False, kst_suffix: bool = False) -> str | None:
    if not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        fmt = "%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M"
        out = dt.astimezone(KST).strftime(fmt)
        return f"{out} KST" if kst_suffix else out
    except Exception:
        return str(s)


def iso_fmt_exact(s) -> str | None:
    return iso_fmt(s, with_seconds=True, kst_suffix=True)


def reset_fmt(val) -> str | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return ts_fmt(val)
    s = str(val).strip()
    if not s:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return ts_fmt(s)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            dt = datetime.datetime.strptime(s, "%Y-%m-%d").replace(
                tzinfo=datetime.timezone.utc)
            return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return s
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", s):
        try:
            dt = datetime.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(
                tzinfo=datetime.timezone.utc)
            return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return s
    if re.search(r"[TtZz+]|:\d{2}:\d{2}", s):
        return iso_fmt(s)
    if ": " in s:
        parts = []
        for part in s.split(", "):
            if ": " not in part:
                parts.append(part)
                continue
            label, rest = part.split(": ", 1)
            rest = rest.strip()
            if re.fullmatch(r"\d+(?:\.\d+)?", rest):
                parts.append(f"{label}: {ts_fmt(rest)}")
            else:
                parts.append(part)
        return ", ".join(parts)
    return s


def human_secs(s):
    if s is None: return "n/a"
    s = int(s)
    if s < 60: return f"{s}s"
    if s < 3600: return f"{s//60}m"
    if s < 86400: return f"{s/3600:.1f}h"
    return f"{s/86400:.1f}d"


def heartbeat_meta(conn, account_id) -> dict:
    latest = conn.execute(
        "SELECT * FROM refresh_log WHERE account_id=? AND kind='heartbeat' ORDER BY ts DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    latest_success = conn.execute(
        "SELECT * FROM refresh_log WHERE account_id=? AND kind='heartbeat' AND success=1 ORDER BY ts DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    if not latest and not latest_success:
        return {
            "heartbeat_status": "unknown",
            "heartbeat_last": None,
            "heartbeat_next": "due now",
            "heartbeat_message": None,
            "heartbeat_next_ts": time.time(),
        }
    next_ts = (latest_success["ts"] + HEARTBEAT_INTERVAL_S) if latest_success else time.time()
    return {
        "heartbeat_status": "success" if latest and latest["success"] else "fail",
        "heartbeat_last": ts_fmt(latest["ts"]) if latest else None,
        "heartbeat_next": ts_fmt(next_ts),
        "heartbeat_message": latest["message"] if latest else None,
        "heartbeat_next_ts": next_ts,
    }


def heartbeat_summary(items: list[dict]) -> dict:
    hb_items = [i for i in items if i.get("provider") in HEARTBEAT_PROVIDERS]
    if not hb_items:
        return {"status": "unknown", "next": None, "accounts": 0}
    failed = [i for i in hb_items if i.get("heartbeat_status") == "fail"]
    unknown = [i for i in hb_items if i.get("heartbeat_status") == "unknown"]
    if failed:
        status = "fail"
    elif unknown:
        status = "unknown"
    else:
        status = "success"
    due = [i for i in hb_items if i.get("heartbeat_next_ts")]
    next_ts = min((i["heartbeat_next_ts"] for i in due), default=None)
    return {
        "status": status,
        "next": ts_fmt(next_ts) if next_ts else None,
        "accounts": len(hb_items),
        "failed": len(failed),
    }


def fmt_window(used, reset_at, window_s):
    parts = []
    if used is not None:
        parts.append(f"{used}% used")
    if window_s:
        parts.append(f"window={human_secs(window_s)}")
    if reset_at:
        parts.append(f"resets {ts_fmt(reset_at)}")
    return ", ".join(parts) if parts else "n/a"


def cmd_status(conn) -> int:
    accounts = store.list_accounts(conn)
    if not accounts:
        print("(no accounts yet. Run: pool.py add <provider>)")
        return 0
    print(f"{'ID':>3} {'PROVIDER':12} {'EMAIL':34} {'STATUS':9} {'TOKEN_EXP':18} EXTRA")
    print("-" * 110)
    for a in accounts:
        tok = store.get_token(conn, a["id"])
        snap = store.latest_snapshot(conn, a["id"])
        exp = ts_fmt(tok["expires_at"]) if tok else "no token"
        status = snap["status"] if snap else "?"
        extra = snap["status_message"] if snap else ""
        print(f"{a['id']:>3} {a['provider']:12} {a['email'] or '?':34} {status:9} {exp:18} {extra}")

    # All providers — show 5h/7d windows where available
    for a in accounts:
        snap = store.latest_snapshot(conn, a["id"])
        if not snap:
            continue
        provider_label = {
            "codex": "Codex", "claude": "Claude", "xai": "Grok",
            "antigravity": "Google", "copilot": "Copilot", "devin": "Devin",
        }.get(a["provider"], a["provider"].title())
        print(f"\n● {provider_label} {a['email']}")
        if snap.get("plan"):
            print(f"    plan: {snap['plan']}")
        if snap.get("primary_used_pct") is not None:
            w1 = "5h" if snap.get("primary_window_s") and snap["primary_window_s"] >= 17000 and snap["primary_window_s"] <= 19000 else \
                 "24h" if snap.get("primary_window_s") == 86400 else \
                 "win" if not snap.get("primary_window_s") else human_secs(snap.get("primary_window_s"))
            print(f"    primary  ({w1}): {fmt_window(snap['primary_used_pct'], snap['primary_reset_at'], snap['primary_window_s'])}")
        if snap.get("secondary_used_pct") is not None:
            w2 = "7d" if snap.get("secondary_window_s") == 604800 else \
                 "24h" if snap.get("secondary_window_s") == 86400 else human_secs(snap.get("secondary_window_s"))
            print(f"    secondary({w2}): {fmt_window(snap['secondary_used_pct'], snap['secondary_reset_at'], snap['secondary_window_s'])}")
        if snap.get("credits_balance") is not None:
            print(f"    credits balance: {snap['credits_balance']}")
        if snap.get("banked_resets") is not None:
            print(f"    banked reset credits: {snap['banked_resets']}")
            for c in store.list_reset_credits(conn, a["id"]):
                flag = "●" if c["status"] == "available" else "○"
                exp = iso_fmt_exact(c["expires_at"]) or c["expires_at"]
                print(f"      {flag} {c['title'] or c['credit_id']}  expires: {exp}")
        # Fallback for providers without window data
        if snap.get("primary_used_pct") is None:
            if snap.get("rate_limit_remaining"):
                print(f"    remaining: {snap['rate_limit_remaining']}")
            if snap.get("rate_limit_reset"):
                print(f"    reset: {reset_fmt(snap['rate_limit_reset'])}")
            if snap.get("rate_limit_limit"):
                print(f"    limit: {snap['rate_limit_limit']}")
    return 0


def claude_extra(snap) -> dict:
    """Extra Claude subscription + window-status fields parsed from raw_json."""
    out: dict = {}
    if not snap or not snap.get("raw_json"):
        return out
    try:
        rj = json.loads(snap["raw_json"])
    except Exception:
        return out
    if not isinstance(rj, dict):
        return out
    prof = rj.get("profile") or {}
    rl = rj.get("ratelimit") or {}
    fable = rj.get("fable") or {}
    if fable:
        if fable.get("used_pct") is not None:
            out["fable_used_pct"] = fable["used_pct"]
        if fable.get("reset_at") is not None:
            out["fable_reset"] = ts_fmt(fable["reset_at"])
        if fable.get("label"):
            out["fable_label"] = fable["label"]
        if fable.get("status"):
            out["fable_status"] = fable["status"]
    if prof:
        out["subscription_status"] = prof.get("subscription_status")
        out["billing_type"] = prof.get("billing_type")
        out["rate_limit_tier"] = prof.get("rate_limit_tier")
        out["extra_usage_enabled"] = prof.get("extra_usage_enabled")
        out["subscription_created"] = iso_fmt(prof.get("subscription_created_at"))
        out["plan_start"] = iso_fmt(prof.get("subscription_created_at"))
        out["member_since"] = iso_fmt(prof.get("member_since"))
        out["display_name"] = prof.get("display_name")
        out["org_name"] = prof.get("org_name")
    if rl:
        out["primary_status"] = rl.get("anthropic-ratelimit-unified-5h-status")
        out["secondary_status"] = rl.get("anthropic-ratelimit-unified-7d-status")
        fallback_pct = rl.get("anthropic-ratelimit-unified-fallback-percentage")
        if fallback_pct is not None:
            try:
                out["fallback_used_pct"] = float(fallback_pct) * 100
            except (TypeError, ValueError):
                pass
        claim = rl.get("anthropic-ratelimit-unified-representative-claim")
        out["binding_window"] = {"five_hour": "5h", "seven_day": "weekly"}.get(claim, claim)
        out["overage_status"] = rl.get("anthropic-ratelimit-unified-overage-status")
    return {k: v for k, v in out.items() if v is not None}


def provider_extra(provider, snap) -> dict:
    """Extra subscription fields parsed from a snapshot's raw_json['extra']."""
    if not snap or not snap.get("raw_json"):
        return {}
    try:
        rj = json.loads(snap["raw_json"])
    except Exception:
        return {}
    if not isinstance(rj, dict):
        return {}
    extra = rj.get("extra") or {}
    if not isinstance(extra, dict):
        return {}
    out: dict = {}
    if provider == "xai":
        out["on_demand_cap"] = extra.get("on_demand_cap")
        out["billing_period_start"] = iso_fmt(extra.get("period_start"))
        out["plan_start"] = iso_fmt(extra.get("period_start"))
        out["plan_reset"] = iso_fmt(extra.get("period_end"))
    elif provider == "antigravity":
        out["tier_id"] = extra.get("tier_id")
        out["tier_description"] = extra.get("tier_description")
        out["active_tier"] = extra.get("active_tier")
        exported = []
        for w in extra.get("usage_windows") or []:
            if not isinstance(w, dict) or w.get("remaining_pct") is None:
                continue
            used = 100.0 - float(w["remaining_pct"])
            exported.append({
                "group": w.get("group"),
                "window": w.get("window"),
                "used_pct": round(max(0.0, min(100.0, used)), 2),
                "reset": ts_fmt(w["reset_at"]) if w.get("reset_at") else None,
            })
        if exported:
            out["usage_windows"] = exported
    elif provider == "copilot":
        out["access_sku"] = extra.get("access_sku")
        out["premium_entitlement"] = extra.get("premium_entitlement")
        out["premium_overage"] = extra.get("premium_overage")
        out["chat_unlimited"] = extra.get("chat_unlimited")
        out["completions_unlimited"] = extra.get("completions_unlimited")
        out["can_upgrade"] = extra.get("can_upgrade")
        out["organizations"] = extra.get("organizations")
        out["github_email"] = extra.get("github_email")
        out["github_name"] = extra.get("github_name")
        # plan_reset from the top-level "reset" field (quota_reset_date)
        reset_date = rj.get("reset")
        if reset_date:
            out["plan_reset"] = iso_fmt(reset_date) or reset_date
    elif provider == "devin":
        out["credit_balance"] = extra.get("credit_balance")
        out["plan_start"] = ts_fmt(extra.get("plan_start_unix")) if extra.get("plan_start_unix") else None
        out["plan_reset"] = ts_fmt(extra.get("plan_reset_unix")) if extra.get("plan_reset_unix") else None
    return {k: v for k, v in out.items() if v is not None}


def codex_extra(conn, account_id) -> dict:
    """Codex subscription metadata from ChatGPT account endpoints."""
    meta = store.get_subscription_meta(conn, account_id)
    if not meta:
        return {}
    def _meta_bool(key: str):
        value = meta.get(key)
        return bool(value) if value is not None else None

    end = meta.get("renews_at") or meta.get("expires_at")
    out = {
        "paid_since": iso_fmt(meta.get("paid_since")),
        "renews_at": iso_fmt(meta.get("renews_at")),
        "expires_at": iso_fmt(meta.get("expires_at")),
        "account_created": iso_fmt(meta.get("account_created_at")),
        "subscription_plan": meta.get("subscription_plan"),
        "has_active_subscription": _meta_bool("has_active_subscription"),
        "is_active_subscription_gratis": _meta_bool("is_active_subscription_gratis"),
        "has_previously_paid_subscription": _meta_bool("has_previously_paid_subscription"),
        "plan_start": iso_fmt(meta.get("paid_since")),
        "plan_reset": iso_fmt(end),
    }
    months = meta.get("previous_paid_months")
    note = meta.get("billing_note")
    if months:
        out["payment_history"] = f"previous {int(months)} months"
    if note:
        out["billing_note"] = note
    return {k: v for k, v in out.items() if v is not None and v != ""}


def plan_label(provider, plan, item) -> tuple:
    """Return (display_name, price_or_None) for a provider's plan."""
    p = (plan or "").strip()
    if provider == "codex":
        m = {"plus": ("Plus", "$20/mo"), "pro": ("Pro", "$200/mo"),
             "free": ("Free", "$0"), "team": ("Team", "$30/user/mo"),
             "business": ("Business", None), "enterprise": ("Enterprise", None)}
        return m.get(p.lower(), (p.title() or None, None))
    if provider == "claude":
        m = {"claude pro": ("Claude Pro", "$20/mo"), "claude max": ("Claude Max", "$100/mo")}
        return m.get(p.lower(), (p or None, None))
    if provider == "copilot":
        m = {"free": ("Copilot Free", "$0"),
             "individual": ("Copilot Pro", "$10/mo"),
             "individual_pro": ("Copilot Pro", "$10/mo"),
             "individual_proplus": ("Copilot Pro+", "$39/mo"),
             "individual_max": ("Copilot Max", "$100/mo"),
             "business": ("Copilot Business", "$19/user/mo"),
             "enterprise": ("Copilot Enterprise", "$39/user/mo")}
        return m.get(p.lower(), (p or None, None))
    if provider == "antigravity":
        tid = item.get("tier_id")
        override = (item.get("tier_override") or "").lower()
        if override:
            if "ultra" in override and "20" in override:
                return ("Google AI Ultra 20x", None)
            if "ultra" in override or "5x" in override:
                return ("Google AI Ultra 5x", None)
            if "pro" in override:
                return ("Google AI Pro", None)
            if "plus" in override:
                return ("Google AI Plus", None)
        m = {"g1-plus-tier": ("Google AI Plus", None),
             "g1-pro-tier": ("Google AI Pro", "$19.99/mo"),
             "g1-ultra-tier": ("Google AI Ultra", None),
             "g1-ultra-5x-tier": ("Google AI Ultra 5x", None),
             "g1-ultra-20x-tier": ("Google AI Ultra 20x", None),
             "free-tier": ("Free", "$0"),
             "standard-tier": ("Antigravity", None)}
        return m.get(tid, (p or None, None))
    if provider == "xai":
        return (p or None, None)
    if provider == "devin":
        m = {"core": ("Core", "$20/mo"), "team": ("Team", "$500/mo")}
        return m.get(p.lower(), (p or None, None))
    return (p or None, None)


def cmd_export(conn) -> int:
    """Write status.json for the menu bar app to read."""
    accounts = store.list_accounts(conn)
    items = []
    for a in accounts:
        tok = store.get_token(conn, a["id"])
        snap = store.latest_snapshot(conn, a["id"])
        credits = store.list_reset_credits(conn, a["id"]) if a["provider"] == "codex" else []
        items.append({
            "id": a["id"],
            "provider": a["provider"],
            "email": a["email"],
            "label": a["label"],
            "plan": snap["plan"] if snap else a["plan"],
            "status": snap["status"] if snap else "unknown",
            "status_message": snap["status_message"] if snap else "",
            "token_expires": ts_fmt(tok["expires_at"]) if tok else None,
            "token_expired": (tok and tok["expires_at"] and tok["expires_at"] < time.time()) or False,
            "primary_used_pct": snap["primary_used_pct"] if snap else None,
            "primary_reset": ts_fmt(snap["primary_reset_at"]) if snap and snap["primary_reset_at"] else None,
            "secondary_used_pct": snap["secondary_used_pct"] if snap else None,
            "secondary_reset": ts_fmt(snap["secondary_reset_at"]) if snap and snap["secondary_reset_at"] else None,
            "credits_balance": snap["credits_balance"] if snap else None,
            "banked_resets": snap["banked_resets"] if snap else None,
            "rate_limit_remaining": snap["rate_limit_remaining"] if snap else None,
            "rate_limit_reset": reset_fmt(snap["rate_limit_reset"]) if snap and snap.get("rate_limit_reset") is not None else None,
            "rate_limit_limit": snap["rate_limit_limit"] if snap else None,
            "sku": snap.get("sku") if snap else None,
            "limited_user_quotas": snap.get("limited_user_quotas") if snap else None,
            "limited_user_reset_date": reset_fmt(snap.get("limited_user_reset_date")) if snap and snap.get("limited_user_reset_date") is not None else None,
            "daily_quota_remaining_percent": int(float(snap["daily_quota_remaining_percent"])) if snap and snap.get("daily_quota_remaining_percent") is not None else None,
            "weekly_quota_remaining_percent": int(float(snap["weekly_quota_remaining_percent"])) if snap and snap.get("weekly_quota_remaining_percent") is not None else None,
            "plan_reset": ts_fmt(snap["plan_reset_unix"]) if snap and snap.get("plan_reset_unix") else None,
            "monthly_used": float(snap["monthly_used"]) if snap and snap.get("monthly_used") is not None else None,
            "monthly_limit": float(snap["monthly_limit"]) if snap and snap.get("monthly_limit") is not None else None,
            "monthly_used_pct": float(snap["monthly_used_pct"]) if snap and snap.get("monthly_used_pct") is not None else None,
            "monthly_period_start": iso_fmt(snap.get("monthly_period_start")) if snap and snap.get("monthly_period_start") else None,
            "monthly_period_end": iso_fmt(snap.get("monthly_period_end")) if snap and snap.get("monthly_period_end") else None,
            "reset_credits": [{"title": c["title"], "status": c["status"],
                               "expires_at": iso_fmt_exact(c["expires_at"]) or c["expires_at"],
                               "granted_at": c.get("granted_at"),
                               "description": c.get("description")} for c in credits],
            "last_poll": ts_fmt(snap["ts"]) if snap else None,
            "tier_override": a.get("tier_override"),
        })
        if a["provider"] in HEARTBEAT_PROVIDERS:
            items[-1].update(heartbeat_meta(conn, a["id"]))
        if a["provider"] == "codex":
            items[-1].update(codex_extra(conn, a["id"]))
        elif a["provider"] == "claude":
            items[-1].update(claude_extra(snap))
        elif a["provider"] in ("xai", "antigravity", "copilot", "devin"):
            items[-1].update(provider_extra(a["provider"], snap))
        name, price = plan_label(a["provider"], items[-1]["plan"], items[-1])
        items[-1]["plan"] = name
        items[-1]["plan_price"] = price
    payload = {
        "generated_at": datetime.datetime.now().isoformat(),
        "account_count": len(items),
        "heartbeat": heartbeat_summary(items),
        "accounts": items,
    }
    STATUS_JSON.parent.mkdir(parents=True, exist_ok=True)
    STATUS_JSON.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {STATUS_JSON} ({len(items)} accounts)")
    return 0
