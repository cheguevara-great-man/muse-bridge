# Muse Bridge Migration — Design Document

Status: research only, 2026-11-01. Nothing in this document has been deployed.
Binding targets for the new deployment: Windows PC **${PC_NAME}**, VPS **${VPS_IP}**.
All identity material (tokens, CA, certificates) is to be generated fresh for these
targets. This document contains no token, key, or certificate contents.

Sources read on ${PC_NAME} under `D:\software\AI_Coding\codex\muse-bridge`:

- Techbook: `muse-bridge-docs-sanitized\01-techbook\Muse-Bridge-架构技术书.md`
  (3014 lines, 160,302 bytes) — chapters 1–3, 5–11 and appendices.
- Plugin source 1.2.12: `muse-bridge-src-1.2.12-sanitized\muse-bridge`
  (`package.json`, `src\extension.js`, `src\muse\muse-bridge-client.js` read in
  full at 1453 lines, `src\muse\compress.js`, `src\muse\permission-manager.js`,
  `src\shell\shell-pool.js` and `src\muse\lane-pipelined-shell.js` skimmed,
  `src\vscode\bridge-settings.js`, `src\vscode\secrets.js`,
  `src\vscode\status-bar.js`).
- Onboarding package: `new-muse-onboarding\new-muse-onboarding` — `bin\mused`
  (43,942 bytes, structure plus the WssMux / Upstream / DedupCache sections read),
  `bin\museapi` (5,846), `bin\musepc` (14,624, head read), `bin\musepcd` (21,702,
  not read in depth), `bin\muse-health-check.sh` (7,156, not read),
  `lib\mused\{compress,microbatch,pool,tls_resume}.py`,
  `systemd\mused.service`, `config\config.json.template`,
  `info\controller-entry.json`, `plugin\muse-bridge-1.3.0.vsix` (345,471).
  The `token\` and `cert\` folders were inventoried by name and size only
  (one token file, 49 bytes; one root CA file, 1,915 bytes — the same size as
  the CA bundled in the plugin source). Their contents were not read.

Critical negative finding: **the VPS relay source `muse-bridge.py` is not in
any delivered package.** A recursive search found no `muse-bridge.py` anywhere
under the project root and no `.py` files at all in the docs package. The
techbook archives the source on the colleague's own VM
(`~/workspace/muse-bridge-docs/01_vps/muse-bridge.py`, 637 lines), which we do
not have. The same applies to the controller-auth patch (`auth_patch.py` v2)
and the token manager (`gen_token.py`). Section (a) is therefore written
precisely enough to reimplement the relay from the techbook's specification,
which conveniently documents function names, line numbers, constants, and
behavioral contracts of the original.

---

## (a) Protocol summary

### A.1 Topology and network entry

Three parties, one rendezvous:

```text
Controller (Muse VM: mused + musepc/museapi)
  |  HTTPS API  /muse-api/...            (path 1)
  |  or controller WSS  /muse-bridge     (path 2, preferred by shipped mused)
  v
VPS relay (muse-bridge.py reimplementation, loopback only)
  |  WSS  /muse-bridge
  v
