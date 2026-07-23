#!/usr/bin/env python3
"""Persistent local control server for the menu-bar app + browser dashboard.

opencodex parity: the macOS app never blocks a terminal to log in. Instead it
runs this loopback server and opens http://127.0.0.1:PORT in the browser. Every
auth action is an HTTP call handled here:

  GET  /                         live accounts panel (adds / reconnects / manages)
  GET  /api/status               status.json payload (built fresh from the DB)
  GET  /api/oauth/providers      providers that support browser OAuth
  POST /api/oauth/login          start a login (server opens the OAuth browser tab)
  GET  /api/oauth/status         poll an in-progress login
  POST /api/oauth/login/cancel   cancel an in-progress login
  POST /api/oauth/login/code     manual paste fallback (remote / blocked localhost)
  POST /api/oauth/logout         remove an account
  POST /api/oauth/swap           swap the local CLI onto a pool account
  POST /api/oauth/refresh        refresh one account's token

Binds 127.0.0.1 only. A single-user localhost control plane, same threat model
as opencodex's proxy: anything that can reach loopback already runs as the user.
"""
from __future__ import annotations
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import oauth
import pool
import store

DEFAULT_PORT = int(os.environ.get("AGENT_POOL_SERVER_PORT", "7817"))
_PANEL_PATH = Path(__file__).with_name("panel.html")

# provider -> LoginFlow. One live browser login per provider at a time, exactly
# like opencodex keys its login state by provider name.
_flows: dict[str, oauth.LoginFlow] = {}
_flows_lock = threading.Lock()
# ThreadingHTTPServer dispatches each request on its own thread, so the server
# owns one shared connection (check_same_thread=False) and serializes every
# access through _db_lock. sqlite's own WAL + busy_timeout handle the poller
# daemon writing on a separate connection.
_db_lock = threading.Lock()
DB = store.connect(check_same_thread=False)


def _persist_login(result: dict, flow: oauth.LoginFlow) -> None:
    """on_complete hook: store the freshly-logged-in account (add or reconnect)."""
    provider = flow.provider
    reconnect_id = flow.meta.get("reconnect_id")
    label = flow.meta.get("label")
    with _db_lock:
        if reconnect_id is not None:
            # Guard against pointing a reconnect at a different identity.
            email = result.get("email") or ""
            existing = store.get_account_by_provider_email(DB, provider, email) if email else None
            if existing and existing["id"] != int(reconnect_id):
                store.log_event(DB, int(reconnect_id), "reconnect", False,
                                f"duplicate account: {email}")
                raise RuntimeError(f"{provider} / {email} is already account #{existing['id']}")
        pool.save_oauth_result(DB, provider, result, label=label,
                               reconnect_id=reconnect_id)
        _export_status()


def _export_status() -> None:
    try:
        import status
        status.write_status(status.build_payload(DB))
    except Exception as e:  # noqa: BLE001
        print(f"server: export-status failed: {e}")


def _status_payload() -> dict:
    import status
    with _db_lock:
        return status.build_payload(DB)


