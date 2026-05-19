#!/usr/bin/env bash
# Stage 20 — sing-box роутер на RU-ноде.
# Маршрутизирует трафик WG-клиентов через N×2 Hysteria2 outbound (exit × direct/warp)
# + direct-ru для российских адресов (SNAT через WAN-интерфейс).
# Iptables tproxy + client-isolation + SNAT. Идемпотентен.

set -euo pipefail

[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}" "${PUB_IP_OUT:?}" "${WG_CLIENT_CIDR:?}" "${HY2_DIRECT_PORT:?}" "${HY2_WARP_PORT:?}" "${EXIT_TAGS:?}"

case "$HOST_TAG" in ru) ;; *) echo "[$HOST_TAG] 20-ru-router is ru-only — skipping"; exit 0;; esac

STAMP_DIR=/var/anysda/.stamps
STAGE='20-ru-router'

mkdir -p /etc/anysda /etc/sing-box /var/lib/sing-box

# ----------------------------------------------------------------------------
# 1. sing-box install
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [1/5] sing-box"
SB_VER='1.10.3'
if [[ ! -x /usr/local/bin/sing-box ]] || ! /usr/local/bin/sing-box version 2>&1 | grep -q "$SB_VER"; then
  curl -sSL "https://github.com/SagerNet/sing-box/releases/download/v${SB_VER}/sing-box-${SB_VER}-linux-amd64.tar.gz" \
    | tar -xz -C /tmp
  install -m0755 "/tmp/sing-box-${SB_VER}-linux-amd64/sing-box" /usr/local/bin/sing-box
  rm -rf "/tmp/sing-box-${SB_VER}-linux-amd64"
fi
/usr/local/bin/sing-box version | head -1 | sed "s/^/[$HOST_TAG]   /"

# ----------------------------------------------------------------------------
# 2. GeoIP / Geosite DB
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [2/5] GeoIP/Geosite"
if [[ ! -f /var/lib/sing-box/geoip.db || $(find /var/lib/sing-box/geoip.db -mtime +7 2>/dev/null) ]]; then
  curl -sSL -o /var/lib/sing-box/geoip.db   'https://github.com/SagerNet/sing-geoip/releases/latest/download/geoip.db'
  curl -sSL -o /var/lib/sing-box/geosite.db 'https://github.com/SagerNet/sing-geosite/releases/latest/download/geosite.db'
fi
ls -la /var/lib/sing-box/{geoip,geosite}.db | sed "s/^/[$HOST_TAG]   /"

# ----------------------------------------------------------------------------
# 3. Secrets: clash-api на RU + пароли Hysteria2 с выходных нод
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [3/5] secrets"
gen_pwd() { openssl rand -base64 32 | tr -d '\n=' | head -c 32; }
[[ -f /etc/anysda/clash-secret.txt ]] || gen_pwd > /etc/anysda/clash-secret.txt
chmod 600 /etc/anysda/clash-secret.txt
RU_CLASH_SECRET=$(cat /etc/anysda/clash-secret.txt)

FS=/tmp/anysda/foreign-secrets.env
[[ -f "$FS" ]] || { echo "[$HOST_TAG] $FS не найден — оркестратор должен был его загрузить"; exit 1; }
source "$FS"

# Проверяем, что для каждой выходной ноды есть все нужные секреты
for _t in $EXIT_TAGS; do
  _T=$(echo "$_t" | tr a-z A-Z)
  eval ": \"\${${_T}_DIRECT:?секрет ${_t} DIRECT не найден в foreign-secrets.env}\""
  eval ": \"\${${_T}_WARP:?секрет ${_t} WARP не найден в foreign-secrets.env}\""
  eval ": \"\${${_T}_OBFS:?секрет ${_t} OBFS не найден в foreign-secrets.env}\""
done

# ----------------------------------------------------------------------------
# 4. Генерация sing-box конфига через Python-скрипт (поддерживает N нод)
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [4/5] sing-box router config"
GEN=/tmp/anysda/gen-router-config.py
[[ -f "$GEN" ]] || { echo "[$HOST_TAG] $GEN не найден"; exit 1; }

WG_OUT_IFACE=$(ip -4 -o route show default | awk '{print $5; exit}')
: "${WG_OUT_IFACE:?}"

