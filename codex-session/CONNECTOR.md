# Muse Bridge (${PC_NAME}) — Scheme B MCP connector

- `custom.muse-bridge` connects Muse to the user's own Muse Bridge MCP service at `https://${VPS_SNI}/mcp` (the user's VPS ${VPS_IP}, nginx 443 SNI vhost, service on loopback 8472). It is the Scheme B front end for the same VPS relay (loopback 8471) and the same headless agent on ${PC_NAME} that Scheme A (mused/musepc) uses. The MCP service forwards every tool call to the relay with its own bridge token, scoped to the single PC `${PC_NAME}`; tools cannot name another target.

- The helper is `~/workspace/custom-connectors/custom.muse-bridge/bin/client.py` (Python 3, standard library only). It exposes `list-tools` and `call-tool` commands. The VPS service itself now offers only `status`. PC tool execution is served LOCALLY by the client (tools `pc_tools` / `pc_call`, below) over the user's own session-attached push channel.
- Tool history: until 2026-11-01 the service exposed shell/file tools (`run_command`, `read_file`, `write_file`, `edit_file`, `list_directory`); the user retired the service that evening. On 2026-11-02 it was briefly revived with Codex delegation tools (`codex_task_*`, backed by a PC-side runner spawning headless `codex exec`); the user rejected that design the same morning — `codex exec` asks a fresh Codex a question, while the intended model is Muse working INSIDE a live Codex session. The `codex_task_*` tools were removed from the service (build preserved in `/opt/muse-mcp/muse-mcp.py.bak-codextools-20261102`; the runner under `C:\Users\${PC_USER}\.muse-bridge\codex-tasks\` is inert and kept only as an artifact).

## Session-attached PC tools (local, via the user's push channel)

- `pc_tools` takes `run` (a 32-hex run id) and optional `name`: it reads the run's manifest and hash-verified tool catalog from the user's installed push skill (`MUSE_PC_SKILL_ROOT`, default `~/workspace/custom-skills/codex_push_persistent_20261021_v2`) and lists the live Codex session's tools or one tool's definition. Purely local; nothing is sent.
- `pc_call` takes `run`, `name`, and exactly one of `arguments` (function tools) or `input` (custom/freeform tools), plus optional `timeout` (default 600, max 1800). It loads the skill's own exchange client unchanged and performs one synchronous call: the request goes to the relay room for that run, the PC's push service hands it to the live Codex session, Codex approves/executes, and the real result (`{type, call_id, output}`) returns in the same call.
- The run's relay credential lives only in the skill's manifest on this VM and is read by the skill's code itself; it never transits the VPS service or the chat. Runs are created by the PC service per Codex turn and are ALWAYS supplied by the user — never invent one, and never reuse an expired one. One call at a time per run; a failed or ambiguous outcome is reported and NEVER replayed.
- These tools bypass the warm daemon (and therefore `muse-b`); invoke them through `client.py call-tool` only.

## Authentication (self-managed OAuth 2.0)

- The service is a full OAuth 2.0 authorization server (RFC 8414 discovery at `/.well-known/oauth-authorization-server`, RFC 7591 dynamic registration at `/register`, authorization code + PKCE S256 at `/authorize` and `/token`, refresh tokens with rotation, 1-hour access tokens).
- This connector does NOT use Muse's hosted OAuth flow. Reason: the service's issuer name is a private `.local` name with a private-CA certificate, so a hosted consent page and server-side token exchange cannot resolve or trust it. Instead the helper performs the authorization-code flow itself from this VM, where the name is reachable through the egress proxy and the CA is pinned.
- The owner approval secret was generated on this VM and lives only in `~/.config/muse-mcp-client/owner-secret` (mode 600). The VPS stores only its SHA-256 in the OAuth state file. The helper reads the secret and submits it to `/authorize` programmatically — possession of that file is the owner consent. Treat the file like a password.
- The helper registers itself once (`client.json` in the same directory) and caches the token pair in `tokens.json` (mode 600), refreshing with rotation; on a 401 it discards the pair and re-authorizes once. Authorization codes burn only on successful exchange, so a lost response self-heals on the next run.
- No credential (owner secret, tokens, proxy credential) is ever printed, logged, or placed in a command argument. The helper parses the egress proxy from the environment at runtime for the CONNECT tunnel and pins the bridge CA at `~/.local/share/muse-bridge/vps-root-ca.pem`.

## Transport notes

- All traffic goes through this VM's egress proxy: HTTP CONNECT addressed to the VPS **IP** (${VPS_IP}:443), then TLS with SNI `${VPS_SNI}`, certificate verified against the pinned private CA. This mirrors mused's pattern and matters: a CONNECT addressed to the `.local` name goes through the proxy's own name resolution, whose route black-holes TLS handshakes, while the IP tunnel connects instantly.
- New flows from this VM to the VPS are intermittently black-holed by the network path (first attempt hangs, retry succeeds); persistent connections are stable. The helper retries connection establishment up to 3 times with a 25-second timeout, but never replays a request that was already sent — a lost response raises instead of risking a duplicate command execution on the PC.
- The service's `status` answers within the relay timeout (~75 s). The local `pc_call` is synchronous by design: it blocks until the live Codex session returns the real result or the run/deadline expires (default 600 s, max 1800 s).

## Warm-connection daemon (default path)

- `bin/daemon.py` is a resident daemon (added 2026-11-01) that holds an **adaptive pool of initialized MCP connections** so routine calls skip the fresh CONNECT + TLS handshake entirely. `client.py` is a shim: it forwards each call to the daemon over the Unix socket `~/.config/muse-mcp-client/daemon.sock`, starting the daemon on demand when it is not running (the musepc->mused self-heal pattern; a runtime restart of this VM kills the daemon and the next call revives it, verified). Pool policy: floor **3** warm connections, hard cap **5**; growth is demand-driven (a request that finds every connection busy opens a new one instead of queueing); connections idle beyond **180 s** beyond the floor are closed, one per maintainer pass; the floor is re-warmed one connection at a time, never in a burst. Tunables: `MUSE_MCP_POOL_MIN`, `MUSE_MCP_POOL_MAX`, `MUSE_MCP_POOL_SHRINK`.
- Measured with the daemon warm: sequential `status` calls ≈ 0.21 s each end to end (vs ≈ 0.77 s per call in direct mode); five parallel 4-second commands finished in 5.4 s (pool grew 3 -> 5 on demand).
- Fallback discipline in the shim: if the daemon is unreachable and cannot be started, or replies that the call was **not executed** (e.g. pool checkout timed out), the shim makes exactly one direct call. If the daemon handled the call — or the outcome is ambiguous (socket broke mid-call) — the shim reports the outcome and **never** re-issues it.
- Set `MUSE_MCP_NO_DAEMON=1` to bypass the daemon. `client.py daemon-status` reports pool size/busy counts; `client.py daemon-stop` stops it (it will auto-start on the next call). Daemon log: `~/.config/muse-mcp-client/daemon.log`.
- Idle keepalive: the far end (nginx) closes idle connections, and a pooled connection otherwise discovers that only by failing the next real call — observed live on 2026-11-01, when all 3 pooled connections were found dead after ~50 min idle and the first calls of a test run absorbed the failures. The maintainer therefore sends a cheap relay-`status` keepalive on floor connections idle beyond ~40 s (the same lesson as mused's 45 s stale eviction); verified: after 105 s fully idle, the first `run_command` succeeded first try at normal warm latency with no connection discards logged.
- The daemon holds the same OAuth token state as the direct client (shared files in `~/.config/muse-mcp-client/`); MCP `initialize` calls are serialized inside the daemon so concurrent pool growth cannot race a token refresh rotation.
- `bin/muse-b` (C, source `bin/muse-b.c`, built with `gcc -O2 -o ~/.local/bin/muse-b bin/muse-b.c`) is the fast entry point for routine calls: it speaks the daemon's socket protocol with ~2 ms startup instead of the Python shim's ~60 ms, prints the daemon's raw JSON response, and preserves the fallback discipline via exit codes (111 = daemon unreachable or call not executed -> use `client.py`, which self-heals; 1 = executed/failed, never retry; 2 = ambiguous, never retry). Measured: B echo P50 449 ms (Python shim) -> 410 ms (muse-b).

## Operations

List the tools the service offers (also the harmless first check that the whole chain, including OAuth, works):

```text
python3 ~/workspace/custom-connectors/custom.muse-bridge/bin/client.py list-tools
```

Read the bridge status (which PCs are online):

```text
python3 ~/workspace/custom-connectors/custom.muse-bridge/bin/client.py call-tool --name status --arguments-file ~/workspace/custom-connectors/custom.muse-bridge/work/empty.json
```

Call one tool inside the live Codex session on ${PC_NAME} (working directory `D:\software\AI_Coding\codex`):

```text
python3 ~/workspace/custom-connectors/custom.muse-bridge/bin/client.py call-tool --name pc_call --arguments-file <arguments-json>
```

- The arguments file holds `run` (required, supplied by the user), `name` (a tool from that run's catalog — check with `pc_tools`), and exactly one of `arguments` / `input`. The result text is the session's real tool result JSON (`{type, call_id, output}`); a denial or failure by Codex is returned the same way, not hidden.
- There is no task id, no polling and no background execution: the call IS the execution, inside the user's live session, under its approvals.

## Relationship to Scheme A

- Scheme A (mused/musepc, see `~/workspace/muse-bridge-deploy/`) reaches the same agent through the same relay with a bridge token held by mused. Scheme B adds the MCP/OAuth layer on the VPS. Either path can be disabled independently: B by `systemctl disable --now muse-mcp` plus removing its nginx locations and setting its `controllers.json` entry `enabled: false`; A by stopping mused and the ${PC_NAME} agent task.
- Measured 2026-11-01: Scheme B's server-side segment (MCP service -> relay -> agent, VPS loopback) answers an echo command at P50 ≈ 280 ms versus Scheme A's full-path P50 ≈ 424 ms from this VM; the difference is the VM->VPS public hop, which B's public path pays as well.

## Related

- Deployment bundle, service source and design notes: `~/workspace/muse-bridge-deploy/` (`mcp/` for this service, `relay/` for the relay, `agent/` for the Windows agent entry, `ab/` for the A/B measurements).
