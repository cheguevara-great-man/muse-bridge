#!/usr/bin/env python3
"""muse-mcp: MCP (Streamable HTTP) + OAuth 2.0 front end for Muse Bridge.

Scheme B of the Muse Bridge deployment. This service runs on the VPS next
to the relay (muse-bridge.py). It exposes a standards-compliant MCP
endpoint protected by OAuth 2.0 bearer tokens, and forwards every tool
call to the relay's controller API on loopback (http://127.0.0.1:8471)
using a dedicated bridge token. The relay routes to the Windows agent;
this service executes nothing itself.

Single file, Python 3.11 standard library only.

Configuration (environment):
  MUSE_MCP_LISTEN            listen address, default 127.0.0.1:8472
  MUSE_MCP_ISSUER            public base URL, default https://${VPS_SNI}
  MUSE_MCP_RELAY             relay base URL, default http://127.0.0.1:8471
  MUSE_MCP_BRIDGE_TOKEN_FILE file holding the relay token for this service,
                             default /etc/muse-bridge/mcp-bridge-token
  MUSE_MCP_STATE             OAuth state JSON file,
                             default /var/lib/muse-mcp/state.json
  MUSE_MCP_TARGET            the one PC this service may reach,
                             default ${PC_NAME} (tools cannot override it)

Security notes:
  - Only SHA-256 hashes of the owner secret, client secrets, authorization
    codes, access tokens and refresh tokens are persisted.
  - The /authorize approval form requires the owner secret; authorization
    fails closed until owner_secret_hash is provisioned in the state file.
  - Logs carry method, path (no query), status and tool names only --
    never tokens, codes, verifiers, secrets or payload contents.
"""

import base64
import hashlib
import hmac
import html
import http.client
import json
import os
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ISSUER = os.environ.get("MUSE_MCP_ISSUER", "https://${VPS_SNI}").rstrip("/")
LISTEN = os.environ.get("MUSE_MCP_LISTEN", "127.0.0.1:8472")
RELAY_BASE = os.environ.get("MUSE_MCP_RELAY", "http://127.0.0.1:8471").rstrip("/")
BRIDGE_TOKEN_FILE = os.environ.get(
    "MUSE_MCP_BRIDGE_TOKEN_FILE", "/etc/muse-bridge/mcp-bridge-token"
)
STATE_PATH = os.environ.get("MUSE_MCP_STATE", "/var/lib/muse-mcp/state.json")
TARGET_PC = os.environ.get("MUSE_MCP_TARGET", "${PC_NAME}")

SERVER_NAME = "muse-bridge-rd925"
SERVER_VERSION = "1.0.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = "2025-06-18"

MAX_BODY_MCP = 4 * 1024 * 1024        # /mcp request body cap
MAX_BODY_SMALL = 64 * 1024            # OAuth endpoint body cap
ACCESS_TOKEN_TTL = 3600               # 1 hour
REFRESH_TOKEN_TTL = 30 * 24 * 3600    # 30 days
AUTH_CODE_TTL = 600                   # 10 minutes
SCOPE = "mcp"

# /authorize approval rate limit: lock the source after this many failed
# owner-secret attempts, for this many seconds.
AUTH_FAIL_LIMIT = 5
AUTH_LOCK_SECONDS = 60