Windows agent on ${PC_NAME} (headless Node, plugin core)
```

- The relay never executes commands. It authenticates, routes by target
  hostname, pairs requests with responses, and translates between the
  controller-facing API and the PC-facing WSS messages.
- Public entry on the VPS is nginx on 443 with `stream` + `ssl_preread`:
  TLS is not decrypted at this layer; routing is by SNI in the ClientHello.
  The bridge SNI (a private name of the form `ai.<deployment>.local`) routes
  to the bridge stack; a second SNI serves the unrelated HTTP proxy product
  and must stay isolated.
- The relay process itself listens on **127.0.0.1:8471 only** (hardcoded
  `LISTEN_HOST`/`LISTEN_PORT` in the original). External WSS also has an
  nginx TLS vhost on 9443 with path `/muse-bridge` mapped to 8471. Direct
  access to 8471 from the network must remain impossible.
- Private PKI: one self-signed root CA (10-year validity). Server certificate
  (~825 days) with SANs covering the VPS IP, the bridge SNI, the wildcard of
  the private suffix, the bare suffix, two short aliases, and `<IP>.nip.io`.
  Clients pin the root CA (bundled file or configured path) and verify with
  `rejectUnauthorized` semantics on; there is no downgrade path.
- For the new VPS specifically (verified 2026-11-01): nginx already serves
  9443 with an unrelated vhost and certificate. The bridge must get its own
  nginx server block and port rather than editing that vhost, and the relay
  must bind loopback only, exactly as in the original design.

### A.2 Relay internals (reimplementation contract)

- Single process, single asyncio event loop (`asyncio.start_server`).
  Multi-process / SO_REUSEPORT is an explicit red line: connection state and
  the request/response pairing table live in process memory. The documented
  scaling ceiling is ~3000 QPS; beyond that, shard by target hostname
  (consistent hashing) so each process owns whole PCs.
- Protocol version constant `BRIDGE_VERSION = "1.1.0"` (server-side; not the
  plugin version).
- Global state:
  - `connected_clients`: set of live WSS connections (both PC clients and
    controller clients).
  - `pending_futures`: `id -> Future`, each future tagged with its client so
    a disconnect can fail-fast (below).
  - `completed_results`: `id -> (msg, completed_at)` for `wait=false`
    requests; TTL 300 s, max 256 entries, oldest evicted first.
- Frame/payload hard cap `MAX_PAYLOAD_SIZE = 64 MB`; oversized frames are cut.
- Request id: taken from the request object if present, else generated as
  `mb_<unix_ms>_<8 hex of uuid4>`.
- Dispatch (`dispatch_request_to_pc`): pop `target`, `pick_client(target)`,
  register future, send the request object as one JSON text frame over the
  PC's WSS, await the future with the request timeout.
- `pick_client` result semantics (dual-dict O(1) after OPT-04; hostname match
  exact first, then case-insensitive):
  - found → dispatch;
  - no PC online → HTTP 503;
  - target named but not online → HTTP 404 (message includes the online list);
  - several PCs online and no target → HTTP 409 (caller must specify).
- Fail-fast: when a PC connection dies, every pending future bound to it is
  settled immediately with `{"ok": false, "error": "Target disconnected
  before result"}` instead of waiting for timeouts.
- `wait=false` mode: dispatch returns `{"ok": true, "id": req_id}` at once;
  the eventual result lands in `completed_results` and is fetched with
  `GET /result/<id>` (60 s long-poll; 404/504 semantics for unknown/expired).

### A.3 Authentication model (two tiers, both must be implemented)

Tier 1 — legacy single token (kept as fallback on both channels):

- Token source on the relay: environment `MUSE_BRIDGE_TOKEN` first, else
  `/etc/muse-bridge/token` (mode 600).
- Verification with `hmac.compare_digest` (constant time).

Tier 2 — controller tokens (`/etc/muse-bridge/controllers.json`, mode 600,
SHA-256 only, plaintext never stored on the VPS):

```json
{
  "<sha256hex of token>": {
    "muse": "<controller id>",
    "pcs": ["<PC hostname>", "..."],
    "created": "<date>",
    "label": "<free text>",
    "enabled": true,
    "allowed_ips": ["<CIDR>", "..."]
  }
}
```

- `allowed_ips` is optional. `super` is a controller with `pcs: ["*"]`.
- The file is hot-reloaded by mtime (an in-memory cache keyed on mtime), so
  adding a controller normally needs no restart; if a new token gets 401 in
  smoke tests, restart the service once and retest.
- Permission rule (`check_target_permission`), enforced on both the HTTP API
  and the WSS handshake, controller tier first, legacy tier as fallback:
  - a normal controller's target must be in its `pcs` (case-insensitive);
    out-of-scope target → **403** with a generic message that does not leak
    the list;
  - a single-PC controller that omits target is auto-resolved to its own PC;
  - `super` (`pcs: ["*"]`) must always name a target explicitly; no target
    → 403;
  - unknown token → **401**.
- One token per controller↔PC pair, used on both sides: the controller's
  mused presents it to the relay (HTTPS header and controller-WSS
  handshake), and the Windows agent presents the same token in its WSS
  handshake. No separate legacy token is issued to the PC.
- Token transport: `X-Bridge-Token` header only in current clients. The
  relay's WSS handshake also accepts `token` / `access_token` query
  parameters and the HTTP API also accepts `Authorization: Bearer`, a body
  `token` field, then query (in that priority order) — server-side legacy
  compatibility that new clients must not use.
- There is deliberately **no loopback bypass**: requests from 127.0.0.1
  authenticate exactly like external ones.
- All endpoints authenticate before routing, including `/status`
  (an earlier version exposed `/status` pre-auth; that was fixed as an
  information leak and must not be reproduced).

### A.4 PC-facing WSS channel (relay ↔ Windows agent)

Connection:

- URL `wss://<vpsHost>:<vpsPort>/muse-bridge`, default port 443.
- Handshake headers: `X-Bridge-Token: <token>`,
  `User-Agent: MuseBridge-NextGen/1.2.6`.
- TLS options on the client: `servername = <sni>`, `rejectUnauthorized`
  default true, `handshakeTimeout` 15 s, `ca` = configured CA file if set,
  else the CA bundled beside the client. The client connects directly
  (explicit fresh HTTPS agent, proxy environment stripped during connect);
  the configured proxy URL, if any, affects only shell subprocesses, never
  the WSS channel.
- Handshake failure handling: a non-101 HTTP response is surfaced via
  `unexpected-response`. Any 4xx except 408/429 is treated as permanent:
  the client marks `auth_failed`, stops reconnecting, and reports the state
  upward. 401/403 therefore means "fix the token", never "retry harder".

Hello and liveness:

- First frame from the PC client after open:

```json
{ "type": "hello", "client": "muse-bridge/1.2.12",
  "hostname": "<os.hostname>", "platform": "win32", "arch": "x64",
  "binary_frame": true }
```

- The relay deduplicates by hostname: a new hello for a hostname that
  already has a connection evicts the old connection (marked closed and
  removed from the client set). There is always at most one live PC
  connection per hostname.
- "Online" is server-acknowledged, not socket-open: the client sends hello
  plus one WS ping and only considers itself online when any frame
  (message, ping, or pong) comes back; 20 s without any server frame is a
  ghost connection and triggers close + reconnect.
- Relay heartbeat: the relay pings each PC connection every 15 s
  (`PING_INTERVAL`) and reaps connections with 35 s of inactivity
  (`PING_TIMEOUT`). The PC client's watchdog checks every 15 s and force-
  reconnects after 45 s of silence. Implement the relay side at 15/35 as
  documented.
- Reconnect (client side, for parity): initial delay 2 s, ×1.5 backoff,
  capped at 30 s, reset to 2 s on acknowledged connect.

Request dispatch (relay → PC), one JSON text frame per request. Every
message carries `id`. Type dispatch on the PC is case-insensitive:

| type (aliases) | handler | request fields |
|---|---|---|
| `cmd`, `bash` | single command | `command`, `timeout` (s, clamp 1–600, default 60), `cwd` |
| `batch` | sequential batch | `commands[]` of `{command, timeout (1–300, default 60), cwd}` |
| `bash_bg`, `task_start`, `task` | background job start | `command`, `cwd` |
| `bash_output`, `task_output`, `monitor` | background job poll | `job_id` |
| `bash_kill`, `task_kill`, `taskstop` | background job kill | `job_id` |
| `read_file`, `read` | file read | `path`, optional `offset`/`limit` (line slice), `bypassCache`, `agent_id` |
| `write_file`, `write` | file write | `path`, `content` (text) or `data` (base64) or binary-frame payload |
| `edit_file`, `edit` | single edit | `path`, `old_string`/`target`, `new_string`/`replacement`, `replace_all` |
| `multi_edit`, `multiedit` | atomic multi-edit | `path`, `edits[]` |
| `notebook_edit`, `notebookedit` | notebook cell edit | `path`, `cell_id`/`cell_index`, `new_source`/`source`, `cell_type`, `edit_mode` |
| `glob_files`, `glob` | glob | `path`, `pattern` (default `**/*`), `max_results`/`head_limit` (default 100) |
| `grep_files`, `grep` | grep | `path`, `pattern`, `include`/`glob`, `case_sensitive`, `max_results`, `-A`/`-B`/`-C` |
| `file_info`, `stat` | stat | `path` |
| `list_dir`, `ls` | list directory | `path` |
| `archive_7z`, `archive` | 7z create | `path`, `targets`/`target`/`files`, `level`, `password`, `timeout` |
| `extract_7z`, `extract` | 7z extract | `path`, `output_dir`/`output`/`dest`, `password`, `timeout` |

Unknown message types are ignored by the PC client (no response), so the
relay must only send types from this table.

Responses (PC → relay). The relay accepts exactly three response types and
pairs them by `id`; anything else is dropped (a historical bug sent
`cmd_result` and hung every call — do not invent new type names):

- Command success: `{ "type": "result", "id", "ok": true, "exitCode": n,
  "stdout": ..., "stderr": ... }` where each of stdout/stderr is either a
  plain string or a compressed field object (A.6), and the message carries
  `content_encoding` when any field is compressed.
- Command error paths (permission denied, null byte, read-only rejection,
  decompression failure): `{ "type": "result", "id", "exit": -1,
  "stdout": "", "stderr": "<reason>" }` — note `exit`, not `exitCode`, and
  no `ok`. Both shapes exist on the wire and pass through the relay
  untouched; consumers already tolerate both.
- Batch: `{ "type": "batch_result", "id", "ok": true,
  "results": [{ "exit", "stdout", "stderr" }, ...] }`.
- Everything else: `{ "type": "file_result", "id", "ok": bool, ... }` with
  per-op fields:
  - read (full): `data` (base64 string or compressed field), `bytes`,
    `hash` (first 16 hex chars of the SHA-256 of the raw bytes), optional
    `content_encoding`; read (slice): the tools engine's line-slice result
    spread into the object;
  - write: `bytes`, `version`, `hash`, `diagnostics`, `message`;
  - edit: `replaced`, `newBytes`, `lines`, `version`, `hash`, `rebased`,
    `summaries`, `diagnostics`, `message`;
  - stat: `size`, `mtime` (ms), `is_file`;
  - list_dir: `entries: [{name}]`, `formattedText`, `total`;
  - background start: `job_id` of the form `bg_<base36 time>_<seq>`;
    poll: `job_id`, `running`, `stdout`, `stderr`, `exit` (null while
    running); kill: `killed: true`;
  - errors: `ok: false, error: "<message>"` (permission failures are
    prefixed `[PermissionDenied] ...`).

Background job semantics on the PC: jobs are spawned directly in Git Bash
(`bash -l -c <command>`), output is accumulated as raw bytes capped at
256 KB per stream (oldest bytes dropped), decoded once at poll time
(UTF-8 first, GBK fallback). Jobs live in the agent process memory; an
agent restart loses them.

There is no chunking at the WSS JSON layer: one frame per message, bounded
by the 64 MB cap. Large-file workflows in the old operation guides were
handled above the protocol (split into 1 MB pieces, per-piece reads,
checksum compare). See A.6 for the binary-frame alternative, which is
implemented on the PC but switched off at the relay by default.

PC-side execution notes the relay designer does not need but the agent
section (b) relies on: commands pass a permission check
(`PermissionManager`, allow/deny rules merged from `~/.muse/settings.json`
and `<workspaceRoot>/.muse/settings.json`, Claude-Code-style
`permissions.allow/deny` with default allow `*`), then a read-only-mode
gate if enabled, then an optional fast-exec path (no shell metacharacters
and first word resolves to a real `.exe`/`.com` → `execFile` directly),
else the shell lanes. `cwd` is validated to exist; commands containing a
null byte are rejected with an error result instead of hanging.

### A.5 Controller-facing channels (controller ↔ relay)

Path 1 — HTTPS API. Base prefix `/muse-api`. All endpoints authenticate
first (token priority in A.3). mused sends headers
`Host: <sni>`, `X-Bridge-Token`, `User-Agent: mused/1.0`, keep-alive.

| endpoint | body / notes |
|---|---|
| `GET /muse-api/status` | version, online client list, pending count, cached result count |
| `POST /muse-api/cmd` | `{command, timeout, cwd, wait, target}` |
| `POST /muse-api/batch` | `{commands, timeout, wait, target}` |
| `POST /muse-api/file` | `{op, ...}` translated by the relay's `type_map`: read→`read_file`, write→`write_file`, edit→`edit_file`, glob→`glob_files`, grep→`grep_files`, info→`file_info`, list→`list_dir`, archive→`archive_7z`, extract→`extract_7z`. Unknown op → 400 listing the supported ops. Remaining body fields (including `target`, `id`, and any `idem_key`) pass through into the WSS message. |
| `GET /muse-api/result/<id>` | poll a `wait=false` result (A.2) |

Path 2 — controller WSS (OPT-03 Scheme A, what the shipped mused prefers).
Same endpoint and TLS/SNI/CA arrangement as the PC channel, with
differences that the relay must implement as a distinct client kind:

- Hello: `{ "type": "hello", "client": "mused/4.0-wss",
  "role": "controller", "hostname": "<VM hostname>", "platform": "linux",
  "arch": "x64" }`. The `role: "controller"` field is the discriminator;
  PC hellos have no `role`.
- The controller channel authenticates with the daemon's own token (read
  from its token file), not a per-request token.
