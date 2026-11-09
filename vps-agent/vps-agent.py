#!/usr/bin/env python3
"""Muse Bridge agent for the VPS endpoint (Scheme A pattern, Linux).

Minimal re-implementation of the PC-side bridge protocol from the spec in
~/workspace/muse-bridge-deploy/DESIGN.md (sections A.4/A.6) and the relay
source (relay/muse-bridge.py). The original plugin core
(src/muse/muse-bridge-client.js) only exists on ${PC_NAME}, so this agent
implements the same wire protocol from scratch: stdlib only, no deps.

Behaviour:
  * Connects to the relay WebSocket at /muse-bridge (default
    ws://127.0.0.1:8471/muse-bridge — the relay lives on this same host,
    so loopback, no TLS/nginx/SNI involved).
  * Authenticates at the upgrade with the X-Bridge-Token header; the token
    is read from a local 0600 file and never logged.
  * First frame is the hello (hostname must be inside the token's pcs).
  * Answers relay pings (15 s) with pongs; reconnects with the documented
    backoff (2 s, x1.5, cap 30 s). 401/403 at handshake = fatal (exit 2):
    fix the token, do not retry harder.
  * Dispatches cmd / batch / read_file / write_file / edit_file /
    list_dir / file_info / glob_files / grep_files and answers with the
    contractual result shapes (result / batch_result / file_result).
    stdout/stderr are always plain strings (the deployed controller has
    compression off; inbound deflate field objects are still decoded).

Configuration: JSON file (default /etc/muse-bridge-agent/config.json):
  { "relay_url": "ws://127.0.0.1:8471/muse-bridge",
    "token_file": "/etc/muse-bridge-agent/token",
    "hostname": "${VPS_NAME}",
    "workspace_root": "/root",
    "read_only": false }
"""

import argparse
import asyncio
import base64
import fnmatch
import glob as glob_mod
import hashlib
import json
import os
import platform
import re
import struct
import subprocess
import sys
import time
import zlib

CLIENT_DESC = "muse-bridge-vps/1.0 (python-stdlib)"
MAX_STREAM = 8 * 1024 * 1024          # per-stream capture cap for cmd
MAX_FRAME = 64 * 1024 * 1024          # relay frame cap


def log(msg):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} [vps-agent] {msg}",
          file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Field compression (DESIGN.md A.6): decode inbound, never encode outbound.
# ---------------------------------------------------------------------------

def decode_field(value):
    """Return the plain value for a possibly-compressed field object."""
    if isinstance(value, dict) and ("enc" in value or value.get("compressed")):
        enc = value.get("enc") or "deflate"
        data = base64.b64decode(value.get("data", ""))
        if enc == "deflate":
            return zlib.decompress(data).decode("utf-8")
        raise ValueError(f"[DecompressError] unsupported encoding {enc!r}")
    return value


# ---------------------------------------------------------------------------
# WebSocket client (hand-rolled RFC 6455, client frames masked)
# ---------------------------------------------------------------------------

class WSError(Exception):
    pass


class FatalAuth(WSError):
    pass