export WG_OUT_IFACE RU_CLASH_SECRET EXIT_TAGS HY2_DIRECT_PORT HY2_WARP_PORT MGMT_IP

for _t in $EXIT_TAGS; do
  _T=$(echo "$_t" | tr a-z A-Z)
  # DOMAIN_{T} и {T}_HY2_DIRECT_PORT берутся из all.env (уже в env после source)
  # {T}_DIRECT/WARP/OBFS — из foreign-secrets.env (только что загружен)
  export "DOMAIN_${_T}"
  export "${_T}_DIRECT" "${_T}_WARP" "${_T}_OBFS"
  # Порт direct: per-exit из all.env или глобальный
  eval "export ${_T}_HY2_DIRECT_PORT=\${${_T}_HY2_DIRECT_PORT:-${HY2_DIRECT_PORT}}"
done

python3 "$GEN" > /etc/sing-box/config.json
chmod 600 /etc/sing-box/config.json

if ! /usr/local/bin/sing-box check -c /etc/sing-box/config.json; then
  echo "[$HOST_TAG] конфиг невалиден — сервис не трогаю"
  exit 1
fi

# ----------------------------------------------------------------------------
# 5. systemd service + iptables (tproxy + SNAT + client isolation)
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [5/5] systemd + iptables"

cat > /etc/systemd/system/sing-box.service <<'EOF'
[Unit]
Description=sing-box (anysda-vpn RU router)
After=network-online.target nss-lookup.target
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart=/usr/local/bin/sing-box run -c /etc/sing-box/config.json
Restart=on-failure
RestartSec=5s
LimitNOFILE=1048576
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable sing-box >/dev/null 2>&1
systemctl restart sing-box
sleep 2

systemctl status sing-box --no-pager -n 4 | head -6 | sed "s/^/[$HOST_TAG]   /"

# ufw: clash-api на 9090, только mgmt-сеть
ufw allow proto tcp from 10.99.0.0/24 to any port 9090 comment 'sing-box clash-api mesh' >/dev/null 2>&1 || true
ufw reload >/dev/null

cat > /etc/sysctl.d/99-anysda-vpn.conf <<'EOF'
net.ipv4.conf.all.route_localnet=1
EOF
sysctl -p /etc/sysctl.d/99-anysda-vpn.conf >/dev/null

cat > /etc/systemd/system/anysda-iptables.service <<EOF
[Unit]
Description=anysda-vpn — iptables rules for redirect + tproxy + peer isolation
After=network-online.target sing-box.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/anysda-iptables.sh up
ExecStop=/usr/local/sbin/anysda-iptables.sh down

[Install]
WantedBy=multi-user.target
EOF

cat > /usr/local/sbin/anysda-iptables.sh <<EOF
#!/usr/bin/env bash
set -euo pipefail
ACTION=\${1:-up}

WG_IFACE='wg0'
REDIRECT_PORT=7895
TPROXY_PORT=7896
WAN_IFACE='${WG_OUT_IFACE}'