- Request frame: `{ "type": "api_request", "req_id": "<id>",
  "method": "POST", "path": "/cmd", "body": { ...API body... } }`.
- Response frame: `{ "type": "api_response", "req_id": "<same id>",
  "status": <HTTP-like status>, "body": "<JSON string of the response
  object>" }`. Note `body` is a JSON-encoded string, not an object.
- Confirmed coverage: the relay's controller handler supports
  `POST /cmd`; mused treats a 404 whose error contains
  `Unsupported controller API` as the signal to fall back to HTTPS for
  that request. A reimplementation may support more paths, but must
  reproduce that 404 error text for unsupported ones so mused's fallback
  keeps working.
- mused-side behavior to design against: it tries WSS first for every
  request and falls back to the HTTPS pool on any WSS failure; its mux
  reconnects with 1 s → 30 s doubling backoff, fails all pending requests
  on disconnect, and runs a silence watchdog (see open questions for the
  known control-frame blind spot). Its WSS client sends no WS pings
  (`ping_interval=None`), so the relay's own 15 s ping cadence is what
  keeps this channel observable; the relay must not reap the controller
  channel on the PC channel's 35 s rule without checking — in practice the
  relay's pings elicit pongs that reset activity on a correct
  implementation.

Idempotency and dedup — where they actually live:

