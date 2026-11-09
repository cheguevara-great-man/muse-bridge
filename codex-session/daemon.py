#!/usr/bin/env python3
"""Warm-connection daemon for the Muse Bridge (Scheme B) connector.

client.py is a short-lived process: every invocation used to pay a
fresh proxy CONNECT + TLS handshake to the VPS, on a network path that
intermittently black-holes new flows. This daemon holds a small
adaptive pool of already-initialized MCP connections (the same
transport and OAuth machinery as client.py, reused by import) and
serves client.py shims over a Unix socket in the connector state dir.

Pool policy (env-tunable):
  MUSE_MCP_POOL_MIN   floor of warm connections        (default 3)
  MUSE_MCP_POOL_MAX   hard cap                         (default 5)
  MUSE_MCP_POOL_SHRINK idle seconds before shrinking  (default 180)

Growth is demand-driven: a request that finds every connection busy
opens a new one (up to the cap) instead of queueing; further requests
queue on the pool. The maintainer thread re-warms the floor one
connection at a time (never a burst) and closes connections that stay
idle past the shrink threshold beyond the floor.

Safety rule inherited from client.py: a tool call is dispatched
exactly once. If it fails mid-flight the connection is discarded and
the error is reported as executed/unknown — never replayed.
"""
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import client as bridge  # noqa: E402  (the connector client module)

STATE_DIR = bridge.STATE_DIR
SOCK_PATH = os.path.join(STATE_DIR, "daemon.sock")
PID_PATH = os.path.join(STATE_DIR, "daemon.pid")
LOG_PATH = os.path.join(STATE_DIR, "daemon.log")

POOL_MIN = int(os.environ.get("MUSE_MCP_POOL_MIN", "3"))
POOL_MAX = int(os.environ.get("MUSE_MCP_POOL_MAX", "5"))
POOL_SHRINK = float(os.environ.get("MUSE_MCP_POOL_SHRINK", "180"))


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [mcp-daemon] {msg}\n"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


class Entry:
    __slots__ = ("mcp", "busy", "last_used")

    def __init__(self):
        self.mcp = bridge.Mcp()
        self.busy = True  # born busy: the creator initializes it
        self.last_used = time.monotonic()


class Pool:
    def __init__(self):
        self.cond = threading.Condition()
        self.entries = []
        self.auth_lock = threading.Lock()  # serialize MCP initialize/OAuth
        self.stopping = False

    # -- creation ------------------------------------------------------
    def _new_entry_locked(self):
        entry = Entry()
        self.entries.append(entry)
        return entry

    def _init_entry(self, entry):
        """Connect + initialize a fresh entry (serialized for OAuth)."""
        with self.auth_lock:
            entry.mcp.initialize()
        entry.last_used = time.monotonic()

    # -- checkout / checkin --------------------------------------------
    def checkout(self, timeout=700):
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                for entry in self.entries:
                    if not entry.busy:
                        entry.busy = True
                        return entry
                if len(self.entries) < POOL_MAX:
                    entry = self._new_entry_locked()
                    break  # initialize outside the pool lock
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.cond.wait(timeout=min(remaining, 30))
        try:
            self._init_entry(entry)
        except Exception:
            with self.cond:
                self.entries.remove(entry)
                self.cond.notify_all()
            raise
        log(f"pool grew to {len(self.entries)}")
        return entry

    def checkin(self, entry, healthy=True):
        with self.cond:
            if entry in self.entries:
                if healthy:
                    entry.busy = False
                    entry.last_used = time.monotonic()
                else:
                    self.entries.remove(entry)
                    try:
                        entry.mcp.http.conn and entry.mcp.http.conn.close()
                    except Exception:
                        pass
                    log(f"pool discarded a connection ({len(self.entries)} left)")
            self.cond.notify_all()

    # -- maintainer ------------------------------------------------------
    def maintainer(self):
        while not self.stopping:
            time.sleep(10)
            try:
                self._maintain()
            except Exception as exc:  # never let the loop die
                log(f"maintainer error: {exc}")

    def _maintain(self):
        # Shrink: drop entries beyond the floor that stayed idle.
        with self.cond:
            now = time.monotonic()
            idle = [e for e in self.entries if not e.busy]
            excess = len(self.entries) - POOL_MIN
            victim = None
            if excess > 0:
                for entry in idle:
                    if now - entry.last_used > POOL_SHRINK:
                        victim = entry
                        break
            if victim is not None:
                self.entries.remove(victim)
        if victim is not None:
            try:
                victim.mcp.http.conn and victim.mcp.http.conn.close()
            except Exception:
                pass
            log(f"pool shrank to {len(self.entries)}")
            return  # one change per pass; re-warm next pass if needed
        # Keepalive: the far end closes idle connections (nginx
        # keepalive timeout), and a pooled connection discovers that
        # only by failing the next real call. Keep floor entries live
        # with a cheap relay-status call once they pass ~40 s idle;
        # a failure is absorbed here (discard + re-warm below) instead
        # of surfacing on a user call. Excess entries are skipped so
        # they can still age out via the shrink path.
        with self.cond:
            now = time.monotonic()
            target = None
            for entry in self.entries[:POOL_MIN]:
                if not entry.busy and now - entry.last_used > 40:
                    entry.busy = True
                    target = entry
                    break
        if target is not None:
            try:
                target.mcp.call_tool("status", {})
                self.checkin(target, healthy=True)
            except Exception:
                self.checkin(target, healthy=False)
            return
        # Re-warm the floor, one connection per pass (no bursts).
        with self.cond:
            need = len(self.entries) < POOL_MIN
            entry = self._new_entry_locked() if need else None
        if entry is not None:
            try:
                self._init_entry(entry)
                with self.cond:
                    entry.busy = False
                    self.cond.notify_all()
                log(f"pool warmed to {len(self.entries)}")
            except Exception as exc:
                with self.cond:
                    if entry in self.entries:
                        self.entries.remove(entry)
                log(f"warm failed (will retry): {exc}")

    def stats(self):
        with self.cond:
            busy = sum(1 for e in self.entries if e.busy)
            return {"pool_total": len(self.entries), "pool_busy": busy,
                    "pool_min": POOL_MIN, "pool_max": POOL_MAX}


