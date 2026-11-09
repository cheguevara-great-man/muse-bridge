#!/usr/bin/env python3
"""Demultiplexer tests for the patched relay (call-level batch).

Reuses test_relay.py's harness (WSClient / FakePC / http) but loads the
patched muse-bridge.py from this directory.
"""

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_relay as tr  # noqa: E402  (harness only; main() is guarded)

spec = importlib.util.spec_from_file_location(
    "muse_bridge_patched", os.path.join(HERE, "muse-bridge.py"))
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

HOSTNAME = tr.HOSTNAME
GOOD = tr.GOOD_TOKEN


def main():
    controllers = {
        tr.sha(GOOD): {"muse": "muse-test", "pcs": [HOSTNAME],
                       "created": "2026-11-01", "label": "test pair",
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
    threading.Thread(target=loop.run_forever, daemon=True).start()
    print(f"patched relay on 127.0.0.1:{port}")

    pc = None
    try:
        pc = tr.FakePC(port, GOOD)
        pc.start()
        deadline = time.time() + 10
        while True:
            _, obj = tr.http(port, "GET", "/muse-api/status", token=GOOD)
            if obj["clients"]:
                break
            assert time.time() < deadline, "PC never came online"
            time.sleep(0.2)
        tr.check("PC online", True)

        # -- 1. mixed call-level batch --------------------------------
        status, obj = tr.http(
            port, "POST", "/muse-api/batch", token=GOOD, body={
                "target": HOSTNAME,
                "requests": [
                    {"id": "a", "method": "POST", "path": "/cmd",
                     "body": {"command": "echo A"}},
                    {"id": "b", "method": "POST", "path": "/file",
                     "body": {"op": "read", "path": "C:\\x.txt"}},
                    {"id": "c", "method": "POST", "path": "/file",
                     "body": {"op": "list", "path": "C:\\"}},
                    {"id": "d", "method": "GET", "path": "/status",
                     "body": {}},
                ]})
        tr.check("mixed batch HTTP 200", status == 200, f"{status} {obj}")
        tr.check("mixed batch ok", obj.get("ok") is True)
        results = {r["id"]: r for r in obj["results"]}
        tr.check("mixed batch 4 results", len(results) == 4, f"{obj}")
        tr.check("sub a cmd 200", results["a"]["status"] == 200
                 and results["a"]["body"].get("stdout")
                 == "hello from pc: echo A", f"{results.get('a')}")
        tr.check("sub b read 200", results["b"]["status"] == 200
                 and results["b"]["body"].get("data") == "aGVsbG8=",
                 f"{results.get('b')}")
        tr.check("sub c list 200", results["c"]["status"] == 200
                 and results["c"]["body"].get("ok") is True,
                 f"{results.get('c')}")
        tr.check("sub d status 200", results["d"]["status"] == 200
                 and HOSTNAME in results["d"]["body"].get("online", []),
                 f"{results.get('d')}")

        # -- 2. partial-failure isolation ------------------------------
        status, obj = tr.http(
            port, "POST", "/muse-api/batch", token=GOOD, body={
                "target": HOSTNAME,
                "requests": [
                    {"id": "ok1", "method": "POST", "path": "/cmd",
                     "body": {"command": "echo 1"}},
                    {"id": "bad", "method": "GET",
                     "path": "/result/xyz", "body": {}},
                    {"id": "nest", "method": "POST", "path": "/batch",
                     "body": {"requests": [
                         {"id": "x", "method": "POST", "path": "/cmd",
                          "body": {"command": "echo x"}}]}},
                    {"id": "ok2", "method": "POST", "path": "/cmd",
                     "body": {"command": "echo 2"}},
                ]})
        tr.check("isolation HTTP 200", status == 200, f"{status} {obj}")
        results = {r["id"]: r for r in obj["results"]}
        tr.check("ok1 still 200", results["ok1"]["status"] == 200)
        tr.check("bad path -> sub 400", results["bad"]["status"] == 400,
                 f"{results.get('bad')}")
        tr.check("nested -> sub 400", results["nest"]["status"] == 400,
                 f"{results.get('nest')}")
        tr.check("ok2 still 200", results["ok2"]["status"] == 200)

        # -- 3. shape validation ---------------------------------------
        status, obj = tr.http(
            port, "POST", "/muse-api/batch", token=GOOD,
            body={"target": HOSTNAME, "requests": []})
        tr.check("empty requests -> 400", status == 400, f"{status}")
        status, obj = tr.http(
            port, "POST", "/muse-api/batch", token=GOOD, body={
                "target": HOSTNAME,
                "requests": [{"id": str(i), "method": "POST",
                              "path": "/status", "body": {}}
                             for i in range(33)]})
        tr.check("33 subs -> 400", status == 400, f"{status} {obj}")

        # -- 4. legacy regressions --------------------------------------
        status, obj = tr.http(
            port, "POST", "/muse-api/batch", token=GOOD, body={
                "target": HOSTNAME,
                "commands": [{"command": "echo L"}]})
        tr.check("legacy commands batch 200", status == 200
                 and obj.get("type") == "batch_result", f"{status} {obj}")
        status, obj = tr.http(
            port, "POST", "/muse-api/batch", token=GOOD,
            body={"target": HOSTNAME})
        tr.check("no commands/requests -> legacy 400",
                 status == 400
                 and obj.get("error") == "commands list is required",
                 f"{status} {obj}")
        status, obj = tr.http(
            port, "POST", "/muse-api/cmd", token=GOOD,
            body={"command": "echo solo", "target": HOSTNAME})
        tr.check("single /cmd 200", status == 200
                 and obj.get("stdout") == "hello from pc: echo solo",
                 f"{status} {obj}")

        print("ALL DEMUX TESTS PASSED")
    finally:
        if pc is not None:
            pc.stop()
        loop.call_soon_threadsafe(loop.stop)


if __name__ == "__main__":
    main()
