#!/usr/bin/env python3
"""Muse Bridge VPS relay — reimplementation from the techbook specification.

Single process, single asyncio event loop, stdlib only.

The relay never executes commands. It authenticates controllers and PCs,
routes requests by target hostname over a persistent WebSocket channel,
pairs requests with responses by id, and translates between the
controller-facing HTTPS API (/muse-api) / controller WSS and the
PC-facing WSS messages. Request and response JSON pass through untouched
(including compressed field objects and both documented result shapes);
idempotency/dedup deliberately lives in the controller daemon (mused),
not here — see DESIGN.md A.5.

One TCP listener serves plain HTTP and WebSocket upgrades on the same
port (TLS is terminated by nginx in front of it). RFC 6455 framing is
hand-rolled below.

Configuration (environment):
  MUSE_BRIDGE_LISTEN      host:port, default 127.0.0.1:8471
  MUSE_BRIDGE_CONTROLLERS path, default /etc/muse-bridge/controllers.json
  MUSE_BRIDGE_TOKEN       legacy single token (else /etc/muse-bridge/token)
  MUSE_BRIDGE_TOKEN_FILE  legacy token file path override
"""

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import struct
import sys
import time
import uuid

BRIDGE_VERSION = "1.1.0"

LISTEN_HOST, LISTEN_PORT = ("127.0.0.1", 8471)
_env_listen = os.environ.get("MUSE_BRIDGE_LISTEN", "")
if _env_listen:
    _h, _, _p = _env_listen.rpartition(":")
    if _h and _p.isdigit():
        LISTEN_HOST, LISTEN_PORT = _h, int(_p)

CONTROLLERS_PATH = os.environ.get(
    "MUSE_BRIDGE_CONTROLLERS", "/etc/muse-bridge/controllers.json")
TOKEN_FILE = os.environ.get(
    "MUSE_BRIDGE_TOKEN_FILE", "/etc/muse-bridge/token")

MAX_PAYLOAD_SIZE = 64 * 1024 * 1024          # documented 64 MB frame cap
PING_INTERVAL = 15                            # seconds, relay -> client
PC_REAP_TIMEOUT = 35                          # seconds of silence, PC channel
# The spec (DESIGN.md d.6) warns the controller channel must not be reaped
# on the PC channel's 35 s rule: mused sends no WS pings of its own and its
# silence watchdog has a control-frame blind spot. Our pings elicit pongs
# that reset activity, so a generous 120 s bound is safe and observable.
CONTROLLER_REAP_TIMEOUT = 120
RESULT_TTL = 300                              # completed_results TTL, seconds
RESULT_CACHE_MAX = 256                        # completed_results max entries
RESULT_POLL_SECONDS = 60                      # GET /result long-poll bound
TIMEOUT_MARGIN = 5                            # added to the request timeout
BATCH_REQUESTS_MAX = 32             # call-level batch sub-request cap
BATCH_SUB_PATHS = ("/cmd", "/file", "/batch", "/status")

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
RESPONSE_TYPES = {"result", "batch_result", "file_result"}

# /file op -> PC message type (DESIGN.md A.5 type_map). Never invent others.
FILE_TYPE_MAP = {
    "read": "read_file",
    "write": "write_file",
    "edit": "edit_file",
    "glob": "glob_files",
    "grep": "grep_files",
    "info": "file_info",
    "list": "list_dir",
    "archive": "archive_7z",
    "extract": "extract_7z",
}

log = logging.getLogger("muse-bridge")


def _json_loads(data):
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8")
    return json.loads(data)


def _json_dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clamp_timeout(value, default=60):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(600, value))


# --------------------------------------------------------------------------
# WebSocket framing (hand-rolled RFC 6455)
# --------------------------------------------------------------------------

