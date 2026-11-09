# A/B measurements — raw results (2026-11-01 night)

Methodology notes:
- Path A probe runs on the Muse VM: museapi -> mused (unix socket) -> HTTPS
  keep-alive pool via egress proxy -> VPS nginx -> relay -> WSS -> ${PC_NAME}
  agent (Git Bash). Full end-to-end path, per-call CLI process (like real use).
- Path B probe runs on the VPS loopback: MCP service -> relay -> WSS ->
  ${PC_NAME} agent. This EXCLUDES the VM->VPS public hop; the delta between the
  two cmd_echo P50s (~145 ms) is the measured cost of that hop plus mused's
  socket/pool processing, and B's true public-path figure is being confirmed
  separately through the connector client from the VM.
- Techbook baselines for the colleague's original deployment: single /cmd
  P50 ~420 ms; HTTPS chain 11.6 req/s at 128 concurrent echo.

## Scheme A (Muse VM, full path) — probe_a.py / probe_a_conc.py
- cmd_echo        n=30  P50 424.1 ms  P95 445.4  P99 483.9  mean 415.2  fails 0
- file_write_10k  n=10  P50 344.6 ms  P95 374.9             mean 350.7  fails 0
- file_read_10k   n=10  P50 304.5 ms  P95 333.7             mean 302.4  fails 0
- concurrency_10        10/10 ok   8.03 req/s   mean 940 ms   max 1242 ms
- concurrency_25        25/25 ok  15.35 req/s   mean 1135 ms  max 1551 ms
- long command (sleep 20, timeout 60): completed, wall 20.69 s
- agent restart recovery (Stop/Start scheduled task): online again in 6.7 s,
  first post-restart command succeeded

## Scheme B (VPS loopback segment) — probe_b.py / probe_b_conc.py
- cmd_echo        n=30  P50 279.6 ms  P95 289.6  P99 291.7  mean 280.9  fails 0
- file_write_10k  n=10  P50 213.5 ms  P95 223.7             mean 214.8  fails 0
- file_read_10k   n=10  P50 169.8 ms  P95 178.4             mean 170.9  fails 0
- concurrency_10        10/10 ok  27.95 req/s   mean 338 ms   max 347 ms
- concurrency_25        25/25 ok  21.43 req/s   mean 684 ms   max 1139 ms

## Scheme B public path (Muse VM, via the connector client, one keep-alive connection) — probe_b_public.py
- cmd_echo        n=30  P50 360.6 ms  P95 385.0  P99 391.1  mean 362.4  fails 0
- file_write_10k  n=10  P50 272.9 ms  P95 331.3             mean 279.3  fails 0
- file_read_10k   n=10  P50 249.5 ms  P95 261.0             mean 250.8  fails 0

Headline comparison (same start point, the Muse VM; echo command):
A full path P50 424 ms vs B full path P50 361 ms — B is ~15% faster
end to end (single persistent connection + no per-call daemon hop),
and B's server-side segment alone is 280 ms.

Transport lesson (root cause of several hours of flaky first calls):
from this VM, an egress-proxy CONNECT addressed to the NAME
${VPS_SNI} black-holes TLS handshakes (the proxy resolves the
name itself and that route drops the handshake), while a CONNECT to
the VPS IP with the TLS SNI set to ${VPS_SNI} — exactly what
mused does — connects instantly. The connector client was changed to
mused's pattern (IP tunnel + SNI name) and the full OAuth dance +
initialize + tools/list then completed in 0.6 s.

New-connection attempts from both the Muse VM (via egress proxy) and
${PC_NAME} to the VPS intermittently hang tonight (first flow black-holed,
retry succeeds). Persistent connections (mused pool, agent WSS) are
stable. All probe numbers above were taken over warm/persistent paths
or with the effect amortized; cold-start first-call latency can spike
to seconds when a new flow is black-holed. This is a property of the
network path, not of either bridge implementation.

## Re-test 2026-11-01 17:16 — actual usage form, B with warm daemon (probe_ab_now.py)

Both paths driven exactly as used in practice (one subprocess per call;
A via museapi on mused's warm pool, B via the connector shim on the new
warm daemon), echo/file rounds interleaved to share network conditions:

| test | A | B (daemon) |
|---|---|---|
| cmd_echo P50 (n=20 rounds) | 407 ms | 527 ms |
| file_write_10k P50 | 405 ms | 499 ms |
| file_read_10k P50 | 294 ms | 412 ms |
| burst 6x echo wall | 1,276 ms, 0 fails | 1,669 ms, 0 fails |
| long cmd 10 s | 10.45 s ok | 10.58 s ok |

