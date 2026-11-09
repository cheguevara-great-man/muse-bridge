set -e
cd /tmp
rm -rf /tmp/mb-mcp && mkdir -p /tmp/mb-mcp
tar -xzf /root/mcp-bundle.tar.gz -C /tmp/mb-mcp

echo ---VHOST-GUARD---
LIVE_HASH=$(sha256sum /etc/nginx/sites-enabled/muse-bridge | awk '{print $1}')
echo "live vhost sha256: $LIVE_HASH"
if [ "$LIVE_HASH" != "2deb86dc08d403cba21736cc7002fb29d999ba5044fa7158b880eb598d777f41" ]; then
  echo "ABORT: live vhost differs from expected v1; not touching nginx"
  exit 3
fi

echo ---INSTALL-PROGRAM---
mkdir -p /opt/muse-mcp /var/lib/muse-mcp
install -m 644 /tmp/mb-mcp/muse-mcp.py /opt/muse-mcp/muse-mcp.py
chmod 700 /var/lib/muse-mcp
install -m 644 /tmp/mb-mcp/muse-mcp.service /etc/systemd/system/muse-mcp.service

echo ---PROVISION-BRIDGE-TOKEN---
if [ -f /etc/muse-bridge/mcp-bridge-token ]; then
  echo "mcp bridge token already exists; skipping"
else
python3 - <<'EOF'
import datetime, hashlib, json, os, secrets
token = "muse_sec_" + secrets.token_hex(20)
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
fi

echo ---PROVISION-OWNER-SECRET---
if [ -f /root/muse-mcp-owner-secret ]; then
  echo "owner secret already exists; skipping"
else
python3 - <<'EOF'
import hashlib, json, os, secrets
secret = secrets.token_urlsafe(32)
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
print("owner approval secret provisioned")
EOF
fi

echo ---NGINX-VHOST-V2---
cp -a /etc/nginx/sites-enabled/muse-bridge /etc/nginx/sites-enabled/muse-bridge.bak.$(date +%Y%m%d%H%M%S)
install -m 644 /tmp/mb-mcp/nginx-muse-bridge-v2.conf /etc/nginx/sites-enabled/muse-bridge
nginx -t

echo ---START-SERVICE---
systemctl daemon-reload
systemctl enable --now muse-mcp
systemctl reload nginx
sleep 2
systemctl is-active muse-mcp nginx muse-bridge
ss -tlnp | grep -E '8472' || true

echo ---SMOKE-DISCOVERY---
curl -s http://127.0.0.1:8472/.well-known/oauth-authorization-server
echo
echo ---SMOKE-MCP-NOAUTH-EXPECT-401---
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8472/mcp -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
echo ---SMOKE-RELAY-STATUS-WITH-MCP-TOKEN---
MCPTOK=$(cat /etc/muse-bridge/mcp-bridge-token)
curl -s -H "X-Bridge-Token: $MCPTOK" http://127.0.0.1:8471/muse-api/status
echo
echo ---BROWSER-GATEWAY-CERT-SANS---
ls /etc/browser-gateway/tls/ || true
for f in /etc/browser-gateway/tls/*.pem /etc/browser-gateway/tls/*.crt; do
  [ -f "$f" ] && echo "== $f" && openssl x509 -in "$f" -noout -subject -ext subjectAltName 2>/dev/null || true
done
echo INSTALL-MCP-DONE
