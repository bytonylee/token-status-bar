"""OAuth flows for all providers: Codex, Claude, xAI, Antigravity, GitHub Copilot.

Each provider function returns a dict with:
  access_token, refresh_token, id_token, expires_at (epoch s), account_id, email, plan, raw
"""
from __future__ import annotations
import base64, hashlib, json, os, secrets, socket, subprocess, threading, time, urllib.parse, urllib.request, urllib.error, http.server, socketserver, sys
from typing import Any

UA = "agent-pool/1.0"


# ─── PKCE ──────────────────────────────────────────────────────────────────
def gen_pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def gen_state() -> str:
    return secrets.token_hex(16)


# ─── browser ───────────────────────────────────────────────────────────────
def open_browser(url: str, incognito: bool = False):
    print(f"Opening browser{' (incognito)' if incognito else ''}:\n{url}\n")
    if incognito:
        try:
            subprocess.Popen(["open", "-na", "Google Chrome", "--args", "--incognito", url],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except Exception:
            print(f"Could not open incognito browser. Open this URL in a private window manually:\n{url}")
            return
    try:
        subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"Could not open browser automatically: {e}\nOpen this URL manually:\n{url}")


# ─── callback server ───────────────────────────────────────────────────────
class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        expected = getattr(self.server, "expected_path", None)
        # Only accept the registered callback path carrying OAuth params;
        # anything else (favicon probes, scanners) gets a 404 and does not
        # complete the flow.
        if (expected and parsed.path != expected) or not ("code" in params or "error" in params):
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body><h2>Authentication complete. You can close this tab.</h2></body></html>")
        # Deliver after responding so the browser tab always gets its success
        # page even if the waiting thread tears the listener down immediately.
        deliver = getattr(self.server, "on_result", None)
        if deliver is not None:
            deliver(params)
        else:  # pragma: no cover - legacy poll-loop compatibility
            self.server.result = params  # type: ignore

    def log_message(self, *args):
        pass


def wait_for_callback(port: int, timeout: int = 300, host: str = "127.0.0.1",
                      path: str | None = None) -> dict:
    """Start a local HTTP server on host:port, wait for OAuth callback, return params.

    host must match the registered redirect URI's host ("localhost" vs
    "127.0.0.1") so the browser's resolution and our listener agree.
    """
    flow = CallbackListener(port, host=host, path=path)
    try:
        result = flow.wait(timeout=timeout)
    finally:
        flow.close()
    if result is None:
        raise TimeoutError(f"No OAuth callback received within {timeout}s on port {port}")
    return result