- The relay itself has **no** request dedup beyond `pending_futures`
  pairing and the `completed_results` cache. If a controller retries a
  request with a new id, the PC executes it again.
- Dedup is implemented in mused (OPT-03 Layer 1): for
  `POST /cmd|/batch|/file` whose body carries `idem_key` matching
  `^[A-Za-z0-9._:-]{8,128}$`, a `DedupCache` provides single-flight
  semantics — the first caller executes, concurrent duplicates wait
  (up to 660 s) and replay its result, later duplicates within the TTL
  replay the cached `(status, body)` verbatim. Cache: 512 entries,
  TTL 600 s, but only 30 s for results over 1 MB. If the owner call
  raises (treated as a link error where the command likely never ran),
  the entry is dropped and one waiter becomes the new owner and
  re-executes.
- Consequence for callers: whoever wants at-most-once behavior for a
  non-idempotent command must supply a fresh `idem_key` per logical
  operation and reuse it across retries. The key is consumed by mused;
  it also passes through the relay inside the body, and the relay ignores
  it. The techbook flags the underlying at-least-once risk (a `/cmd` that
  executed, then whose channel dropped, can be re-executed over the
  fallback path) as an explicitly unresolved decision in the old project;
  our deployment should adopt the `idem_key` convention from day one.

### A.6 Compression and binary frames (end-to-end, relay passes through)

Application-level compression is end-to-end between mused and the PC
client; the relay forwards the bytes/JSON untouched and spends no CPU on
it. Two encodings: `deflate` (zlib level 1) and `zstd` (level 1, preferred
when both ends support it). Two forms:

- Field form: `{ "enc": "deflate"|"zstd", "data": "<base64>" }`.
- Message hint: top-level `content_encoding` set to the actual algorithm
  when any field is compressed. A legacy form (`compressed: true` with
  `data` being base64 of zlib bytes, for file writes) must still decode.

Encoding decision (per field / per file): skip below 2,048 bytes; skip
extensions in the no-compress set (`.zip .gz .tgz .7z .rar .bz2 .xz .zst
.png .jpg .jpeg .gif .webp .mp4 .mkv .mp3 .pdf .woff2 .exe .dll .so .pyc
.whl`); for payloads over 64 KB, probe-compress the first 1 KB and skip if
it does not shrink below 870 bytes; skip if the compressed form is not
smaller. Decompression is guarded: 256 MB output cap, and a ratio fuse
rejects inputs under 1 KB that expand past 10 MB.

Which fields: inbound to the PC, `command` (cmd/bash/bg), `commands`
(batch, the whole array as one compressed string/object), `data` and
`content` (write). Outbound from the PC, `stdout`/`stderr` of cmd results
are decided independently, and file-read `data`. A decompression failure
on the PC returns a `[DecompressError] ...` error result and the command
is **never executed**. mused applies the mirror-image field decode on
every response on both its WSS and HTTPS paths, regardless of its
`compress_enabled` setting; that setting only governs whole-body HTTP
`Content-Encoding: deflate`, and the standing deployment rule is
`compress_enabled=false` and `microbatch_enabled=false` on the controller
(the PC-side decode path was never production-validated in the old
deployment, and the relay has no microbatch demultiplexer wired in).

Binary frames (OPT-05 Phase 2, PC receive path implemented; relay sending
is behind the environment switch `MUSE_BRIDGE_BINARY_FRAME`, default off
in the old production and never activated there):

```text
byte 0      magic 0x4D
byte 1      flags (0x01 = payload is zstd-compressed)
bytes 2..3  header length, uint16 big-endian
bytes 4..   JSON header (the normal message object; must contain id and type)
rest        raw payload bytes
```

The PC routes a frame to this parser only on the WS library's `isBinary`
flag (never on buffer inspection — JSON text starts with 0x7B and was
historically misrouted). Malformed frames are logged and dropped, the
connection stays up. A write request arriving this way takes its payload
from the frame body instead of base64 fields. The PC advertises support
in hello (`binary_frame`); the relay may only send binary frames to
clients that advertise it, and only when its own switch is on.

### A.7 Relay implementation language: Python vs Node (evaluation)

Context: the user has explicitly removed the Python-only constraint —
anything needed may be installed on the VPS. The VPS currently runs
Debian 12 with Python 3.11.2 and no Node, has ~1.5 GB RAM available and
ample disk, and already runs nginx plus several gateway services that must
not be disturbed. The original relay was Python (asyncio, 637 lines) on a
1 vCPU / 1 GB VPS.

Option 1 — Python 3.11 reimplementation (from the techbook spec):

- Fidelity: the spec is written against the Python file — function names
  (`handle_connection`, `handle_ws_session`, `dispatch_request_to_pc`,
  `pick_client`, `load_controllers` semantics), constants, and quirks
  (fail-fast settlement payload, completed-results cache, the
  `Unsupported controller API` error text) map 1:1. Reimplementation risk
  is lowest here, and the contract-test ideas in the techbook (chapter
  2.7) translate directly.
- Zero new runtime on the VPS; the systemd unit shape is known
  (`/usr/local/bin/muse-bridge.py`, `/etc/muse-bridge/*`); the controller
  stack (mused) is also Python, so one language covers everything except
  the Windows agent.
