"""Opt-in synchronous PC RPC adapter. Never executes tools or contacts a relay.

Inspired by remote PC clients: Muse's native cloud tool waits for the real
Codex result, instead of ending its agent turn and reading a result next turn.
The filesystem is the private transport; no new public listener is required.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
from pathlib import Path
import secrets
import threading
import time
from dataclasses import dataclass, field

from .deepseek_harness import _last_completed_response, _responses_events
from .muse_network import configure_network
from .muse_transport import (MUSE_CHAT_INPUT_MAX_WIRE_BYTES, MUSE_CHAT_REQUEST_MAX_BYTES,
                             chat_request_bytes, send_chat)
from .muse_service import MuseSessions, ServiceError, Session, gateway_factory, make_server
from .muse_tools import PROTOCOL, MailboxCompatibilityError, MailboxError, WorkspaceTools

LIVE_PROTOCOL = "codex-pc-rpc-v1"
_LOG = logging.getLogger(__name__)
MAX_CALLS = 64
PC_POLL_SECONDS = 0.25
PC_CONNECTION_IDLE_SECONDS = 300


class PCWorkspace(WorkspaceTools):
    def check_compatible(self, gateway):
        super().check_compatible(gateway)
        source = self.read_bytes(gateway, self.root + "/scripts/pc.py", optional=True)
        if source is None or b'LIVE_PROTOCOL = "codex-pc-rpc-v1"' not in source:
            raise MailboxCompatibilityError("PC RPC helper is not installed")

    def numbered_path(self, run, kind, sequence):
        self.path(run, "manifest")  # Apply the same strict run validation.
        if kind not in {"request", "result"} or type(sequence) is not int or not 1 <= sequence <= MAX_CALLS:
            raise MailboxError("Invalid PC RPC identity")
        return f"{self.root}/runs/{run}/{kind}-{sequence:04d}.json"

    def prepare(self, gateway, tools, timeout):
        digest = self.catalog(gateway, tools)
        run = secrets.token_hex(16)
        self.write(gateway, self.path(run, "manifest"), {
            "schema_version": 2, "protocol": LIVE_PROTOCOL, "run": run,
            "expires_at": time.time() + timeout, "catalog_sha256": digest,
        })
        return run

    def prompt(self, run, previous_run=""):
        return ("Codex PC tool connection for this task. Connection/run: " + run + ".\n"
                f"The helper is documented at ~/{self.root}/SKILL.md. "
                "It requests tools from the user's Codex client and waits for actual results; "
                "the client owns approval and execution. It does not execute anything in the cloud.\n"
                f"List tools: python3 ~/{self.root}/scripts/mailbox.py tools --run {run}\n"
                f"Read a definition: python3 ~/{self.root}/scripts/mailbox.py tools --run {run} --name <name>\n"
                f"Call: python3 ~/{self.root}/scripts/pc.py --run {run} (JSON request on stdin).\n"
                "Use only tools needed for the user's task. The call waits; after it returns, continue "
                "from its actual result in this same turn. If the cloud command runner yields a still-running "
                "process/session handle, use its normal process-wait facility until pc.py exits; a running "
                "handle is not the tool result. Do not finish with an 'awaiting Codex' message. "
                "Respect all permission checks. If this "
                "connection is refused or an outcome is uncertain, report that instead of working around it.")

    def live_request(self, gateway, run, sequence, tools):
        value = self.read(gateway, self.numbered_path(run, "request", sequence), optional=True)
        return self.validate_request(value, run, sequence, tools)

    @staticmethod
    def validate_request(value, run, sequence, tools):
        if value is None:
            return None
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or value.get("protocol") != LIVE_PROTOCOL or value.get("run") != run
                or type(value.get("sequence")) is not int or value["sequence"] != sequence
                or type(value.get("expires_at")) not in (int, float)
                or not math.isfinite(value["expires_at"]) or value["expires_at"] <= time.time()
                or value["expires_at"] > time.time() + 1801):
            raise MailboxError("Invalid or expired PC RPC request")
        definition = next((tool for tool in tools if tool["name"] == value.get("name")), None)
        if definition is None:
            raise MailboxError("Unavailable PC tool")
        custom = definition["type"] == "custom"
        field = "input" if custom else "arguments"
        if (set(value) != {"schema_version", "protocol", "run", "sequence", "expires_at", "name", field}
                or not isinstance(value[field], str if custom else dict)):
            raise MailboxError("Invalid PC tool arguments")
        return {"name": value["name"], field: value[field], "freeform": custom}, value["expires_at"]

    def publish_result(self, gateway, state, result):
        self.write(gateway, self.numbered_path(state.run, "result", state.sequence), {
            "schema_version": 1, "protocol": LIVE_PROTOCOL, "run": state.run,
            "sequence": state.sequence, "result": result,
        })


@dataclass
class PCTurn(Session):
    run: str = ""
    baseline: int = 0
    sequence: int = 1
    history_after: int = 0
    submitted_text: str | None = field(default=None, repr=False)
    gateway: object | None = field(default=None, repr=False)
    gateway_timer: threading.Timer | None = field(default=None, repr=False)


class PCSessions(MuseSessions):
    def __init__(self, factory, **kwargs):
        kwargs.setdefault("poll", PC_POLL_SECONDS)
        super().__init__(factory, **kwargs)
        if self.workspace is None:
            raise ValueError("PC RPC requires its own explicit tool workspace")
        self.workspace = PCWorkspace(self.workspace.root)
        self._closed = False

    def _restore_completed_state(self, row):
        return PCTurn(session_id=row["session_id"], head=row["response_id"],
                      restored_digest=row["digest"],
                      history_after=row["history_after"])

    @staticmethod
    def _take_gateway(state):
        # The conversation lock owns the socket. Never share a Noise stream between
        # requests/threads, and detach the timer before the next RPC uses it.
        if state.gateway_timer is not None:
            state.gateway_timer.cancel()
            state.gateway_timer = None
        gateway, state.gateway = state.gateway, None
        return gateway

    def _close_gateway(self, state):
        gateway = self._take_gateway(state)
        if gateway is not None:
            gateway.close()

    def _park_gateway(self, state, gateway, *, idle_seconds=None):
        idle = PC_CONNECTION_IDLE_SECONDS if idle_seconds is None else idle_seconds
        if state.pending is not None:
            idle = min(idle, state.pending["expires"] - time.monotonic())
        state.gateway = gateway
        if self._closed or idle <= 0:
            self._close_gateway(state)
            return
        timer = threading.Timer(idle, lambda: self._expire_gateway(state, timer))
        timer.daemon = True
        state.gateway_timer = timer
        try:
            timer.start()
        except Exception:
            self._close_gateway(state)
            raise

    def _expire_gateway(self, state, timer):
        with state.lock:
            # A cancelled timer may already be waiting for the lock. It must not
            # close the same socket after a later round renewed its idle lease.
            if state.gateway_timer is timer:
                self._close_gateway(state)

    def close(self):
        self._closed = True
        with self.lock:
            states = {id(state): state for state in self.snapshots.values()}
        for state in states.values():
            if isinstance(state, PCTurn) and state.lock.acquire(blocking=False):
                try:
                    self._close_gateway(state)
                finally:
                    state.lock.release()

    @staticmethod
    def _history(gateway, state, timeout):
        value = gateway.call_json("chat.history", body={
            "session_id": state.session_id, "limit": 50, "after_seq": state.history_after,
        }, timeout=timeout)
        events = value.get("chat_events") if isinstance(value, dict) else None
        if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
            raise ServiceError("Muse returned an invalid conversation history")
        # Only advance past our exact submitted user message. Keep every
        # assistant event in this turn: in-progress/completed statuses can be
        # updated in place under the SAME seq, so a newest-event cursor is unsafe.
        if state.submitted_text is not None:
            for event in events:
                payload = event.get("payload")
                if (event.get("event_name") == "message.user" and isinstance(payload, dict)
                        and payload.get("content") == state.submitted_text
                        and type(event.get("seq")) is int and event["seq"] > state.history_after):
                    state.history_after = event["seq"]
                    # Delayed completion events from the previous turn must not
                    # be accepted as this new user task's answer.
                    state.baseline = max(state.baseline, event["seq"])
                    state.submitted_text = None
                    break
        return events

    def generate(self, body, progress=lambda: None):
        if self._closed:
            raise ServiceError("Muse PC service is stopping", 503)
        if body.get("tool_protocol") != PROTOCOL:
            previous = body.get("previous_response_id")
            with self.lock:
                state = self.snapshots.get(previous) if isinstance(previous, str) else None
            if state is not None and state.pending:
                raise ServiceError("PC RPC requires its matching tool result", 409, "muse_web_tool_result_invalid")
            return super().generate(body, progress)
        tools, results = body.get("tools"), body.get("tool_results", [])
        if (body.get("model") != "default" or not isinstance(body.get("input"), str)
                or not body["input"].strip() or body.get("reasoning")
                or not isinstance(tools, list) or not tools or any(not isinstance(t, dict)
                    or t.get("type") not in {"function", "custom"} or not isinstance(t.get("name"), str)
                    or not t["name"] for t in tools)
                or len({t["name"] for t in tools}) != len(tools) or not isinstance(results, list)):
            raise ServiceError("Invalid PC RPC request", 400)
        previous = body.get("previous_response_id")
        if previous is not None and (not isinstance(previous, str) or not previous):
            raise ServiceError("Invalid previous_response_id", 400)
        # Fail before reserving a relay or starting a cloud conversation. The
        # The transport sends one Noise frame. Include room for the PC helper
        # and RPC envelope, while measuring UTF-8 rather than character count.
        if len(json.dumps(body["input"], ensure_ascii=False).encode("utf-8")) > MUSE_CHAT_INPUT_MAX_WIRE_BYTES:
            raise ServiceError("Muse input exceeds the tested single-message budget; no message sent", 413,
                               "muse_web_request_too_large")
        with self.lock:
            state = self.snapshots.get(previous) if previous else None
            if (previous and isinstance(state, Session) and not isinstance(state, PCTurn)
                    and state.valid and state.head == previous and state.pending is None):
                # History imports use the text-only Muse protocol. Their final
                # response is still an authorized checkpoint for the SAME VM
                # conversation; promote it before starting the first PC turn.
                # Reuse the lock and all response aliases, never replay history.
                if not state.lock.acquire(blocking=False):
                    raise ServiceError("Muse conversation is already generating", 409, "muse_web_busy")
                try:
                    upgraded = PCTurn(session_id=state.session_id, head=state.head,
                                      valid=state.valid, pending=None, lock=state.lock)
                    for response_id, candidate in list(self.snapshots.items()):
                        if candidate is state:
                            self.snapshots[response_id] = upgraded
                    state = upgraded
                finally:
                    state.lock.release()
        if previous and (not isinstance(state, PCTurn) or not state.valid or state.head != previous):
            raise ServiceError("Previous PC response not found; no automatic replay", 404, "muse_web_session_missing")
        if results and (state is None or state.pending is None):
            raise ServiceError("No pending PC call for this result", 409, "muse_web_tool_result_invalid")
        if not self.slots.acquire(blocking=False):
            raise ServiceError("Muse parallel request limit reached", 429, "muse_web_busy")
        gateway, acquired, submitted, keep_gateway = None, False, False, False
        prepared_run = ""
        stage = "connect"
        try:
            if state is not None:
                acquired = state.lock.acquire(blocking=False)
                if not acquired:
                    raise ServiceError("Muse conversation is already generating", 409, "muse_web_busy")
                if not state.valid or state.head != previous:
                    raise ServiceError("Previous PC response not found", 404, "muse_web_session_missing")
            pending = state.pending if state else None
            if pending:
                if body.get("tool_result_has_context") is not False:
                    raise ServiceError("PC RPC cannot append task context while its cloud call is waiting", 409,
                                       "muse_web_tool_result_invalid")
                expected = "custom_tool_call_output" if pending["custom"] else "function_call_output"
                if (time.monotonic() >= pending["expires"] or tools != pending["tools"]
                        or len(results) != 1 or not isinstance(results[0], dict)
                        or set(results[0]) != {"type", "call_id", "output"}
                        or results[0].get("call_id") != pending["call_id"]
                        or results[0].get("type") != expected
                        or not isinstance(results[0].get("output"), (str, list))):
                    raise ServiceError("Expected the matching unexpired PC result; no replay", 409,
                                       "muse_web_tool_result_invalid")
            progress()
            deadline = time.monotonic() + self.timeout
            gateway = self._take_gateway(state) if state is not None else None
            reused = gateway is not None
            if gateway is None:
                gateway = self.factory()
                self.workspace.check_compatible(gateway)
            if state is None:
                # Upload/verify the tool catalog and reserve the relay BEFORE
                # creating a persistent Muse chat. Slow preparation or a
                # disconnected Codex client must not leave an empty chat for
                # every retry. The new chat has no prior events to poll.
                stage = "prepare"
                prepared_run = self.workspace.prepare(gateway, tools, self.timeout + self.result_timeout)
                progress()
                stage = "session_start"
                value = gateway.call_json("session.start", body={"method": "/api/session/start", "params": {
                    "origin": "fresh", "lifecycle": "persistent", "title": "Codex PC " + secrets.token_hex(4),
                }}, timeout=min(30, self.timeout))
                session_id = (value.get("session_id") or value.get("id")) if isinstance(value, dict) else None
                if not isinstance(session_id, str) or not session_id:
                    raise ServiceError("Muse did not create an isolated PC conversation")
                state = PCTurn(session_id)
                acquired = state.lock.acquire(blocking=False)
            if pending:
                # Resume the SAME running native cloud call. Do not send a new user message.
                progress()
                self._check_result_delivery(gateway, state)
                if time.monotonic() >= pending["expires"]:
                    raise ServiceError("PC call expired before result delivery; no replay", 409,
                                       "muse_web_tool_result_invalid")
                self._invalidate_recovery(state)
                submitted = True  # Result publication can itself have an ambiguous outcome.
                self.workspace.publish_result(gateway, state, results[0])
                state.pending = None
                state.sequence += 1
            else:
                if prepared_run:
                    state.baseline = 0
                    state.run = prepared_run
                else:
                    stage = "history_preflight"
                    try:
                        events = self._history(gateway, state, min(30, self.timeout))
                    except ServiceError:
                        raise
                    except Exception:
                        # Only a new user task may recover a stale idle connection.
                        # This is a read-only preflight: no task/catalog/result has
                        # been written or submitted. Never retry a waiting tool result.
                        if not reused:
                            raise
                        gateway.close()
                        gateway = None
                        gateway = self.factory()
                        self.workspace.check_compatible(gateway)
                        events = self._history(gateway, state, min(30, self.timeout))
                    self._verify_restored(state, events)
                    state.baseline = max((e.get("seq", 0) for e in events), default=0)
                    state.history_after = max(state.history_after, state.baseline)
                    stage = "prepare"
                    state.run = self.workspace.prepare(gateway, tools, self.timeout + self.result_timeout)
                state.sequence = 1
                progress()
                # Put tool wiring first, then the actual user task last.
                # On long Codex histories, ending with bridge instructions can
                # make Muse respond to the relay rather than the user's ask.
                state.submitted_text = self.workspace.prompt(state.run) + "\n\n" + body["input"]
                stage = "chat_stream"
                wire_body = chat_request_bytes(state.session_id, self.node_id, state.submitted_text)
                if len(wire_body) > MUSE_CHAT_REQUEST_MAX_BYTES:
                    raise ServiceError("Muse single-message request is too large; no message sent", 413,
                                       "muse_web_request_too_large")
                self._invalidate_recovery(state)
                submitted = True
                send_chat(gateway, wire_body)
            stage = "output_wait"
            response = self._wait_for_output(gateway, state, tools, deadline, progress)
            keep_gateway = True
            return response
        except MailboxCompatibilityError:
            raise ServiceError("Independent PC RPC helper is missing; no message sent", 503,
                               "muse_web_tools_unavailable") from None
        except MailboxError:
            if state is not None and submitted:
                state.valid = False
            raise ServiceError("Invalid PC RPC request; no automatic repair or replay", 502,
                               "muse_web_tool_format_invalid") from None
        except (ServiceError, BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            if state is not None and submitted:
                state.valid = False
            raise
        except Exception as error:
            # Never include exception text, payloads or credentials in logs.
            _LOG.warning("muse_pc_unexpected_failure stage=%s kind=%s submitted=%s",
                         stage, type(error).__name__, submitted)
            if state is not None and submitted:
                state.valid = False
            raise ServiceError("PC RPC transport failed; no automatic replay", 502,
                               "muse_web_transport_error") from None
        finally:
            try:
                if state is None and prepared_run and hasattr(self.workspace, "discard"):
                    self.workspace.discard(prepared_run)
                if gateway is not None:
                    if keep_gateway and state is not None and state.valid:
                        self._park_gateway(state, gateway)
                    else:
                        if state is not None:
                            self._close_gateway(state)
                        gateway.close()
            finally:
                if state is not None and acquired:
                    state.lock.release()
                self.slots.release()

    def _check_result_delivery(self, gateway, state):
        events = self._history(gateway, state, min(30, self.timeout))
        try:
            terminal = self._terminal(events, state.baseline)
        except ServiceError:
            state.valid = False
            raise
        if terminal is not None:
            state.valid = False
            raise ServiceError("Muse stopped waiting before result delivery; no replay", 409,
                               "muse_web_tool_result_invalid")

    def _wait_for_output(self, gateway, state, tools, deadline, progress):
        while time.monotonic() < deadline:
            progress()
            events = self._history(gateway, state, max(0.1, min(30, deadline - time.monotonic())))
            # Wait for our exact user message before delivering this turn's output.
            confirmed = state.submitted_text is None
            terminal = self._terminal(events, state.baseline) if confirmed else None
            live = (self.workspace.live_request(gateway, state.run, state.sequence, tools)
                    if confirmed and state.sequence <= MAX_CALLS else None)
            if live and terminal:
                raise ServiceError("Muse finished before the PC call was delivered; no tool executed", 409,
                                   "muse_web_tool_result_invalid")
            if live:
                call, expires_at = live
                response = self._response("", call)
                state.pending = {"call_id": response["output"][0]["call_id"], "custom": call["freeform"],
                                 "tools": tools, "expires": time.monotonic() +
                                 min(self.result_timeout, max(0, expires_at - time.time()))}
                progress()
                return self._remember(state, response)
            if terminal:
                text = terminal.get("display_text") or terminal.get("content")
                if isinstance(text, str) and text.strip():
                    progress()
                    return self._remember(state, self._response(text))
            until = min(deadline, time.monotonic() + self.poll)
            while time.monotonic() < until:
                time.sleep(min(0.2, max(0, until - time.monotonic())))
                progress()
        raise ServiceError("PC RPC timed out; no automatic replay; check Muse", 504, "muse_web_timeout")

    @staticmethod
    def _terminal(events, baseline):
        terminal = None
        for event in sorted(events, key=lambda e: e.get("seq", 0)):
            payload = event.get("payload")
            if (event.get("event_name") != "message.assistant" or event.get("seq", 0) <= baseline
                    or not isinstance(payload, dict) or event.get("reply_to_message_id")
                    or payload.get("reply_to_message_id")):
                continue
            metadata = payload.get("metadata")
            if isinstance(metadata, dict) and (metadata.get("message_kind") == "commentary"
                                               or metadata.get("recipient") == "commentary"):
                # A completed commentary message is not the completion of the agent turn.
                continue
            if payload.get("status") in {"failed", "cancelled", "error"}:
                raise ServiceError("Muse generation did not complete", 502, "muse_web_generation_failed")
            if payload.get("status") == "completed":
                terminal = payload
        return terminal

    @staticmethod
    def _response(text, call=None):
        value = _last_completed_response(list(_responses_events(
            "default", text, [call] if call else [], response_id="resp_muse_" + secrets.token_hex(16))))
        value["usage"] = None
        return value

    def _remember(self, state, response):
        evicted = []
        with self.lock:
            state.head = response["id"]
            self.snapshots[response["id"]] = state
            while len(self.snapshots) > self.max_snapshots:
                response_id, previous = self.snapshots.popitem(last=False)
                if response_id == previous.head and previous is not state:
                    evicted.append(previous)
        self._remember_completed(state, response)
        for previous in evicted:
            if isinstance(previous, PCTurn) and previous.lock.acquire(blocking=False):
                try:
                    self._close_gateway(previous)
                finally:
                    previous.lock.release()
        return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--tool-workspace", required=True)
    parser.add_argument("--proxy")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    configure_network(args.proxy if args.proxy is not None else config.get("proxy", "auto"))
    sessions = PCSessions(gateway_factory(config), tool_workspace=args.tool_workspace)
    server = make_server(config, sessions)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        sessions.close()


if __name__ == "__main__":
    main()