def log(msg):
    """stderr log line; callers must never pass secrets or payload data."""
    sys.stderr.write("muse-mcp %s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg))
    sys.stderr.flush()


def sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def b64url_nopad(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def pkce_challenge(verifier):
    return b64url_nopad(hashlib.sha256(verifier.encode("ascii")).digest())


# ---------------------------------------------------------------------------
# Persistent OAuth state (hashes only, atomic writes)
# ---------------------------------------------------------------------------

class State:
    """JSON-file-backed store for clients, codes and token hashes."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {
            "owner_secret_hash": None,
            "clients": {},
            "codes": {},
            "access_tokens": {},
            "refresh_tokens": {},
        }
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            log("state file %s not found; starting with empty state" % self.path)
            return
        except (OSError, ValueError) as exc:
            log("ERROR: cannot load state file %s (%s); starting empty (fails closed)" % (self.path, exc))
            return
        if isinstance(raw, dict):
            for key in self.data:
                if key in raw:
                    self.data[key] = raw[key]
        if not self.data.get("owner_secret_hash"):
            log("WARNING: owner_secret_hash is not provisioned; /authorize will fail closed")

    def save(self):
        """Persist atomically. Caller must hold self.lock."""
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass


STATE = State(STATE_PATH)

# /authorize failure tracking: source -> {"count": n, "locked_until": ts}
_auth_failures = {}
_auth_failures_lock = threading.Lock()


def auth_source_locked(source):
    with _auth_failures_lock:
        rec = _auth_failures.get(source)
        return bool(rec and rec.get("locked_until", 0) > time.time())


def auth_source_record_failure(source):
    with _auth_failures_lock:
        rec = _auth_failures.setdefault(source, {"count": 0, "locked_until": 0})
        rec["count"] += 1
        if rec["count"] >= AUTH_FAIL_LIMIT:
            rec["locked_until"] = time.time() + AUTH_LOCK_SECONDS
            rec["count"] = 0


def auth_source_record_success(source):
    with _auth_failures_lock:
        _auth_failures.pop(source, None)


# ---------------------------------------------------------------------------
# Relay client
# ---------------------------------------------------------------------------

def read_bridge_token():
    with open(BRIDGE_TOKEN_FILE, "r", encoding="utf-8") as fh:
        return fh.read().strip()


def relay_request(method, path, body_obj, timeout):
    """One call to the relay's controller API on loopback.

    Returns (http_status, response_text). Raises OSError / http.client
    exceptions on transport failure; callers convert those to tool errors.
    The bridge token is read from its file on every call and never logged.
    """
    token = read_bridge_token()
    if not token:
        raise RuntimeError("bridge token file is empty: %s" % BRIDGE_TOKEN_FILE)
    url = urlparse(RELAY_BASE)
    if url.scheme == "https":
        conn = http.client.HTTPSConnection(url.hostname, url.port or 443, timeout=timeout)
    else:
        conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=timeout)
    payload = None
    headers = {"X-Bridge-Token": token, "User-Agent": "muse-mcp/1.0"}
    if body_obj is not None:
        payload = json.dumps(body_obj).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, data.decode("utf-8", "replace")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "status",
        "description": "Show Muse Bridge relay status: which PCs are online.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]

# NOTE (2026-11-02): PC tool execution does not live in this service.
# Per the user's design correction, tool calls are executed by the live
# Codex session on the PC through the user's own codex-pc-rpc-v1 push
# channel; the connector serves those tools locally (pc_tools/pc_call
# in bin/client.py). The earlier codex_task_* delegation tools (which
# spawned headless `codex exec` runs) were removed at the user's
# direction; their implementation is preserved in the deployment
# backups, not in this file.

TOOL_NAMES = [t["name"] for t in TOOLS]

def _field_text(value):
    """stdout/stderr on the wire is a string or a compressed-field object."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _format_cmd_result(res):
    """Render a relay /cmd result object as (text, is_error).

    Two shapes exist on the wire (DESIGN.md A.4): success carries
    ok + exitCode; error paths carry exit and no ok. Both are handled.
    """
    is_error = res.get("ok") is not True
    exit_code = res.get("exitCode", res.get("exit"))
    parts = []
    if is_error or exit_code not in (None, 0):
        parts.append("[exit %s]" % (exit_code if exit_code is not None else "error"))
    stdout = _field_text(res.get("stdout"))
    stderr = _field_text(res.get("stderr"))
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(stderr)
    if res.get("error"):
        parts.append(_field_text(res.get("error")))
    return ("\n".join(parts) if parts else "(no output)"), is_error


def _relay_json_tool(method, path, body_obj, timeout, is_cmd=False):
    """Shared tool plumbing: relay call -> (text, is_error) for tools/call."""
    try:
        status, text = relay_request(method, path, body_obj, timeout)
    except FileNotFoundError:
        return ("bridge token file not found: %s" % BRIDGE_TOKEN_FILE, True)
    except Exception as exc:  # transport failure, empty token, timeout
        return ("relay request failed: %s" % exc, True)
    if status != 200:
        return ("relay returned HTTP %d: %s" % (status, text[:500]), True)
    try:
        res = json.loads(text)
    except ValueError:
        return ("relay returned a non-JSON response: %s" % text[:500], True)
    if not isinstance(res, dict):
        return (text, False)
    if is_cmd:
        return _format_cmd_result(res)
    return (json.dumps(res, ensure_ascii=False), res.get("ok") is False)


def _clamp_timeout(value, default=120):
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(600, timeout))


def call_tool(name, args):
    """Execute one tool. Returns (text, is_error), or None for unknown tool.

    The target PC is fixed by configuration; any caller-supplied routing
    fields in args are ignored by design.
    """
    if not isinstance(args, dict):
        args = {}
    if name == "status":
        return _relay_json_tool("GET", "/muse-api/status", None, 30)
    return None


# ---------------------------------------------------------------------------
# MCP JSON-RPC (Streamable HTTP, stateless)
# ---------------------------------------------------------------------------

def _rpc_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _rpc_result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def handle_rpc(request):
    """Handle one JSON-RPC request object.

    Returns a response dict, or None when the message is a notification
    (no response is sent for notifications).
    """
    if not isinstance(request, dict) or "method" not in request:
        return _rpc_error(request.get("id") if isinstance(request, dict) else None,
                          -32600, "invalid request")
    method = request.get("method")
    req_id = request.get("id")
    params = request.get("params") or {}

    if req_id is None:
        # Notification (e.g. notifications/initialized): accept silently.
        return None

    if method == "initialize":
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        version = requested if requested in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
        return _rpc_result(req_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "ping":
        return _rpc_result(req_id, {})
    if method == "tools/list":
        return _rpc_result(req_id, {"tools": TOOLS})
    if method == "tools/call":
        if not isinstance(params, dict):
            return _rpc_error(req_id, -32602, "invalid params")
        name = params.get("name")
        outcome = call_tool(name, params.get("arguments") or {})
        if outcome is None:
            return _rpc_error(req_id, -32602, "unknown tool: %s" % name)
        text, is_error = outcome
        return _rpc_result(req_id, {
            "content": [{"type": "text", "text": text}],
            "isError": bool(is_error),
        })
    return _rpc_error(req_id, -32601, "method not found: %s" % method)


# ---------------------------------------------------------------------------
# OAuth 2.0 authorization server
# ---------------------------------------------------------------------------

def oauth_error(status, error, description):
    return status, {"error": error, "error_description": description}


def validate_redirect_uri(uri):
    """https anywhere, http only for loopback (RFC 7591 + OAuth practice)."""
    if not isinstance(uri, str) or not uri or len(uri) > 2048:
        return False
    if "#" in uri:  # fragments are not allowed in redirect URIs
        return False
    url = urlparse(uri)
    if url.scheme == "https":
        return bool(url.hostname)
    if url.scheme == "http":
        return url.hostname in ("127.0.0.1", "localhost", "::1")
    return False


def get_client(client_id):
    if not isinstance(client_id, str):
        return None
    with STATE.lock:
        client = STATE.data["clients"].get(client_id)
        return dict(client) if client else None


def resolve_redirect_uri(client, redirect_uri):
    """Exact-match validation; returns the URI to use or None."""
    registered = client.get("redirect_uris") or []
    if redirect_uri is None:
        if len(registered) == 1:
            return registered[0]
        return None
    if redirect_uri in registered:
        return redirect_uri
    return None


def authenticate_client(form, basic_creds=None):
    """Resolve + authenticate the token-endpoint client.

    Returns (client, None) or (None, (status, error_dict)). Public clients
    (registered with token_endpoint_auth_method "none") authenticate by
    client_id alone and rely on PKCE; confidential clients must present
    their client_secret (client_secret_post, or Basic for interop).
    basic_creds is an optional (client_id, client_secret) tuple parsed
    from an HTTP Basic Authorization header.
    """
    client_id = form.get("client_id")
    client_secret = form.get("client_secret")
    # HTTP Basic is accepted as an equivalent of client_secret_post.
    if client_id is None and basic_creds:
        client_id, client_secret = basic_creds
    client = get_client(client_id)
    if client is None:
        return None, oauth_error(400, "invalid_client", "unknown client_id")
    if client.get("secret_hash"):
        if not client_secret or not hmac.compare_digest(
                sha256_hex(client_secret), client["secret_hash"]):
            return None, oauth_error(401, "invalid_client", "client authentication failed")
    return client, None


def issue_token_pair(client_id, scope):
    access = secrets.token_urlsafe(32)    # 256-bit opaque token
    refresh = secrets.token_urlsafe(32)
    now = int(time.time())
    with STATE.lock:
        STATE.data["access_tokens"][sha256_hex(access)] = {
            "client_id": client_id, "scope": scope, "expires": now + ACCESS_TOKEN_TTL,
        }
        STATE.data["refresh_tokens"][sha256_hex(refresh)] = {
            "client_id": client_id, "scope": scope, "expires": now + REFRESH_TOKEN_TTL,
        }
        STATE.save()
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL,
        "refresh_token": refresh,
        "scope": scope,
    }


# --- /authorize approval form ------------------------------------------------

_FORM_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:34rem;margin:3rem auto;"
    "padding:0 1rem;line-height:1.5}label{display:block;margin:1rem 0 .3rem}"
    "input[type=password]{width:100%;padding:.5rem;font-size:1rem}"
    "button{margin-top:1.2rem;padding:.6rem 1.4rem;font-size:1rem}"
    ".err{color:#b00020;font-weight:600}.warn{background:#fff4e5;"
    "border:1px solid #e0a800;padding:.6rem .9rem;border-radius:.4rem}"
)