POOL = Pool()


def handle_request(req):
    """Returns (response_dict, executed_flag)."""
    op = req.get("op")
    if op == "ping":
        return {"ok": True, "result": "pong"}, False
    if op == "stats":
        return {"ok": True, "result": POOL.stats()}, False
    if op not in ("call_tool", "list_tools"):
        return {"ok": False, "error": f"unknown op: {op}",
                "executed": False}, False
    if op == "call_tool" and req.get("name") not in bridge.ALLOWED_TOOLS:
        return {"ok": False, "error": f"tool not allowed: {req.get('name')}",
                "executed": False}, False
    entry = POOL.checkout()
    if entry is None:
        return {"ok": False, "error": "pool exhausted (checkout timeout)",
                "executed": False}, False
    try:
        if op == "list_tools":
            result = entry.mcp.list_tools()
        else:
            result = entry.mcp.call_tool(req["name"], req.get("arguments") or {})
        POOL.checkin(entry, healthy=True)
        return {"ok": True, "result": result}, True
    except Exception as exc:
        # The call may have executed; drop the connection, never replay.
        POOL.checkin(entry, healthy=False)
        return {"ok": False, "error": str(exc), "executed": True}, True


def serve_client(conn):
    try:
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
            if len(buf) > 4 * 1024 * 1024:
                return
        req = json.loads(buf.split(b"\n", 1)[0])
        resp, _ = handle_request(req)
        conn.sendall(json.dumps(resp).encode() + b"\n")
    except Exception as exc:
        try:
            conn.sendall(json.dumps(
                {"ok": False, "error": f"daemon error: {exc}",
                 "executed": True}).encode() + b"\n")
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main():
    os.makedirs(STATE_DIR, exist_ok=True)
    # Refuse to start a second daemon: if the socket answers, one lives.
    if os.path.exists(SOCK_PATH):
        try:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(2)
            probe.connect(SOCK_PATH)
            probe.close()
            log("another daemon is already serving; exiting")
            return 0
        except OSError:
            try:
                os.unlink(SOCK_PATH)
            except OSError:
                pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, 0o600)
    server.listen(16)
    with open(PID_PATH, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    os.chmod(PID_PATH, 0o600)
    threading.Thread(target=POOL.maintainer, daemon=True,
                     name="pool-maintainer").start()
    log(f"daemon up (pid {os.getpid()}, pool floor {POOL_MIN}, cap {POOL_MAX})")
    try:
        while True:
            conn, _ = server.accept()
            threading.Thread(target=serve_client, args=(conn,),
                             daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        POOL.stopping = True
        server.close()
        for path in (SOCK_PATH, PID_PATH):
            try:
                os.unlink(path)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