class Handler(BaseHTTPRequestHandler):
    server_version = "token-bar/1.0"

    # ── plumbing ────────────────────────────────────────────────────────────
    def log_message(self, *args):  # silence default stderr spam
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _body_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 65536:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            return {}

    # ── routing ─────────────────────────────────────────────────────────────
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._serve_panel()
        if path == "/api/status":
            return self._json(_status_payload())
        if path == "/api/oauth/providers":
            return self._json({"providers": list(oauth.BROWSER_FLOWS.keys())})
        if path == "/api/oauth/status":
            return self._oauth_status()
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/oauth/login":
            return self._oauth_login()
        if path == "/api/oauth/login/cancel":
            return self._oauth_cancel()
        if path == "/api/oauth/login/code":
            return self._oauth_manual_code()
        if path == "/api/oauth/logout":
            return self._oauth_logout()
        if path == "/api/oauth/swap":
            return self._oauth_swap()
        if path == "/api/oauth/refresh":
            return self._oauth_refresh()
        return self._json({"error": "not found"}, 404)

    # ── panel ───────────────────────────────────────────────────────────────
    def _serve_panel(self):
        try:
            html = _PANEL_PATH.read_bytes()
        except OSError:
            html = b"<h1>token-bar</h1><p>panel.html missing</p>"
        self._send(200, html, "text/html; charset=utf-8")

    # ── oauth ───────────────────────────────────────────────────────────────
    def _oauth_login(self):
        body = self._body_json()
        provider = str(body.get("provider", "")).strip().lower()
        if provider not in oauth.BROWSER_FLOWS:
            return self._json({"error": "unknown oauth provider"}, 400)
        reconnect_id = body.get("account_id")
        label = body.get("label")
        meta = {}
        if reconnect_id is not None:
            with _db_lock:
                acct = store.get_account(DB, int(reconnect_id))
            if not acct:
                return self._json({"error": "unknown account for reauth"}, 404)
            meta["reconnect_id"] = int(reconnect_id)
        if label:
            meta["label"] = str(label)[:80]
        with _flows_lock:
            prev = _flows.get(provider)
            if prev and prev.status == "pending":
                prev.cancel()
            flow = oauth.LoginFlow(provider, on_complete=_persist_login, meta=meta)
            _flows[provider] = flow
        try:
            auth_url = flow.start()
        except Exception as e:  # noqa: BLE001
            return self._json({"error": str(e)}, 409)
        # Open the browser server-side: a dashboard window.open() after an await
        # is popup-blocked, so the server (running on the user's machine) opens it.
        oauth.open_browser(auth_url)
        return self._json({"url": auth_url, "provider": provider})

    def _oauth_status(self):
        provider = parse_qs(urlparse(self.path).query).get("provider", [""])[0].strip().lower()
        with _flows_lock:
            flow = _flows.get(provider)
        if not flow:
            return self._json({"provider": provider, "status": "idle"})
        return self._json(flow.to_status())

    def _oauth_cancel(self):
        provider = str(self._body_json().get("provider", "")).strip().lower()
        with _flows_lock:
            flow = _flows.get(provider)
        if flow:
            flow.cancel()
        return self._json({"ok": True})

    def _oauth_manual_code(self):
        body = self._body_json()
        provider = str(body.get("provider", "")).strip().lower()
        pasted = str(body.get("input") or body.get("code") or "")
        if len(pasted) > 4096:
            return self._json({"error": "input too long"}, 400)
        with _flows_lock:
            flow = _flows.get(provider)
        if not flow or flow.status != "pending":
            return self._json({"error": "no login in progress"}, 409)
        try:
            flow.submit_code(pasted)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": str(e)}, 400)
        return self._json({"ok": True})

    def _oauth_logout(self):
        account_id = self._body_json().get("account_id")
        if account_id is None:
            return self._json({"error": "missing account_id"}, 400)
        with _db_lock:
            acct = store.get_account(DB, int(account_id))
            if not acct:
                return self._json({"error": "account not found"}, 404)
            store.delete_account(DB, int(account_id))
            store.log_event(DB, None, "onboard", True, f"removed {acct['provider']} {acct['email']}")
            _export_status()
        return self._json({"ok": True})

    def _oauth_swap(self):
        body = self._body_json()
        account_id = body.get("account_id")
        if account_id is None:
            return self._json({"error": "missing account_id"}, 400)
        with _db_lock:
            acct = store.get_account(DB, int(account_id))
            if not acct:
                return self._json({"error": "account not found"}, 404)
            import swap
            try:
                swap.cmd_swap(DB, acct["provider"], int(account_id), force=True)
                _export_status()
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e)}, 409)
        return self._json({"ok": True})

    def _oauth_refresh(self):
        account_id = self._body_json().get("account_id")
        if account_id is None:
            return self._json({"error": "missing account_id"}, 400)
        with _db_lock:
            try:
                pool.cmd_refresh(int(account_id))
                _export_status()
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e)}, 409)
        return self._json({"ok": True})


def serve(port: int = DEFAULT_PORT, host: str = "127.0.0.1") -> int:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print(f"token-bar server listening on http://{host}:{httpd.server_address[1]}")
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