def render_authorize_form(params, error=None):
    hidden = "".join(
        '<input type="hidden" name="%s" value="%s">' % (html.escape(k, quote=True),
                                                        html.escape(v, quote=True))
        for k, v in params.items()
    )
    client_label = params.get("_client_name") or params.get("client_id", "")
    error_html = '<p class="err">%s</p>' % html.escape(error) if error else ""
    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Authorize Muse Bridge access</title>
<style>%s</style></head><body>
<h1>Authorize Muse Bridge access</h1>
<p class="warn">This authorizes the client <strong>%s</strong> (a Muse
connector) to <strong>run commands and read/write files on %s</strong>
through your Muse Bridge. Only approve requests you started yourself.</p>
%s
<form method="post" action="/authorize">
%s
<label for="owner_secret">Owner approval secret</label>
<input type="password" id="owner_secret" name="owner_secret"
       autocomplete="current-password" required>
<button type="submit">Approve access</button>
</form>
</body></html>""" % (
        _FORM_STYLE,
        html.escape(client_label),
        html.escape(TARGET_PC),
        error_html,
        hidden,
    )


def render_error_page(title, message):
    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>%s</title>
<style>%s</style></head><body><h1>%s</h1><p>%s</p></body></html>""" % (
        html.escape(title), _FORM_STYLE, html.escape(title), html.escape(message))


