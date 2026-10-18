#!/usr/bin/env bash
# Called by emergency deployment after its system user and state directory exist.
# Arguments: exit IP; optional dedicated UDP port. Never changes normal sing-box.
set -euo pipefail
source_dir=$(dirname "$(readlink -f "$0")")
server_ip=${1:?exit IP required}
port=${2:-18443}
getent passwd vpn2-emergency >/dev/null
if ! getent passwd vpn2-hy2 >/dev/null; then
  useradd --system --no-create-home --shell /usr/sbin/nologin vpn2-hy2
fi
apt-get install -y -qq qrencode curl openssl nftables sudo
install -d -m 0755 /usr/local/lib/vpn2-emergency/vpn2_emergency /etc/vpn2
install -m 0644 "$source_dir/vpn2_emergency/hy2.py" /usr/local/lib/vpn2-emergency/vpn2_emergency/hy2.py
install -m 0644 "$source_dir/vpn2-emergency-hy2.service" /etc/systemd/system/vpn2-emergency-hy2.service
install -d -o vpn2-emergency -g vpn2-emergency -m 0750 /var/lib/vpn2-emergency/hy2
active=0
systemctl is-active --quiet vpn2-emergency-hy2.service && active=1
python3 - "$server_ip" "$port" "$active" <<'PY'
import ipaddress
import json
from pathlib import Path
import sys

ip = str(ipaddress.ip_address(sys.argv[1]))
port = int(sys.argv[2])
if not 1024 <= port <= 65535:
    raise SystemExit('invalid emergency UDP port')
path = Path('/etc/vpn2/emergency-hy2.json')
desired = {'server_ip': ip, 'port': port}
if path.exists() and json.loads(path.read_text()) != desired and sys.argv[3] == '1':
    # Issued profiles name the current endpoint; change it with the next generation.
    raise SystemExit('stop emergency role before changing its endpoint settings')
path.write_text(json.dumps(desired) + '\n')
PY
cat > /etc/sudoers.d/vpn2-emergency-hy2 <<'EOF'
vpn2-emergency ALL=(root) NOPASSWD: /usr/bin/systemctl start vpn2-emergency-hy2.service, /usr/bin/systemctl restart vpn2-emergency-hy2.service, /usr/bin/systemctl stop vpn2-emergency-hy2.service
EOF
chmod 0440 /etc/sudoers.d/vpn2-emergency-hy2
visudo -cf /etc/sudoers.d/vpn2-emergency-hy2
systemctl daemon-reload
if ! systemctl is-active --quiet vpn2-emergency-hy2.service; then
  python3 /usr/local/lib/vpn2-emergency/vpn2_emergency/hy2.py close
fi
if command -v ufw >/dev/null; then
  ufw allow "$port/udp" comment 'emergency hysteria2'
fi