class _DualStackHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threaded callback server.

    Threaded so the browser hitting the redirect resolves the waiter on the
    spot instead of on a 1s poll tick, and so a stray probe on the port cannot
    wedge the single request slot the old blocking loop relied on.
    """
    daemon_threads = True
    allow_reuse_address = True


class CallbackListener:
    """A live loopback listener that captures a single OAuth redirect.

    `localhost` can resolve to ::1 before 127.0.0.1 on some systems, so when the
    redirect URI advertises `localhost` over an IPv4 bind we also bind ::1 on the
    same port (best effort) — mirrors opencodex's loopbackBindHostnames.
    """

    def __init__(self, port: int, host: str = "127.0.0.1", path: str | None = None):
        self._event = threading.Event()
        self._result: dict | None = None
        self._servers: list[http.server.HTTPServer] = []
        primary = _DualStackHTTPServer((host, port), CallbackHandler)
        self.port = primary.server_address[1]
        self._wire(primary, path)
        # Dual-stack: bind ::1 on the resolved port too when advertising localhost.
        for extra_host in self._extra_hosts(host):
            try:
                extra = _DualStackHTTPServer((extra_host, self.port), CallbackHandler)
            except OSError:
                continue  # IPv6 unavailable, or the port is already covered — ignore.
            self._wire(extra, path)

    @staticmethod
    def _extra_hosts(host: str) -> list[str]:
        return ["::1"] if host.strip().lower() == "localhost" else []

    def _wire(self, server: http.server.HTTPServer, path: str | None) -> None:
        server.result = None  # type: ignore[attr-defined]
        server.expected_path = path  # type: ignore[attr-defined]
        server.on_result = self._deliver  # type: ignore[attr-defined]
        self._servers.append(server)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2},
                         daemon=True).start()

    def _deliver(self, params: dict) -> None:
        # First redirect wins; later ones (or the IPv6 twin) are ignored.
        if self._result is None:
            self._result = params
            self._event.set()

    def inject(self, params: dict) -> None:
        """Feed a manually pasted code/state as if it arrived on the callback."""
        self._deliver(params)

    def wait(self, timeout: int = 300) -> dict | None:
        self._event.wait(timeout)
        return self._result

    @property
    def redirect_host(self) -> str:
        return getattr(self._servers[0], "advertise_host", "")

    def close(self) -> None:
        for server in self._servers:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        self._servers.clear()


# ─── HTTP helper ───────────────────────────────────────────────────────────
def http_post(url: str, data: dict, headers: dict | None = None) -> tuple[int, Any]:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return _do(req)


def http_post_json(url: str, data: dict, headers: dict | None = None) -> tuple[int, Any]:
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return _do(req)


def http_get(url: str, headers: dict | None = None) -> tuple[int, Any, dict]:
    req = urllib.request.Request(url, method="GET")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw), dict(r.headers)
            except json.JSONDecodeError:
                return r.status, raw.decode(errors="replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace"), dict(e.headers)
    except urllib.error.URLError as e:
        return 0, str(e.reason), {}


def _do(req: urllib.request.Request) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw.decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except urllib.error.URLError as e:
        return 0, str(e.reason)


def decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * (4 - len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


# ─── Codex (OpenAI) ────────────────────────────────────────────────────────
CODEX = {
    "auth_url": "https://auth.openai.com/oauth/authorize",
    "token_url": "https://auth.openai.com/oauth/token",
    "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
    "redirect_uri": "http://localhost:1455/auth/callback",
    "scope": "openid email profile offline_access",
    "port": 1455,
}


def login_codex(incognito: bool = False) -> dict:
    verifier, challenge = gen_pkce()
    state = gen_state()
    params = {
        "client_id": CODEX["client_id"],
        "response_type": "code",
        "redirect_uri": CODEX["redirect_uri"],
        "scope": CODEX["scope"],
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "login",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    auth_url = f"{CODEX['auth_url']}?{urllib.parse.urlencode(params)}"
    open_browser(auth_url, incognito)
    print("Waiting for Codex callback on port 1455...")
    result = wait_for_callback(CODEX["port"], host="localhost", path="/auth/callback")
    if result.get("error"):
        raise RuntimeError(f"Codex OAuth error: {result.get('error_description', result['error'])}")
    if result.get("state") != state:
        raise RuntimeError("Codex OAuth state mismatch")
    code = result["code"]
    st, tok = http_post(CODEX["token_url"], {
        "grant_type": "authorization_code",
        "client_id": CODEX["client_id"],
        "code": code,
        "redirect_uri": CODEX["redirect_uri"],
        "code_verifier": verifier,
    })
    if st != 200:
        raise RuntimeError(f"Codex token exchange failed: {st} {tok}")
    claims = decode_jwt_payload(tok.get("id_token", ""))
    auth_info = claims.get("https://api.openai.com/auth", {})
    email = claims.get("email") or claims.get("https://api.openai.com/profile", {}).get("email", "")
    return {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "id_token": tok.get("id_token"),
        "expires_at": time.time() + tok.get("expires_in", 3600),
        "account_id": auth_info.get("chatgpt_account_id", ""),
        "email": email,
        "plan": auth_info.get("chatgpt_plan_type", ""),
        "raw": tok,
    }


def refresh_codex(refresh_token: str) -> dict:
    st, tok = http_post(CODEX["token_url"], {
        "grant_type": "refresh_token",
        "client_id": CODEX["client_id"],
        "refresh_token": refresh_token,
    })
    if st != 200:
        raise RuntimeError(f"Codex refresh failed: {st} {tok}")
    return {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", refresh_token),
        "id_token": tok.get("id_token"),
        "expires_at": time.time() + tok.get("expires_in", 3600),
        "raw": tok,
    }


# ─── Claude (Anthropic) ────────────────────────────────────────────────────
CLAUDE = {
    "auth_url": "https://claude.ai/oauth/authorize",
    "token_url": "https://api.anthropic.com/v1/oauth/token",
    "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
    "scope": "user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload",
    "port": 54545,
}


def login_claude(incognito: bool = False) -> dict:
    verifier, challenge = gen_pkce()
    state = gen_state()
    redirect = f"http://localhost:{CLAUDE['port']}/callback"
    params = {
        "code": "true",
        "client_id": CLAUDE["client_id"],
        "response_type": "code",
        "redirect_uri": redirect,
        "scope": CLAUDE["scope"],
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{CLAUDE['auth_url']}?{urllib.parse.urlencode(params)}"
    open_browser(auth_url, incognito)
    print(f"Waiting for Claude callback on port {CLAUDE['port']}...")
    result = wait_for_callback(CLAUDE["port"], host="localhost", path="/callback")
    if result.get("error"):
        raise RuntimeError(f"Claude OAuth error: {result.get('error_description', result['error'])}")
    if result.get("state") != state:
        raise RuntimeError("Claude OAuth state mismatch")
    st, tok = http_post_json(CLAUDE["token_url"], {
        "grant_type": "authorization_code",
        "client_id": CLAUDE["client_id"],
        "code": result["code"],
        "state": state,
        "redirect_uri": redirect,
        "code_verifier": verifier,
    }, {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"})
    if st != 200:
        raise RuntimeError(f"Claude token exchange failed: {st} {tok}")
    # Extract email + account_id from the token response
    email = ""
    account_id = ""
    if isinstance(tok, dict):
        acct = tok.get("account") or {}
        email = acct.get("email_address", "")
        account_id = acct.get("uuid", "")
    return {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "id_token": tok.get("id_token", ""),
        "expires_at": time.time() + tok.get("expires_in", 3600),
        "account_id": account_id,
        "email": email,
        "plan": "",
        "raw": tok,
    }


def refresh_claude(refresh_token: str) -> dict:
    st, tok = http_post_json(CLAUDE["token_url"], {
        "grant_type": "refresh_token",
        "client_id": CLAUDE["client_id"],
        "refresh_token": refresh_token,
    }, {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"})
    if st != 200:
        raise RuntimeError(f"Claude refresh failed: {st} {tok}")
    return {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", refresh_token),
        "id_token": tok.get("id_token", ""),
        "expires_at": time.time() + tok.get("expires_in", 3600),
        "raw": tok,
    }


# ─── xAI / Grok ────────────────────────────────────────────────────────────
XAI = {
    "auth_url": "https://auth.x.ai/oauth2/authorize",
    "token_url": "https://auth.x.ai/oauth2/token",
    "client_id": "b1a00492-073a-47ea-816f-4c329264a828",
    "scope": "openid profile email offline_access grok-cli:access api:access",
    "port": 56121,
}


def login_xai(incognito: bool = False) -> dict:
    verifier, challenge = gen_pkce()
    state = gen_state()
    nonce = secrets.token_hex(16)
    redirect = f"http://127.0.0.1:{XAI['port']}/callback"
    params = {
        "response_type": "code",
        "client_id": XAI["client_id"],
        "redirect_uri": redirect,
        "scope": XAI["scope"],
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
        "plan": "generic",
    }
    auth_url = f"{XAI['auth_url']}?{urllib.parse.urlencode(params)}"
    open_browser(auth_url, incognito)
    print(f"Waiting for xAI callback on port {XAI['port']}...")
    result = wait_for_callback(XAI["port"], host="127.0.0.1", path="/callback")
    if result.get("error"):
        raise RuntimeError(f"xAI OAuth error: {result.get('error_description', result['error'])}")
    if result.get("state") != state:
        raise RuntimeError("xAI OAuth state mismatch")
    st, tok = http_post(XAI["token_url"], {
        "grant_type": "authorization_code",
        "client_id": XAI["client_id"],
        "code": result["code"],
        "redirect_uri": redirect,
        "code_verifier": verifier,
    })
    if st != 200:
        raise RuntimeError(f"xAI token exchange failed: {st} {tok}")
    claims = decode_jwt_payload(tok.get("id_token", ""))
    return {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "id_token": tok.get("id_token", ""),
        "expires_at": time.time() + tok.get("expires_in", 3600),
        "account_id": claims.get("sub", ""),
        "email": claims.get("email", ""),
        "plan": "",
        "raw": tok,
    }


def refresh_xai(refresh_token: str) -> dict:
    st, tok = http_post(XAI["token_url"], {
        "grant_type": "refresh_token",
        "client_id": XAI["client_id"],
        "refresh_token": refresh_token,
    })
    if st != 200:
        raise RuntimeError(f"xAI refresh failed: {st} {tok}")
    return {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", refresh_token),
        "id_token": tok.get("id_token", ""),
        "expires_at": time.time() + tok.get("expires_in", 3600),
        "raw": tok,
    }


# ─── Antigravity (Google OAuth) ────────────────────────────────────────────
# Google OAuth credentials are loaded from a gitignored file
# (secrets/antigravity.env) so they are never committed. Env vars override
# the file if set.
def _load_antigravity_creds():
    env_id = os.environ.get("ANTIGRAVITY_CLIENT_ID", "")
    env_secret = os.environ.get("ANTIGRAVITY_CLIENT_SECRET", "")
    if env_id and env_secret:
        return env_id, env_secret
    # Look for antigravity.env in the user data dir (~/solo/token-status-bar/secrets/)
    # so it is never bundled inside the read-only .app.
    data_dir = os.environ.get("AGENT_POOL_DATA_DIR",
                              str(os.path.expanduser("~/solo/token-status-bar/secrets")))
    env_path = os.path.join(data_dir, "antigravity.env")
    file_id, file_secret = "", ""
    try:
        # Best-effort tighten: this file holds OAuth client credentials.
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("ANTIGRAVITY_CLIENT_ID="):
                    file_id = line.split("=", 1)[1]
                elif line.startswith("ANTIGRAVITY_CLIENT_SECRET="):
                    file_secret = line.split("=", 1)[1]
    except FileNotFoundError:
        pass
    return env_id or file_id, env_secret or file_secret

_AG_CLIENT_ID, _AG_CLIENT_SECRET = _load_antigravity_creds()
ANTIGRAVITY = {
    "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
    "token_url": "https://oauth2.googleapis.com/token",
    "client_id": _AG_CLIENT_ID,
    "client_secret": _AG_CLIENT_SECRET,
    "scope": "https://www.googleapis.com/auth/cloud-platform https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/userinfo.profile https://www.googleapis.com/auth/cclog https://www.googleapis.com/auth/experimentsandconfigs",
    "port": 51121,
}


def _ensure_antigravity_creds():
    if ANTIGRAVITY["client_id"] and ANTIGRAVITY["client_secret"]:
        return
    client_id, client_secret = _load_antigravity_creds()
    ANTIGRAVITY["client_id"] = client_id
    ANTIGRAVITY["client_secret"] = client_secret


def login_antigravity(incognito: bool = False) -> dict:
    _ensure_antigravity_creds()
    if not ANTIGRAVITY["client_id"] or not ANTIGRAVITY["client_secret"]:
        raise RuntimeError(
            "Antigravity Google OAuth credentials missing. Put them in "
            "secrets/antigravity.env (ANTIGRAVITY_CLIENT_ID, "
            "ANTIGRAVITY_CLIENT_SECRET) or export them as env vars."
        )
    state = gen_state()
    redirect = f"http://localhost:{ANTIGRAVITY['port']}/oauth-callback"
    params = {
        "client_id": ANTIGRAVITY["client_id"],
        "response_type": "code",
        "redirect_uri": redirect,
        "scope": ANTIGRAVITY["scope"],
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = f"{ANTIGRAVITY['auth_url']}?{urllib.parse.urlencode(params)}"
    open_browser(auth_url, incognito)
    print(f"Waiting for Antigravity callback on port {ANTIGRAVITY['port']}...")
    result = wait_for_callback(ANTIGRAVITY["port"], host="localhost", path="/oauth-callback")
    if result.get("error"):
        raise RuntimeError(f"Antigravity OAuth error: {result.get('error_description', result['error'])}")
    if result.get("state") != state:
        raise RuntimeError("Antigravity OAuth state mismatch")
    st, tok = http_post(ANTIGRAVITY["token_url"], {
        "grant_type": "authorization_code",
        "client_id": ANTIGRAVITY["client_id"],
        "code": result["code"],
        "redirect_uri": redirect,
        "client_secret": ANTIGRAVITY["client_secret"],
    })
    if st != 200:
        raise RuntimeError(f"Antigravity token exchange failed: {st} {tok}")
    # Fetch user info
    email = ""
    st2, userinfo = http_get("https://www.googleapis.com/oauth2/v2/userinfo",
                             {"Authorization": f"Bearer {tok['access_token']}"})[:2]
    if isinstance(userinfo, dict):
        email = userinfo.get("email", "")
    return {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "id_token": tok.get("id_token", ""),
        "expires_at": time.time() + tok.get("expires_in", 3600),
        "account_id": "",
        "email": email,
        "plan": "",
        "raw": tok,
    }


def refresh_antigravity(refresh_token: str) -> dict:
    _ensure_antigravity_creds()
    st, tok = http_post(ANTIGRAVITY["token_url"], {
        "grant_type": "refresh_token",
        "client_id": ANTIGRAVITY["client_id"],
        "client_secret": ANTIGRAVITY["client_secret"],
        "refresh_token": refresh_token,
    })
    if st != 200:
        raise RuntimeError(f"Antigravity refresh failed: {st} {tok}")
    return {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", refresh_token),
        "id_token": tok.get("id_token", ""),
        "expires_at": time.time() + tok.get("expires_in", 3600),
        "raw": tok,
    }


# ─── GitHub Copilot (device flow) ──────────────────────────────────────────
COPILOT = {
    "device_code_url": "https://github.com/login/device/code",
    "token_url": "https://github.com/login/oauth/access_token",
    "copilot_token_url": "https://api.github.com/copilot_internal/v2/token",
    "client_id": "Iv1.b507a08c87ecfe98",
    "scope": "read:user",
}


def login_copilot(incognito: bool = False) -> dict:
    # Step 1: request device code
    st, resp = http_post_json(COPILOT["device_code_url"], {
        "client_id": COPILOT["client_id"],
        "scope": COPILOT["scope"],
    }, {"User-Agent": "agent-pool/1.0", "Accept": "application/json"})
    if st != 200:
        raise RuntimeError(f"Copilot device code request failed: {st} {resp}")
    device_code = resp["device_code"]
    user_code = resp["user_code"]
    verification_uri = resp.get("verification_uri", "https://github.com/login/device")
    interval = resp.get("interval", 5)
    expires_in = resp.get("expires_in", 899)

    print(f"\n=== GitHub Copilot Device Flow ===")
    print(f"Open: {verification_uri}")
    print(f"Enter code: {user_code}")
    open_browser(verification_uri)
    print(f"Waiting for authorization (expires in {expires_in}s)...")

    # Step 2: poll for token
    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
        st, tok = http_post_json(COPILOT["token_url"], {
            "client_id": COPILOT["client_id"],
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }, {"User-Agent": "agent-pool/1.0", "Accept": "application/json"})
        if st == 200 and isinstance(tok, dict) and tok.get("access_token"):
            break
        if isinstance(tok, dict):
            err = tok.get("error", "")
            if err == "authorization_pending":
                continue
            elif err == "slow_down":
                interval += 5
                continue
            elif err == "expired_token":
                raise RuntimeError("Copilot device code expired")
            else:
                raise RuntimeError(f"Copilot token poll error: {tok}")
    else:
        raise RuntimeError("Copilot device flow timed out")

    github_token = tok["access_token"]

    # Step 3: fetch GitHub user info
    st2, user = http_get("https://api.github.com/user",
                         {"Authorization": f"token {github_token}", "User-Agent": "agent-pool/1.0"})[:2]
    username = ""
    github_user_id = ""
    if isinstance(user, dict):
        username = user.get("login", "")
        github_user_id = str(user.get("id", ""))

    # Step 4: exchange for Copilot token
    st3, ctoken_resp = http_get(COPILOT["copilot_token_url"],
                                {"Authorization": f"token {github_token}",
                                 "User-Agent": "agent-pool/1.0",
                                 "X-GitHub-Api-Version": "2025-04-01"})[:2]

    copilot_token = ""
    copilot_expires = 0
    if isinstance(ctoken_resp, dict):
        copilot_token = ctoken_resp.get("token", "")
        copilot_expires = ctoken_resp.get("expires_at", 0)

    return {
        "access_token": github_token,  # the github oauth token (long-lived)
        "refresh_token": None,  # GitHub device flow has no refresh token
        "id_token": "",
        "expires_at": copilot_expires or (time.time() + 7200),  # copilot token expiry
        "account_id": github_user_id,
        "email": username,  # GitHub username as identifier
        "plan": "copilot",
        "raw": {"github_token": github_token, "copilot_token": copilot_token,
                "copilot_expires_at": copilot_expires, "user": user if isinstance(user, dict) else {}},
    }


def refresh_copilot(github_token: str) -> dict:
    """Copilot tokens can't be refreshed via OAuth; re-exchange the github token for a new copilot token."""
    st, resp = http_get(COPILOT["copilot_token_url"],
                        {"Authorization": f"token {github_token}",
                         "User-Agent": "agent-pool/1.0",
                         "X-GitHub-Api-Version": "2025-04-01"})[:2]
    if st != 200:
        raise RuntimeError(f"Copilot token refresh failed: {st} {resp}")
    return {
        "access_token": github_token,
        "copilot_token": resp.get("token", ""),
        "expires_at": resp.get("expires_at", 0),
        "raw": resp,
    }


