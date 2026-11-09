# Muse Bridge MCP service (Scheme B) — deployment notes (NOT yet deployed)

Everything here is a plan for the VPS (${VPS_IP}, Debian 12,
Python 3.11). Nothing in this folder has been installed anywhere.
Prerequisite: the Scheme A relay (`muse-bridge.service`, loopback
127.0.0.1:8471) and its nginx 443 vhost for `${VPS_SNI}` are
already deployed, per `../relay/DEPLOY.md`.

The MCP service is a second, independent front end for the same relay:

- It listens on **127.0.0.1:8472 only** and has no TLS of its own; the
  existing nginx vhost terminates TLS and proxies to it.
- It speaks MCP Streamable HTTP at `/mcp`, protected by OAuth 2.0
  bearer tokens it issues itself (`/authorize`, `/token`, `/register`,
  `/.well-known/*`). Authorization fails closed until the owner
  approval secret is provisioned (step 3).
- It forwards tool calls to the relay at `http://127.0.0.1:8471` with
  its **own** bridge token (step 2), scoped to the single PC `${PC_NAME}`.
  Tools cannot name a target; the service always routes to ${PC_NAME}.
- Rollback is independent of the relay: disabling this service does not
  affect Scheme A (mused/musepc) at all.

## Files and where they go

| local file | VPS location |
|---|---|
| `muse-mcp.py` | `/opt/muse-mcp/muse-mcp.py` |
| `muse-mcp.service` | `/etc/systemd/system/muse-mcp.service` |
| OAuth state | `/var/lib/muse-mcp/state.json` (dir mode 700, file mode 600; created by the service/provisioning) |
| bridge token for this service | `/etc/muse-bridge/mcp-bridge-token` (mode 600, plaintext token) |
| owner approval secret (plaintext, owner only) | `/root/muse-mcp-owner-secret` (mode 600) |
| relay controller registry | `/etc/muse-bridge/controllers.json` (one entry added, step 2) |

## Install steps

Run everything as root on the VPS. Take a timestamped backup of any
file before replacing it, as with the relay deployment.

### 1. Program and state directory

```bash
mkdir -p /opt/muse-mcp /var/lib/muse-mcp
install -m 644 muse-mcp.py /opt/muse-mcp/muse-mcp.py
chmod 700 /var/lib/muse-mcp
```

### 2. Bridge token for the MCP service (third token)

The MCP service gets its own controller token, distinct from the mused
controller token and the ${PC_NAME} agent token. The plaintext lives only
in `/etc/muse-bridge/mcp-bridge-token`; `controllers.json` stores only
its SHA-256. The relay hot-reloads `controllers.json` by mtime, so no
relay restart is needed (if a smoke test still gets 401, restart
`muse-bridge` once and retest).

```bash
python3 - <<'EOF'
import datetime, hashlib, json, os, secrets

token = "muse_sec_" + secrets.token_hex(20)   # muse_sec_ + 40 hex chars
path = "/etc/muse-bridge/mcp-bridge-token"
with open(path, "w", encoding="utf-8") as fh:
    fh.write(token + "\n")
os.chmod(path, 0o600)

registry_path = "/etc/muse-bridge/controllers.json"
with open(registry_path, "r", encoding="utf-8") as fh:
    registry = json.load(fh)
registry[hashlib.sha256(token.encode()).hexdigest()] = {
    "muse": "muse-rd925",
    "pcs": ["${PC_NAME}"],
    "label": "mcp service (scheme B)",
    "enabled": True,
    "created": datetime.date.today().isoformat(),
}
tmp = registry_path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(registry, fh, indent=1, sort_keys=True)
os.replace(tmp, registry_path)
os.chmod(registry_path, 0o600)
print("mcp bridge token provisioned; controllers.json updated")
EOF
```

### 3. Owner approval secret

