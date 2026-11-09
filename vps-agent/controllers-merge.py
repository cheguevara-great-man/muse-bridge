#!/usr/bin/env python3
"""Add/remove the VPS agent endpoint in the relay's controllers.json.

Used by install-on-vps.sh and rollback-on-vps.sh. The relay hot-reloads
controllers.json on mtime change, so no relay restart is needed and no
existing service is disturbed.

install:
  * Backs the file up to controllers.json.bak-<timestamp> (mode 600 kept).
  * Adds one entry keyed by sha256(agent token):
    {muse: "muse-rd925", pcs: ["${VPS_NAME}"], label: "vps agent (scheme A)",
     enabled: true, created: <iso>}. Idempotent.
  * Extends the pcs list of existing CONTROLLER entries (label without
    "agent", pcs a non-empty list drawn from {"${PC_NAME}"}) to also include
    "${VPS_NAME}", so the existing mused controller token can address the new
    endpoint. Agent-side entries are never touched.

rollback:
  * Removes the agent entry (found via the agent token's hash).
  * Removes "${VPS_NAME}" from every pcs list that contains it (restoring the
    pre-install shape; lists are never left empty because "${VPS_NAME}" was
    only ever appended to lists that already held another PC, and the
    agent entry itself is deleted outright).

Only token HASH prefixes and labels are printed — never token plaintext.
"""

import argparse
import datetime
import hashlib
import json
import os
import shutil
import sys

HOSTNAME = "${VPS_NAME}"
CONTROLLERS = "/etc/muse-bridge/controllers.json"


def sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def backup(path):
    if os.path.exists(path):
        stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        dst = f"{path}.bak-{stamp}"
        shutil.copy2(path, dst)
        print(f"backup: {dst}")
        return dst
    return None


def do_install(args):
    token = open(args.token_file, encoding="utf-8").read().strip()
    agent_hash = sha256_hex(token)
    data = load(args.controllers)
    backup(args.controllers)
    changed = False
    if agent_hash not in data:
        data[agent_hash] = {
            "muse": "muse-rd925",
            "pcs": [HOSTNAME],
            "created": datetime.datetime.now(
                datetime.timezone.utc).isoformat(),
            "label": "vps agent (scheme A)",
            "enabled": True,
        }
        changed = True
        print(f"added agent entry hash={agent_hash[:8]} pcs=[{HOSTNAME}]")
    else:
        print(f"agent entry already present hash={agent_hash[:8]}")
    for key, entry in data.items():
        if not isinstance(entry, dict) or key == agent_hash:
            continue
        label = str(entry.get("label", ""))
        pcs = entry.get("pcs")
        if "agent" in label.lower():
            continue                       # never widen agent-side tokens
        if (isinstance(pcs, list) and pcs and HOSTNAME not in pcs
                and set(pcs) <= {"${PC_NAME}"}):
            entry["pcs"] = pcs + [HOSTNAME]
            changed = True
            print(f"extended pcs for entry hash={key[:8]} "
                  f"label={label!r} -> {entry['pcs']}")
    if changed:
        save(args.controllers, data)
    print("install merge done")


def do_rollback(args):
    token = ""
    if os.path.exists(args.token_file):
        token = open(args.token_file, encoding="utf-8").read().strip()
    data = load(args.controllers)
    backup(args.controllers)
    changed = False
    if token:
        agent_hash = sha256_hex(token)
        if agent_hash in data:
            del data[agent_hash]
            changed = True
            print(f"removed agent entry hash={agent_hash[:8]}")
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        pcs = entry.get("pcs")
        if isinstance(pcs, list) and HOSTNAME in pcs and len(pcs) > 1:
            entry["pcs"] = [p for p in pcs if p != HOSTNAME]
            changed = True
            print(f"removed {HOSTNAME} from pcs of entry hash={key[:8]}")
    if changed:
        save(args.controllers, data)
    print("rollback merge done")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("install", "rollback"))
    parser.add_argument("--controllers", default=CONTROLLERS)
    parser.add_argument("--token-file",
                        default="/etc/muse-bridge-agent/token")
    args = parser.parse_args()
    if args.mode == "install":
        do_install(args)
    else:
        do_rollback(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