class WSClient:
    def __init__(self, url, token):
        m = re.match(r"ws://([^/:]+)(?::(\d+))?(/.*)?$", url)
        if not m:
            raise ValueError(f"only ws:// relay URLs supported, got {url!r}")
        self.host = m.group(1)
        self.port = int(m.group(2) or 80)
        self.path = m.group(3) or "/muse-bridge"
        self.token = token
        self.reader = None
        self.writer = None
        self._send_lock = asyncio.Lock()

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection(
            self.host, self.port)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"X-Bridge-Token: {self.token}\r\n"
            "\r\n"
        )
        self.writer.write(request.encode("latin-1"))
        await self.writer.drain()
        # Read status line + headers up to the blank line.
        raw = await self.reader.readuntil(b"\r\n\r\n")
        head = raw.decode("latin-1", "replace")
        status_line = head.split("\r\n", 1)[0]
        if " 101 " not in status_line:
            code = status_line.split(" ")[1] if " " in status_line else "?"
            if code in ("401", "403"):
                raise FatalAuth(f"relay handshake rejected: {status_line}")
            raise WSError(f"relay handshake failed: {status_line}")

    async def send_frame(self, opcode, payload: bytes):
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
        async with self._send_lock:
            self.writer.write(bytes(header) + mask + masked)
            await self.writer.drain()

    async def send_json(self, obj):
        await self.send_frame(0x1, json.dumps(
            obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    async def read_frame(self):
        head = await self.reader.readexactly(2)
        fin = head[0] & 0x80
        opcode = head[0] & 0x0F
        masked = head[1] & 0x80
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", await self.reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await self.reader.readexactly(8))[0]
        mask_key = await self.reader.readexactly(4) if masked else b""
        if length > MAX_FRAME:
            raise WSError("frame too large")
        payload = await self.reader.readexactly(length) if length else b""
        if masked:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        if not fin:
            raise WSError("fragmented frames unsupported")
        return opcode, payload

    def close(self):
        try:
            if self.writer:
                self.writer.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Execution handlers
# ---------------------------------------------------------------------------

def clamp_timeout(value, default=60, hi=600):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(hi, value))


class Agent:
    def __init__(self, cfg):
        self.cfg = cfg
        self.workspace = cfg.get("workspace_root") or os.path.expanduser("~")
        self.read_only = cfg.get("read_only") is True

    def resolve(self, path):
        if not path:
            return self.workspace
        path = os.path.expanduser(str(path))
        if not os.path.isabs(path):
            path = os.path.join(self.workspace, path)
        return os.path.normpath(path)

    # ---- cmd / batch -------------------------------------------------
    async def run_one(self, command, cwd, timeout):
        if "\x00" in command:
            return {"exit": -1, "stdout": "",
                    "stderr": "command contains a null byte"}
        workdir = self.resolve(cwd) if cwd else self.workspace
        if not os.path.isdir(workdir):
            return {"exit": -1, "stdout": "",
                    "stderr": f"cwd does not exist: {workdir}"}
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", "-c", command, cwd=workdir,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
            out, err = await proc.communicate()
            return {"exit": -1,
                    "stdout": out[:MAX_STREAM].decode("utf-8", "replace"),
                    "stderr": (err[:MAX_STREAM].decode("utf-8", "replace")
                               + f"\n[command timed out after {timeout:g}s]")}
        return {"exit": proc.returncode,
                "stdout": out[:MAX_STREAM].decode("utf-8", "replace"),
                "stderr": err[:MAX_STREAM].decode("utf-8", "replace")}

    async def handle_cmd(self, msg):
        command = decode_field(msg.get("command", ""))
        timeout = clamp_timeout(msg.get("timeout"), 60)
        r = await self.run_one(str(command), msg.get("cwd"), timeout)
        if r["exit"] == -1 and not r["stdout"]:
            return {"type": "result", "id": msg["id"], "exit": -1,
                    "stdout": "", "stderr": r["stderr"]}
        return {"type": "result", "id": msg["id"], "ok": r["exit"] == 0,
                "exitCode": r["exit"], "stdout": r["stdout"],
                "stderr": r["stderr"]}

    async def handle_batch(self, msg):
        commands = msg.get("commands")
        if isinstance(commands, (str, dict)):     # possibly compressed field
            commands = json.loads(decode_field(commands))
        results = []
        for item in commands or []:
            if not isinstance(item, dict):
                continue
            timeout = clamp_timeout(item.get("timeout"), 60, hi=300)
            results.append(await self.run_one(
                str(item.get("command", "")), item.get("cwd"), timeout))
        return {"type": "batch_result", "id": msg["id"], "ok": True,
                "results": results}

    # ---- files ---------------------------------------------------------
    def handle_read(self, msg):
        path = self.resolve(msg.get("path"))
        try:
            raw = open(path, "rb").read()
        except OSError as exc:
            return {"type": "file_result", "id": msg["id"], "ok": False,
                    "error": str(exc)}
        offset, limit = msg.get("offset"), msg.get("limit")
        if offset is not None or limit is not None:
            lines = raw.decode("utf-8", "replace").splitlines()
            start = max(0, int(offset or 0))
            end = start + int(limit) if limit else len(lines)
            sliced = lines[start:end]
            return {"type": "file_result", "id": msg["id"], "ok": True,
                    "text": "\n".join(sliced), "start_line": start,
                    "lines_returned": len(sliced), "total_lines": len(lines),
                    "bytes": len(raw)}
        return {"type": "file_result", "id": msg["id"], "ok": True,
                "data": base64.b64encode(raw).decode("ascii"),
                "bytes": len(raw),
                "hash": hashlib.sha256(raw).hexdigest()[:16]}

    def handle_write(self, msg):
        if self.read_only:
            return {"type": "file_result", "id": msg["id"], "ok": False,
                    "error": "[PermissionDenied] agent is read-only"}
        path = self.resolve(msg.get("path"))
        try:
            if msg.get("data") is not None:
                data_field = msg["data"]
                if isinstance(data_field, dict):
                    raw = decode_field(data_field)
                    raw = raw.encode("utf-8") if isinstance(raw, str) else raw
                else:
                    raw = base64.b64decode(data_field)
            else:
                content = decode_field(msg.get("content", ""))
                raw = str(content).encode("utf-8")
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(raw)
        except (OSError, ValueError) as exc:
            return {"type": "file_result", "id": msg["id"], "ok": False,
                    "error": str(exc)}
        return {"type": "file_result", "id": msg["id"], "ok": True,
                "bytes": len(raw),
                "hash": hashlib.sha256(raw).hexdigest()[:16],
                "version": int(os.path.getmtime(path) * 1000),
                "message": f"wrote {len(raw)} bytes to {path}"}

    def handle_edit(self, msg):
        if self.read_only:
            return {"type": "file_result", "id": msg["id"], "ok": False,
                    "error": "[PermissionDenied] agent is read-only"}
        path = self.resolve(msg.get("path"))
        old = msg.get("old_string", msg.get("target", ""))
        new = msg.get("new_string", msg.get("replacement", ""))
        try:
            text = open(path, "r", encoding="utf-8").read()
        except OSError as exc:
            return {"type": "file_result", "id": msg["id"], "ok": False,
                    "error": str(exc)}
        if old not in text:
            return {"type": "file_result", "id": msg["id"], "ok": False,
                    "error": "old_string not found in file"}
        if msg.get("replace_all"):
            replaced = text.count(old)
            text = text.replace(old, new)
        else:
            replaced = 1
            text = text.replace(old, new, 1)
        raw = text.encode("utf-8")
        with open(path, "wb") as fh:
            fh.write(raw)
        return {"type": "file_result", "id": msg["id"], "ok": True,
                "replaced": replaced, "newBytes": len(raw),
                "lines": text.count("\n") + 1,
                "hash": hashlib.sha256(raw).hexdigest()[:16],
                "version": int(os.path.getmtime(path) * 1000),
                "message": f"replaced {replaced} occurrence(s)"}

    def handle_list(self, msg):
        path = self.resolve(msg.get("path"))
        try:
            names = sorted(os.listdir(path))
        except OSError as exc:
            return {"type": "file_result", "id": msg["id"], "ok": False,
                    "error": str(exc)}
        entries = [{"name": n} for n in names]
        return {"type": "file_result", "id": msg["id"], "ok": True,
                "entries": entries, "total": len(entries),
                "formattedText": "\n".join(names)}

    def handle_info(self, msg):
        path = self.resolve(msg.get("path"))
        try:
            st = os.stat(path)
        except OSError as exc:
            return {"type": "file_result", "id": msg["id"], "ok": False,
                    "error": str(exc)}
        return {"type": "file_result", "id": msg["id"], "ok": True,
                "size": st.st_size, "mtime": int(st.st_mtime * 1000),
                "is_file": os.path.isfile(path),
                "is_dir": os.path.isdir(path)}

    def handle_glob(self, msg):
        base = self.resolve(msg.get("path"))
        pattern = str(msg.get("pattern", "**/*"))
        max_results = int(msg.get("max_results",
                                  msg.get("head_limit", 100)) or 100)
        found = []
        for hit in glob_mod.glob(os.path.join(base, pattern),
                                 recursive=True):
            found.append(os.path.relpath(hit, base))
            if len(found) >= max_results:
                break
        return {"type": "file_result", "id": msg["id"], "ok": True,
                "matches": sorted(found), "total": len(found)}

    def handle_grep(self, msg):
        base = self.resolve(msg.get("path"))
        pattern = str(msg.get("pattern", ""))
        include = msg.get("include", msg.get("glob"))
        flags = 0 if msg.get("case_sensitive") else re.IGNORECASE
        max_results = int(msg.get("max_results", 100) or 100)
        try:
            rx = re.compile(pattern, flags)
        except re.error as exc:
            return {"type": "file_result", "id": msg["id"], "ok": False,
                    "error": f"bad pattern: {exc}"}
        matches = []
        files = [base] if os.path.isfile(base) else [
            os.path.join(root, name)
            for root, _dirs, names in os.walk(base) for name in names]
        for fpath in files:
            if include and not fnmatch.fnmatch(
                    os.path.basename(fpath), str(include)):
                continue
            try:
                with open(fpath, "r", encoding="utf-8",
                          errors="replace") as fh:
                    for lineno, line in enumerate(fh, 1):
                        if rx.search(line):
                            matches.append({
                                "file": os.path.relpath(fpath, base)
                                if os.path.isdir(base) else fpath,
                                "line": lineno,
                                "text": line.rstrip("\n")[:300]})
                        if len(matches) >= max_results:
                            break
            except OSError:
                continue
            if len(matches) >= max_results:
                break
        return {"type": "file_result", "id": msg["id"], "ok": True,
                "matches": matches, "total": len(matches)}

    # ---- dispatch ------------------------------------------------------
    async def dispatch(self, ws, msg):
        mtype = str(msg.get("type", "")).lower()
        try:
            if mtype in ("cmd", "bash"):
                result = await self.handle_cmd(msg)
            elif mtype == "batch":
                result = await self.handle_batch(msg)
            elif mtype in ("read_file", "read"):
                result = self.handle_read(msg)
            elif mtype in ("write_file", "write"):
                result = self.handle_write(msg)
            elif mtype in ("edit_file", "edit"):
                result = self.handle_edit(msg)
            elif mtype in ("list_dir", "ls"):
                result = self.handle_list(msg)
            elif mtype in ("file_info", "stat"):
                result = self.handle_info(msg)
            elif mtype in ("glob_files", "glob"):
                result = self.handle_glob(msg)
            elif mtype in ("grep_files", "grep"):
                result = self.handle_grep(msg)
            elif mtype in ("archive_7z", "extract_7z", "archive", "extract"):
                result = {"type": "file_result", "id": msg.get("id"),
                          "ok": False,
                          "error": "7z operations not supported by "
                                   "the VPS agent"}
            else:
                return            # unknown types are ignored (spec A.4)
        except ValueError as exc:  # decompression failures etc.
            result = {"type": "result", "id": msg.get("id"), "exit": -1,
                      "stdout": "", "stderr": str(exc)}
        except Exception as exc:   # never let one request kill the session
            result = {"type": "file_result", "id": msg.get("id"),
                      "ok": False, "error": f"agent error: {exc}"}
        await ws.send_json(result)


async def session(cfg, agent):
    ws = WSClient(cfg.get("relay_url", "ws://127.0.0.1:8471/muse-bridge"),
                  agent_token(cfg))
    await ws.connect()
    hello = {"type": "hello", "client": CLIENT_DESC,
             "hostname": cfg.get("hostname", "${VPS_NAME}"),
             "platform": "linux", "arch": platform.machine()}
    await ws.send_json(hello)
    log(f"connected, hello sent as {hello['hostname']}")
    tasks = set()
    try:
        while True:
            opcode, payload = await ws.read_frame()
            if opcode == 0x9:
                await ws.send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                raise WSError("relay closed the connection")
            if opcode != 0x1:
                continue
            try:
                msg = json.loads(payload.decode("utf-8"))
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id"):
                task = asyncio.ensure_future(agent.dispatch(ws, msg))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
    finally:
        ws.close()


def agent_token(cfg):
    token_file = cfg.get("token_file", "/etc/muse-bridge-agent/token")
    with open(token_file, "r", encoding="utf-8") as fh:
        return fh.read().strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",
                        default="/etc/muse-bridge-agent/config.json")
    args = parser.parse_args()
    with open(args.config, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not agent_token(cfg):
        log("token file missing or empty — fix the token, exiting")
        return 2
    agent = Agent(cfg)
    delay = 2.0
    while True:
        try:
            asyncio.run(session(cfg, agent))
            delay = 2.0
        except FatalAuth:
            log("relay rejected the token (401/403) — exiting, not retrying")
            return 2
        except (WSError, OSError, asyncio.IncompleteReadError) as exc:
            log(f"connection lost: {exc}; reconnect in {delay:.0f}s")
        time.sleep(delay)
        delay = min(30.0, delay * 1.5)


if __name__ == "__main__":
    raise SystemExit(main())
