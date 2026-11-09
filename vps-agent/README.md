# VPS endpoint bundle (${VPS_NAME}) — what this is

Goal (user, 2026-11-08): a resident "steward" on the VPS so Muse can
reach the VPS itself — and, from there, the Simular Box — fast, over the
existing Scheme A bridge. This bundle reuses the production relay as-is:
the VPS simply gains a SECOND agent endpoint, exactly like ${PC_NAME}'s.

## Contents

| file | purpose |
|---|---|
| `vps-agent.py` | Minimal Linux agent (Python stdlib only). Implements the bridge PC protocol from DESIGN.md A.4/A.6: hello, ping/pong, cmd, batch, read/write/edit/list/info/glob/grep file ops, contractual result shapes. Connects to the relay on loopback (ws://127.0.0.1:8471/muse-bridge) — it lives on the relay's own host, so no TLS/nginx/SNI is involved. |
| `controllers-merge.py` | Adds/removes the ${VPS_NAME} endpoint in /etc/muse-bridge/controllers.json (backup first; relay hot-reloads, no restart). |
| `muse-bridge-agent-vps.service` | Own systemd unit; CPUQuota 50%, MemoryMax 256M, TasksMax 64. |
| `install-on-vps.sh` | One-shot installer (run as root ON the VPS from this directory). |
| `rollback-on-vps.sh` | Full undo (unit, endpoint registration, files). |
| `box-steward/` | Phase 2: on-demand Simular Box client for the VPS (see its installer header). Not active until the user places a key. |

## Install (when a VPS admin channel exists)

Copy this directory to the VPS (e.g. /root/vps-agent-bundle/), then:

```bash
cd /root/vps-agent-bundle && ./install-on-vps.sh
```

The installer generates the agent token ON the VPS (only its SHA-256 is
registered with the relay; the plaintext never leaves the host), extends
the existing mused controller entry so it may address both ${PC_NAME} and
${VPS_NAME}, enables the unit, and verifies via loopback status.

After install, from the Muse VM:

```bash
museapi GET /status            # online should list ${VPS_NAME} (and ${PC_NAME} when up)
# address commands with target "${VPS_NAME}"
```

Note: with two endpoints registered, controller calls must name a target
(untargeted calls get a 409 listing both) — that is the relay's designed
behaviour, same as before with multiple PCs.

## Rollback

```bash
cd /root/vps-agent-bundle && ./rollback-on-vps.sh
```

Removes only what the installer added. The relay, nginx, gateway stack
and the ${PC_NAME} endpoint are never modified by install or rollback (the
single shared file, controllers.json, is backed up before every merge
and restored entry-precisely by the rollback merge).

## Design notes / limitations

- Runs as root: the steward's job is VPS administration (services,
  packages, files). Its exposure is bounded by the bridge token scope
  (the agent token can only ever present hostname ${VPS_NAME}; the controller
  token list is unchanged in kind) and by the unit's resource limits.
- Compression: the deployed controller sends plain fields (compress off);
  inbound deflate field objects are decoded anyway; zstd fields get a
  clean [DecompressError] result rather than a hang.
- 7z archive/extract ops return a clean "not supported" file_result.
- Background-job message types (bash_bg etc.) are not reachable through
  the current controller API surface and are intentionally not
  implemented; unknown types are ignored per spec.