def ws_accept_key(key: str) -> str:
    digest = hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def ws_unmask(payload: bytes, mask_key: bytes) -> bytes:
    """Unmask with one whole-buffer big-int XOR (C speed, no per-byte loop)."""
    n = len(payload)
    if n == 0:
        return payload
    pattern = (mask_key * (n // 4 + 1))[:n]
    xored = int.from_bytes(payload, "big") ^ int.from_bytes(pattern, "big")
    return xored.to_bytes(n, "big")


def ws_build_frame(opcode: int, payload: bytes) -> bytes:
    """Server -> client frame: FIN set, never masked."""
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n <= 0xFFFF:
        header.append(126)
        header += struct.pack("!H", n)
    else:
        header.append(127)
        header += struct.pack("!Q", n)
    return bytes(header) + payload


class WSClosed(Exception):
    pass


class WSConn:
    """One upgraded WebSocket connection (PC client or controller client)."""

    def __init__(self, reader, writer, auth, peer_ip):
        self.reader = reader
        self.writer = writer
        self.auth = auth                # AuthContext from the handshake
        self.peer_ip = peer_ip
        self.send_lock = asyncio.Lock()
        self.last_activity = time.monotonic()
        self.closed = False
        # Filled in by the hello frame:
        self.kind = None                # "pc" | "controller"
        self.hostname = ""
        self.client_desc = ""
        self.platform = ""
        self.hello = {}

    # -- sending ------------------------------------------------------
    async def _send_frame(self, opcode, payload: bytes):
        if self.closed:
            raise WSClosed()
        frame = ws_build_frame(opcode, payload)
        async with self.send_lock:
            try:
                self.writer.write(frame)
                await self.writer.drain()
            except (ConnectionError, OSError):
                self.closed = True
                raise WSClosed()

    async def send_text(self, text: str):
        await self._send_frame(0x1, text.encode("utf-8"))

    async def send_json(self, obj):
        await self.send_text(_json_dumps(obj))

    async def send_ping(self):
        await self._send_frame(0x9, b"")

    async def send_pong(self, payload: bytes):
        await self._send_frame(0xA, payload)

    async def close(self, code=1000, reason=b""):
        if self.closed:
            return
        self.closed = True
        payload = struct.pack("!H", code) + reason
        try:
            async with self.send_lock:
                self.writer.write(ws_build_frame(0x8, payload))
                await self.writer.drain()
        except (ConnectionError, OSError):
            pass
        try:
            self.writer.close()
        except OSError:
            pass

    def abort(self):
        self.closed = True
        try:
            self.writer.close()
        except OSError:
            pass

    # -- receiving ----------------------------------------------------
    async def read_frame(self):
        """Return (opcode, payload bytes). Raises WSClosed on EOF/close."""
        try:
            head = await self.reader.readexactly(2)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            raise WSClosed()
        fin = head[0] & 0x80
        opcode = head[0] & 0x0F
        masked = head[1] & 0x80
        length = head[1] & 0x7F
        try:
            if length == 126:
                length = struct.unpack("!H", await self.reader.readexactly(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", await self.reader.readexactly(8))[0]
            mask_key = await self.reader.readexactly(4) if masked else b""
            if length > MAX_PAYLOAD_SIZE:
                await self.close(1009, b"frame too large")
                raise WSClosed()
            payload = await self.reader.readexactly(length) if length else b""
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            raise WSClosed()
        if masked:
            payload = ws_unmask(payload, mask_key)
        self.last_activity = time.monotonic()
        if not fin:
            # Fragmentation is not used by either client; reject cleanly.
            await self.close(1003, b"fragmented frames unsupported")
            raise WSClosed()
        return opcode, payload


# --------------------------------------------------------------------------
# Authentication (DESIGN.md A.3 — two tiers, no loopback bypass)
# --------------------------------------------------------------------------

class AuthContext:
    def __init__(self, kind, muse, pcs, hash_prefix):
        self.kind = kind                # "controller" | "legacy"
        self.muse = muse                # controller id, or "legacy"
        self.pcs = pcs                  # list[str]; ["*"] means super
        self.hash_prefix = hash_prefix  # first 8 of the token hash, for logs

    @property
    def is_super(self):
        return self.kind == "controller" and self.pcs == ["*"]


class ControllerStore:
    """controllers.json keyed by sha256(token); mtime hot-reload cache."""

    def __init__(self, path):
        self.path = path
        self._mtime = None
        self._entries = {}

    def _load(self):
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            self._mtime = None
            self._entries = {}
            return
        if mtime == self._mtime:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._entries = data
                self._mtime = mtime
        except (OSError, ValueError):
            log.warning("controllers file unreadable; treating as empty")

    def lookup(self, token_hash: str):
        self._load()
        entry = self._entries.get(token_hash)
        if not isinstance(entry, dict) or not entry.get("enabled", False):
            return None
        return entry


class Authenticator:
    def __init__(self, controllers_path, legacy_token=None, token_file=None):
        self.store = ControllerStore(controllers_path)
        self._legacy_env = legacy_token
        self._token_file = token_file

    def _legacy_token(self):
        if self._legacy_env:
            return self._legacy_env
        try:
            with open(self._token_file, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return None

    def authenticate(self, token, peer_ip):
        """Return an AuthContext, or None for an unknown token (-> 401)."""
        if not token:
            return None
        token_hash = _sha256_hex(token)
        entry = self.store.lookup(token_hash)
        if entry is not None:
            if not self._ip_allowed(entry, peer_ip):
                return "ip_denied"
            pcs = entry.get("pcs") or []
            return AuthContext("controller", entry.get("muse", ""),
                               list(pcs), token_hash[:8])
        legacy = self._legacy_token()
        if legacy and hmac.compare_digest(token, legacy):
            return AuthContext("legacy", "legacy", ["*"], token_hash[:8])
        return None

    @staticmethod
    def _ip_allowed(entry, peer_ip):
        allowed = entry.get("allowed_ips")
        if not allowed:
            return True
        try:
            ip = ipaddress.ip_address(peer_ip)
        except ValueError:
            return False
        for cidr in allowed:
            try:
                if ip in ipaddress.ip_network(cidr, strict=False):
                    return True
            except ValueError:
                continue
        return False


def extract_token(headers, body, query):
    """Priority: X-Bridge-Token, Authorization: Bearer, body, query."""
    token = headers.get("x-bridge-token")
    if token:
        return token.strip()
    authz = headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        return authz[7:].strip()
    if isinstance(body, dict):
        value = body.get("token")
        if isinstance(value, str) and value:
            return value
    for key in ("token", "access_token"):
        values = query.get(key)
        if values:
            return values[0]
    return None


def effective_peer_ip(peer_ip, headers):
    """Behind nginx the socket peer is loopback; only then trust XFF."""
    if peer_ip in ("127.0.0.1", "::1"):
        forwarded = headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return peer_ip


class PermissionDenied(Exception):
    pass


def check_target_permission(auth: AuthContext, target):
    """Return the effective target, auto-resolving a missing one.

    Rules (A.3): controller target must be in pcs (case-insensitive) else
    PermissionDenied (generic 403); single-PC controller auto-resolves;
    super without a target is denied; legacy callers are unrestricted.
    """
    if auth.kind == "legacy":
        return target
    if target:
        if auth.is_super:
            return target
        if any(target.lower() == pc.lower() for pc in auth.pcs):
            return target
        raise PermissionDenied("Forbidden")
    # No target named:
    if auth.is_super:
        raise PermissionDenied("Forbidden")
    if len(auth.pcs) == 1:
        return auth.pcs[0]
    return None  # several PCs: pick_client will answer 409


# --------------------------------------------------------------------------
# Relay core
# --------------------------------------------------------------------------

class Relay:
    def __init__(self, controllers_path=CONTROLLERS_PATH, legacy_token=None,
                 token_file=TOKEN_FILE):
        self.auth = Authenticator(controllers_path, legacy_token, token_file)
        self.clients = set()          # all live WSConn (PCs + controllers)
        self.pcs = {}                 # hostname -> WSConn (PC channel only)
        self.pending = {}             # req_id -> (Future, WSConn)
        self.inflight = set()         # req_ids dispatched with wait=false
        self.completed = {}           # req_id -> (msg dict, completed_at)
        self._tasks = set()

    # ---------------- public server plumbing ----------------
    async def start(self, host, port):
        return await asyncio.start_server(self._handle_conn, host, port)

    def _spawn(self, coro):
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ---------------- connection entry: HTTP or WS upgrade ----------------
    async def _handle_conn(self, reader, writer):
        peer = writer.get_extra_info("peername")
        peer_ip = peer[0] if peer else ""
        ws = None
        try:
            while True:  # HTTP keep-alive loop; WS upgrade exits the loop
                request = await self._read_http_request(reader)
                if request is None:
                    break
                method, target, version, headers, body = request
                parsed = _parse_target(target)
                if headers.get("upgrade", "").lower() == "websocket":
                    await self._ws_upgrade(reader, writer, headers, body,
                                           parsed, peer_ip)
                    return
                status, obj, keep_alive = await self._handle_http(
                    method, parsed, headers, body, peer_ip, version)
                await self._write_http_response(writer, status, obj,
                                                keep_alive)
                if not keep_alive:
                    break
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        except Exception:  # never let one connection kill the loop
            log.exception("connection handler failed")
        finally:
            try:
                writer.close()
            except OSError:
                pass

    async def _read_http_request(self, reader):
        line = await reader.readline()
        if not line:
            return None
        parts = line.decode("latin-1").strip().split(" ")
        if len(parts) != 3:
            raise ConnectionError("bad request line")
        method, target, version = parts
        headers = {}
        while True:
            hline = await reader.readline()
            if hline in (b"\r\n", b"\n", b""):
                break
            name, _, value = hline.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()
        body = b""
        length = headers.get("content-length")
        if length:
            body = await reader.readexactly(int(length))
        return method, target, version, headers, body

    async def _write_http_response(self, writer, status, obj, keep_alive):
        reason = _HTTP_REASONS.get(status, "OK")
        payload = _json_dumps(obj).encode("utf-8")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            f"Connection: {'keep-alive' if keep_alive else 'close'}\r\n\r\n"
        ).encode("latin-1")
        writer.write(head + payload)
        await writer.drain()

    # ---------------- authentication helper ----------------
    def _authenticate(self, headers, body_obj, query, peer_ip):
        """Return AuthContext, or an (status, obj) error tuple."""
        ip = effective_peer_ip(peer_ip, headers)
        token = extract_token(headers, body_obj, query)
        auth = self.auth.authenticate(token, ip)
        if auth is None:
            log.info("auth failed from %s (ip %s)", peer_ip, ip)
            return 401, {"ok": False, "error": "Unauthorized"}
        if auth == "ip_denied":
            return 403, {"ok": False, "error": "Forbidden"}
        log.debug("auth ok muse=%s hash=%s", auth.muse, auth.hash_prefix)
        return auth

    # ---------------- HTTP API ----------------
    async def _handle_http(self, method, parsed, headers, body, peer_ip,
                           version):
        path, query = parsed
        keep_alive = (version == "HTTP/1.1"
                      and headers.get("connection", "").lower() != "close")
        body_obj = None
        if body:
            try:
                body_obj = _json_loads(body)
            except ValueError:
                return 400, {"ok": False, "error": "Invalid JSON body"}, \
                    keep_alive
            if not isinstance(body_obj, dict):
                return 400, {"ok": False,
                             "error": "JSON body must be an object"}, \
                    keep_alive
        # Every endpoint authenticates before routing (no loopback bypass,
        # /status included — the pre-auth /status leak must not recur).
        auth = self._authenticate(headers, body_obj, query, peer_ip)
        if isinstance(auth, tuple):
            return auth[0], auth[1], keep_alive
        status, obj = await self.handle_api(auth, method, path,
                                            body_obj or {}, query)
        return status, obj, keep_alive

    async def handle_api(self, auth, method, path, body, query):
        """Shared handler for HTTPS /muse-api/* and controller-WS paths.

        Controller-WS paths arrive as /cmd, /status, ... and are mapped
        onto the same /muse-api routes.
        """
        if not path.startswith("/muse-api"):
            path = "/muse-api" + path
        if method == "GET" and path == "/muse-api/status":
            return 200, self._status_obj()
        if method == "GET" and path.startswith("/muse-api/result/"):
            req_id = path[len("/muse-api/result/"):].strip("/")
            if not req_id:
                return 404, {"ok": False, "error": "Unknown result id"}
            return await self._poll_result(req_id)
        if method == "POST" and path == "/muse-api/cmd":
            if not body.get("command"):
                return 400, {"ok": False, "error": "command is required"}
            msg = self._build_msg(body, "cmd")
            timeout = _clamp_timeout(body.get("timeout"), 60)
            return await self._dispatch_and_reply(auth, body, msg, timeout)
        if method == "POST" and path == "/muse-api/batch":
            if isinstance(body.get("requests"), list):
                return await self._handle_batch_requests(auth, body)
            commands = body.get("commands")
            if not isinstance(commands, list) or not commands:
                return 400, {"ok": False,
                             "error": "commands list is required"}
            msg = self._build_msg(body, "batch")
            sub = [_clamp_timeout(c.get("timeout"), 60)
                   for c in commands if isinstance(c, dict)]
            timeout = _clamp_timeout(body.get("timeout"),
                                     max(sub) if sub else 60)
            return await self._dispatch_and_reply(auth, body, msg, timeout)
        if method == "POST" and path == "/muse-api/file":
            op = body.get("op")
            if op not in FILE_TYPE_MAP:
                return 400, {
                    "ok": False,
                    "error": f"Unsupported op {op!r}. Supported ops: "
                             + ", ".join(sorted(FILE_TYPE_MAP)),
                }
            msg = self._build_msg(body, FILE_TYPE_MAP[op], strip=("op",))
            timeout = _clamp_timeout(body.get("timeout"), 60)
            return await self._dispatch_and_reply(auth, body, msg, timeout)
        # Anything else on the controller surface: the error body MUST
        # contain this exact text — mused treats it as the signal to fall
        # back from controller WSS to HTTPS (DESIGN.md A.5).
        return 404, {"ok": False,
                     "error": f"Unsupported controller API: {method} {path}"}

    # ---------------- call-level batch (demultiplexer) ----------------
    async def _handle_batch_requests(self, auth, body):
        """Fan a {"requests": [...]} call-level batch out to the target.

        Each sub-request re-enters handle_api as an ordinary single call
        (cmd / file / legacy batch / status), so the PC agent only ever
        sees the same single-op messages it already handles — the
        demultiplexing lives entirely here in the relay.  Sub-requests
        are dispatched concurrently; one sub-request failing never fails
        the others (partial-failure isolation).  The whole batch goes to
        the outer request's target: a sub-request cannot redirect its
        share to a different PC.
        """
        requests = body.get("requests")
        if not requests:
            return 400, {"ok": False, "error": "requests list is required"}
        if len(requests) > BATCH_REQUESTS_MAX:
            return 400, {"ok": False,
                         "error": f"too many sub-requests "
                                  f"(max {BATCH_REQUESTS_MAX})"}
        outer_target = body.get("target")

        async def run_one(index, item):
            if not isinstance(item, dict):
                return {"id": None, "status": 400, "body": {
                    "ok": False, "error": "sub-request must be an object"}}
            sub_id = item.get("id") or f"sub{index}"
            method = str(item.get("method") or "POST").upper()
            path = str(item.get("path") or "")
            sub_body = item.get("body")
            if not isinstance(sub_body, dict):
                return {"id": sub_id, "status": 400, "body": {
                    "ok": False,
                    "error": "sub-request body must be an object"}}
            if path not in BATCH_SUB_PATHS:
                return {"id": sub_id, "status": 400, "body": {
                    "ok": False,
                    "error": f"sub-request path {path!r} "
                             f"not allowed in batch"}}
            if path == "/batch" and isinstance(sub_body.get("requests"),
                                               list):
                return {"id": sub_id, "status": 400, "body": {
                    "ok": False,
                    "error": "nested call-level batch is not allowed"}}
            sub_body = dict(sub_body)
            if outer_target is not None:
                sub_body["target"] = outer_target
            # Every sub-request is awaited (no fire-and-forget inside a
            # batch) and gets a fresh relay-assigned dispatch id.
            sub_body.pop("wait", None)
            sub_body.pop("id", None)
            status, obj = await self.handle_api(auth, method, path,
                                                sub_body, {})
            return {"id": sub_id, "status": status, "body": obj}

        results = await asyncio.gather(
            *(run_one(i, item) for i, item in enumerate(requests)))
        return 200, {"ok": True, "results": list(results)}

    def _status_obj(self):
        return {
            "ok": True,
            "version": BRIDGE_VERSION,
            "clients": [
                {"hostname": c.hostname, "platform": c.platform,
                 "arch": c.hello.get("arch", ""), "client": c.client_desc}
                for c in self.pcs.values()
            ],
            "online": sorted(self.pcs),
            "controllers": sum(1 for c in self.clients
                               if c.kind == "controller"),
            "pending": len(self.pending),
            "cached_results": len(self.completed),
        }

    @staticmethod
    def _build_msg(body, msg_type, strip=()):
        """Copy the request for the PC; the relay only re-types and pops
        routing keys. Everything else — including compressed field
        objects and any idem_key — passes through untouched."""
        msg = dict(body)
        for key in ("target", "wait", "token", "access_token") + tuple(strip):
            msg.pop(key, None)
        msg["type"] = msg_type
        return msg

    # ---------------- dispatch ----------------
    def _pick_client(self, target):
        """Return (conn, status, error_obj) per A.2 semantics."""
        if target:
            conn = self.pcs.get(target)
            if conn is None:
                lowered = target.lower()
                for name, candidate in self.pcs.items():
                    if name.lower() == lowered:
                        conn = candidate
                        break
            if conn is None:
                return None, 404, {
                    "ok": False,
                    "error": f"Target '{target}' not online",
                    "online": sorted(self.pcs),
                }
            return conn, 0, None
        if not self.pcs:
            return None, 503, {"ok": False, "error": "No PC online",
                               "online": []}
        if len(self.pcs) > 1:
            return None, 409, {
                "ok": False,
                "error": "Multiple PCs online; specify a target",
                "online": sorted(self.pcs),
            }
        return next(iter(self.pcs.values())), 0, None

    async def _dispatch_and_reply(self, auth, body, msg, timeout):
        try:
            target = check_target_permission(auth, body.get("target"))
        except PermissionDenied:
            return 403, {"ok": False, "error": "Forbidden"}
        conn, status, error = self._pick_client(target)
        if conn is None:
            return status, error
        req_id = body.get("id") or f"mb_{int(time.time() * 1000)}_" \
                                   f"{uuid.uuid4().hex[:8]}"
        msg["id"] = req_id
        if req_id in self.pending:
            return 409, {"ok": False,
                         "error": f"Duplicate request id {req_id}"}
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending[req_id] = (future, conn)
        try:
            await conn.send_json(msg)
        except WSClosed:
            self.pending.pop(req_id, None)
            return 503, {"ok": False,
                         "error": "Target disconnected before result"}
        if body.get("wait") is False:
            # Answer at once; the eventual result lands in the cache and
            # is fetched via GET /result/<id>.
            self.inflight.add(req_id)
            self._spawn(self._collect_result(req_id, future, timeout))
            return 200, {"ok": True, "id": req_id}
        try:
            # Request timeout (body timeout, clamped 1..600) plus margin.
            result = await asyncio.wait_for(future, timeout + TIMEOUT_MARGIN)
            return 200, result
        except asyncio.TimeoutError:
            self.pending.pop(req_id, None)
            return 504, {"ok": False,
                         "error": f"Request {req_id} timed out"}
        except asyncio.CancelledError:
            self.pending.pop(req_id, None)
            raise

    async def _collect_result(self, req_id, future, timeout):
        try:
            result = await asyncio.wait_for(
                asyncio.shield(future), timeout + TIMEOUT_MARGIN)
        except asyncio.TimeoutError:
            self.pending.pop(req_id, None)
            result = {"ok": False, "error": f"Request {req_id} timed out"}
        except asyncio.CancelledError:
            return
        self.inflight.discard(req_id)
        self._store_completed(req_id, result)

    def _store_completed(self, req_id, result):
        now = time.monotonic()
        expired = [key for key, (_, ts) in self.completed.items()
                   if now - ts > RESULT_TTL]
        for key in expired:
            del self.completed[key]
        self.completed[req_id] = (result, now)
        while len(self.completed) > RESULT_CACHE_MAX:
            oldest = next(iter(self.completed))
            del self.completed[oldest]

    def _cached_result(self, req_id):
        entry = self.completed.get(req_id)
        if entry is None:
            return None
        result, ts = entry
        if time.monotonic() - ts > RESULT_TTL:
            del self.completed[req_id]
            return None
        return result

    async def _poll_result(self, req_id):
        deadline = time.monotonic() + RESULT_POLL_SECONDS
        while True:
            result = self._cached_result(req_id)
            if result is not None:
                return 200, result
            if req_id not in self.inflight and req_id not in self.pending:
                return 404, {"ok": False, "error": "Unknown result id"}
            if time.monotonic() >= deadline:
                return 504, {"ok": False,
                             "error": f"Result {req_id} not ready (expired)"}
            await asyncio.sleep(0.2)

    # ---------------- WebSocket sessions ----------------
    async def _ws_upgrade(self, reader, writer, headers, body, parsed,
                          peer_ip):
        path, query = parsed
        if path != "/muse-bridge":
            await self._write_http_response(
                writer, 404, {"ok": False, "error": "Unknown WS path"},
                False)
            return
        key = headers.get("sec-websocket-key")
        if not key:
            await self._write_http_response(
                writer, 400, {"ok": False, "error": "Missing Sec-WebSocket-Key"},
                False)
            return
        # Authenticate before routing, handshake included (A.3).
        auth = self._authenticate(headers, None, query, peer_ip)
        if isinstance(auth, tuple):
            await self._write_http_response(writer, auth[0], auth[1], False)
            return
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {ws_accept_key(key)}\r\n\r\n"
        ).encode("latin-1")
        writer.write(response)
        await writer.drain()
        conn = WSConn(reader, writer, auth, effective_peer_ip(peer_ip,
                                                              headers))
        self.clients.add(conn)
        try:
            await self._ws_session(conn)
        finally:
            self.clients.discard(conn)
            self._on_ws_disconnect(conn)

    async def _ws_session(self, conn: WSConn):
        heartbeat = self._spawn(self._heartbeat(conn))
        try:
            while not conn.closed:
                opcode, payload = await conn.read_frame()
                if opcode == 0x9:            # ping -> pong
                    await conn.send_pong(payload)
                    continue
                if opcode == 0xA:            # pong: activity already noted
                    continue
                if opcode == 0x8:            # close
                    await conn.close(1000)
                    break
                if opcode == 0x2:            # binary: relay never originates
                    continue                 # binary frames; drop inbound
                try:
                    message = _json_loads(payload)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if conn.kind is None:
                    if message.get("type") != "hello":
                        await conn.close(1003, b"first frame must be hello")
                        break
                    if not await self._register_hello(conn, message):
                        break
                    continue
                if conn.kind == "pc":
                    self._handle_pc_message(conn, message)
                elif message.get("type") == "api_request":
                    self._spawn(self._handle_api_request(conn, message))
        except WSClosed:
            pass
        finally:
            heartbeat.cancel()
            conn.abort()

    async def _register_hello(self, conn: WSConn, hello) -> bool:
        conn.hello = hello
        conn.hostname = str(hello.get("hostname", ""))
        conn.client_desc = str(hello.get("client", ""))
        conn.platform = str(hello.get("platform", ""))
        if hello.get("role") == "controller":
            conn.kind = "controller"
            log.info("controller online muse=%s host=%s",
                     conn.auth.muse, conn.hostname)
            return True
        conn.kind = "pc"
        # The PC presents the pair's controller token: its hostname must
        # be inside that token's pcs (super and legacy are unrestricted).
        auth = conn.auth
        if auth.kind == "controller" and not auth.is_super:
            if not any(conn.hostname.lower() == pc.lower()
                       for pc in auth.pcs):
                log.info("PC hello out of scope muse=%s host=%s",
                         auth.muse, conn.hostname)
                await conn.close(4403, b"Forbidden")
                return False
        old = self.pcs.get(conn.hostname)
        if old is not None and old is not conn:
            # One live PC connection per hostname: evict the older one.
            log.info("evicting older connection for %s", conn.hostname)
            self.clients.discard(old)
            await old.close(1000, b"replaced")
            self._settle_pending_for(old)
        self.pcs[conn.hostname] = conn
        log.info("PC online host=%s client=%s", conn.hostname,
                 conn.client_desc)
        return True

    def _handle_pc_message(self, conn: WSConn, message):
        # Pair responses by id; the message passes through untouched
        # (both result shapes, compressed fields and all). Response
        # types are exactly result / batch_result / file_result — never
        # invent new names (a historical cmd_result hung every call).
        if message.get("type") not in RESPONSE_TYPES:
            return
        req_id = message.get("id")
        entry = self.pending.pop(req_id, None)
        if entry is None:
            return
        future, _owner = entry
        if not future.done():
            future.set_result(message)

    async def _handle_api_request(self, conn: WSConn, message):
        req_id = message.get("req_id", "")
        method = str(message.get("method", "POST")).upper()
        path = str(message.get("path", ""))
        body = message.get("body") or {}
        if isinstance(body, str):
            try:
                body = _json_loads(body)
            except ValueError:
                body = {}
        if not isinstance(body, dict):
            body = {}
        status, obj = await self.handle_api(conn.auth, method, path, body,
                                            {})
        try:
            # body is a JSON-encoded string, not an object (A.5).
            await conn.send_json({
                "type": "api_response",
                "req_id": req_id,
                "status": status,
                "body": _json_dumps(obj),
            })
        except WSClosed:
            pass

    async def _heartbeat(self, conn: WSConn):
        try:
            while not conn.closed:
                await asyncio.sleep(PING_INTERVAL)
                if conn.closed:
                    break
                silence = time.monotonic() - conn.last_activity
                limit = (PC_REAP_TIMEOUT if conn.kind != "controller"
                         else CONTROLLER_REAP_TIMEOUT)
                if silence > limit:
                    log.info("reaping silent %s host=%s",
                             conn.kind or "unknown", conn.hostname)
                    await conn.close(1001, b"ping timeout")
                    break
                await conn.send_ping()
        except (asyncio.CancelledError, WSClosed):
            pass

    def _settle_pending_for(self, conn: WSConn):
        # Fail-fast: never leave callers waiting out a timeout for a PC
        # that is already gone (payload text is contractual, A.2).
        payload = {"ok": False,
                   "error": "Target disconnected before result"}
        for req_id, (future, owner) in list(self.pending.items()):
            if owner is conn:
                del self.pending[req_id]
                if not future.done():
                    future.set_result(payload)

    def _on_ws_disconnect(self, conn: WSConn):
        if conn.kind == "pc" and self.pcs.get(conn.hostname) is conn:
            del self.pcs[conn.hostname]
            log.info("PC offline host=%s", conn.hostname)
        self._settle_pending_for(conn)


_HTTP_REASONS = {
    200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
    404: "Not Found", 409: "Conflict", 503: "Service Unavailable",
    504: "Gateway Timeout",
}


def _parse_target(target: str):
    from urllib.parse import urlsplit, parse_qs
    parts = urlsplit(target)
    return parts.path, parse_qs(parts.query)


async def _serve():
    relay = Relay(
        legacy_token=os.environ.get("MUSE_BRIDGE_TOKEN") or None)
    server = await relay.start(LISTEN_HOST, LISTEN_PORT)
    log.info("muse-bridge %s listening on %s:%d",
             BRIDGE_VERSION, LISTEN_HOST, LISTEN_PORT)
    async with server:
        await server.serve_forever()


def main():
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("MUSE_BRIDGE_LOGLEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
