#!/usr/bin/env python3
"""Muse Bridge (Scheme B) connector client.

Speaks MCP Streamable HTTP to the owner's own MCP service at
https://${VPS_SNI}/mcp (VPS ${VPS_IP}, private CA pinned),
through this VM's egress proxy with an HTTP CONNECT tunnel.

Authentication is OAuth 2.0 authorization-code + PKCE against the same
service, self-managed locally: the owner approval secret lives only in
~/.config/muse-mcp-client/owner-secret (mode 600, generated on this VM;
the VPS stores only its SHA-256). The client registers itself once
(dynamic registration), runs the approval flow programmatically with
that secret, and caches the token pair in tokens.json (mode 600),
refreshing with rotation as needed. No credential is ever printed or
logged.

Usage:
  client.py list-tools
  client.py call-tool --name run_command --arguments-file args.json
"""
import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time

# The network modules (ssl, http.client, urllib.parse, base64, hashlib,
# secrets) cost ~65 ms of interpreter startup and are only needed on the
# direct path / inside the daemon. The per-call shim path stays lean by
# loading them lazily, on first actual use.
_net_loaded = False


def _load_net_modules():
    global _net_loaded, base64, hashlib, http, secrets, ssl, urllib
    if _net_loaded:
        return
    import base64
    import hashlib
    import http.client
    import secrets
    import ssl
    import urllib.parse
    _net_loaded = True

HOST = "${VPS_SNI}"
PORT = 443
VPS_IP = "${VPS_IP}"
ISSUER = f"https://{HOST}"
MCP_URL_PATH = "/mcp"
CA_PATH = os.path.expanduser("~/.local/share/muse-bridge/vps-root-ca.pem")
STATE_DIR = os.path.expanduser("~/.config/muse-mcp-client")
OWNER_SECRET_PATH = os.path.join(STATE_DIR, "owner-secret")
CLIENT_PATH = os.path.join(STATE_DIR, "client.json")
TOKENS_PATH = os.path.join(STATE_DIR, "tokens.json")
REDIRECT_URI = "http://127.0.0.1:8765/callback"
ALLOWED_TOOLS = {
    "status",
}

# ---------------------------------------------------------------------------
# Session-attached PC tools (served locally, NOT by the VPS service)
#
# Per the user's design (their codex-pc-rpc-v1 push channel): a tool call
# is executed by the live Codex session that owns the run, through that
# session's own approvals, and the result returns synchronously. The
# exchange client is the user's installed skill, loaded unchanged; the
# run's relay credential lives in the skill's manifest file on this VM
# and is read by that code itself -- it never transits this client, the
# VPS service, or the chat. Runs are created by the PC service per Codex
# turn and are always supplied by the user; never invent one. A failed
# or ambiguous call is reported, never replayed. These tools bypass the
# daemon fast path; client.py is their only entry point.
# ---------------------------------------------------------------------------

LOCAL_TOOLS = {"pc_tools", "pc_call"}
PC_SKILL_ROOT = os.path.expanduser(os.environ.get(
    "MUSE_PC_SKILL_ROOT",
    "~/workspace/custom-skills/codex_push_persistent_20261021_v2"))


def _load_pc_modules():
    import importlib.util
    scripts = os.path.join(PC_SKILL_ROOT, "scripts")
    loaded = {}
    for mod_name in ("mailbox", "persistent", "pc"):
        if mod_name in loaded:
            continue
        spec = importlib.util.spec_from_file_location(
            mod_name, os.path.join(scripts, mod_name + ".py"))
        module = importlib.util.module_from_spec(spec)
        # Register before exec so the modules' own cross-imports
        # (`from mailbox import ...`, `from persistent import exchange`)
        # resolve to these instances.
        import sys as _sys
        _sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        loaded[mod_name] = module
    return loaded["mailbox"], loaded["pc"]


