#!/usr/bin/env python3
"""Codex task runner for ${PC_NAME} (stdlib only, Windows).

Backs the Muse MCP codex tools. A detached supervisor process owns each
`codex exec` run: `start` spawns the supervisor and returns at once; the
supervisor spawns the native codex.exe (found next to the npm shim),
waits for it, and records the real exit code.

Layout (next to this file):
  tasks/<task_id>/prompt.txt       task text given to codex (stdin)
  tasks/<task_id>/events.jsonl     codex --json event stream
  tasks/<task_id>/last_message.txt codex -o final agent message
  tasks/<task_id>/exit.txt         real exit code, written when the run ends
  tasks/<task_id>/meta.json        workdir/model/resume/started/pids
  tasks/<task_id>/runner.err       codex stderr
  workspace/                       default workdir for delegated tasks

Subcommands (each prints exactly one JSON object on stdout):
  start  --prompt-b64 B64 [--workdir-b64 B64] [--model M] [--resume SID]
  status --id TASK_ID
  result --id TASK_ID [--max-chars N]
  cancel --id TASK_ID
Internal:
  _run   --id TASK_ID              supervisor body
"""
import base64
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.abspath(__file__))
TASKS = os.path.join(ROOT, "tasks")
DEFAULT_WS = os.path.join(ROOT, "workspace")
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._\-]+$")
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def out(obj):
    print(json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()


def task_dir(task_id):
    return os.path.join(TASKS, task_id)


def load_meta(task_id):
    path = os.path.join(task_dir(task_id), "meta.json")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_meta(task_id, meta):
    with open(os.path.join(task_dir(task_id), "meta.json"), "w",
              encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False)


def pid_alive(pid):
    if not pid:
        return False
    try:
        res = subprocess.run(
            ["tasklist", "/FI", "PID eq %d" % int(pid), "/NH"],
            capture_output=True, text=True, timeout=15)
        return str(pid) in res.stdout
    except Exception:
        return False


def find_codex_exe():
    shim = shutil.which("codex")
    if shim:
        node_dir = os.path.dirname(shim)
        pattern = os.path.join(
            node_dir, "node_modules", "@openai", "codex", "node_modules",
            "@openai", "codex-win32-*", "vendor", "*", "bin", "codex.exe")
        hits = glob.glob(pattern)
        if hits:
            return hits[0]
    return None


def scan_events(task_id):
    """Extract (session_id, usage, last_agent_message) from the JSONL stream."""
    session_id = None
    usage = None
    last_msg = ""
    path = os.path.join(task_dir(task_id), "events.jsonl")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"thread.started"' in line and session_id is None:
                        m = re.search(r'"thread_id"\s*:\s*"([^"]+)"', line)
                        if m:
                            session_id = m.group(1)
                    if '"turn.completed"' in line:
                        m = re.search(r'"usage"\s*:\s*(\{[^}]*\})', line)
                        if m:
                            try:
                                usage = json.loads(m.group(1))
                            except ValueError:
                                pass
                    if '"agent_message"' in line:
                        m = re.search(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', line)
                        if m:
                            try:
                                last_msg = json.loads('"%s"' % m.group(1))
                            except ValueError:
                                last_msg = m.group(1)
        except OSError:
            pass
    return session_id, usage, last_msg


def current_state(task_id):
    meta = load_meta(task_id)
    if meta is None:
        return None
    exit_path = os.path.join(task_dir(task_id), "exit.txt")
    state = meta.get("state", "running")
    exit_code = None
    if os.path.isfile(exit_path):
        try:
            with open(exit_path, "r", encoding="utf-8") as fh:
                exit_code = int(fh.read().strip() or "0")
        except (OSError, ValueError):
            exit_code = None
        state = "cancelled" if state == "cancelled" else "finished"
    elif state == "running" and not pid_alive(meta.get("supervisor_pid")) \
            and not pid_alive(meta.get("pid")):
        state = "lost"  # both supervisor and codex vanished, no exit marker
    session_id, usage, _ = scan_events(task_id)
    return {
        "ok": True,
        "task_id": task_id,
        "state": state,
        "exit_code": exit_code,
        "workdir": meta.get("workdir"),
        "model": meta.get("model"),
        "session_id": session_id or meta.get("session_id"),
        "resumed_from": meta.get("resume"),
        "started_at": meta.get("started_at"),
        "finished_at": meta.get("finished_at"),
        "pid": meta.get("pid"),
        "usage": usage,
    }


def cmd_start(args):
    try:
        prompt = base64.b64decode(args.get("prompt_b64") or "").decode("utf-8")
    except Exception as exc:
        return out({"ok": False, "error": "prompt-b64 decode failed: %s" % exc})
    if not prompt.strip():
        return out({"ok": False, "error": "empty task prompt"})
    workdir = DEFAULT_WS
    if args.get("workdir_b64"):
        try:
            workdir = base64.b64decode(args["workdir_b64"]).decode("utf-8")
        except Exception as exc:
            return out({"ok": False, "error": "workdir-b64 decode failed: %s" % exc})
    workdir = os.path.abspath(workdir)
    model = args.get("model")
    if model and not SAFE_TOKEN.match(model):
        return out({"ok": False, "error": "invalid model token"})
    resume = args.get("resume")
    if resume and not SAFE_TOKEN.match(resume):
        return out({"ok": False, "error": "invalid resume session id"})
    if not find_codex_exe():
        return out({"ok": False, "error": "codex.exe not found next to the codex shim"})
    os.makedirs(TASKS, exist_ok=True)
    os.makedirs(workdir, exist_ok=True)
    for name in os.listdir(TASKS):
        st = current_state(name)
        if st and st["state"] == "running" and \
                os.path.abspath(st["workdir"] or "") == workdir:
            return out({"ok": False, "error": "workdir busy",
                        "busy_task_id": name, "workdir": workdir})
    task_id = "ct_%d_%s" % (int(time.time()), uuid.uuid4().hex[:8])
    tdir = task_dir(task_id)
    os.makedirs(tdir)
    with open(os.path.join(tdir, "prompt.txt"), "w", encoding="utf-8") as fh:
        fh.write(prompt)
    meta = {"task_id": task_id, "state": "running", "workdir": workdir,
            "model": model, "resume": resume,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "finished_at": None, "pid": None, "supervisor_pid": None,
            "session_id": None}
    save_meta(task_id, meta)
    sup = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "_run", "--id", task_id],
        creationflags=DETACHED, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, cwd=ROOT)
    meta["supervisor_pid"] = sup.pid
    save_meta(task_id, meta)
    out(current_state(task_id))


