#!/usr/bin/env python3
"""Simular Box steward for the VPS — on-demand MCP session keeper.

WHY THIS EXISTS: the Muse VM client cannot run here. It never holds the
raw API key — it inserts a `hsurr:*` surrogate that Muse's platform
(authd/Sentinel, VM-local) swaps for the real credential on egress. On the
VPS there is no surrogate service, so this client reads the raw key from a
local 0600 file instead:  /etc/box-steward/key  (placed there BY THE USER;
see install-box-steward.sh). The key is never printed or logged.

ON-DEMAND MODEL (as specified by the user):
  * Nothing runs and no connection exists while Box is unused.
  * The first `call` starts a small keeper process, which opens ONE MCP
    session to https://mcp.box.simular.ai/mcp and holds it warm.
  * Later calls reuse the warm session via a unix socket — no repeated
    initialize handshake, and several tool calls can be issued back to
    back inside one CLI invocation (see `call` with --batch-file).
  * The keeper exits by itself after IDLE_LIMIT seconds (default 3600 =
    60 minutes) with no requests. `stop` tears it down immediately.

Usage:
  boxsteward.py call --name box_status --arguments-file args.json
  boxsteward.py batch --batch-file calls.json   # [{name, arguments}, ...]
  boxsteward.py status                          # keeper state (local only)
  boxsteward.py stop

Stdlib only.
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ENDPOINT = "https://mcp.box.simular.ai/mcp"
KEY_FILE = os.environ.get("BOX_STEWARD_KEY_FILE", "/etc/box-steward/key")
STATE_DIR = os.environ.get("BOX_STEWARD_STATE", "/run/box-steward")
SOCK_PATH = os.path.join(STATE_DIR, "steward.sock")
PID_PATH = os.path.join(STATE_DIR, "steward.pid")
LOG_PATH = os.path.join(STATE_DIR, "steward.log")
IDLE_LIMIT = int(os.environ.get("BOX_STEWARD_IDLE", "3600"))
ALLOWED_TOOLS = {
    "box_list", "box_status", "box_snapshots", "usage", "billing",
    "box_ensure", "box_wake", "shell", "computer_state", "list_dir",
    "read_file",
}
PROTOCOLS = {"2025-11-25", "2025-06-18", "2025-03-26"}


def log(msg):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} [box-steward] {msg}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    print(line, file=sys.stderr, flush=True)


class McpError(RuntimeError):
    pass


class McpClient:
    """One warm MCP session (same wire behaviour as the VM client)."""

    def __init__(self, key):
        self.key = key
        self.session = None
        self.protocol = "2025-11-25"
        self.sequence = 0
        self.opener = urllib.request.build_opener()

    def send(self, method, params, *, notification=False):
        self.sequence += 1
        request_id = None if notification else self.sequence
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            body["id"] = request_id
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol,
            "Authorization": f"Bearer {self.key}",
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(
            ENDPOINT, data=json.dumps(body).encode(),
            headers=headers, method="POST")
        try:
            with self.opener.open(request, timeout=45) as response:
                if session := response.headers.get("Mcp-Session-Id"):
                    self.session = session
                if notification:
                    if response.status == 202:
                        return None
                    raise McpError("init notification not accepted")
                return self.read_result(response, request_id)
        except urllib.error.HTTPError as error:
            raise McpError(f"MCP returned HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise McpError("MCP request did not complete") from None

    @staticmethod
    def decode(payload, request_id):
        message = json.loads(payload)
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise McpError("invalid JSON-RPC message")
        if type(message.get("id")) is not int or message["id"] != request_id:
            return None
        if "error" in message:
            raise McpError("MCP returned a JSON-RPC error")
        if not isinstance(message.get("result"), dict):
            raise McpError("MCP returned no result object")
        return message["result"]

    @classmethod
    def read_result(cls, response, request_id):
        content_type = response.headers.get_content_type()
        if content_type == "application/json":
            result = cls.decode(response.read(), request_id)
            if result is None:
                raise McpError("wrong request ID")
            return result
        if content_type != "text/event-stream":
            raise McpError("unsupported content type")
        data = []
        while True:
            line = response.readline()
            if not line:
                raise McpError("event stream ended early")
            line = line.rstrip(b"\r\n")
            if line.startswith(b"data:"):
                data.append(line[5:].lstrip(b" "))
            elif not line and data:
                result = cls.decode(b"\n".join(data), request_id)
                data.clear()
                if result is not None:
                    return result

    def initialize(self):
        self.session = None
        result = self.send("initialize", {
            "protocolVersion": self.protocol,
            "capabilities": {},
            "clientInfo": {"name": "box-steward", "version": "1"},
        })
        protocol = result.get("protocolVersion")
        if protocol not in PROTOCOLS or not isinstance(
                result.get("capabilities", {}).get("tools"), dict):
            raise McpError("server did not negotiate a supported protocol")
        self.protocol = protocol
        self.send("notifications/initialized", {}, notification=True)

    def call_tool(self, name, arguments):
        if name not in ALLOWED_TOOLS:
            raise McpError(f"tool {name!r} not in ALLOWED_TOOLS")
        try:
            return self.send("tools/call",
                             {"name": name, "arguments": arguments})
        except McpError:
            # Session may have expired server-side: re-init once, retry once.
            self.initialize()
            return self.send("tools/call",
                             {"name": name, "arguments": arguments})


# ---------------------------------------------------------------------------
# Keeper (daemon) side
# ---------------------------------------------------------------------------

def serve():
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        key = open(KEY_FILE, encoding="utf-8").read().strip()
    except OSError:
        log(f"key file {KEY_FILE} unreadable — keeper not started")
        return 2
    if not key:
        log("key file empty — keeper not started")
        return 2
    client = McpClient(key)
    try:
        client.initialize()
    except McpError as exc:
        log(f"initial MCP session failed: {exc}")
        return 1
    log(f"keeper up pid={os.getpid()} idle_limit={IDLE_LIMIT}s")
    with open(PID_PATH, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, 0o600)
    server.listen(16)
    server.settimeout(30)
    last_used = time.monotonic()
    stopping = False

    def _stop(_sig, _frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while not stopping:
        if time.monotonic() - last_used > IDLE_LIMIT:
            log("idle limit reached — tearing down session and exiting")
            break
        try:
            conn, _ = server.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        with conn:
            conn.settimeout(120)
            try:
                chunks = []
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if b"\n" in chunk:
                        break
                request = json.loads(b"".join(chunks).decode("utf-8"))
                calls = request.get("calls", [])
                results = []
                for call in calls:
                    try:
                        results.append({"ok": True, "result": client.call_tool(
                            call["name"], call.get("arguments", {}))})
                    except (McpError, KeyError) as exc:
                        results.append({"ok": False, "error": str(exc)})
                last_used = time.monotonic()
                conn.sendall((json.dumps(
                    {"results": results}, ensure_ascii=False)
                    + "\n").encode("utf-8"))
            except (ValueError, OSError) as exc:
                log(f"request handling failed: {exc}")
    server.close()
    for path in (SOCK_PATH, PID_PATH):
        try:
            os.unlink(path)
        except OSError:
            pass
    log("keeper down")
    return 0


# ---------------------------------------------------------------------------
# CLI side
# ---------------------------------------------------------------------------

def keeper_alive():
    if not os.path.exists(PID_PATH):
        return False
    try:
        pid = int(open(PID_PATH, encoding="utf-8").read().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, OSError):
        return False


def ensure_keeper():
    os.makedirs(STATE_DIR, exist_ok=True)
    if keeper_alive() and os.path.exists(SOCK_PATH):
        return
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "serve"],
        stdout=open(LOG_PATH, "ab"), stderr=subprocess.STDOUT,
        start_new_session=True)
    for _ in range(100):
        if os.path.exists(SOCK_PATH) and keeper_alive():
            return
        if proc.poll() is not None:
            raise McpError(
                f"keeper exited early (rc={proc.returncode}); see {LOG_PATH}")
        time.sleep(0.1)
    raise McpError("keeper did not come up in 10 s")


def send_calls(calls):
    ensure_keeper()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(180)
        sock.connect(SOCK_PATH)
        sock.sendall((json.dumps({"calls": calls}) + "\n").encode("utf-8"))
        sock.shutdown(socket.SHUT_WR)
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data.decode("utf-8"))["results"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    call = sub.add_parser("call")
    call.add_argument("--name", required=True)
    call.add_argument("--arguments-file")
    batch = sub.add_parser("batch")
    batch.add_argument("--batch-file", required=True)
    sub.add_parser("status")
    sub.add_parser("stop")
    args = parser.parse_args()

    if args.command == "serve":
        return serve()
    if args.command == "status":
        alive = keeper_alive()
        print(json.dumps({"keeper_alive": alive,
                          "socket": os.path.exists(SOCK_PATH),
                          "idle_limit_seconds": IDLE_LIMIT}))
        return 0
    if args.command == "stop":
        if keeper_alive():
            pid = int(open(PID_PATH, encoding="utf-8").read().strip())
            os.kill(pid, signal.SIGTERM)
            print("keeper stopping")
        else:
            print("keeper not running")
        return 0
    if args.command == "call":
        arguments = {}
        if args.arguments_file:
            arguments = json.loads(
                open(args.arguments_file, encoding="utf-8").read())
        results = send_calls([{"name": args.name, "arguments": arguments}])
    else:  # batch
        calls = json.loads(open(args.batch_file, encoding="utf-8").read())
        results = send_calls(calls)
    print(json.dumps(results, ensure_ascii=False))
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except McpError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