# ─── Devin (API key — no OAuth) ────────────────────────────────────────────
def login_devin(api_key: str) -> dict:
    """Devin uses API keys, not OAuth. Get key from app.devin.ai dashboard."""
    # Validate the key by fetching org info
    st, resp = http_get("https://api.devin.ai/v1/user",
                        {"Authorization": f"Bearer {api_key}", "User-Agent": UA})[:2]
    if st != 200:
        raise RuntimeError(f"Devin API key validation failed: HTTP {st}: {str(resp)[:120]}")
    email = ""
    org_id = ""
    if isinstance(resp, dict):
        email = resp.get("email", "")
        org_id = str(resp.get("organization_id", resp.get("org_id", "")))
    return {
        "access_token": api_key,
        "refresh_token": None,
        "id_token": "",
        "expires_at": 0,  # API keys don't expire
        "account_id": org_id,
        "email": email or "devin-user",
        "plan": "",
        "raw": {"api_key": api_key, "user_info": resp if isinstance(resp, dict) else {}},
    }


# ─── registry ──────────────────────────────────────────────────────────────
LOGIN_FUNCS = {
    "codex": login_codex,
    "claude": login_claude,
    "xai": login_xai,
    "antigravity": login_antigravity,
    "copilot": login_copilot,
}

REFRESH_FUNCS = {
    "codex": refresh_codex,
    "claude": refresh_claude,
    "xai": refresh_xai,
    "antigravity": refresh_antigravity,
    "copilot": refresh_copilot,
}