Isolated follow-up series (back-to-back, warm): A echo P50 409 ms
(394-413, 10/10); B echo via daemon P50 528 ms (511-577, 8/8); B status
via daemon P50 246 ms (6/6); B echo DIRECT (no daemon) P50 1,184 ms
(689-1,404) — the daemon roughly halves B's per-call cost and tightens
its spread, but warm B still trails warm A by ~120 ms per call (shim
process + daemon dispatch + the MCP service hop; B's in-process ceiling
measured this morning was 361 ms).

Stability finding: the interleaved run's first minute produced the
day's only failures (A 3/20, B 2/20 on echo; everything after warm-up,
including all file/burst/long tests, was failure-free). Daemon log
evidence: after ~50 min idle, all 3 pooled B connections had been
closed by the far end (nginx keepalive) and were discovered dead by
the first real calls at 17:16:55 — discarded and re-warmed within
seconds. A's failures fell in the same cold window (mused had just
been revived by the warm-up call). Warm-state stability of both paths
is excellent; the weak point was idle-cold recovery.

Fix applied to the B daemon the same day: the maintainer now sends a
cheap relay-status keepalive on floor connections idle >40 s (the
mused pool's 45 s stale-eviction is the same lesson), absorbing dead
connections in the background instead of on the next user call.

Keepalive verification (same day): daemon restarted with the fix,
floor warmed to 3, then 105 s fully idle; the first run_command
afterwards succeeded first try in 0.498 s with pool still at 3 and
zero discards in the daemon log.

## Optimization pass 2026-11-01 17:5x — segment budget + shim diet

Segment measurements (medians): bare python3 startup 24 ms; B shim
process 90 ms (of which ~65 ms was importing ssl/http.client/etc. that
the shim path never uses); museapi overhead only ~30 ms (its 125 ms
--health figure is a real upstream status round trip); one extra bash
spawn on the PC ~= 7 ms (the agent spawns bash with no login/rc files —
no shell-startup win available; shellPool stays off); PC<->VPS direct
RTT ~= 167 ms (curl connect time, x3 consistent) — the single largest
shared cost in both paths and pure network physics. Relay /cmd awaits
an asyncio future (no polling quantization); muse-mcp creates a fresh
loopback connection to the relay per call (~1-3 ms — pooling it and
nginx upstream keepalive were evaluated and skipped: gains below the
path's run-to-run noise, nonzero restart risk).

Change shipped: client.py loads the network modules lazily (direct
path and daemon only). Shim 90 -> 62 ms; B status 209 -> 165 ms.

Full interleaved re-probe after the change (probe_ab_now.py, warm,
ZERO failures on both paths across all tests — the keepalive fix
holding): echo P50 A 408 / B 449 (was 407 / 527; gap ~120 -> ~41 ms,
the remainder being the MCP service translation hop); write_10k
337 / 399; read_10k 295 / 334; burst_6 wall 1,086 / 1,238 ms;
long_cmd_10s 10.46 / 10.49 s. A unchanged (no code touched; its
run-over-run deltas are network variance). Remaining floor for both:
~167 ms PC<->VPS RTT + ~100 ms VM->VPS hop + process/dispatch costs.

## Optimization round 2 2026-11-01 18:1x — C shims (user: A primary, B backup, RDC last)

The remaining per-call tax was interpreter startup (python3 floor
~24 ms + imports). Two tiny C clients now speak the daemon protocols
directly: muse-a (mused's length-prefixed JSON framing; output
byte-identical to museapi; exit 253 = daemon down, mirroring museapi's
OPT-02 convention; source muse-bridge-deploy/shims/muse-a.c) and
muse-b (the B daemon's line protocol; exit 111 = use the python
client, preserving its self-heal/fallback discipline; source in the
connector's bin/). The python clients remain bootstrap/repair/fallback.

Interleaved re-probe with C entry points (probe_c.py), zero failures,
zero daemon fallbacks — echo P50 A 358 / B 410 ms (journey: A 424 ->
407 -> 358; B 1184 direct -> 527 daemon -> 449 lazy-imports -> 410 C
shim); write_10k 287 / 332; read_10k 247 / 302; burst_6 wall 559 /
1,177 ms (A's burst halved: six python startups no longer contend;
B's burst is bounded by its pool cap of 5); long_cmd_10s 10.36 /
10.47 s. Residual A-B gap ~50 ms is the MCP service translation hop.
Both daemons were found dead after another VM runtime restart during
this work and self-healed via musepc / the python client exactly as
designed.
