#!/usr/bin/env bash
# Called by stage 23 after it created the user, the state directory and
# /opt/vpn2-emergency/vpn2_emergency (hy2.py included) and installed qrencode,
# curl, openssl, nftables and polkitd: nothing is downloaded from here on.
# Arguments: exit IP; optional dedicated UDP port. Never changes normal sing-box.
set -euo pipefail
source_dir=$(dirname "$(readlink -f "$0")")
server_ip=${1:?exit IP required}
port=${2:-18443}
getent passwd vpn2-emergency >/dev/null
if ! getent passwd vpn2-hy2 >/dev/null; then
  useradd --system --no-create-home --shell /usr/sbin/nologin vpn2-hy2
fi
module=/opt/vpn2-emergency/vpn2_emergency/hy2.py
install -d -m 0755 /opt/vpn2-emergency/vpn2_emergency /etc/vpn2
install -m 0644 "$source_dir/vpn2_emergency/hy2.py" "$module"
for unit in vpn2-emergency-hy2.service vpn2-emergency-hy2-probe.service; do
  install -m 0644 "$source_dir/$unit" "/etc/systemd/system/$unit"
done
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
# The agent unit has NoNewPrivileges, so sudo is unusable there; polkit is asked
# over D-Bus by systemd itself and grants exactly these verbs on these units.
cat > /etc/polkit-1/rules.d/50-vpn2-emergency-hy2.rules <<'EOF'
polkit.addRule(function (action, subject) {
  if (action.id == "org.freedesktop.systemd1.manage-units" &&
      (action.lookup("unit") == "vpn2-emergency-hy2.service" ||
       action.lookup("unit") == "vpn2-emergency-hy2-probe.service") &&
      subject.user == "vpn2-emergency") {
    var verb = action.lookup("verb");
    if (verb == "start" || verb == "stop" || verb == "restart") {
      return polkit.Result.YES;
    }
  }
});
EOF
chmod 0644 /etc/polkit-1/rules.d/50-vpn2-emergency-hy2.rules
systemctl daemon-reload
if ! systemctl is-active --quiet vpn2-emergency-hy2.service; then
  python3 "$module" close
fi
if command -v ufw >/dev/null; then
  ufw allow "$port/udp" comment 'emergency hysteria2'
fi
