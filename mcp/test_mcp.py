#!/usr/bin/env python3
"""Integration test for muse-mcp.py (Scheme B MCP + OAuth service).

Stdlib only. Starts a stub relay on an ephemeral port, starts muse-mcp.py
as a subprocess against it with a temp state file, then walks the full
OAuth 2.0 + PKCE flow and the MCP tool surface with raw http.client.

Run:  python3 test_mcp.py
Exit code 0 when every check passes.
"""

import base64
import hashlib
import http.client
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE = os.path.join(HERE, "muse-mcp.py")

FAKE_BRIDGE_TOKEN = "muse_sec_" + "f" * 40          # obvious test fixture
OWNER_SECRET = "test-owner-secret-0123456789abcdef"  # obvious test fixture
EXPIRED_ACCESS_TOKEN = "expired-test-access-token"   # seeded pre-expired

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition)))
    print("%s %s%s" % ("PASS" if condition else "FAIL", name,
                       (" -- " + str(detail)) if (detail and not condition) else ""))


def sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def b64url(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ---------------------------------------------------------------------------
# Stub relay: emulates GET /muse-api/status, POST /muse-api/cmd, /muse-api/file
# ---------------------------------------------------------------------------

class StubRelay:
    def __init__(self):
        self.calls = []  # (method, path, headers dict, parsed body or None)
        outer = self

        class StubHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def _reply(self, obj):
                data = json.dumps(obj).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _record(self, body):
                outer.calls.append((self.command, self.path,
                                    dict(self.headers), body))

            def do_GET(self):
                self._record(None)
                if self.path == "/muse-api/status":
                    self._reply({
                        "ok": True, "version": "1.1.0",
                        "clients": [{"hostname": "${PC_NAME}", "platform": "win32",
                                     "arch": "x64", "client": "muse-bridge/1.2.12"}],
                        "online": ["${PC_NAME}"], "pending": 0, "cached_results": 0,
                    })
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError:
                    body = None
                self._record(body)
                if self.path == "/muse-api/cmd":
                    self._reply({
                        "type": "result", "id": (body or {}).get("id", "mb_test"),
                        "ok": True, "exitCode": 0,
                        "stdout": "stub-stdout\n", "stderr": "",
                    })
                elif self.path == "/muse-api/file":
                    op = (body or {}).get("op")
                    if op == "read":
                        payload = b"hello file"
                        self._reply({
                            "type": "file_result", "id": (body or {}).get("id", "mb_test"),
                            "ok": True,
                            "data": base64.b64encode(payload).decode("ascii"),
                            "bytes": len(payload),
                            "hash": sha256_hex("hello file")[:16],
                        })
                    elif op == "write":
                        self._reply({"type": "file_result", "ok": True, "bytes": 5,
                                     "hash": "0123456789abcdef"})
                    elif op == "edit":
                        self._reply({"type": "file_result", "ok": True, "replaced": 1,
                                     "hash": "0123456789abcdef"})
                    elif op == "list":
                        self._reply({"type": "file_result", "ok": True,
                                     "entries": [{"name": "a.txt"}, {"name": "b.txt"}],
                                     "total": 2})
                    else:
                        self._reply({"ok": False, "error": "unsupported op"})
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def last_call(self, path):
        for method, p, headers, body in reversed(self.calls):
            if p == path:
                return method, headers, body
        return None, None, None


# ---------------------------------------------------------------------------
# Minimal HTTP client (no redirect following)
# ---------------------------------------------------------------------------

def request(port, method, path, body=None, headers=None, form=False):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    hdrs = dict(headers or {})
    data = None
    if body is not None:
        if form:
            data = urlencode(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = body
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    raw = resp.read()
    out_headers = {k.lower(): v for k, v in resp.getheaders()}
    status = resp.status
    conn.close()
    return status, out_headers, raw


def request_json(port, method, path, body=None, headers=None, form=False):
    status, hdrs, raw = request(port, method, path, body, headers, form)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        parsed = None
    return status, hdrs, parsed, raw


def wait_ready(port, proc, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("muse-mcp exited early with code %s" % proc.returncode)
        try:
            status, _h, _b = request(port, "GET", "/.well-known/oauth-authorization-server")
            if status == 200:
                return
        except OSError:
            pass
        time.sleep(0.2)
    raise RuntimeError("muse-mcp did not become ready in %ss" % timeout)


# ---------------------------------------------------------------------------
# Test flow
# ---------------------------------------------------------------------------

def main():
    stub = StubRelay()
    stub.start()
    tmp = tempfile.mkdtemp(prefix="muse-mcp-test-")
    state_path = os.path.join(tmp, "state.json")
    token_path = os.path.join(tmp, "bridge-token")
    with open(token_path, "w", encoding="utf-8") as fh:
        fh.write(FAKE_BRIDGE_TOKEN + "\n")

    # Provision state: owner secret hash + one pre-expired access token.
    seeded = {
        "owner_secret_hash": sha256_hex(OWNER_SECRET),
        "clients": {},
        "codes": {},
        "access_tokens": {
            sha256_hex(EXPIRED_ACCESS_TOKEN): {
                "client_id": "mcp_seed", "scope": "mcp", "expires": int(time.time()) - 60,
            }
        },
        "refresh_tokens": {},
    }
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(seeded, fh)

    port = free_port()
    issuer = "http://127.0.0.1:%d" % port
    env = dict(os.environ)
    env.update({
        "MUSE_MCP_LISTEN": "127.0.0.1:%d" % port,
        "MUSE_MCP_ISSUER": issuer,
        "MUSE_MCP_STATE": state_path,
        "MUSE_MCP_BRIDGE_TOKEN_FILE": token_path,
        "MUSE_MCP_RELAY": "http://127.0.0.1:%d" % stub.port,
        "MUSE_MCP_TARGET": "${PC_NAME}",
    })
    proc = subprocess.Popen(
        [sys.executable, SERVICE], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    access_token = None
    try:
        wait_ready(port, proc)

        # -- discovery ------------------------------------------------------
        status, _h, meta, _r = request_json(port, "GET", "/.well-known/oauth-authorization-server")
        check("discovery: authorization server metadata shape",
              status == 200 and meta.get("issuer") == issuer
              and meta.get("authorization_endpoint") == issuer + "/authorize"
              and meta.get("token_endpoint") == issuer + "/token"
              and meta.get("registration_endpoint") == issuer + "/register"
              and "S256" in meta.get("code_challenge_methods_supported", [])
              and "authorization_code" in meta.get("grant_types_supported", [])
              and "refresh_token" in meta.get("grant_types_supported", []))
        status, _h, prm, _r = request_json(port, "GET", "/.well-known/oauth-protected-resource")
        check("discovery: protected resource metadata shape",
              status == 200 and prm.get("resource") == issuer + "/mcp"
              and prm.get("authorization_servers") == [issuer])

        # -- /mcp auth gate ---------------------------------------------------
        rpc_list = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        status, hdrs, _b = request(port, "POST", "/mcp", rpc_list)
        check("/mcp without token -> 401 + WWW-Authenticate resource_metadata",
              status == 401 and "resource_metadata" in hdrs.get("www-authenticate", "")
              and "/.well-known/oauth-protected-resource" in hdrs.get("www-authenticate", ""))
        status, _h, _b = request(port, "POST", "/mcp", rpc_list,
                                 {"Authorization": "Bearer totally-unknown-token"})
        check("/mcp with unknown access token -> 401", status == 401)
        status, _h, _b = request(port, "POST", "/mcp", rpc_list,
                                 {"Authorization": "Bearer " + EXPIRED_ACCESS_TOKEN})
        check("/mcp with expired access token -> 401", status == 401)

        # -- registration -----------------------------------------------------
        redirect_uri = "http://127.0.0.1:8765/callback"
        status, _h, reg, _r = request_json(port, "POST", "/register", {
            "client_name": "test-connector",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        })
        client_id = (reg or {}).get("client_id")
        check("register: public client created (201, no secret)",
              status == 201 and bool(client_id) and "client_secret" not in (reg or {}))

        # -- authorize ----------------------------------------------------------
        verifier = "test-verifier-" + secrets.token_hex(24)
        challenge = b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        auth_params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": "state-xyz",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "mcp",
        }
        status, hdrs, raw = request(port, "GET", "/authorize?" + urlencode(auth_params))
        form_html = raw.decode("utf-8", "replace")
        check("authorize GET: approval form rendered (mentions ${PC_NAME}, owner secret field)",
              status == 200 and "owner_secret" in form_html and "${PC_NAME}" in form_html)

        bad_params = dict(auth_params, redirect_uri="https://evil.example/callback")
        status, hdrs, _raw = request(port, "GET", "/authorize?" + urlencode(bad_params))
        check("authorize GET: unregistered redirect_uri -> 400, no redirect",
              status == 400 and "location" not in hdrs)

        form = dict(auth_params, owner_secret="definitely-wrong")
        status, hdrs, _raw = request(port, "POST", "/authorize", form, form=True)
        check("authorize POST: wrong owner secret -> 403, no code issued",
              status == 403 and "location" not in hdrs)

        form = dict(auth_params, owner_secret=OWNER_SECRET)
        status, hdrs, _raw = request(port, "POST", "/authorize", form, form=True)
        location = hdrs.get("location", "")
        loc_query = parse_qs(urlparse(location).query)
        code = (loc_query.get("code") or [None])[0]
        check("authorize POST: correct owner secret -> 302 with code + state",
              status == 302 and bool(code) and (loc_query.get("state") or [None])[0] == "state-xyz")

        # -- token exchange ------------------------------------------------------
        token_form = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": "wrong-verifier-" + "x" * 30,
        }
        status, _h, err, _r = request_json(port, "POST", "/token", token_form, form=True)
        check("token: wrong PKCE verifier -> 400 invalid_grant",
              status == 400 and (err or {}).get("error") == "invalid_grant")

        token_form["code_verifier"] = verifier
        status, _h, tokens, _r = request_json(port, "POST", "/token", token_form, form=True)
        access_token = (tokens or {}).get("access_token")
        refresh_token = (tokens or {}).get("refresh_token")
        check("token: correct verifier -> access + refresh tokens",
              status == 200 and bool(access_token) and bool(refresh_token)
              and (tokens or {}).get("token_type") == "Bearer")

        status, _h, err, _r = request_json(port, "POST", "/token", token_form, form=True)
        check("token: authorization code is single-use (reuse -> 400)",
              status == 400 and (err or {}).get("error") == "invalid_grant")

        auth = {"Authorization": "Bearer " + access_token}

        # -- MCP surface -----------------------------------------------------------
        status, _h, init, _r = request_json(port, "POST", "/mcp", {
            "jsonrpc": "2.0", "id": 10, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "0"}},
        }, auth)
        init_result = (init or {}).get("result") or {}
        check("mcp initialize: negotiates protocol + serverInfo",
              status == 200 and init_result.get("protocolVersion") == "2025-03-26"
              and (init_result.get("serverInfo") or {}).get("name") == "muse-bridge-rd925")

        status, _h, _raw = request(port, "POST", "/mcp",
                                   {"jsonrpc": "2.0", "method": "notifications/initialized"}, auth)
        check("mcp notifications/initialized -> 202", status == 202)

        status, _h, lst, _r = request_json(port, "POST", "/mcp", rpc_list, auth)
        names = sorted(t["name"] for t in ((lst or {}).get("result") or {}).get("tools", []))
        check("tools/list: exactly the 6 bridge tools",
              status == 200 and names == ["edit_file", "list_directory", "read_file",
                                          "run_command", "status", "write_file"],
              detail=names)

        status, _h, call, _r = request_json(port, "POST", "/mcp", {
            "jsonrpc": "2.0", "id": 20, "method": "tools/call",
            "params": {"name": "run_command",
                       "arguments": {"command": "echo hello", "timeout": 30}},
        }, auth)
        result = (call or {}).get("result") or {}
        text = "".join(c.get("text", "") for c in result.get("content", []))
        _m, stub_headers, stub_body = stub.last_call("/muse-api/cmd")
        check("tools/call run_command: canned stdout returned, not an error",
              status == 200 and "stub-stdout" in text and result.get("isError") is False)
        check("relay saw the bridge token, fixed target ${PC_NAME} and the command",
              stub_headers is not None
              and stub_headers.get("X-Bridge-Token") == FAKE_BRIDGE_TOKEN
              and (stub_body or {}).get("target") == "${PC_NAME}"
              and (stub_body or {}).get("command") == "echo hello")

        status, _h, call, _r = request_json(port, "POST", "/mcp", {
            "jsonrpc": "2.0", "id": 21, "method": "tools/call",
            "params": {"name": "run_command",
                       "arguments": {"command": "echo again", "target": "SOMEONE-ELSE"}},
        }, auth)
        _m, _sh, stub_body = stub.last_call("/muse-api/cmd")
        check("tools/call run_command: caller-supplied target cannot override ${PC_NAME}",
              status == 200 and (stub_body or {}).get("target") == "${PC_NAME}")

        status, _h, call, _r = request_json(port, "POST", "/mcp", {
            "jsonrpc": "2.0", "id": 22, "method": "tools/call",
            "params": {"name": "read_file",
                       "arguments": {"path": "D:\\docs\\note.txt", "offset": 2, "length": 5}},
        }, auth)
        result = (call or {}).get("result") or {}
        text = "".join(c.get("text", "") for c in result.get("content", []))
        _m, _sh, stub_body = stub.last_call("/muse-api/file")
        expected_data = base64.b64encode(b"hello file").decode("ascii")
        check("tools/call read_file: file_result passed through as JSON text",
              status == 200 and expected_data in text and result.get("isError") is False)
        check("read_file mapped to relay op 'read' with offset/limit slice fields",
              (stub_body or {}).get("op") == "read"
              and (stub_body or {}).get("offset") == 2
              and (stub_body or {}).get("limit") == 5)

        status, _h, call, _r = request_json(port, "POST", "/mcp", {
            "jsonrpc": "2.0", "id": 23, "method": "tools/call",
            "params": {"name": "edit_file",
                       "arguments": {"path": "D:\\docs\\note.txt",
                                     "old_text": "before", "new_text": "after"}},
        }, auth)
        _m, _sh, stub_body = stub.last_call("/muse-api/file")
        check("tools/call edit_file: maps to op 'edit' with old_string/new_string",
              status == 200 and (stub_body or {}).get("op") == "edit"
              and (stub_body or {}).get("old_string") == "before"
              and (stub_body or {}).get("new_string") == "after")

        status, _h, call, _r = request_json(port, "POST", "/mcp", {
            "jsonrpc": "2.0", "id": 24, "method": "tools/call",
            "params": {"name": "no_such_tool", "arguments": {}},
        }, auth)
        check("tools/call unknown tool -> JSON-RPC error",
              status == 200 and "error" in (call or {}))

        status, _h, _b = request(port, "GET", "/mcp", None, auth)
        check("GET /mcp -> 405", status == 405)

        # -- refresh rotation -------------------------------------------------------
        refresh_form = {"grant_type": "refresh_token", "client_id": client_id,
                        "refresh_token": refresh_token}
        status, _h, rotated, _r = request_json(port, "POST", "/token", refresh_form, form=True)
        new_access = (rotated or {}).get("access_token")
        new_refresh = (rotated or {}).get("refresh_token")
        check("refresh grant: rotates to a fresh token pair",
              status == 200 and bool(new_access) and bool(new_refresh)
              and new_refresh != refresh_token and new_access != access_token)
        access_token = new_access

        status, _h, err, _r = request_json(port, "POST", "/token", refresh_form, form=True)
        check("refresh grant: presented refresh token is revoked on use (reuse -> 400)",
              status == 400 and (err or {}).get("error") == "invalid_grant")

        # -- state persistence across restart -----------------------------------------
        proc.terminate()
        proc.wait(timeout=10)
        proc = subprocess.Popen(
            [sys.executable, SERVICE], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_ready(port, proc)
        status, _h, lst, _r = request_json(
            port, "POST", "/mcp", rpc_list,
            {"Authorization": "Bearer " + access_token})
        check("state persists across restart (access token still valid)",
              status == 200 and "result" in (lst or {}))
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        stub.stop()

    passed = sum(1 for _n, ok in RESULTS if ok)
    print("SUMMARY: %d/%d checks passed" % (passed, len(RESULTS)))
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