def validate_authorize_params(params):
    """Validate an authorize request (GET query or POST form fields).

    Returns (client, redirect_uri, cleaned_params) or (None, None, error).
    Errors are rendered as pages, never redirected to unvalidated URIs.
    """
    if params.get("response_type") != "code":
        return None, None, "unsupported response_type (only 'code' is supported)"
    client = get_client(params.get("client_id"))
    if client is None:
        return None, None, "unknown client_id"
    redirect_uri = resolve_redirect_uri(client, params.get("redirect_uri"))
    if redirect_uri is None:
        return None, None, "redirect_uri does not match a registered URI"
    challenge = params.get("code_challenge")
    if not challenge or params.get("code_challenge_method") != "S256":
        return None, None, "PKCE with code_challenge_method=S256 is required"
    cleaned = {
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": params.get("scope") or SCOPE,
    }
    if params.get("state"):
        cleaned["state"] = params["state"]
    if client.get("client_name"):
        cleaned["_client_name"] = client["client_name"]
    return client, redirect_uri, cleaned


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

def _first(mapping, key, default=None):
    values = mapping.get(key)
    if not values:
        return default
    return values[0]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "muse-mcp/1.0"

    # -- plumbing -----------------------------------------------------------

    def log_message(self, fmt, *args):  # silence the default access log
        pass

    def _send(self, status, body, content_type="application/json", extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)
        path = urlparse(self.path).path
        log("%s %s -> %d" % (self.command, path, status))

    def _send_json(self, status, obj, extra_headers=None):
        self._send(status, json.dumps(obj), "application/json", extra_headers)

    def _read_body(self, cap):
        """Read the request body with a size cap; None means 'too large'."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > cap:
            self.close_connection = True
            return None
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _source(self):
        # nginx overwrites X-Forwarded-For with the real client address;
        # fall back to the socket peer (loopback in production).
        forwarded = self.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0]

    # -- routing ------------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send_json(200, {"service": "muse-mcp", "status": "ok"})
        elif path == "/.well-known/oauth-authorization-server":
            self._send_json(200, {
                "issuer": ISSUER,
                "authorization_endpoint": ISSUER + "/authorize",
                "token_endpoint": ISSUER + "/token",
                "registration_endpoint": ISSUER + "/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
                "scopes_supported": [SCOPE],
            })
        elif path == "/.well-known/oauth-protected-resource":
            self._send_json(200, {
                "resource": ISSUER + "/mcp",
                "authorization_servers": [ISSUER],
                "bearer_methods_supported": ["header"],
                "scopes_supported": [SCOPE],
            })
        elif path == "/authorize":
            self._authorize_get()
        elif path == "/mcp":
            self._send_json(405, {"error": "method_not_allowed",
                                  "error_description": "use POST for MCP"},
                            {"Allow": "POST"})
        else:
            self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/mcp":
            self._mcp_post()
        elif path == "/register":
            self._register_post()
        elif path == "/authorize":
            self._authorize_post()
        elif path == "/token":
            self._token_post()
        else:
            self._send_json(404, {"error": "not_found"})

    def do_HEAD(self):
        self.do_GET()

    # -- MCP ----------------------------------------------------------------

    def _bearer_valid(self):
        auth = self.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            return False
        token = auth[len("Bearer "):].strip()
        if not token:
            return False
        key = sha256_hex(token)
        now = time.time()
        with STATE.lock:
            entry = STATE.data["access_tokens"].get(key)
            if entry and entry.get("expires", 0) <= now:
                del STATE.data["access_tokens"][key]
                STATE.save()
                entry = None
        return entry is not None

    def _mcp_post(self):
        if not self._bearer_valid():
            challenge = ('Bearer error="invalid_token", '
                         'resource_metadata="%s/.well-known/oauth-protected-resource"' % ISSUER)
            self._send_json(401, {"error": "invalid_token",
                                  "error_description": "a valid OAuth access token is required"},
                            {"WWW-Authenticate": challenge})
            return
        body = self._read_body(MAX_BODY_MCP)
        if body is None:
            self._send_json(413, {"error": "request_too_large"})
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, _rpc_error(None, -32700, "parse error"))
            return
        if isinstance(payload, list):
            responses = [r for r in (handle_rpc(item) for item in payload) if r is not None]
            if not responses:
                self._send(202, b"")
                return
            self._send_json(200, responses)
            return
        if isinstance(payload, dict):
            tool_name = None
            if payload.get("method") == "tools/call":
                tool_name = (payload.get("params") or {}).get("name")
            response = handle_rpc(payload)
            if response is None:
                self._send(202, b"")
                return
            if tool_name:
                log("tools/call %s" % tool_name)
            self._send_json(200, response)
            return
        self._send_json(400, _rpc_error(None, -32600, "invalid request"))

    # -- dynamic client registration (RFC 7591) ------------------------------

    def _register_post(self):
        body = self._read_body(MAX_BODY_SMALL)
        if body is None:
            self._send_json(413, {"error": "request_too_large"})
            return
        try:
            meta = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid_client_metadata",
                                  "error_description": "request body must be JSON"})
            return
        if not isinstance(meta, dict):
            self._send_json(400, {"error": "invalid_client_metadata",
                                  "error_description": "request body must be a JSON object"})
            return
        redirect_uris = meta.get("redirect_uris")
        if (not isinstance(redirect_uris, list) or not redirect_uris
                or not all(validate_redirect_uri(u) for u in redirect_uris)):
            self._send_json(400, {
                "error": "invalid_redirect_uri",
                "error_description": "redirect_uris must be a non-empty array of "
                                     "https URIs (http allowed for loopback only)",
            })
            return
        auth_method = meta.get("token_endpoint_auth_method") or "none"
        if auth_method not in ("none", "client_secret_post"):
            self._send_json(400, {"error": "invalid_client_metadata",
                                  "error_description": "unsupported token_endpoint_auth_method"})
            return
        client_id = "mcp_" + secrets.token_hex(16)
        client = {
            "client_id": client_id,
            "redirect_uris": redirect_uris,
            "client_name": meta.get("client_name") if isinstance(meta.get("client_name"), str) else None,
            "token_endpoint_auth_method": auth_method,
            "grant_types": meta.get("grant_types") or ["authorization_code", "refresh_token"],
            "response_types": meta.get("response_types") or ["code"],
            "created": int(time.time()),
        }
        response = {
            "client_id": client_id,
            "client_id_issued_at": client["created"],
            "redirect_uris": redirect_uris,
            "grant_types": client["grant_types"],
            "response_types": client["response_types"],
            "token_endpoint_auth_method": auth_method,
        }
        if client["client_name"]:
            response["client_name"] = client["client_name"]
        if auth_method == "client_secret_post":
            client_secret = secrets.token_urlsafe(32)
            client["secret_hash"] = sha256_hex(client_secret)
            response["client_secret"] = client_secret  # returned once, never stored raw
        with STATE.lock:
            STATE.data["clients"][client_id] = client
            STATE.save()
        log("registered client (auth method %s)" % auth_method)
        self._send_json(201, response)

    # -- /authorize ------------------------------------------------------------

    def _authorize_get(self):
        query = parse_qs(urlparse(self.path).query)
        params = {k: _first(query, k) for k in
                  ("response_type", "client_id", "redirect_uri", "state",
                   "code_challenge", "code_challenge_method", "scope")}
        client, _uri, cleaned = validate_authorize_params(params)
        if client is None:
            self._send(400, render_error_page("Authorization error", cleaned),
                       "text/html; charset=utf-8")
            return
        form_params = {k: v for k, v in cleaned.items() if v is not None}
        self._send(200, render_authorize_form(form_params),
                   "text/html; charset=utf-8", {"Cache-Control": "no-store"})

    def _authorize_post(self):
        body = self._read_body(MAX_BODY_SMALL)
        if body is None:
            self._send_json(413, {"error": "request_too_large"})
            return
        form_multi = parse_qs(body.decode("utf-8", "replace"))
        keys = ("response_type", "client_id", "redirect_uri", "state",
                "code_challenge", "code_challenge_method", "scope")
        params = {k: _first(form_multi, k) for k in keys}
        client, redirect_uri, cleaned = validate_authorize_params(params)
        if client is None:
            self._send(400, render_error_page("Authorization error", cleaned),
                       "text/html; charset=utf-8")
            return
        form_params = {k: v for k, v in cleaned.items() if v is not None}
        source = self._source()
        if auth_source_locked(source):
            self._send(429, render_authorize_form(
                form_params, "Too many failed attempts; try again in a minute."),
                "text/html; charset=utf-8", {"Cache-Control": "no-store"})
            return
        with STATE.lock:
            owner_hash = STATE.data.get("owner_secret_hash")
        submitted = _first(form_multi, "owner_secret") or ""
        if not owner_hash:
            log("ERROR: /authorize attempted but owner_secret_hash is not provisioned")
            self._send(503, render_authorize_form(
                form_params, "This service is not provisioned yet (owner secret missing)."),
                "text/html; charset=utf-8", {"Cache-Control": "no-store"})
            return
        if not hmac.compare_digest(sha256_hex(submitted), owner_hash):
            auth_source_record_failure(source)
            log("authorize: owner secret rejected for client %s" % client["client_id"])
            self._send(403, render_authorize_form(
                form_params, "Incorrect owner approval secret."),
                "text/html; charset=utf-8", {"Cache-Control": "no-store"})
            return
        auth_source_record_success(source)
        code = secrets.token_urlsafe(32)  # 256-bit authorization code
        with STATE.lock:
            # Opportunistically drop expired codes.
            now = time.time()
            STATE.data["codes"] = {
                k: v for k, v in STATE.data["codes"].items() if v.get("expires", 0) > now
            }
            STATE.data["codes"][sha256_hex(code)] = {
                "client_id": client["client_id"],
                "redirect_uri": redirect_uri,
                "code_challenge": cleaned["code_challenge"],
                "scope": cleaned.get("scope") or SCOPE,
                "expires": int(now) + AUTH_CODE_TTL,
                "used": False,
            }
            STATE.save()
        query = {"code": code}
        if cleaned.get("state"):
            query["state"] = cleaned["state"]
        separator = "&" if "?" in redirect_uri else "?"
        location = redirect_uri + separator + urlencode(query)
        log("authorize: code issued for client %s" % client["client_id"])
        self._send(302, b"", "text/plain", {"Location": location, "Cache-Control": "no-store"})

    # -- /token ----------------------------------------------------------------

    def _token_post(self):
        body = self._read_body(MAX_BODY_SMALL)
        if body is None:
            self._send_json(413, {"error": "request_too_large"})
            return
        form_multi = parse_qs(body.decode("utf-8", "replace"))
        form = {k: _first(form_multi, k) for k in form_multi}
        # Accept HTTP Basic as an alias of client_secret_post.
        basic_creds = None
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[len("Basic "):]).decode("utf-8")
                basic_id, _, basic_secret = decoded.partition(":")
                basic_creds = (basic_id, basic_secret)
            except (ValueError, UnicodeDecodeError):
                pass
        client, err = authenticate_client(form, basic_creds)
        if err is not None:
            status, payload = err
            self._send_json(status, payload, {"Cache-Control": "no-store"})
            return
        grant_type = form.get("grant_type")
        if grant_type == "authorization_code":
            self._token_exchange_code(client, form)
        elif grant_type == "refresh_token":
            self._token_refresh(client, form)
        else:
            status, payload = oauth_error(400, "unsupported_grant_type",
                                          "grant_type must be authorization_code or refresh_token")
            self._send_json(status, payload, {"Cache-Control": "no-store"})

    def _token_exchange_code(self, client, form):
        def fail(error, description, status=400):
            s, payload = oauth_error(status, error, description)
            self._send_json(s, payload, {"Cache-Control": "no-store"})

        code = form.get("code")
        verifier = form.get("code_verifier")
        if not code or not verifier:
            fail("invalid_request", "code and code_verifier are required")
            return
        key = sha256_hex(code)
        with STATE.lock:
            entry = STATE.data["codes"].get(key)
            entry = dict(entry) if entry else None
        if entry is None:
            fail("invalid_grant", "unknown authorization code")
            return
        if entry.get("used"):
            fail("invalid_grant", "authorization code has already been used")
            return
        if entry.get("expires", 0) <= time.time():
            fail("invalid_grant", "authorization code has expired")
            return
        if entry.get("client_id") != client["client_id"]:
            fail("invalid_grant", "authorization code was issued to a different client")
            return
        if form.get("redirect_uri") != entry.get("redirect_uri"):
            fail("invalid_grant", "redirect_uri does not match the authorization request")
            return
        if not hmac.compare_digest(pkce_challenge(verifier), entry.get("code_challenge", "")):
            fail("invalid_grant", "PKCE code_verifier does not match code_challenge")
            return
        # Single use: burn the code only now that every check has passed,
        # so a mistyped verifier does not destroy a legitimate code.
        with STATE.lock:
            current = STATE.data["codes"].get(key)
            if current is None or current.get("used"):
                fail("invalid_grant", "authorization code has already been used")
                return
            current["used"] = True
            STATE.save()
        tokens = issue_token_pair(client["client_id"], entry.get("scope") or SCOPE)
        log("token: issued access token for client %s" % client["client_id"])
        self._send_json(200, tokens, {"Cache-Control": "no-store", "Pragma": "no-cache"})

    def _token_refresh(self, client, form):
        def fail(error, description, status=400):
            s, payload = oauth_error(status, error, description)
            self._send_json(s, payload, {"Cache-Control": "no-store"})

        presented = form.get("refresh_token")
        if not presented:
            fail("invalid_request", "refresh_token is required")
            return
        key = sha256_hex(presented)
        with STATE.lock:
            entry = STATE.data["refresh_tokens"].get(key)
            if entry is None:
                fail("invalid_grant", "unknown refresh token")
                return
            if entry.get("expires", 0) <= time.time():
                del STATE.data["refresh_tokens"][key]
                STATE.save()
                fail("invalid_grant", "refresh token has expired")
                return
            if entry.get("client_id") != client["client_id"]:
                fail("invalid_grant", "refresh token was issued to a different client")
                return
            scope = entry.get("scope") or SCOPE
            # Rotation: the presented refresh token is revoked on use.
            del STATE.data["refresh_tokens"][key]
            STATE.save()
        tokens = issue_token_pair(client["client_id"], scope)
        log("token: rotated refresh token for client %s" % client["client_id"])
        self._send_json(200, tokens, {"Cache-Control": "no-store", "Pragma": "no-cache"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    host, _, port = LISTEN.rpartition(":")
    host = host or "127.0.0.1"
    server = ThreadingHTTPServer((host, int(port)), Handler)
    server.daemon_threads = True
    if not STATE.data.get("owner_secret_hash"):
        log("WARNING: owner_secret_hash not provisioned in %s; "
            "/authorize will fail closed until it is set" % STATE_PATH)
    log("muse-mcp %s listening on %s (issuer %s, relay %s, target %s)"
        % (SERVER_VERSION, LISTEN, ISSUER, RELAY_BASE, TARGET_PC))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