The `/authorize` approval form asks for this secret; only its SHA-256
goes into the OAuth state file. The plaintext is kept at
`/root/muse-mcp-owner-secret` (mode 600) solely so the owner can
retrieve it later (e.g. over SSH) when approving a connector. Never
paste it into chat, tickets, or logs.

Provision while the service is stopped (the service holds its state in
memory and would overwrite an external edit on its next write):

```bash
systemctl stop muse-mcp 2>/dev/null || true
python3 - <<'EOF'
import hashlib, json, os, secrets

secret = secrets.token_urlsafe(32)   # 32 random bytes, urlsafe-encoded
with open("/root/muse-mcp-owner-secret", "w", encoding="utf-8") as fh:
    fh.write(secret + "\n")
os.chmod("/root/muse-mcp-owner-secret", 0o600)

state_path = "/var/lib/muse-mcp/state.json"
state = {}
if os.path.exists(state_path):
    with open(state_path, "r", encoding="utf-8") as fh:
        state = json.load(fh)
state["owner_secret_hash"] = hashlib.sha256(secret.encode()).hexdigest()
tmp = state_path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(state, fh, indent=1, sort_keys=True)
os.replace(tmp, state_path)
os.chmod(state_path, 0o600)
print("owner approval secret provisioned (plaintext in /root/muse-mcp-owner-secret)")
EOF
```

### 4. systemd unit

```bash
install -m 644 muse-mcp.service /etc/systemd/system/muse-mcp.service
systemctl daemon-reload
systemctl enable --now muse-mcp
systemctl status muse-mcp        # expect: active, listening on 127.0.0.1:8472
curl -s http://127.0.0.1:8472/.well-known/oauth-authorization-server
```

The unit runs as root (matching the relay deployment pattern), with
`ProtectSystem=strict` and `ReadWritePaths=/var/lib/muse-mcp` as its
only writable path; the bridge token file under `/etc/muse-bridge/` is
read-only to it, which is all it needs.

### 5. nginx: add locations to the EXISTING bridge vhost

Back up the vhost first, then add the locations below **inside the
existing `server { ... }` block for `${VPS_SNI}`** in
`/etc/nginx/sites-enabled/muse-bridge` (the block created by the relay
deployment, next to the existing `/muse-bridge` and `/muse-api/`
locations). Do not create a second server block, and do not touch the
default 444 block or any other vhost.

```bash
cp -a /etc/nginx/sites-enabled/muse-bridge \
      /etc/nginx/sites-enabled/muse-bridge.bak.$(date +%Y%m%d%H%M%S)
```

```nginx
    # Scheme B: MCP endpoint (long calls: commands may run up to 600 s)
    location /mcp {
        proxy_pass http://127.0.0.1:8472;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    # Scheme B: OAuth endpoints + discovery
    location = /authorize {
        proxy_pass http://127.0.0.1:8472;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 60s;
    }

    location = /token {
        proxy_pass http://127.0.0.1:8472;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 60s;
    }

    location = /register {
        proxy_pass http://127.0.0.1:8472;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 60s;
    }

    location /.well-known/ {
        proxy_pass http://127.0.0.1:8472;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 60s;
    }
```

```bash
nginx -t && systemctl reload nginx
```

`X-Forwarded-For` is overwritten (never appended), exactly as in the
relay locations: the MCP service uses it only for /authorize rate
limiting and trusts it only because nginx sets it.

## Smoke tests

### A. Discovery and the /mcp auth gate (from any client with the CA)

```bash
CA=~/.local/share/muse-bridge/vps-root-ca.pem
curl -s --cacert "$CA" --resolve ${VPS_SNI}:443:${VPS_IP} \
     https://${VPS_SNI}/.well-known/oauth-authorization-server
curl -si --cacert "$CA" --resolve ${VPS_SNI}:443:${VPS_IP} \
     -X POST https://${VPS_SNI}/mcp \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# expect: HTTP 401 and a WWW-Authenticate: Bearer ... resource_metadata=... header
```

### B. Full OAuth dance on the VPS loopback (owner secret never printed)

