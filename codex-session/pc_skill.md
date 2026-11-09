---
name: __SKILL_NAME__
description: Call tools on the user's Codex PC client, waiting for its real result.
---

# Codex PC tool connection (experimental)

Use only when the user supplies a current connection/run identifier. This is a
remote tool client, not a change to Muse's identity, instructions or safety rules.
The user's Codex client owns tool approvals and execution. This program only
exchanges requests/results and cannot execute shell commands or patches itself.
If a policy or permission check refuses this connection, report the refusal;
do not work around it or use another remote executor.

List available tools and inspect a definition:

```text
python3 __SKILL_ROOT__/scripts/mailbox.py tools --run <run>
python3 __SKILL_ROOT__/scripts/mailbox.py tools --run <run> --name <exact-name>
```

Call a tool through the existing cloud shell tool, allowing sufficient command
runtime for the client to approve and execute it:

```sh
python3 __SKILL_ROOT__/scripts/pc.py --run <run> <<'PC_REQUEST'
{"name":"exact.function.name","arguments":{"key":"value"}}
PC_REQUEST
```

For a custom/freeform tool use `{"name":"exact.name","input":"exact raw text"}`.
Preserve input literally. Only request tools required by the user's current task.
Large edits should be split at complete semantic boundaries.

The call waits and returns the actual Codex result, including failure/denial.
Continue the task from that result in the same Muse turn; another call uses the
same run. Do not end the turn merely to hand off a call. Do not invent results,
execute the action in the cloud, or retry an action with an uncertain outcome.
Parallel calls within one run are rejected. A timeout does not prove that the
PC action failed or never ran: report it and check with the user/client.