def cmd_run(args):
    """Supervisor body: spawn codex, wait, record the real exit code."""
    task_id = args.get("id") or ""
    meta = load_meta(task_id)
    if meta is None:
        return
    tdir = task_dir(task_id)
    exe = find_codex_exe()
    if not exe:
        with open(os.path.join(tdir, "exit.txt"), "w") as fh:
            fh.write("127")
        return
    cmd = [exe, "exec"]
    if meta.get("resume"):
        cmd += ["resume", meta["resume"]]
    cmd += ["--json", "--skip-git-repo-check"]
    if not meta.get("resume"):
        # `codex exec resume` has no -s option in 0.142 (verified live);
        # a resumed session keeps the sandbox of its original run.
        cmd += ["-s", "workspace-write"]
    cmd += ["-o", os.path.join(tdir, "last_message.txt")]
    if meta.get("model"):
        cmd += ["-m", meta["model"]]
    events = open(os.path.join(tdir, "events.jsonl"), "wb")
    err = open(os.path.join(tdir, "runner.err"), "wb")
    prompt_fh = open(os.path.join(tdir, "prompt.txt"), "rb")
    proc = subprocess.Popen(cmd, cwd=meta["workdir"], stdin=prompt_fh,
                            stdout=events, stderr=err)
    meta["pid"] = proc.pid
    save_meta(task_id, meta)
    code = proc.wait()
    meta["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    if meta.get("state") == "running":
        meta["state"] = "finished"
    session_id, _, _ = scan_events(task_id)
    meta["session_id"] = session_id
    save_meta(task_id, meta)
    with open(os.path.join(tdir, "exit.txt"), "w") as fh:
        fh.write(str(code))


def cmd_status(args):
    st = current_state(args.get("id") or "")
    if st is None:
        return out({"ok": False, "error": "unknown task id"})
    out(st)


def cmd_result(args):
    task_id = args.get("id") or ""
    st = current_state(task_id)
    if st is None:
        return out({"ok": False, "error": "unknown task id"})
    try:
        max_chars = max(500, min(60000, int(args.get("max_chars") or 12000)))
    except (TypeError, ValueError):
        max_chars = 12000
    last = ""
    lm = os.path.join(task_dir(task_id), "last_message.txt")
    if os.path.isfile(lm):
        with open(lm, "r", encoding="utf-8", errors="replace") as fh:
            last = fh.read()
    if not last:
        _, _, last = scan_events(task_id)
    st["last_message"] = last[-max_chars:]
    st["last_message_truncated"] = len(last) > max_chars
    if st["state"] in ("finished", "lost") and not last:
        errp = os.path.join(task_dir(task_id), "runner.err")
        if os.path.isfile(errp):
            with open(errp, "r", encoding="utf-8", errors="replace") as fh:
                st["stderr_tail"] = fh.read()[-2000:]
    out(st)


def cmd_cancel(args):
    task_id = args.get("id") or ""
    meta = load_meta(task_id)
    if meta is None:
        return out({"ok": False, "error": "unknown task id"})
    for key in ("pid", "supervisor_pid"):
        pid = meta.get(key)
        if pid and pid_alive(pid):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, text=True, timeout=30)
    meta["state"] = "cancelled"
    meta["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    save_meta(task_id, meta)
    with open(os.path.join(task_dir(task_id), "exit.txt"), "w") as fh:
        fh.write("-15")
    out(current_state(task_id))


def main():
    argv = sys.argv[1:]
    if not argv:
        return out({"ok": False, "error": "subcommand required"})
    sub = argv[0]
    args = {}
    i = 1
    while i < len(argv):
        if argv[i].startswith("--") and i + 1 < len(argv):
            args[argv[i][2:].replace("-", "_")] = argv[i + 1]
            i += 2
        else:
            i += 1
    if sub == "start":
        cmd_start(args)
    elif sub == "_run":
        cmd_run(args)
    elif sub == "status":
        cmd_status(args)
    elif sub == "result":
        cmd_result(args)
    elif sub == "cancel":
        cmd_cancel(args)
    else:
        out({"ok": False, "error": "unknown subcommand: %s" % sub})


if __name__ == "__main__":
    main()