- Known costs, all documented with measurements: Python's per-byte WS
  unmasking was the one real bottleneck (fixed in the original with a
  small C extension: 156 ms/MB → 0.22 ms/MB; the 64-bit-word variant is
  within 1.07× of the AVX2 one, i.e. memory-bandwidth-bound). On
  Python 3.11 we can (i) start with an int-based XOR unmask (whole-buffer
  big-int XOR is C-speed and adequate at our scale), and (ii) only build
  the C extension for cp311 if benchmarks demand it. `orjson` is
  available for 3.11 (dumps of 100 KB: 342 µs → 23 µs in their bench) and
  `uvloop` is optional with a documented stdlib fallback.
- The hand-rolled RFC 6455 framing must be reimplemented carefully
  (the original did its own `read_frame`/`send_frame`); this is the main
  engineering risk, mitigated by the protocol's simplicity here (JSON
  text frames, one ping cadence, 64 MB cap) and by contract tests
  against the real PC client and mused.

Option 2 — Node implementation (sharing code with the Windows agent):

- Real advantages: the `ws` library is mature (masking handled natively,
  `bufferutil` acceleration), JSON is native and fast, and the event
  loop model matches the single-process state design exactly.
- The code-sharing benefit is smaller than it first looks: by design the
  relay must **not** decode the end-to-end compressed fields or binary
  payloads — it routes ids and forwards frames. The genuinely shared
  logic would be limited to constants (no-compress extension set, size
  thresholds) and test fixtures, which are cheap to duplicate. The auth
  and routing logic exists only in the relay; the shell/file engines
  exist only in the agent.
- Costs: installing and maintaining a second runtime on a small,
  already-busy VPS (the user permits this, but it is still new patching
  surface next to unrelated production gateways); the reference behaviors
  would be re-derived from a Python-shaped spec into a different
  concurrency idiom (thread-crossing mused is Python; our relay's peers
  in test would be one Python and one Node implementation — same as
  option 1, so no test savings); large-frame JSON parsing still blocks
  the single loop, just at a higher threshold.

Recommendation: **Python 3.11 for the relay.** The deciding factors are
spec fidelity (the only reference we have is a Python-shaped
specification, and the original source is unavailable in either
language), zero new runtime on the VPS, one language across relay and
controller, and a documented performance ceiling (~3000 QPS, 128-way
concurrency measured end-to-end) that is far above this deployment's
single-user load. Node's advantages are real but mostly accrue to code
the relay deliberately does not run. Revisit only if A/B measurements
show the relay's event loop saturating, in which case the documented
escalation is the C unmask extension and orjson first, a rewrite last.

Implementation notes for the Python relay (from the spec, not the old
binary): stdlib-first asyncio; `orjson` behind the same `_json_loads` /
`_json_dumps` wrapper pattern with stdlib fallback; split header/payload
writes on send (zero-copy habit from §2.4.3 of the techbook); systemd
unit with the program at a deployment-owned path, token and
controllers.json under `/etc/muse-bridge/` at mode 600, timestamped
backup before every replacement, rollback = restore backup + restart.

---

## (b) Windows headless agent: reuse / replace plan (source 1.2.12)

The source splits cleanly. Everything protocol-bearing lives in
`src/muse/`, `src/shell/`, `src/utils/`; only three small files touch the
VS Code API.

Reuse unchanged:

| file | role |
|---|---|
| `src/muse/muse-bridge-client.js` | the whole WSS client: connect/hello/ack, heartbeat watchdog, reconnect, dispatch, all handlers, binary frames |
| `src/muse/lane-pipelined-shell.js` | Git Bash lane: resident `bash -l` workers, baseline pool (config 16, clamp 1–32) bursting to max (config 32, clamp 64), 5 min idle reap, high/normal priority queues, per-git-repo serialization |
| `src/shell/shell-pool.js` | native pool: 2× `powershell.exe` + 2× `cmd.exe` resident workers, acquire/release, 30 s default timeout, worker destroyed (taskkill /F /T) and replaced on timeout/crash, 500-use recycle |
| `src/muse/git-bash-guardian.js` | orphan process-tree reaping, shell subprocess proxy env |
| `src/muse/claude-tools.js` | file engine (Read/Write/Edit/MultiEdit/NotebookEdit/Glob/Grep/LS/7z) with MVCC version snapshots (512 MB cache), three-tier edit matching, LSP-diagnostics hook input |
| `src/muse/compress.js` | the A.6 codec, shared verbatim with the controller side's spec |
| `src/muse/permission-manager.js` | allow/deny rules (A.4) |
| `src/muse/tool-resolver.js` | tool path resolution and `~/.muse/tools.json` |
| `src/muse/shell-decode.js` | UTF-8/GBK output decoding |
| `src/muse/lane-windowed-sftp.js` | SFTP lane with 512 MB cache (referenced by client status; see open questions) |
| `src/utils/hardware.js`, `src/utils/mask.js` | small utilities |
| `src/muse/clash-selfheal.js` | optional; only used when the self-heal option is enabled. Keep the file, keep the option off. |

Runtime dependency: `ws` ^8.22.0 (optional `bufferutil` ^4.1.0). Node 24 is
already installed on ${PC_NAME}.

Replace (the `src/vscode/` trio plus `src/extension.js`):

1. `src/extension.js` → a new headless entry (`agent.js`). It must build
   the exact `MuseBridgeClient` options object the extension builds:
   `{ vpsHost, vpsPort, token, sni, caCertPath, readOnly, clashSelfHeal,
   gitBashPath, poolSize, maxPoolSize, shellPool, workspaceRoot,
   localProxyUrl, onStatusChange, logger, vscode, getLspDiagnostics }`.
   Headless substitutions: `logger` writes to a rotating file and stdout;
   `onStatusChange` records state (`connecting` / `online` / `offline` /
   `auth_failed`) to log plus a small status JSON file; `vscode: null`
   (the client only uses it for warning popups, which are already
   null-guarded); `getLspDiagnostics: null` (the tools engine accepts
   null; the only loss is the "diagnostics piggyback" appended to
   write/edit results — an editor-only feature with no protocol impact);
   `workspaceRoot` from configuration instead of the VS Code workspace.
   Process lifecycle (start/stop/restart on config change) replaces the
   extension's activate/deactivate and command registrations; the
   interactive commands (menu, set-token prompt, status popup) have no
   headless equivalent and are dropped by design.
