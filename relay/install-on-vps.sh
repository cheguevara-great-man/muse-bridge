set -e
cd /tmp
rm -rf /tmp/mb-deploy && mkdir -p /tmp/mb-deploy
tar -xzf /root/muse-bridge-deploy-bundle.tar.gz -C /tmp/mb-deploy
install -m 755 /tmp/mb-deploy/muse-bridge.py /opt/muse-bridge/muse-bridge.py
install -m 644 /tmp/mb-deploy/muse-bridge.service /etc/systemd/system/muse-bridge.service
install -m 644 /tmp/mb-deploy/nginx-muse-bridge.conf /etc/nginx/sites-enabled/muse-bridge
nginx -t
systemctl daemon-reload
systemctl enable --now muse-bridge
systemctl reload nginx
sleep 2
echo ---LISTEN---
ss -tlnp | grep -E '8471|:443 ' || true
echo ---STATUS-NOAUTH---
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8471/muse-api/status
TOKEN=$(cat /root/muse-bridge-ca/agent-token)
echo ---STATUS-AUTH-LOOPBACK---
curl -s -H "X-Bridge-Token: $TOKEN" http://127.0.0.1:8471/muse-api/status
echo
echo ---STATUS-AUTH-NGINX-TLS---
curl -s --resolve ${VPS_SNI}:443:127.0.0.1 --cacert /etc/muse-bridge/ca-crt.pem -H "X-Bridge-Token: $TOKEN" https://${VPS_SNI}/muse-api/status
echo
echo ---SERVICE---
systemctl is-active muse-bridge nginx
echo INSTALL-DONE