if [[ "\$ACTION" == "up" ]]; then
  sysctl -w net.ipv4.conf.all.route_localnet=1 >/dev/null
  sysctl -w net.ipv4.conf.\$WG_IFACE.route_localnet=1 >/dev/null 2>&1 || true

  iptables -C FORWARD -i \$WG_IFACE -o \$WG_IFACE -j DROP 2>/dev/null || \\
    iptables -A FORWARD -i \$WG_IFACE -o \$WG_IFACE -j DROP

  iptables -t nat -C POSTROUTING -o \$WAN_IFACE -s ${WG_CLIENT_CIDR} -j MASQUERADE 2>/dev/null || \\
    iptables -t nat -A POSTROUTING -o \$WAN_IFACE -s ${WG_CLIENT_CIDR} -j MASQUERADE

  iptables -t nat -C PREROUTING -i \$WG_IFACE -p udp --dport 53 ! -d 10.8.0.1 -j DNAT --to-destination 10.8.0.1:53 2>/dev/null || \\
    iptables -t nat -A PREROUTING -i \$WG_IFACE -p udp --dport 53 ! -d 10.8.0.1 -j DNAT --to-destination 10.8.0.1:53
  iptables -t nat -C PREROUTING -i \$WG_IFACE -p tcp --dport 53 ! -d 10.8.0.1 -j DNAT --to-destination 10.8.0.1:53 2>/dev/null || \\
    iptables -t nat -A PREROUTING -i \$WG_IFACE -p tcp --dport 53 ! -d 10.8.0.1 -j DNAT --to-destination 10.8.0.1:53

  iptables -t nat -C PREROUTING -i \$WG_IFACE -p tcp -j REDIRECT --to-port \$REDIRECT_PORT 2>/dev/null || \\
    iptables -t nat -A PREROUTING -i \$WG_IFACE -p tcp -j REDIRECT --to-port \$REDIRECT_PORT

  iptables -C INPUT -p tcp --dport \$REDIRECT_PORT ! -i \$WG_IFACE -j REJECT 2>/dev/null || \\
    iptables -I INPUT 1 -p tcp --dport \$REDIRECT_PORT ! -i \$WG_IFACE -j REJECT
  iptables -C INPUT -p tcp -i \$WG_IFACE --dport \$REDIRECT_PORT -j ACCEPT 2>/dev/null || \\
    iptables -I INPUT 2 -p tcp -i \$WG_IFACE --dport \$REDIRECT_PORT -j ACCEPT

  if ip link show \$WG_IFACE >/dev/null 2>&1; then
    iptables-legacy -t mangle -C PREROUTING -i \$WG_IFACE -p udp --dport 53 -j RETURN 2>/dev/null || \\
      iptables-legacy -t mangle -A PREROUTING -i \$WG_IFACE -p udp --dport 53 -j RETURN
    iptables-legacy -t mangle -C PREROUTING -i \$WG_IFACE -p tcp --dport 53 -j RETURN 2>/dev/null || \\
      iptables-legacy -t mangle -A PREROUTING -i \$WG_IFACE -p tcp --dport 53 -j RETURN
    iptables-legacy -t mangle -C PREROUTING -i \$WG_IFACE -p udp -j TPROXY --tproxy-mark 0x1/0x1 --on-port \$TPROXY_PORT 2>/dev/null || \\
      iptables-legacy -t mangle -A PREROUTING -i \$WG_IFACE -p udp -j TPROXY --tproxy-mark 0x1/0x1 --on-port \$TPROXY_PORT
    ip rule list 2>/dev/null | grep -q 'fwmark 0x1' || ip rule add fwmark 0x1 lookup 100
    ip route show table 100 2>/dev/null | grep -q 'local default' || ip route add local default dev lo table 100
    iptables -C INPUT -m mark --mark 0x1/0x1 -j ACCEPT 2>/dev/null || \\
      iptables -I INPUT 3 -m mark --mark 0x1/0x1 -j ACCEPT
    echo "redirect tcp:\$REDIRECT_PORT + tproxy udp:\$TPROXY_PORT on \$WG_IFACE: enabled"
  else
    echo "redirect/tproxy на \$WG_IFACE: deferred (интерфейс не поднят — запустить после stage 30)"
  fi

elif [[ "\$ACTION" == "down" ]]; then
  iptables -D FORWARD -i \$WG_IFACE -o \$WG_IFACE -j DROP 2>/dev/null || true
  iptables -t nat -D POSTROUTING -o \$WAN_IFACE -s ${WG_CLIENT_CIDR} -j MASQUERADE 2>/dev/null || true
  iptables -t nat -D PREROUTING -i \$WG_IFACE -p udp --dport 53 ! -d 10.8.0.1 -j DNAT --to-destination 10.8.0.1:53 2>/dev/null || true
  iptables -t nat -D PREROUTING -i \$WG_IFACE -p tcp --dport 53 ! -d 10.8.0.1 -j DNAT --to-destination 10.8.0.1:53 2>/dev/null || true
  iptables -t nat -D PREROUTING -i \$WG_IFACE -p tcp -j REDIRECT --to-port \$REDIRECT_PORT 2>/dev/null || true
  iptables -D INPUT -p tcp --dport \$REDIRECT_PORT ! -i \$WG_IFACE -j REJECT 2>/dev/null || true
  iptables -D INPUT -p tcp -i \$WG_IFACE --dport \$REDIRECT_PORT -j ACCEPT 2>/dev/null || true
  iptables -D INPUT -m mark --mark 0x1/0x1 -j ACCEPT 2>/dev/null || true
  iptables-legacy -t mangle -D PREROUTING -i \$WG_IFACE -p udp --dport 53 -j RETURN 2>/dev/null || true
  iptables-legacy -t mangle -D PREROUTING -i \$WG_IFACE -p tcp --dport 53 -j RETURN 2>/dev/null || true
  iptables-legacy -t mangle -D PREROUTING -i \$WG_IFACE -p udp -j TPROXY --tproxy-mark 0x1/0x1 --on-port \$TPROXY_PORT 2>/dev/null || true
  ip rule del fwmark 0x1 lookup 100 2>/dev/null || true
  ip route flush table 100 2>/dev/null || true
