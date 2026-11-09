#!/usr/bin/env python3
"""Integration test for muse-bridge.py (stdlib only).

Starts the relay in-process on an ephemeral loopback port with a
temporary controllers.json, connects a hand-rolled fake PC client over
WebSocket, and exercises the documented contract: auth tiers and target
permissions, /status, /cmd, wait=false + /result polling, the /file
type_map, the controller-WS channel with its exact fallback text, and
fail-fast settlement when a PC disconnects mid-request.

Run:  python3 test_relay.py
"""

import asyncio
import base64
import hashlib
import http.client as http_client
import importlib.util
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "muse_bridge", os.path.join(HERE, "muse-bridge.py"))
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

GOOD_TOKEN = "test-token-good-0123456789abcdef"
OTHER_TOKEN = "test-token-other-0123456789abcdef"
SUPER_TOKEN = "test-token-super-0123456789abcdef"
HOSTNAME = "TESTPC"


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Minimal blocking WebSocket client (client frames masked, per RFC 6455)
# ---------------------------------------------------------------------------

class WSClient:
    def __init__(self, port, path="/muse-bridge", token=None):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.sock.settimeout(10)
        self.buf = bytearray()
        key = base64.b64encode(os.urandom(16)).decode()
        headers = [
            f"GET {path} HTTP/1.1",
            f"Host: 127.0.0.1:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if token:
            headers.append(f"X-Bridge-Token: {token}")
        self.sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("handshake EOF")
            response += chunk
        head, _, rest = response.partition(b"\r\n\r\n")
        assert b"101" in head.split(b"\r\n")[0], head
        self.buf += rest

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("socket EOF")
            self.buf += chunk
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n <= 0xFFFF:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def send_text(self, text):
        self._send_frame(0x1, text.encode())

    def send_json(self, obj):
        self.send_text(json.dumps(obj))

    def recv_json(self, timeout=10):
        self.sock.settimeout(timeout)
        while True:
            head = self._read(2)
            opcode = head[0] & 0x0F
            length = head[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8))[0]
            payload = self._read(length) if length else b""
            if opcode == 0x9:
                self._send_frame(0xA, payload)     # ping -> pong
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                raise ConnectionError("server closed the connection")
            if opcode == 0x1:
                return json.loads(payload.decode())

    def close(self):
        try:
            self._send_frame(0x8, struct.pack("!H", 1000))
        except OSError:
            pass
        self.sock.close()

    def abort(self):
        self.sock.close()


# ---------------------------------------------------------------------------
# Fake PC: answers cmd with a result frame, file ops with file_result
# ---------------------------------------------------------------------------

class FakePC(threading.Thread):
    def __init__(self, port, token):
        super().__init__(daemon=True)
        self.ws = WSClient(port, token=token)
        self.hang_received = threading.Event()
        self.stop_flag = threading.Event()

    def run(self):
        self.ws.send_json({
            "type": "hello", "client": "muse-bridge/1.2.12",
            "hostname": HOSTNAME, "platform": "win32", "arch": "x64",
            "binary_frame": True,
        })
        self.ws.sock.settimeout(0.5)
        while not self.stop_flag.is_set():
            try:
                message = self.ws.recv_json(timeout=0.5)
            except (socket.timeout, TimeoutError):
                continue
            except (ConnectionError, OSError):
                break
            mtype = message.get("type")
            if mtype == "cmd":
                command = message.get("command", "")
                if command == "hang":
                    self.hang_received.set()
                    continue        # never answered: tests fail-fast
                self.ws.send_json({
                    "type": "result", "id": message["id"], "ok": True,
                    "exitCode": 0,
                    "stdout": f"hello from pc: {command}", "stderr": "",
                })
            elif mtype == "batch":
                self.ws.send_json({
                    "type": "batch_result", "id": message["id"], "ok": True,
                    "results": [{"exit": 0, "stdout": "b", "stderr": ""}],
                })
            elif mtype == "read_file":
                self.ws.send_json({
                    "type": "file_result", "id": message["id"], "ok": True,
                    "data": "aGVsbG8=", "bytes": 5,
                    "hash": "2cf24dba5fb0a30e",
                })
            elif mtype is not None:
                self.ws.send_json({
                    "type": "file_result", "id": message["id"], "ok": True,
                })

    def stop(self):
        self.stop_flag.set()
        self.ws.abort()


