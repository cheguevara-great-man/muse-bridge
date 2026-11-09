#!/bin/bash
# Muse Bridge — VPS endpoint agent installer (Scheme A pattern).
# Run AS ROOT on the VPS (${VPS_IP}), from this bundle directory.
#
# What it does:
#   1. Installs the minimal Python agent to /opt/muse-bridge-agent/
#   2. Generates a fresh agent token at /etc/muse-bridge-agent/token (0600)
#      — the plaintext NEVER leaves the VPS and is never printed; only its
#      SHA-256 is registered in the relay's controllers.json.
#   3. Merges the new endpoint (hostname "${VPS_NAME}") into
#      /etc/muse-bridge/controllers.json (timestamped backup first; the
#      relay hot-reloads it — no relay restart, no nginx change).
#   4. Installs + enables its own systemd unit with CPU/memory limits.
#   5. Verifies loopback: relay status must list ${VPS_NAME} online.
#
# What it does NOT touch: muse-bridge.service, nginx (any vhost),
# browser-gateway, sing-box, gost, HY2 — nothing else.
# Rollback: ./rollback-on-vps.sh (same directory).
set -e
cd "$(dirname "$0")"

if [ "$(id -u)" != "0" ]; then echo "must run as root" >&2; exit 1; fi
command -v python3 >/dev/null || { echo "python3 missing" >&2; exit 1; }
[ -f /opt/muse-bridge/muse-bridge.py ] || {
  echo "relay not found at /opt/muse-bridge/muse-bridge.py" >&2; exit 1; }

mkdir -p /opt/muse-bridge-agent /etc/muse-bridge-agent
install -m 755 vps-agent.py /opt/muse-bridge-agent/vps-agent.py
install -m 644 controllers-merge.py /opt/muse-bridge-agent/controllers-merge.py
[ -f README.md ] && install -m 644 README.md /opt/muse-bridge-agent/README.md

if [ ! -s /etc/muse-bridge-agent/token ]; then
  umask 077
  head -c 36 /dev/urandom | base64 | tr -d '\n' > /etc/muse-bridge-agent/token
  echo >> /etc/muse-bridge-agent/token
  chmod 600 /etc/muse-bridge-agent/token
  echo "generated fresh agent token (stored only in /etc/muse-bridge-agent/token)"
else
  echo "agent token already exists — keeping it"
fi

cat > /etc/muse-bridge-agent/config.json <<'EOF'
{
  "relay_url": "ws://127.0.0.1:8471/muse-bridge",
  "token_file": "/etc/muse-bridge-agent/token",
  "hostname": "${VPS_NAME}",
  "workspace_root": "/root",
  "read_only": false
}
EOF
chmod 644 /etc/muse-bridge-agent/config.json

python3 /opt/muse-bridge-agent/controllers-merge.py install

install -m 644 muse-bridge-agent-vps.service \
  /etc/systemd/system/muse-bridge-agent-vps.service
systemctl daemon-reload
systemctl enable --now muse-bridge-agent-vps
sleep 3

echo ---SERVICE---
systemctl is-active muse-bridge-agent-vps
echo ---RELAY-STATUS-VIA-AGENT-TOKEN---
TOKEN=$(cat /etc/muse-bridge-agent/token)
curl -s -H "X-Bridge-Token: $TOKEN" http://127.0.0.1:8471/muse-api/status
echo
echo ---DONE---
echo "Endpoint ${VPS_NAME} should appear in the status 'online' list above."
echo "From the Muse VM: museapi GET /status, then target '${VPS_NAME}'."
echo "Rollback: $PWD/rollback-on-vps.sh"