fi
EOF
chmod +x /usr/local/sbin/anysda-iptables.sh

systemctl daemon-reload
systemctl enable anysda-iptables >/dev/null 2>&1
systemctl restart anysda-iptables

# ----------------------------------------------------------------------------
# 6. Sing-box manual routes: host-side apply script + systemd.path watcher
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [6/6] sing-box manual-routes watcher"

cat > /usr/local/sbin/anysda-apply-routes.py << 'PYEOF'
#!/usr/bin/env python3
import json, subprocess, sys, tempfile, os

MANUAL_ROUTES = '/etc/wireguard/manual-routes.json'
SB_CONFIG     = '/etc/sing-box/config.json'
SB_CONFIG_BASE= '/etc/sing-box/config-base.json'
SB_BIN        = '/usr/local/bin/sing-box'

if not os.path.exists(SB_CONFIG_BASE) and os.path.exists(SB_CONFIG):
    import shutil
    shutil.copy2(SB_CONFIG, SB_CONFIG_BASE)

if not os.path.exists(SB_CONFIG_BASE):
    print('config-base.json missing', file=sys.stderr); sys.exit(1)

try:
    with open(MANUAL_ROUTES) as f: rows = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    rows = []

with open(SB_CONFIG_BASE) as f: cfg = json.load(f)

manual_rules = []
for row in rows:
    rule = {'outbound': row['outbound']}
    if row['type'] == 'domain':
        val = row['value']
        rule['domain_suffix'] = [val[2:] if val.startswith('*.') else val]
    else:
        rule['ip_cidr'] = [row['value']]
    manual_rules.append(rule)

cfg['route']['rules'] = manual_rules + cfg['route']['rules']

with tempfile.NamedTemporaryFile('w', dir='/etc/sing-box', delete=False, suffix='.tmp') as tmp:
    json.dump(cfg, tmp, indent=2); tmp_path = tmp.name

try:
    subprocess.run([SB_BIN, 'check', '-c', tmp_path], check=True, capture_output=True)
    os.replace(tmp_path, SB_CONFIG)
except subprocess.CalledProcessError as e:
    os.unlink(tmp_path); print(f'Config check failed: {e}', file=sys.stderr); sys.exit(1)

subprocess.run(['systemctl', 'restart', 'sing-box'], check=True)
print(f'Applied {len(manual_rules)} manual route(s), sing-box restarted')
PYEOF
chmod +x /usr/local/sbin/anysda-apply-routes.py

cat > /etc/systemd/system/anysda-apply-routes.service << 'EOF'
[Unit]
Description=anysda-vpn — apply manual sing-box routes from panel container
After=sing-box.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/anysda-apply-routes.py
EOF

cat > /etc/systemd/system/anysda-apply-routes.path << 'EOF'
[Unit]
Description=Watch /etc/wireguard/manual-routes.json for changes

[Path]
PathModified=/etc/wireguard/manual-routes.json
Unit=anysda-apply-routes.service

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable anysda-apply-routes.path >/dev/null 2>&1
systemctl start  anysda-apply-routes.path

echo "[$HOST_TAG] iptables FORWARD wg0→wg0 DROP (peer isolation):"
iptables -L FORWARD -n -v --line-numbers | grep wg0 | sed "s/^/[$HOST_TAG]   /"

mkdir -p "$STAMP_DIR"
touch "$STAMP_DIR/$STAGE"
echo "[$HOST_TAG] $STAGE done"