2. `src/vscode/bridge-settings.js` → a settings module returning the same
   shape from a JSON file (suggested:
   `%USERPROFILE%\.muse-bridge\config.json`) with the same defaults:
   `enabled` false, `vpsHost` (no default — must be set to the new VPS),
   `vpsPort` 443, `readOnly` false, `gitBashPath` "", `poolSize` 16,
   `maxPoolSize` 32, `shellPool` true on win32, `proxyUrl` "".
   **Gap found in 1.2.12:** `getBridgeConfig()` does not return `sni`,
   `caCertPath`, `clashSelfHeal`, `debugCmd`, or `binaryFrame`, although
   `extension.js` reads `cfg.sni` / `cfg.caCertPath` / `cfg.clashSelfHeal`
   — in the shipped plugin those silently fall back to the client's
   built-in defaults (SNI `ai.vpsXX.local`, bundled CA). The headless
   settings module must return these fields explicitly, or the new
   deployment's SNI and CA cannot actually be configured.
3. `src/vscode/secrets.js` → a token module. Reuse is nearly free:
   `getBridgeToken(context)` already falls back, in order, to
   (1) SecretStorage when a context is provided, (2) environment
   `MUSE_BRIDGE_TOKEN`, (3) files `~/.muse/token`,
   `~/.muse-bridge-token`, `./vpsXX_credentials.txt`,
   `~/Documents/vscode_env/vpsXX_credentials.txt`, accepting a bare
   `muse_sec_…` token or a `Token:`/`安全 Token:` labelled line. Headless:
   pass a stub context without `secrets`, provision the fresh controller
   token at `~/.muse/token`, and restrict that file's ACL to the logon
   user. `setBridgeToken`/`clearBridgeToken` become file operations or
   no-ops; the token key name `muse_bridge.token` is only a
   SecretStorage concept and disappears with the plugin.
4. `src/vscode/status-bar.js` → a status sink with the same state
   vocabulary (`stopped`, `starting`, `connecting`, `online`, `offline`,
   `error`) writing to log/status file. No functional replacement needed.

Two packaging decisions:

- **Bundled CA:** `src/muse/vps-root-ca.pem` inside the source tree is
  the *old* deployment's root CA. The client silently falls back to it
  whenever `caCertPath` is empty. For the new deployment either delete
  that file from the agent package and set `caCertPath` explicitly to the
  freshly generated CA, or overwrite it with the new CA at packaging
  time. Leaving it in place untouched would pin trust to the colleague's
  CA — not acceptable.
- **Shell semantics:** with `shellPool` on, `cmd` requests run in the
  native PowerShell pool worker; with it off, they run in the Git Bash
  lane. The old production's command conventions (and the controller's
  backslash pre-doubling, section (c)) assume Git Bash downstream.
  Recommendation: ship with `shellPool: false` for behavioral fidelity,
  and let the A/B tests decide whether to enable the pool (its measured
  benefit is cold-start latency: pooled cmd ~1–3 ms vs 150–300 ms spawn).
- **Git Bash resolution:** `resolveBash()` order is configured
  `gitBashPath` → `~/.muse/tools.json` (`gitSh`/`gitBash` entries) →
  candidates `%USERPROFILE%\git\bin\{sh,bash}.exe`,
  `C:\Program Files\Git\bin\…`, `C:\Program Files (x86)\Git\bin\…`.
  ${PC_NAME}'s Git actually lives under the per-user Programs folder, which
  is **not** in the candidate list, so deployment must pin `gitBashPath`
  in the agent config (or add a tools.json entry). Without a bash the
  client refuses to start.
- **Hosting on ${PC_NAME}** (decided with the user): a per-user logon
  scheduled task running `node agent.js` under the user's own account
  (the account has no local Administrators membership, and none is
  needed) — not a SYSTEM service, not the VS Code plugin. The agent stays
  online independent of VS Code; files it writes are ordinary disk files
  and appear in the user's VS Code like any external tool's edits.

---

## (c) Controller-side setup facts (mused / musepc on the Muse VM)

