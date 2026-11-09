#!/bin/bash
# Muse Bridge — VPS endpoint agent rollback. Run AS ROOT on the VPS.
# Removes ONLY what install-on-vps.sh added:
#   1. stops + disables + removes the muse-bridge-agent-vps unit
#   2. removes the ${VPS_NAME} endpoint from /etc/muse-bridge/controllers.json
#      (backup first; relay hot-reloads, no restart) and the "${VPS_NAME}"
#      entries appended to controller pcs lists
#   3. removes /opt/muse-bridge-agent and /etc/muse-bridge-agent
# The relay, nginx, gateway stack and the ${PC_NAME} endpoint are untouched.
set -e
if [ "$(id -u)" != "0" ]; then echo "must run as root" >&2; exit 1; fi

systemctl disable --now muse-bridge-agent-vps 2>/dev/null || true
rm -f /etc/systemd/system/muse-bridge-agent-vps.service
systemctl daemon-reload

if [ -f /opt/muse-bridge-agent/controllers-merge.py ]; then
  python3 /opt/muse-bridge-agent/controllers-merge.py rollback || true
fi

rm -rf /opt/muse-bridge-agent /etc/muse-bridge-agent
echo "rollback done: ${VPS_NAME} endpoint removed"
