# Muse Bridge relay — deployment notes (NOT yet deployed)

Everything here is a plan for the VPS (${VPS_IP}, Debian 12,
Python 3.11). Nothing in this folder has been installed anywhere.

## Files and where they go

| local file | VPS location |
|---|---|
| `muse-bridge.py` | `/opt/muse-bridge/muse-bridge.py` |
| `muse-bridge.service` | `/etc/systemd/system/muse-bridge.service` |
| controllers registry | `/etc/muse-bridge/controllers.json` (mode 600, SHA-256 keys only, created by the token step) |
| TLS material | `/etc/muse-bridge/tls/` (server cert + key + pinned root CA, generated fresh for this deployment) |

The relay binds **127.0.0.1:8471 only** and has no TLS of its own.
nginx terminates TLS and proxies both transports to that port.
Direct access to 8471 from the network must remain impossible
(loopback bind; do not add firewall openings for it).

## Install steps

1. Create the directories: `/opt/muse-bridge/`, `/etc/muse-bridge/`,
   `/etc/muse-bridge/tls/`.
2. Copy `muse-bridge.py` to `/opt/muse-bridge/muse-bridge.py`.
3. Make sure `/etc/muse-bridge/controllers.json` exists (mode 600)
   before first start, or the relay will only accept the legacy token
   from `MUSE_BRIDGE_TOKEN` / `/etc/muse-bridge/token`.
4. Install the unit, then `systemctl daemon-reload` and
   `systemctl enable --now muse-bridge`.
5. Smoke test on the VPS itself (auth is enforced on loopback too):
   `curl -H "X-Bridge-Token: <token>" http://127.0.0.1:8471/muse-api/status`
   must return the status JSON; without the header it must return 401.
6. Add the nginx vhost below, test with `nginx -t`, reload nginx.

## nginx vhost (dedicated server block — do not edit existing vhosts)

```nginx
server {
    listen 443 ssl;
    server_name ${VPS_SNI};

    ssl_certificate     /etc/muse-bridge/tls/server.crt;
    ssl_certificate_key /etc/muse-bridge/tls/server.key;

    # PC + controller WebSocket channel
    location /muse-bridge {
        proxy_pass http://127.0.0.1:8471;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    # Controller HTTPS API
    location /muse-api/ {
        proxy_pass http://127.0.0.1:8471;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

Notes:

- If port 443 on this VPS is ever claimed by an SNI (`ssl_preread`)
  router, add an SNI mapping for `${VPS_SNI}` to this vhost's
  backend instead of binding 443 twice. Listen on a dedicated
  alternative port rather than touching unrelated vhosts.
- Long proxy timeouts are required: `/cmd` may legitimately run for
  up to 600 s and `GET /result/<id>` long-polls for 60 s.
- The relay reads the real client IP from `X-Forwarded-For` only
  because the socket peer is nginx on loopback; the vhost must keep
  overwriting that header (as above), never append client-supplied
  values.

## Rollback

1. Remove the nginx vhost file, `nginx -t`, reload nginx.
2. `systemctl disable --now muse-bridge` and remove the unit,
   `systemctl daemon-reload`.
3. Optionally remove `/opt/muse-bridge/` and `/etc/muse-bridge/`.
   Nothing else on the VPS is touched by this deployment, so no other
   service needs restoration.