# ---------------------------------------------------------------------------
# Test plumbing
# ---------------------------------------------------------------------------

def http(port, method, path, token=None, body=None, timeout=30):
    conn = http_client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {}
    payload = None
    if token:
        headers["X-Bridge-Token"] = token
    if body is not None:
        payload = json.dumps(body)
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    return response.status, json.loads(raw.decode())


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"  ok: {name}")


def main():
    controllers = {
        sha(GOOD_TOKEN): {"muse": "muse-test", "pcs": [HOSTNAME],
                          "created": "2026-11-01", "label": "test pair",
                          "enabled": True},
        sha(OTHER_TOKEN): {"muse": "muse-other", "pcs": ["OTHERPC"],
                           "created": "2026-11-01", "label": "other pair",
                           "enabled": True},
        sha(SUPER_TOKEN): {"muse": "super", "pcs": ["*"],
                           "created": "2026-11-01", "label": "super",
                           "enabled": True},
    }
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(controllers, tmp)
    tmp.close()

    relay = bridge.Relay(controllers_path=tmp.name, legacy_token=None,
                         token_file="/nonexistent/muse-bridge-token")
    loop = asyncio.new_event_loop()
    server = loop.run_until_complete(relay.start("127.0.0.1", 0))
    port = server.sockets[0].getsockname()[1]
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    print(f"relay on 127.0.0.1:{port}")

    pc = None
    try:
        # -- auth --------------------------------------------------
        status, _ = http(port, "GET", "/muse-api/status")
        check("no token -> 401", status == 401, f"got {status}")
        status, _ = http(port, "GET", "/muse-api/status", token="bogus")
        check("bad token -> 401", status == 401, f"got {status}")

        # -- status before any PC ----------------------------------
        status, obj = http(port, "GET", "/muse-api/status", token=GOOD_TOKEN)
        check("status 200", status == 200)
        check("status version", obj["version"] == "1.1.0")
        check("status clients empty", obj["clients"] == [])
        check("status counts", isinstance(obj["pending"], int)
              and isinstance(obj["cached_results"], int))

        # -- PC comes online ----------------------------------------
        pc = FakePC(port, GOOD_TOKEN)
        pc.start()
        deadline = time.time() + 10
        while True:
            _, obj = http(port, "GET", "/muse-api/status", token=GOOD_TOKEN)
            if obj["clients"]:
                break
            assert time.time() < deadline, "PC never came online"
            time.sleep(0.2)
        check("status lists PC",
              obj["clients"][0]["hostname"] == HOSTNAME
              and obj["clients"][0]["platform"] == "win32")

        # -- permission rules ---------------------------------------
        status, obj = http(port, "POST", "/muse-api/cmd", token=GOOD_TOKEN,
                           body={"command": "echo x", "target": "OTHERPC"})
        check("out-of-scope target -> 403", status == 403, f"got {status}")
        status, obj = http(port, "POST", "/muse-api/cmd", token=SUPER_TOKEN,
                           body={"command": "echo x"})
        check("super without target -> 403", status == 403,
              f"got {status}")

        # -- /cmd round trips ---------------------------------------
        status, obj = http(port, "POST", "/muse-api/cmd", token=GOOD_TOKEN,
                           body={"command": "echo hi", "target": HOSTNAME})
        check("/cmd 200", status == 200, f"got {status} {obj}")
        check("/cmd stdout passes through",
              obj.get("stdout") == "hello from pc: echo hi", f"{obj}")
        check("/cmd result shape untouched",
              obj.get("ok") is True and obj.get("exitCode") == 0)

        status, obj = http(port, "POST", "/muse-api/cmd", token=GOOD_TOKEN,
                           body={"command": "echo auto"})
        check("/cmd auto-resolve single-PC controller",
              status == 200
              and obj.get("stdout") == "hello from pc: echo auto",
              f"got {status} {obj}")

        # -- wait=false + /result polling ----------------------------
        status, obj = http(port, "POST", "/muse-api/cmd", token=GOOD_TOKEN,
                           body={"command": "echo later", "target": HOSTNAME,
                                 "wait": False, "id": "test-wait-1"})
        check("wait=false immediate id",
              status == 200 and obj == {"ok": True, "id": "test-wait-1"},
              f"got {status} {obj}")
        status, obj = http(port, "GET", "/muse-api/result/test-wait-1",
                           token=GOOD_TOKEN)
        check("/result returns cached result",
              status == 200
              and obj.get("stdout") == "hello from pc: echo later",
              f"got {status} {obj}")
        status, _ = http(port, "GET", "/muse-api/result/does-not-exist",
                         token=GOOD_TOKEN)
        check("/result unknown id -> 404", status == 404, f"got {status}")

        # -- /file type_map ------------------------------------------
        status, obj = http(port, "POST", "/muse-api/file", token=GOOD_TOKEN,
                           body={"op": "read", "path": "C:\\x.txt",
                                 "target": HOSTNAME})
        check("/file read round trip",
              status == 200 and obj.get("data") == "aGVsbG8="
              and obj.get("type") == "file_result", f"got {status} {obj}")
        status, obj = http(port, "POST", "/muse-api/file", token=GOOD_TOKEN,
                           body={"op": "dance", "path": "x",
                                 "target": HOSTNAME})
        check("/file unknown op -> 400 listing ops",
              status == 400 and "Supported ops" in obj.get("error", ""),
              f"got {status} {obj}")

        # -- controller WSS -------------------------------------------
        ctl = WSClient(port, token=GOOD_TOKEN)
        ctl.send_json({"type": "hello", "client": "mused/4.0-wss",
                       "role": "controller", "hostname": "test-vm",
                       "platform": "linux", "arch": "x64"})
        ctl.send_json({"type": "api_request", "req_id": "r1",
                       "method": "POST", "path": "/cmd",
                       "body": {"command": "echo via ws",
                                "target": HOSTNAME}})
        reply = ctl.recv_json()
        check("controller-WS reply type",
              reply.get("type") == "api_response"
              and reply.get("req_id") == "r1", f"{reply}")
        inner = json.loads(reply["body"])
        check("controller-WS /cmd round trip",
              reply["status"] == 200
              and inner.get("stdout") == "hello from pc: echo via ws",
              f"{reply}")
        ctl.send_json({"type": "api_request", "req_id": "r2",
                       "method": "POST", "path": "/nope", "body": {}})
        reply = ctl.recv_json()
        check("controller-WS unsupported path -> 404 + exact fallback text",
              reply["status"] == 404
              and "Unsupported controller API" in reply["body"],
              f"{reply}")
        ctl.close()

        # -- PC disconnect fail-fast -----------------------------------
        outcome = {}

        def hanging_call():
            outcome["value"] = http(
                port, "POST", "/muse-api/cmd", token=GOOD_TOKEN,
                body={"command": "hang", "target": HOSTNAME,
                      "timeout": 120}, timeout=30)

        caller = threading.Thread(target=hanging_call, daemon=True)
        started = time.time()
        caller.start()
        assert pc.hang_received.wait(10), "PC never saw the hanging cmd"
        pc.stop()                       # PC vanishes mid-request
        pc = None
        caller.join(15)
        check("disconnect call settled", "value" in outcome)
        status, obj = outcome["value"]
        check("disconnect fail-fast payload",
              obj.get("ok") is False
              and obj.get("error") == "Target disconnected before result",
              f"got {status} {obj}")
        check("disconnect settled fast", time.time() - started < 15,
              f"took {time.time() - started:.1f}s")
    finally:
        if pc is not None:
            pc.stop()
        loop.call_soon_threadsafe(loop.stop)
        os.unlink(tmp.name)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
