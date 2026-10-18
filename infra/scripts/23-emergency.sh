#!/usr/bin/env bash
# Stage 23: only the observer runs during normal operation.
set -euo pipefail
[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}"
SOURCE_DIR=${EMERGENCY_SOURCE_DIR:-/tmp/anysda/vpn2_emergency}
CONFIG_SOURCE=${EMERGENCY_CONFIG_SOURCE:-/tmp/anysda/emergency.json}
test -f "$SOURCE_DIR/agent.py"
test -s "$CONFIG_SOURCE"
python3 -m json.tool "$CONFIG_SOURCE" >/dev/null
if command -v ufw >/dev/null; then
  while read -r peer port; do
    ufw allow proto udp from "$peer" to any port "$port" comment 'vpn2 emergency observer'
  done < <(python3 - "$CONFIG_SOURCE" <<'PY'
import json
import sys
config = json.load(open(sys.argv[1]))
for node, address in config['nodes'].items():
    if node != config['node']:
        print(address['host'], config['nodes'][config['node']]['port'])
PY
)
fi
getent passwd vpn2-emergency >/dev/null || useradd --system --home-dir /var/lib/vpn2-emergency --shell /usr/sbin/nologin vpn2-emergency
install -d -m 0700 -o vpn2-emergency -g vpn2-emergency /var/lib/vpn2-emergency
install -d -m 0755 /opt/vpn2-emergency/vpn2_emergency
install -d -m 0750 -o root -g vpn2-emergency /etc/vpn2/emergency
install -m 0640 -o root -g vpn2-emergency "$CONFIG_SOURCE" /etc/vpn2/emergency/agent.json
for module in "$SOURCE_DIR"/*.py; do
  install -m 0644 "$module" /opt/vpn2-emergency/vpn2_emergency/
done
cat > /etc/systemd/system/vpn2-emergency.service <<'UNIT'
[Unit]
Description=VPN emergency observer
After=network-online.target
Wants=network-online.target
[Service]
User=vpn2-emergency
Group=vpn2-emergency
WorkingDirectory=/opt/vpn2-emergency
ExecStart=/usr/bin/python3 -B -m vpn2_emergency.agent --config /etc/vpn2/emergency/agent.json --state-dir /var/lib/vpn2-emergency
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/vpn2-emergency
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LogRateLimitIntervalSec=60
LogRateLimitBurst=10
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable vpn2-emergency.service
systemctl restart vpn2-emergency.service
systemctl is-active --quiet vpn2-emergency.service
install -d /var/anysda/.stamps
touch /var/anysda/.stamps/23-emergency
printf '[%s] 23-emergency done\n' "$HOST_TAG"