PROVIDERS = list(LOGIN_FUNCS.keys()) + ["devin"]


# ─── server-driven login flows (browser dashboard) ─────────────────────────
# opencodex parity: the app never blocks a terminal. The local server starts a
# flow (POST /api/oauth/login), opens the browser itself, and the dashboard
# polls GET /api/oauth/status until the loopback callback lands. Each provider
# is described declaratively so the flow manager owns the state machine
# (listener + CSRF + background exchange) instead of every provider function.

def _authorize_codex(state: str, challenge: str) -> str:
    params = {
        "client_id": CODEX["client_id"],
        "response_type": "code",
        "redirect_uri": CODEX["redirect_uri"],
        "scope": CODEX["scope"],
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "login",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    return f"{CODEX['auth_url']}?{urllib.parse.urlencode(params)}"


def _exchange_codex(code: str, state: str, verifier: str) -> dict:
    st, tok = http_post(CODEX["token_url"], {
        "grant_type": "authorization_code",
        "client_id": CODEX["client_id"],
        "code": code,
        "redirect_uri": CODEX["redirect_uri"],
        "code_verifier": verifier,
    })
    if st != 200:
        raise RuntimeError(f"Codex token exchange failed: {st} {tok}")
    claims = decode_jwt_payload(tok.get("id_token", ""))
    auth_info = claims.get("https://api.openai.com/auth", {})
    email = claims.get("email") or claims.get("https://api.openai.com/profile", {}).get("email", "")
    return {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "id_token": tok.get("id_token"),
        "expires_at": time.time() + tok.get("expires_in", 3600),
        "account_id": auth_info.get("chatgpt_account_id", ""),
        "email": email,
        "plan": auth_info.get("chatgpt_plan_type", ""),
        "raw": tok,
    }


def _authorize_claude(state: str, challenge: str) -> str:
    redirect = f"http://localhost:{CLAUDE['port']}/callback"
    params = {
        "code": "true",
        "client_id": CLAUDE["client_id"],
        "response_type": "code",
        "redirect_uri": redirect,
        "scope": CLAUDE["scope"],
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{CLAUDE['auth_url']}?{urllib.parse.urlencode(params)}"


def _exchange_claude(code: str, state: str, verifier: str) -> dict:
    redirect = f"http://localhost:{CLAUDE['port']}/callback"
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
    st, tok = http_post_json(CLAUDE["token_url"], {
        "grant_type": "authorization_code",
        "client_id": CLAUDE["client_id"],
        "code": code,
        "state": state,
        "redirect_uri": redirect,
        "code_verifier": verifier,
    }, {"User-Agent": ua})
    if st != 200:
        raise RuntimeError(f"Claude token exchange failed: {st} {tok}")
    email, account_id = "", ""
    if isinstance(tok, dict):
        acct = tok.get("account") or {}
        email = acct.get("email_address", "")
        account_id = acct.get("uuid", "")
    return {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "id_token": tok.get("id_token", ""),
        "expires_at": time.time() + tok.get("expires_in", 3600),
        "account_id": account_id,
        "email": email,
        "plan": "",
        "raw": tok,
    }


def _authorize_xai(state: str, challenge: str) -> str:
    redirect = f"http://127.0.0.1:{XAI['port']}/callback"
    params = {
        "response_type": "code",
        "client_id": XAI["client_id"],
        "redirect_uri": redirect,
        "scope": XAI["scope"],
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": secrets.token_hex(16),
        "plan": "generic",
    }
    return f"{XAI['auth_url']}?{urllib.parse.urlencode(params)}"


def _exchange_xai(code: str, state: str, verifier: str) -> dict:
    redirect = f"http://127.0.0.1:{XAI['port']}/callback"
    st, tok = http_post(XAI["token_url"], {
        "grant_type": "authorization_code",
        "client_id": XAI["client_id"],
        "code": code,
        "redirect_uri": redirect,
        "code_verifier": verifier,
    })
    if st != 200:
        raise RuntimeError(f"xAI token exchange failed: {st} {tok}")
    claims = decode_jwt_payload(tok.get("id_token", ""))
    return {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "id_token": tok.get("id_token", ""),
        "expires_at": time.time() + tok.get("expires_in", 3600),
        "account_id": claims.get("sub", ""),
        "email": claims.get("email", ""),
        "plan": "",
        "raw": tok,
    }


def _authorize_antigravity(state: str, challenge: str) -> str:
    _ensure_antigravity_creds()
    if not ANTIGRAVITY["client_id"] or not ANTIGRAVITY["client_secret"]:
        raise RuntimeError(
            "Antigravity Google OAuth credentials missing. Put them in "
            "secrets/antigravity.env (ANTIGRAVITY_CLIENT_ID, "
            "ANTIGRAVITY_CLIENT_SECRET) or export them as env vars."
        )
    redirect = f"http://localhost:{ANTIGRAVITY['port']}/oauth-callback"
    params = {
        "client_id": ANTIGRAVITY["client_id"],
        "response_type": "code",
        "redirect_uri": redirect,
        "scope": ANTIGRAVITY["scope"],
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{ANTIGRAVITY['auth_url']}?{urllib.parse.urlencode(params)}"


def _exchange_antigravity(code: str, state: str, verifier: str) -> dict:
    _ensure_antigravity_creds()
    redirect = f"http://localhost:{ANTIGRAVITY['port']}/oauth-callback"
    st, tok = http_post(ANTIGRAVITY["token_url"], {
        "grant_type": "authorization_code",
        "client_id": ANTIGRAVITY["client_id"],
        "code": code,
        "redirect_uri": redirect,
        "client_secret": ANTIGRAVITY["client_secret"],
    })
    if st != 200:
        raise RuntimeError(f"Antigravity token exchange failed: {st} {tok}")
    email = ""
    userinfo = http_get("https://www.googleapis.com/oauth2/v2/userinfo",
                        {"Authorization": f"Bearer {tok['access_token']}"})[1]
    if isinstance(userinfo, dict):
        email = userinfo.get("email", "")
    return {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "id_token": tok.get("id_token", ""),
        "expires_at": time.time() + tok.get("expires_in", 3600),
        "account_id": "",
        "email": email,
        "plan": "",
        "raw": tok,
    }


# Each spec: which loopback (host/port/path) to advertise, whether the flow uses
# PKCE, and the authorize-URL + token-exchange callables above.
BROWSER_FLOWS: dict[str, dict] = {
    "codex": {"host": "localhost", "port": CODEX["port"], "path": "/auth/callback",
              "pkce": True, "authorize": _authorize_codex, "exchange": _exchange_codex},
    "claude": {"host": "localhost", "port": CLAUDE["port"], "path": "/callback",
               "pkce": True, "authorize": _authorize_claude, "exchange": _exchange_claude},
    "xai": {"host": "127.0.0.1", "port": XAI["port"], "path": "/callback",
            "pkce": True, "authorize": _authorize_xai, "exchange": _exchange_xai},
    "antigravity": {"host": "localhost", "port": ANTIGRAVITY["port"], "path": "/oauth-callback",
                    "pkce": False, "authorize": _authorize_antigravity, "exchange": _exchange_antigravity},
}


def mask_email(email: str | None) -> str:
    """a***@domain — never surface a full address to the browser panel."""
    if not email or "@" not in email:
        return email or ""
    local, _, domain = email.partition("@")
    head = local[0] if local else ""
    return f"{head}***@{domain}"


class LoginFlow:
    """One in-progress browser OAuth login, owned by the local server.

    Lifecycle: start() builds the auth URL + starts the loopback listener and a
    background worker that waits for the redirect, verifies CSRF state, and
    exchanges the code. status() is what the dashboard polls; submit_code()
    feeds a manually pasted redirect for hosts where localhost is unreachable.
    """

    def __init__(self, provider: str, on_complete=None, meta: dict | None = None):
        if provider not in BROWSER_FLOWS:
            raise ValueError(f"{provider} is not a browser OAuth provider")
        self.provider = provider
        self.spec = BROWSER_FLOWS[provider]
        self.state = gen_state()
        self.verifier = ""
        self.auth_url = ""
        self.status = "pending"          # pending|complete|error|cancelled
        self.error: str | None = None
        self.result: dict | None = None
        self.started_at = time.time()
        self.meta = meta or {}           # caller context (e.g. reconnect id / label)
        self._on_complete = on_complete  # called(result, flow) in worker thread
        self._listener: CallbackListener | None = None
        self._worker: threading.Thread | None = None

    def start(self) -> str:
        challenge = ""
        if self.spec["pkce"]:
            self.verifier, challenge = gen_pkce()
        self.auth_url = self.spec["authorize"](self.state, challenge)
        self._listener = CallbackListener(self.spec["port"], host=self.spec["host"],
                                          path=self.spec["path"])
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()
        return self.auth_url

    def _run(self) -> None:
        try:
            params = self._listener.wait(timeout=300) if self._listener else None
            if self.status == "cancelled":
                return
            if params is None:
                self._fail("No OAuth callback received within 300s")
                return
            if params.get("error"):
                self._fail(params.get("error_description", params["error"]))
                return
            if params.get("state") != self.state:
                self._fail("OAuth state mismatch")
                return
            result = self.spec["exchange"](params["code"], self.state, self.verifier)
            self.result = result
            if self._on_complete is not None:
                # Persist before flipping to complete so a dashboard that polls
                # the instant it sees "complete" always finds the account saved.
                try:
                    self._on_complete(result, self)  # (result, flow)
                except Exception as e:  # noqa: BLE001
                    self._fail(f"login succeeded but saving failed: {e}")
                    return
            self.status = "complete"
        except Exception as e:  # noqa: BLE001 - surfaced to the dashboard
            self._fail(str(e))
        finally:
            self._close_listener()

    def _fail(self, message: str) -> None:
        self.error = message
        if self.status not in ("complete", "cancelled"):
            self.status = "error"

    def submit_code(self, pasted: str) -> None:
        """Inject a manually pasted redirect URL / code into the waiting flow."""
        if not self._listener:
            raise RuntimeError("flow not started")
        parsed = urllib.parse.urlparse(pasted.strip())
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        if "code" not in params:
            # Bare "code" or "code#state" paste (same PKCE session).
            raw = pasted.strip()
            code, _, st = raw.partition("#")
            params = {"code": code, "state": st or self.state}
        self._listener.inject(params)

    def cancel(self) -> None:
        self.status = "cancelled"
        self._close_listener()

    def _close_listener(self) -> None:
        if self._listener:
            self._listener.close()
            self._listener = None

    def to_status(self) -> dict:
        out = {"provider": self.provider, "status": self.status,
               "auth_url": self.auth_url, "started_at": self.started_at}
        if self.error:
            out["error"] = self.error
        if self.result:
            out["email"] = mask_email(self.result.get("email"))
            out["plan"] = self.result.get("plan") or ""
        return out
