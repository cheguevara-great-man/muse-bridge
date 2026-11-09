"""Synchronous Codex PC tool client. Exchanges files; never executes an action."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import secrets
import sys
import time

from mailbox import MAX_BYTES, manifest_tools, read_json, reject_constant

LIVE_PROTOCOL = "codex-pc-rpc-v1"
MAX_CALLS = 64


def read_result(path, run, sequence, field):
    value = read_json(path)
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or value.get("protocol") != LIVE_PROTOCOL or value.get("run") != run
            or type(value.get("sequence")) is not int or value["sequence"] != sequence
            or not isinstance(value.get("result"), dict)
            or set(value["result"]) != {"type", "call_id", "output"}
            or value["result"]["type"] != ("custom_tool_call_output" if field == "input" else "function_call_output")
            or not isinstance(value["result"]["call_id"], str) or not value["result"]["call_id"]
            or not isinstance(value["result"]["output"], (str, list))):
        raise ValueError("invalid PC tool result")
    return value["result"]


def publish(path, value):
    """Publish complete bytes exclusively, including against another caller."""
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, allow_nan=False)
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def call(root, run, request, *, timeout=600, poll=0.25):
    if not isinstance(run, str) or not re.fullmatch(r"[0-9a-f]{32}", run):
        raise ValueError("invalid run")
    if not math.isfinite(timeout) or not 0 < timeout <= 1800:
        raise ValueError("invalid timeout")
    directory = root / "runs" / run
    manifest = read_json(directory / "manifest.json")
    if (not isinstance(manifest, dict) or manifest.get("run") != run
            or manifest.get("schema_version") != 2
            or manifest.get("protocol") != LIVE_PROTOCOL
            or type(manifest.get("expires_at")) not in (int, float)
            or not math.isfinite(manifest["expires_at"])):
        raise ValueError("invalid PC tool manifest")
    tools = manifest_tools(root, manifest)
    if not isinstance(request, dict) or request.get("name") not in tools:
        raise ValueError("unknown tool")
    field = "input" if tools[request["name"]]["type"] == "custom" else "arguments"
    if set(request) != {"name", field} or not isinstance(request[field], str if field == "input" else dict):
        raise ValueError("invalid tool input")
    deadline = min(manifest["expires_at"], time.time() + timeout)
    if deadline <= time.time():
        raise ValueError("PC tool run expired")
    lock = directory / "call.lock"
    lock.mkdir()  # No concurrent calls and no automatic recovery/replay after a crash.
    try:
        for sequence in range(1, MAX_CALLS + 1):
            path = directory / f"request-{sequence:04d}.json"
            if not path.exists():
                break
            if not (directory / f"result-{sequence:04d}.json").exists():
                raise ValueError("previous call has no confirmed result; not resubmitted")
            previous = read_json(path)
            previous_tool = tools[previous["name"]]
            read_result(directory / f"result-{sequence:04d}.json", run, sequence,
                        "input" if previous_tool["type"] == "custom" else "arguments")
        else:
            raise ValueError("PC tool call limit reached")
        publish(path, {"schema_version": 1, "protocol": LIVE_PROTOCOL, "run": run,
                       "sequence": sequence, "expires_at": deadline, **request})
        result_path = directory / f"result-{sequence:04d}.json"
        # Stay in this native cloud tool call, exactly as a remote RPC client does.
        while time.time() < deadline:
            if result_path.exists():
                return read_result(result_path, run, sequence, field)
            time.sleep(min(poll, max(0, deadline - time.time())))
        raise ValueError("PC tool result timed out; outcome unknown; do not replay the action")
    finally:
        lock.rmdir()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("oversized request")
        request = json.loads(raw, parse_constant=reject_constant)
        result = call(Path(__file__).resolve().parents[1], args.run, request, timeout=args.timeout)
        sys.stdout.buffer.write((json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))
    except (OSError, ValueError, TypeError, KeyError):
        parser.exit(2, "PC tool call failed or expired; check the client before retrying.\n")


if __name__ == "__main__":
    main()

