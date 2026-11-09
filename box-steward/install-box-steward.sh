#!/bin/bash
# Box steward installer — run AS ROOT on the VPS, AFTER phase 1 or standalone.
# Installs the on-demand Simular Box client. It does NOT start anything and
# CANNOT work until the user places the raw API key on the VPS:
#
#   1. At https://box.simular.ai (keys page) create/copy a key (cb_...).
#      NOTE: consider issuing a SEPARATE key for the VPS steward so it can
#      be revoked independently of the Muse VM connector.
#   2. On the VPS:  install -m 600 /dev/null /etc/box-steward/key
#      then paste the key as the file's single line (e.g. with nano/vim).
#   3. Verify:  /opt/box-steward/boxsteward.py call --name box_status
#      (first call warms the session; status shows keeper state via
#       /opt/box-steward/boxsteward.py status; it self-stops after
#       60 idle minutes, or immediately via .../boxsteward.py stop)
#
# Why the key cannot come from the Muse VM: the VM connector uses Muse's
# dynamic-credential surrogate (authd swaps a hsurr:* placeholder for the
# real key on egress); the raw key is not present on the VM in any file.
#
# Rollback: /opt/box-steward/boxsteward.py stop; rm -rf /opt/box-steward
#           /etc/box-steward /run/box-steward
set -e
cd "$(dirname "$0")"
if [ "$(id -u)" != "0" ]; then echo "must run as root" >&2; exit 1; fi
mkdir -p /opt/box-steward /etc/box-steward
install -m 755 boxsteward.py /opt/box-steward/boxsteward.py
chmod 700 /etc/box-steward
if [ -f /etc/box-steward/key ]; then
  chmod 600 /etc/box-steward/key
  echo "key file present (contents not read). Steward is ready:"
  echo "  /opt/box-steward/boxsteward.py call --name box_status"
else
  echo "installed, but NO KEY yet — nothing was started."
  echo "Place the Simular API key at /etc/box-steward/key (mode 600),"
  echo "see the header of this script for the exact steps."
fi