Run on the VPS. The owner secret and all tokens stay inside shell
variables; nothing secret is echoed (the script prints only statuses
and the final command output).

```bash
python3 - <<'EOF'
import base64, hashlib, http.client, json, urllib.parse

BASE = ("127.0.0.1", 8472)
owner_secret = open("/root/muse-mcp-owner-secret").read().strip()
verifier = "smoke-verifier-0123456789abcdef0123456789abcdef"
challenge = base64.urlsafe_b64encode(
    hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

def call(method, path, body=None, form=False):
    conn = http.client.HTTPConnection(*BASE, timeout=30)
    headers, data = {}, None
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body)
            headers["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8", "replace")
    hdrs = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, hdrs, raw

# 1. register a throwaway public client
status, _, raw = call("POST", "/register", {
    "client_name": "smoke-test",
    "redirect_uris": ["http://127.0.0.1:8765/callback"],
    "token_endpoint_auth_method": "none"})
assert status == 201, raw
client_id = json.loads(raw)["client_id"]

# 2. approve with the owner secret
params = {"response_type": "code", "client_id": client_id,
          "redirect_uri": "http://127.0.0.1:8765/callback",
          "code_challenge": challenge, "code_challenge_method": "S256",
          "state": "smoke"}
form = dict(params, owner_secret=owner_secret)
status, hdrs, _ = call("POST", "/authorize", form, form=True)
assert status == 302, status
code = urllib.parse.parse_qs(
    urllib.parse.urlparse(hdrs["location"]).query)["code"][0]

# 3. exchange the code (PKCE)
status, _, raw = call("POST", "/token", {
    "grant_type": "authorization_code", "client_id": client_id,
    "code": code, "redirect_uri": params["redirect_uri"],
    "code_verifier": verifier}, form=True)
assert status == 200, raw
access = json.loads(raw)["access_token"]

# 4. call a tool end to end: MCP -> relay -> ${PC_NAME} agent
conn = http.client.HTTPConnection(*BASE, timeout=60)
conn.request("POST", "/mcp", body=json.dumps({
    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
    "params": {"name": "run_command",
               "arguments": {"command": "echo mcp-scheme-b-ok && hostname"}}}),
    headers={"Content-Type": "application/json",
             "Authorization": "Bearer " + access})
resp = conn.getresponse()
result = json.loads(resp.read().decode())
text = result["result"]["content"][0]["text"]
assert "mcp-scheme-b-ok" in text and "${PC_NAME}" in text, text
print("SMOKE OK:", text.strip().splitlines())
EOF
```

### C. Connector approval (owner, one time)

When the Muse custom connector first connects, it dynamically
registers itself and redirects the owner to
`https://${VPS_SNI}/authorize?...`. The owner opens that page,
enters the owner approval secret (retrieved from
`/root/muse-mcp-owner-secret` on the VPS), and approves. Access tokens
last 1 hour; the connector refreshes them automatically (30-day
refresh tokens, rotated on each use).

## Rollback

1. Remove the five added nginx locations from
   `/etc/nginx/sites-enabled/muse-bridge` (or restore the timestamped
   backup made in step 5), then `nginx -t` and reload nginx. The
   default 444 block and all other vhosts stay as they were.
2. `systemctl disable --now muse-mcp`, remove
   `/etc/systemd/system/muse-mcp.service`, `systemctl daemon-reload`.
3. Optionally remove `/opt/muse-mcp/` and `/var/lib/muse-mcp/`.
   Keep `/root/muse-mcp-owner-secret` only if a reinstall is planned;
   otherwise delete it.
4. Revoke this service's relay access without touching the other
   controllers: in `/etc/muse-bridge/controllers.json`, set
   `"enabled": false` on the entry labelled `mcp service (scheme B)`
   (the relay hot-reloads it), and/or delete
   `/etc/muse-bridge/mcp-bridge-token`. Scheme A is unaffected.