def _pc_result(payload, is_error=False):
    return {"content": [{"type": "text",
                          "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": is_error}


def _local_pc_call(name, arguments):
    import re
    from pathlib import Path
    root = Path(PC_SKILL_ROOT)
    run = arguments.get("run")
    if not isinstance(run, str) or not re.fullmatch(r"[0-9a-f]{32}", run):
        return _pc_result(
            {"ok": False, "error": "a valid 'run' id (32 hex chars) is "
             "required; runs are supplied by the user, never invented"},
            True)
    try:
        mailbox, pc_client = _load_pc_modules()
        manifest = mailbox.read_json(root / "runs" / run / "manifest.json")
        if not isinstance(manifest, dict) or manifest.get("run") != run:
            raise ValueError("invalid manifest")
        if name == "pc_tools":
            tools = mailbox.manifest_tools(root, manifest)
            tool_name = arguments.get("name")
            if tool_name:
                if tool_name not in tools:
                    raise ValueError("unknown tool")
                return _pc_result({"ok": True, "run": run,
                                   "tool": tools[tool_name]})
            return _pc_result({
                "ok": True, "run": run,
                "expires_at": manifest.get("expires_at"),
                "live": (manifest.get("expires_at") or 0) > time.time(),
                "tools": [{"name": t["name"], "type": t["type"]}
                          for t in tools.values()]})
        # pc_call
        tool_name = arguments.get("name")
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("a tool 'name' is required")
        has_args = "arguments" in arguments
        has_input = "input" in arguments
        if has_args == has_input:
            raise ValueError(
                "give exactly one of 'arguments' (function tools) or "
                "'input' (custom/freeform tools)")
        request = {"name": tool_name}
        request["arguments" if has_args else "input"] = \
            arguments["arguments" if has_args else "input"]
        timeout = arguments.get("timeout", 600)
        result = pc_client.call(root, run, request, timeout=float(timeout))
        return _pc_result({"ok": True, "run": run, "result": result})
    except Exception as exc:
        return _pc_result(
            {"ok": False, "run": run,
             "error": "%s: %s" % (type(exc).__name__, exc)}, True)
PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]


def _save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


class BridgeHttp:
    """One keep-alive HTTPS connection to the bridge host via the egress
    proxy CONNECT tunnel.

    The VM->VPS path intermittently black-holes brand-new flows, so
    connection establishment (TCP + CONNECT + TLS) is retried up to 3
    times with a short 25 s timeout. Once a request has been sent it is
    NEVER replayed: a lost response raises instead of risking a double
    command execution on the PC (the OAuth dance self-heals by issuing
    a fresh code on the next run; token codes burn only on success).
    """

    CONNECT_TIMEOUT = 25

    def __init__(self):
        _load_net_modules()
        self.conn = None

    def _connect(self):
        proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
        if not proxy:
            raise RuntimeError("https_proxy is not set; the bridge is only "
                               "reachable through this VM's egress proxy")
        u = urllib.parse.urlparse(proxy)
        ctx = ssl.create_default_context(cafile=CA_PATH)
        proxy_auth = None
        if u.username:
            proxy_auth = base64.b64encode(
                f"{urllib.parse.unquote(u.username)}:"
                f"{urllib.parse.unquote(u.password or '')}".encode()).decode()
        last_exc = None
        for attempt in range(3):
            raw = None
            try:
                # Tunnel to the VPS by IP, then wrap TLS with the SNI name.
                # This mirrors mused's proven pattern on this VM: CONNECT
                # to the .local name goes through the proxy's own name
                # resolution, whose route black-holes TLS handshakes,
                # while CONNECT to the IP with an explicit SNI works.
                raw = socket.create_connection(
                    (u.hostname, u.port or 3128), timeout=self.CONNECT_TIMEOUT)
                raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                req = (f"CONNECT {VPS_IP}:{PORT} HTTP/1.1\r\n"
                       f"Host: {VPS_IP}:{PORT}\r\n")
                if proxy_auth:
                    req += f"Proxy-Authorization: Basic {proxy_auth}\r\n"
                req += "\r\n"
                raw.sendall(req.encode("ascii"))
                resp = b""
                while b"\r\n\r\n" not in resp:
                    chunk = raw.recv(4096)
                    if not chunk:
                        raise ConnectionError("proxy closed during CONNECT")
                    resp += chunk
                    if len(resp) > 65536:
                        raise ConnectionError("proxy CONNECT response too large")
                status_line = resp.split(b"\r\n", 1)[0].decode("latin1")
                if " 200 " not in status_line and not status_line.endswith(" 200"):
                    raise ConnectionError(f"CONNECT rejected: {status_line}")
                wrapped = ctx.wrap_socket(raw, server_hostname=HOST)
                conn = http.client.HTTPSConnection(
                    HOST, PORT, context=ctx, timeout=130)
                conn.sock = wrapped  # pre-connected; http.client skips connect()
                self.conn = conn
                return
            except (http.client.HTTPException, OSError, ConnectionError) as exc:
                last_exc = exc
                if raw is not None:
                    try:
                        raw.close()
                    except Exception:
                        pass
                if attempt < 2:
                    time.sleep(2)
        raise RuntimeError(f"bridge connect failed after 3 attempts: {last_exc}")

    def request(self, method, path, body=None, headers=None, timeout=None):
        hdrs = {"User-Agent": "muse-custom-connector/1.0"}
        if headers:
            hdrs.update(headers)
        data = None
        if body is not None:
            if isinstance(body, (dict, list)):
                data = json.dumps(body).encode()
                hdrs.setdefault("Content-Type", "application/json")
            elif isinstance(body, str):
                data = body.encode()
            else:
                data = body
        if self.conn is None:
            self._connect()
        try:
            if self.conn.sock is not None:
                self.conn.sock.settimeout(timeout if timeout is not None else 130)
            self.conn.request(method, path, body=data, headers=hdrs)
            resp = self.conn.getresponse()
            raw = resp.read()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, raw
        except (http.client.HTTPException, OSError) as exc:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None
            raise RuntimeError(
                f"bridge request failed after send (not replayed): {exc}") from exc


class OAuth:
    def __init__(self, http):
        _load_net_modules()
        self.http = http

    def _register(self):
        status, _, raw = self.http.request("POST", "/register", {
            "client_name": "muse-bridge connector (Muse VM)",
            "redirect_uris": [REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        })
        if status != 201:
            raise RuntimeError(f"client registration failed: HTTP {status}")
        client = json.loads(raw)
        _save_json(CLIENT_PATH, {"client_id": client["client_id"]})
        return client["client_id"]

    def client_id(self):
        saved = _load_json(CLIENT_PATH)
        if saved and saved.get("client_id"):
            return saved["client_id"]
        return self._register()

    def _store_tokens(self, payload):
        _save_json(TOKENS_PATH, {
            "access_token": payload["access_token"],
            "refresh_token": payload.get("refresh_token"),
            "expires_at": time.time() + float(payload.get("expires_in", 3600)),
        })

    def _token_request(self, form):
        body = urllib.parse.urlencode(form)
        status, _, raw = self.http.request(
            "POST", "/token", body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        if status != 200:
            raise RuntimeError(f"token endpoint failed: HTTP {status}: "
                               f"{raw[:200].decode('utf-8', 'replace')}")
        payload = json.loads(raw)
        self._store_tokens(payload)
        return payload["access_token"]

    def _full_dance(self):
        owner_secret = open(OWNER_SECRET_PATH).read().strip()
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        cid = self.client_id()
        form = {
            "response_type": "code", "client_id": cid,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge, "code_challenge_method": "S256",
            "state": "muse-connector", "owner_secret": owner_secret,
        }
        status, hdrs, _ = self.http.request(
            "POST", "/authorize", urllib.parse.urlencode(form),
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        if status != 302:
            raise RuntimeError(f"authorize failed: HTTP {status} "
                               "(owner secret rejected or client invalid)")
        location = hdrs.get("location", "")
        code = urllib.parse.parse_qs(
            urllib.parse.urlparse(location).query).get("code", [None])[0]
        if not code:
            raise RuntimeError("authorize returned no code")
        return self._token_request({
            "grant_type": "authorization_code", "client_id": cid,
            "code": code, "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        })

    def access_token(self):
        tokens = _load_json(TOKENS_PATH)
        if tokens and tokens.get("access_token") and \
                tokens.get("expires_at", 0) > time.time() + 60:
            return tokens["access_token"]
        if tokens and tokens.get("refresh_token"):
            try:
                return self._token_request({
                    "grant_type": "refresh_token",
                    "client_id": self.client_id(),
                    "refresh_token": tokens["refresh_token"],
                })
            except RuntimeError:
                pass  # fall through to a fresh authorization
        return self._full_dance()


class Mcp:
    def __init__(self):
        self.http = BridgeHttp()
        self.oauth = OAuth(self.http)
        self._seq = 0
        self._initialized = False

    def _rpc(self, method, params=None, timeout=None):
        self._seq += 1
        body = {"jsonrpc": "2.0", "id": self._seq, "method": method}
        if params is not None:
            body["params"] = params
        status, _, raw = self.http.request(
            "POST", MCP_URL_PATH, body,
            headers={"Authorization": "Bearer " + self.oauth.access_token()},
            timeout=timeout)
        if status == 401:
            # Token rejected server-side (revoked/expired): drop the
            # cached pair and re-authorize once.
            try:
                os.unlink(TOKENS_PATH)
            except OSError:
                pass
            status, _, raw = self.http.request(
                "POST", MCP_URL_PATH, body,
                headers={"Authorization": "Bearer " + self.oauth.access_token()},
                timeout=timeout)
        if status != 200:
            raise RuntimeError(f"MCP HTTP {status}: {raw[:300].decode('utf-8', 'replace')}")
        return json.loads(raw)

    def initialize(self):
        if self._initialized:
            return
        result = None
        for proto in PROTOCOLS:
            res = self._rpc("initialize", {
                "protocolVersion": proto, "capabilities": {},
                "clientInfo": {"name": "muse-bridge-connector", "version": "1.0.0"}})
            if "result" in res:
                result = res["result"]
                break
        if result is None:
            raise RuntimeError("MCP initialize failed for all protocol versions")
        self._seq += 1
        self.http.request(
            "POST", MCP_URL_PATH,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={"Authorization": "Bearer " + self.oauth.access_token()})
        self._initialized = True

    def list_tools(self):
        self.initialize()
        res = self._rpc("tools/list")
        return res["result"]["tools"]

    def call_tool(self, name, arguments):
        if name not in ALLOWED_TOOLS:
            raise RuntimeError(f"tool not allowed by this connector: {name}")
        self.initialize()
        timeout = None
        if name.startswith("codex_task_"):
            # The service answers within its relay timeout (~75 s);
            # the Codex run itself is asynchronous.
            timeout = 180
        res = self._rpc("tools/call", {"name": name, "arguments": arguments},
                        timeout=timeout)
        if "error" in res:
            raise RuntimeError(f"MCP error: {json.dumps(res['error'])[:300]}")
        return res["result"]


# ---------------------------------------------------------------------------
# Warm-connection daemon fast path.
#
# A resident daemon (daemon.py, same directory) holds a small adaptive
# pool of initialized MCP connections so routine calls skip the fresh
# proxy CONNECT + TLS handshake this VM's path handles so badly. This
# client is then just a shim: it asks the daemon, starting it on demand
# (the musepc->mused self-heal pattern). Fallback rules are strict:
#   - daemon unreachable and cannot be started  -> direct mode below
#   - daemon replies "not executed"             -> direct mode below
#   - daemon handled the call, or the outcome is ambiguous
#                                               -> NEVER fall back
#     (re-issuing could execute the tool twice on the PC)
# Set MUSE_MCP_NO_DAEMON=1 to skip the daemon entirely.
# ---------------------------------------------------------------------------
_DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))
DAEMON_PY = os.path.join(_DAEMON_DIR, "daemon.py")
DAEMON_SOCK = os.path.join(STATE_DIR, "daemon.sock")
DAEMON_PID = os.path.join(STATE_DIR, "daemon.pid")
DAEMON_LOG = os.path.join(STATE_DIR, "daemon.log")


class _DaemonDown(Exception):
    """The daemon could not be reached at all (nothing was sent)."""


class _Ambiguous(Exception):
    """The daemon took the request but the outcome is unknown."""


def _daemon_request(req, timeout):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(DAEMON_SOCK)
    except OSError as exc:
        sock.close()
        raise _DaemonDown(str(exc)) from exc
    try:
        sock.settimeout(timeout)
        sock.sendall(json.dumps(req).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                raise _Ambiguous(
                    "daemon closed mid-call; the tool call may have executed")
            buf += chunk
            if len(buf) > 16 * 1024 * 1024:
                raise _Ambiguous("daemon response too large")
        return json.loads(buf.split(b"\n", 1)[0])
    except _Ambiguous:
        raise
    except (OSError, ValueError) as exc:
        raise _Ambiguous(f"daemon call outcome unknown: {exc}") from exc
    finally:
        sock.close()


def _ensure_daemon():
    try:
        _daemon_request({"op": "ping"}, timeout=3)
        return True
    except _Ambiguous:
        return True  # something is listening; the real call will surface it
    except _DaemonDown:
        pass
    try:
        log_fh = open(DAEMON_LOG, "ab")
        subprocess.Popen(
            [sys.executable, DAEMON_PY], stdin=subprocess.DEVNULL,
            stdout=log_fh, stderr=log_fh, start_new_session=True,
            close_fds=True)
    except OSError:
        return False
    deadline = time.time() + 25
    while time.time() < deadline:
        try:
            _daemon_request({"op": "ping"}, timeout=3)
            return True
        except _Ambiguous:
            return True
        except _DaemonDown:
            time.sleep(0.5)
    return False


def _emit_call_result(result):
    for item in result.get("content", []):
        if item.get("type") == "text":
            print(item["text"])
    return 1 if result.get("isError") else 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="muse-bridge-client")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-tools")
    call = sub.add_parser("call-tool")
    call.add_argument("--name", required=True)
    call.add_argument("--arguments-file", required=True)
    sub.add_parser("daemon-status")
    sub.add_parser("daemon-stop")
    args = parser.parse_args(argv)

    if args.command == "daemon-status":
        try:
            resp = _daemon_request({"op": "stats"}, timeout=5)
            print(json.dumps({"daemon": "running", **resp.get("result", {})}))
        except _DaemonDown:
            print(json.dumps({"daemon": "not running"}))
        return 0
    if args.command == "daemon-stop":
        try:
            pid = int(open(DAEMON_PID).read().strip())
            os.kill(pid, signal.SIGTERM)
            print(f"daemon (pid {pid}) signalled to stop")
        except (OSError, ValueError):
            print("daemon is not running")
        return 0

    arguments = None
    if args.command == "call-tool":
        with open(args.arguments_file, "r", encoding="utf-8") as fh:
            arguments = json.load(fh)
        if args.name in LOCAL_TOOLS:
            return _emit_call_result(_local_pc_call(args.name, arguments))
        req = {"op": "call_tool", "name": args.name, "arguments": arguments}
        timeout = (min(int(arguments.get("timeout", 120)), 600) + 800
                   if args.name == "run_command" else 900)
    else:
        req = {"op": "list_tools"}
        timeout = 180

    if os.environ.get("MUSE_MCP_NO_DAEMON") != "1" and _ensure_daemon():
        try:
            resp = _daemon_request(req, timeout=timeout)
        except _Ambiguous as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if resp.get("ok"):
            if args.command == "list-tools":
                print(json.dumps(resp["result"], indent=1,
                                 ensure_ascii=False))
                return 0
            return _emit_call_result(resp["result"])
        if resp.get("executed"):
            print(f"error: {resp.get('error')}", file=sys.stderr)
            return 1
        # Daemon explicitly did not execute (e.g. pool exhausted):
        # fall through to a direct call, exactly once.

    client = Mcp()
    if args.command == "list-tools":
        print(json.dumps(client.list_tools(), indent=1, ensure_ascii=False))
        return 0
    result = client.call_tool(args.name, arguments)
    return _emit_call_result(result)


if __name__ == "__main__":
    sys.exit(main())