File layout (from the onboarding package; `<HOME>` = the VM user's home):

- `~/.local/bin/mused` — the daemon (Python 3, single file, ~1114 lines).
- `~/.local/bin/museapi` — thin CLI client for the daemon.
- `~/.local/bin/musepcd` — resident front daemon used by musepc v4.
- `~/.local/bin/musepc` — also kept in the workspace bin; human/agent
  entry point (bash).
- `~/.local/bin/muse-health-check.sh` — periodic health checker (with the
  systemd timer below).
- `~/.local/lib/mused/` — the daemon's modules, on `PYTHONPATH`:
  `pool.py` (HTTPS connection pool), `tls_resume.py` (resumable TLS
  connection), `compress.py` (HTTP body compression), `microbatch.py`
  (request batcher, kept disabled).
- `~/.config/mused/config.json` (mode 600) and `~/.config/mused/token`
  (mode 600, the controller token, separate from the config).
- `~/.local/share/mused/mused.log` — daemon log, rotating 10 MB × 5, plus
  stderr.
- `~/.local/share/muse-bridge/vps-root-ca.pem` — where the config
  template points `ca_cert_path` on the VM side.
- systemd (user unit by default at `~/.config/systemd/user/mused.service`;
  a system-unit variant exists): `Environment` sets `PYTHONPATH=<HOME>/
  .local/lib/mused` and `HOME=<HOME>`; `ExecStart=<HOME>/.local/bin/
  mused`; `WorkingDirectory=<HOME>`; `Restart=always`, `RestartSec=5`,
  start limits 120 s / burst 5; `NoNewPrivileges=true`; output to
  journald. The egress proxy environment (`https_proxy` / `HTTPS_PROXY`)
  is injected at install time and is specific to the VM's runtime
  egress — values are environment material, are not reproduced here, and
  must be set for *this* VM at install time. Both upstream paths depend
  on it: the HTTPS pool CONNECTs through it, and the WSS mux reads the
  same variables.

`config.json` fields (template-verbatim keys; values shown are the
template's defaults — host, SNI, and CA path get the new deployment's
values at install time):

| key | template value / default | meaning |
|---|---|---|
| `vps_host` | (new VPS IP) | relay address |
| `vps_port` | 443 | |
| `sni` | (new bridge SNI) | TLS SNI for nginx routing; also sent as the HTTPS `Host` header |
| `ca_cert_path` | `<HOME>/.local/share/muse-bridge/vps-root-ca.pem` | pinned root CA |
| `token_file` | `<HOME>/.config/mused/token` | controller token file |
| `socket_path` | `/tmp/mused.sock` | local IPC socket |
| `heartbeat_interval` | 30 | seconds; heartbeat is `GET /muse-api/status` — even a 401 counts as "chain alive" |
| `compress_enabled` | false | standing rule: off (A.6) |
| `microbatch_enabled` | false | standing rule: off (A.6) |

Daemon behavior worth knowing before operating it:

- Upstream order: controller WSS first (A.5 path 2), HTTPS pool as
  fallback on any failure or on the relay's `Unsupported controller API`
  404. The WSS channel authenticates with the daemon token from
  `token_file` and is started lazily, waiting up to 8 s for readiness
  before the first fallback.
- Per-request read timeout is derived from the body: `timeout` clamped
  to 1–600 s plus 5 s margin; for `/batch`, the max sub-timeout; for
  `/cmd`/`/batch`/`/file` without an explicit timeout, 65 s (the PC-side
  default execution timeout is 60 s; the historical 10 s socket default
  silently abandoned slow commands).
- Local IPC: Unix socket, frame = 4-byte big-endian length + JSON
  (`struct "!I"`). Request frame `{ "token", "method", "path", "body" }`;
  response frame `{ "status": <int>, "body": "<raw upstream text>" }`,
  with `"error"` added on local failures (`status: 0`). One request per
  socket connection; the daemon handles each connection on a thread.
- Graceful shutdown on SIGTERM/SIGINT closes the listener, drops the
  upstream, and deletes the socket file. Operational rules from the old
  project that remain sound: never `pkill -f mused` (it matches the
  caller's own command line), never delete `/tmp/mused.sock` by hand,
  keep exactly one daemon instance.

CLI surface:

- `museapi [--target <PC>] <METHOD> <path> [<json body>]` — reads the
  token file, fills `body["target"]` from `--target` (overriding the
  body's), sends one frame, prints the upstream response.
  `museapi --health` is a tokenless liveness probe: exit 0 if the daemon
  answers (any status, including 401), 3 if the daemon is not running,
  1 on error. API failures surface to callers as exit code **253**
  (OPT-02), deliberately outside the remote command's own exit-code
  space.
- `musepc [--target <PC>] [--timeout <s>] [--cwd <dir>] '<command>'` →
  `POST /cmd` with `wait: true`. `musepc --read <path>` and
  `musepc --write <path> <content>` map to `POST /file` ops. Default
  target comes from `MUSEPC_TARGET` when `--target` is absent (set it
  to ${PC_NAME} for this deployment). In `cmd` mode musepc pre-doubles
  backslashes before JSON encoding, because the downstream chain strips
  one level (a command containing a single-backslash Windows path must
  go through musepc, or be encoded with this in mind, not hand-posted).
- musepc v4 first tries the resident `musepcd` on `/tmp/musepcd.sock`
  (a header-text socket protocol with a 128-wide bounded semaphore for
  overload protection, per techbook chapter 9) and falls back to the
  museapi path when the resident daemon is unavailable;
  `MUSEPC_NO_RESIDENT=1` forces the legacy path. musepc only performs
  its self-heal (`_ensure_mused`: systemd restart if the unit is
  enabled, else a careful manual relaunch) when the socket is missing
  or a call returns 253 — never on every invocation.

---

## (d) Open questions and ambiguities

1. **Relay and auth-patch sources are absent** (see the negative finding
   above). Everything in A.2–A.3 is reconstructed from the techbook,
   the runbook, and `controller-entry.json`. The exact verification
   order inside the production relay (controller tier vs legacy tier on
   each channel) is documented in prose in two places that agree, but
   there is no code to diff against. The reimplementation needs contract
   tests against the real mused and the real PC client before it is
   trusted.
2. **TLS termination topology at 443 is under-specified.** The techbook
   describes nginx `stream`/`ssl_preread` as an L4 passthrough that does
   not decrypt, and separately an nginx TLS vhost on 9443 mapping
   `/muse-bridge` to the plaintext relay on 8471. What terminates TLS
   for the SNI-routed 443 path (presumably the same TLS vhost reached
   via the stream layer) is not spelled out. Our design must pick and
   document one chain; the constraint from the VPS survey (separate
   server block/port, do not edit the existing 9443 vhost) makes a
   dedicated new TLS vhost the natural choice.
3. **Controller-WSS path coverage** beyond `POST /cmd` is undocumented
   (A.5). Whether the production relay also served `/file` and `/batch`
   over the controller WSS cannot be determined from the sources.
4. **`/file` read response shape.** The techbook's API example shows
   `{ "content": "<base64>", "encoding": "base64" }`, while the plugin
   returns `{ "data": ..., "bytes": ..., "hash": ... }`. A translation
   presumably happens in the relay (or the example is simplified); it is
   not specified anywhere we can read. mused's field-decode is tolerant
   of envelopes, but the exact relay-side mapping must be decided and
   tested.
5. **Result-shape duality** (`exit` vs `ok`/`exitCode`, A.4) is real in
   the 1.2.12 client and is load-bearing for mused's parsing; a
   reimplemented relay passes frames through so it is safe, but any
   future client refactor must not "normalize" these shapes without
   checking mused.
6. **mused constants not verified in the shipped binary:** the WSS
   watchdog silence threshold (techbook says 40 s; the flaw that
   websockets control frames do not reset it is documented as unfixed)
   and `WssMux.MAX_FRAME`. Also, whether the relay reaps the controller
   channel on the same 35 s rule as PC channels interacts with that
   watchdog bug; expect periodic controller-WSS reconnects in practice
   and do not treat them as incidents.
7. **Who mints `idem_key`?** mused only consumes `body.idem_key`; none
   of the museapi/musepc portions read show a generator. The convention
   (caller-generated per logical operation) is inferred, not documented.
8. **Plugin version skew:** the onboarding package ships a
   `muse-bridge-1.3.0.vsix`, the sanitized source is 1.2.12, and the
   techbook text variously says 1.2.11/1.2.12 is current. Any 1.3.0
   protocol deltas are unknown. The headless agent is built from the
   1.2.12 source and announces `muse-bridge/1.2.12`; if the relay
   reimplementation is validated against it, the vsix is not needed at
   all (consistent with the user's service-form decision).
9. **SFTP lane's protocol role** could not be resolved from the parts
   read: `WindowedSftpLane` is constructed by the client and exposed in
   status metrics, but no WSS dispatch type in 1.2.12 routes to it
   directly. It is reused unchanged; if it is dead weight it can be
   dropped later with evidence.
10. **Egress on this Muse VM** must be confirmed at install time: the
    shipped unit's proxy variables belong to the colleague's VM
    runtime. mused cannot reach the VPS without a working egress route
    (CONNECT proxy or direct), and which one this VM uses is an install
    time fact, not a document fact.
11. **The old deployment's VPS ran Debian 13 / Python 3.13** (per the
    techbook), ours runs Debian 12 / Python 3.11.2. Nothing in the
    protocol depends on it, but the C unmask extension and any
    version-specific wheels must be built for cp311 if used.

---

## (e) Performance methodology and recorded baselines (from the techbook)

Methodology used by the old project (chapters 2.4, 10, 11), worth
reusing for our A/B tests:

- **End-to-end functional load test:** 128 parallel agents, each
  assigned a different function, all traversing the full production
  chain (agent → musepc → musepcd → mused → VPS → PC). Coverage included
  every CLI flag, malformed-input handling, file operations (Chinese
  filenames, nested paths, binary PE files, 10 KB and 1 MB files verified
  by MD5), and infrastructure invariants (socket permissions, token file
  permissions, zero token leakage into logs, single-instance locks,
  byte-identical output between daemon and legacy paths, identical
  exit-code/timeout semantics).
- **Layer isolation:** the same load was run against successive layers
  to attribute bottlenecks — daemon socket direct vs the bash thin
  client being the decisive comparison (below).
- **Failure injection:** daemon killed (auto-restart in 3 s), socket
  file deleted (~13 s recovery via the outer chain), 50 rapid
  connect/disconnect cycles (no FD/thread leaks), PC offline (correct
  "not online" routing results, counted as environment failures, not
  regressions).
- **Microbenchmarks at the relay:** payload-sized benches for unmasking
  and JSON on the VPS itself, reported with before/after tables.

Recorded numbers (old chain: colleague VM → old VPS → a 20-core Windows
PC; treat as reference magnitudes, not predictions for ${PC_NAME}):

| measurement | result |
|---|---|
| Single `/cmd`, full chain | P50 ≈ 420 ms |
| 128 concurrent echo, SSH legacy path | 41.35 s total, 3.1 req/s |
| 128 concurrent echo, HTTPS path | 11.02 s total, 11.6 req/s (3.75×) |
| Daemon socket direct, 128 concurrent | 66.9 req/s, 0 errors |
| Daemon socket direct, 64 concurrent | 71.4 req/s |
| musepc thin client (bash + socat), 128 concurrent | 128/128 success but 5.1 req/s — bash spawn overhead (~172 ms per invocation) is the bottleneck |
| Final integration: 128 workers × 5-step workflow | 640 calls, 0 failures, no leftover files on the PC |
| WS unmask, 10 MB payload (VPS) | 156 ms/MB → 0.22 ms/MB with the C extension (~700×; AVX2 only 1.07× over the 64-bit variant — memory-bandwidth bound ≈ 4.6 GB/s) |
| JSON (VPS, Python 3.13) | loads 227 B: 6.69 µs → 0.76 µs; loads 104 KB: 260 µs → 72 µs; dumps 100 KB: 342 µs → 23 µs (orjson) |
| zstd vs zlib on log payloads | ≈ 16× faster |
| Binary frames, 100 KB payload | −25.0 % bytes on the wire |
| Pooled native shell (PC) | cmd latency ~150–300 ms cold spawn → ~1–3 ms pooled |

Implications for our A/B design (solutions A and B share the same
Windows agent and relay, per the user's plan): measure at the same
layers they did — controller-CLI wall time, mused socket direct, relay
dispatch, and PC execution — and keep model-side generation time out of
the tool timings, as the user required. Their experience says the
expected bottlenecks, in order, are: per-invocation process spawn on
the controller side (use the resident/socket paths, not bash wrappers,
in the harness), PC-side shell spawn (pooling decision in section (b)),
and only then relay CPU. Their relay numbers also say a Python relay
at this scale is not the constraint: the full-chain 128-concurrency
test reached its limits in the controller daemon's connection handling
and PC-side Windows execution queuing, not in the VPS event loop.

## Addendum (2026-11-09): call-level batch demultiplexer LIVE

The relay now implements the requests-format demultiplexer at POST /muse-api/batch: a body {"requests":[{"id","method","path","body"}...], "target", "timeout"} is fanned out by Relay._handle_batch_requests — each sub-request re-enters handle_api as an ordinary single call (sub-paths /cmd, /file, /batch legacy, /status; max 32; nested requests-format rejected; outer target forced on all subs; wait/id stripped per sub) and results return as {"ok":true,"results":[{"id","status","body"}...]} with partial-failure isolation. Legacy {"commands":[...]} behavior unchanged. Deployed 2026-11-09 (file sha256 fdc00490…, backup /opt/muse-bridge/muse-bridge.py.bak-20261109-pre-demux on ${VPS_NAME}); local tests: test_batch_demux.py + full test_relay.py pass. mused microbatch remains OFF (not needed: callers send explicit lists). Verified live: mixed cmd+list+read in one trip to both ${VPS_NAME} and ${PC_NAME}; ${PC_NAME} agent needed no change.
