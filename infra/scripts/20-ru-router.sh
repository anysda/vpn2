#!/usr/bin/env bash
# Stage 20 — sing-box роутер на RU-ноде.
# Заворачивает исходящий трафик outline-ss-server'а (user `outline`) в sing-box
# через iptables REDIRECT (TCP) и fwmark+TPROXY (UDP). Sing-box роутит по
# geoip/geosite в hy2-туннели до exit-нод (см. gen-router-config.py).
# Идемпотентен.

set -euo pipefail

[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}" "${PUB_IP_OUT:?}" "${HY2_DIRECT_PORT:?}" "${HY2_WARP_PORT:?}" "${EXIT_TAGS:?}"

case "$HOST_TAG" in ru) ;; *) echo "[$HOST_TAG] 20-ru-router is ru-only — skipping"; exit 0;; esac

STAMP_DIR=/var/anysda/.stamps
STAGE='20-ru-router'

mkdir -p /etc/anysda /etc/sing-box /var/lib/sing-box

# ----------------------------------------------------------------------------
# 1. sing-box install
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [1/6] sing-box"
SB_VER='1.10.3'
if [[ ! -x /usr/local/bin/sing-box ]] || ! /usr/local/bin/sing-box version 2>&1 | grep -q "$SB_VER"; then
  curl -sSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
    "https://github.com/SagerNet/sing-box/releases/download/v${SB_VER}/sing-box-${SB_VER}-linux-amd64.tar.gz" \
    | tar -xz -C /tmp
  install -m0755 "/tmp/sing-box-${SB_VER}-linux-amd64/sing-box" /usr/local/bin/sing-box
  rm -rf "/tmp/sing-box-${SB_VER}-linux-amd64"
fi
/usr/local/bin/sing-box version | head -1 | sed "s/^/[$HOST_TAG]   /"

# ----------------------------------------------------------------------------
# 2. GeoIP / Geosite DB
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [2/6] GeoIP/Geosite"
if [[ ! -f /var/lib/sing-box/geoip.db || $(find /var/lib/sing-box/geoip.db -mtime +7 2>/dev/null) ]]; then
  curl -sSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
    -o /var/lib/sing-box/geoip.db   'https://github.com/SagerNet/sing-geoip/releases/latest/download/geoip.db'
  curl -sSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
    -o /var/lib/sing-box/geosite.db 'https://github.com/SagerNet/sing-geosite/releases/latest/download/geosite.db'
fi
ls -la /var/lib/sing-box/{geoip,geosite}.db | sed "s/^/[$HOST_TAG]   /"

# ----------------------------------------------------------------------------
# 3. Secrets: clash-api на RU + пароли Hysteria2 с выходных нод
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [3/6] secrets"
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
echo "[$HOST_TAG] [4/6] sing-box router config"
GEN=/tmp/anysda/gen-router-config.py
[[ -f "$GEN" ]] || { echo "[$HOST_TAG] $GEN не найден"; exit 1; }

WAN_IFACE=$(ip -4 -o route show default | awk '{print $5; exit}')
: "${WAN_IFACE:?}"
# Backward-compat: gen-router-config.py читает WG_OUT_IFACE (имя из v1).
export WG_OUT_IFACE="$WAN_IFACE"

export RU_CLASH_SECRET EXIT_TAGS HY2_DIRECT_PORT HY2_WARP_PORT MGMT_IP

for _t in $EXIT_TAGS; do
  _T=$(echo "$_t" | tr a-z A-Z)
  export "DOMAIN_${_T}"
  export "${_T}_DIRECT" "${_T}_WARP" "${_T}_OBFS"
  eval "export ${_T}_HY2_DIRECT_PORT=\${${_T}_HY2_DIRECT_PORT:-${HY2_DIRECT_PORT}}"
done

# Write the BASE config (without manual routes). The manual-routes watcher
# derives /etc/sing-box/config.json = config-base.json + panel rules, so
# config-base.json MUST be refreshed on every run — a stale base silently
# drops inbounds added to gen-router-config.py since it was first
# snapshotted (this is how the wg/ovpn TPROXY :7898 inbound got lost).
python3 "$GEN" > /etc/sing-box/config-base.json
chmod 600 /etc/sing-box/config-base.json

if ! /usr/local/bin/sing-box check -c /etc/sing-box/config-base.json; then
  echo "[$HOST_TAG] конфиг невалиден — сервис не трогаю"
  exit 1
fi
cp /etc/sing-box/config-base.json /etc/sing-box/config.json
chmod 600 /etc/sing-box/config.json

# ----------------------------------------------------------------------------
# 5. systemd service + iptables (user-owner REDIRECT + fwmark TPROXY)
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [5/6] systemd + iptables"

cat > /etc/systemd/system/sing-box.service <<'EOF'
[Unit]
Description=sing-box (anysda-vpn2 RU router)
After=network-online.target nss-lookup.target
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart=/usr/local/bin/sing-box run -c /etc/sing-box/config.json
# Restart=always (а не on-failure): роутер обязан подниматься после ЛЮБОЙ
# остановки, включая чистый SIGTERM — иначе случайный `systemctl stop`/`kill`
# (или pkill по имени) кладёт маршрутизацию насовсем.
Restart=always
RestartSec=2s
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

cat > /etc/systemd/system/anysda-iptables.service <<'EOF'
[Unit]
Description=anysda-vpn2 — iptables rules for outline-ss-server -> sing-box redirect/tproxy
After=network-online.target outline-ss-server.service sing-box.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/anysda-iptables.sh up
ExecStop=/usr/local/sbin/anysda-iptables.sh down

[Install]
WantedBy=multi-user.target
EOF

cat > /usr/local/sbin/anysda-iptables.sh <<'IPTSEOF'
#!/usr/bin/env bash
# Catch outbound TCP/UDP from `outline` user → REDIRECT/TPROXY → sing-box.
# Uses custom chains because nftables backend rejects multiple -d/! -d on
# a single rule.
set -euo pipefail
ACTION=${1:-up}

OWNER_USER='outline'
REDIRECT_PORT=7895
TPROXY_PORT=7896
MARK=0x1
TABLE=100

PRIVATE_NETS=(
  '127.0.0.0/8'
  '10.0.0.0/8'
  '172.16.0.0/12'
  '192.168.0.0/16'
  '169.254.0.0/16'
  '224.0.0.0/4'
)

if [[ "$ACTION" == "up" ]]; then
  sysctl -w net.ipv4.conf.all.route_localnet=1 >/dev/null

  # ─── TCP custom chain in nat OUTPUT ───────────────────────────────────
  iptables -t nat -N ANYSDA_OUTLINE_TCP 2>/dev/null || true
  iptables -t nat -F ANYSDA_OUTLINE_TCP
  for net in "${PRIVATE_NETS[@]}"; do
    iptables -t nat -A ANYSDA_OUTLINE_TCP -d "$net" -j RETURN
  done
  iptables -t nat -A ANYSDA_OUTLINE_TCP -p tcp -j REDIRECT --to-port "$REDIRECT_PORT"

  iptables -t nat -C OUTPUT -m owner --uid-owner "$OWNER_USER" -p tcp -j ANYSDA_OUTLINE_TCP 2>/dev/null || \
    iptables -t nat -A OUTPUT -m owner --uid-owner "$OWNER_USER" -p tcp -j ANYSDA_OUTLINE_TCP

  # ─── UDP custom chain in mangle OUTPUT (mark + route table trick) ─────
  iptables -t mangle -N ANYSDA_OUTLINE_UDP 2>/dev/null || true
  iptables -t mangle -F ANYSDA_OUTLINE_UDP
  for net in "${PRIVATE_NETS[@]}"; do
    iptables -t mangle -A ANYSDA_OUTLINE_UDP -d "$net" -j RETURN
  done
  iptables -t mangle -A ANYSDA_OUTLINE_UDP -p udp -j MARK --set-mark "$MARK"

  iptables -t mangle -C OUTPUT -m owner --uid-owner "$OWNER_USER" -p udp -j ANYSDA_OUTLINE_UDP 2>/dev/null || \
    iptables -t mangle -A OUTPUT -m owner --uid-owner "$OWNER_USER" -p udp -j ANYSDA_OUTLINE_UDP

  ip rule list 2>/dev/null | grep -q "fwmark $MARK lookup $TABLE" || \
    ip rule add fwmark "$MARK" lookup "$TABLE"
  ip route show table "$TABLE" 2>/dev/null | grep -q 'local default' || \
    ip route add local 0.0.0.0/0 dev lo table "$TABLE"

  iptables -t mangle -C PREROUTING -m mark --mark "$MARK" -p udp -j TPROXY --tproxy-mark "${MARK}/${MARK}" --on-port "$TPROXY_PORT" 2>/dev/null || \
    iptables -t mangle -A PREROUTING -m mark --mark "$MARK" -p udp -j TPROXY --tproxy-mark "${MARK}/${MARK}" --on-port "$TPROXY_PORT"

  iptables -C INPUT -m mark --mark "${MARK}/${MARK}" -j ACCEPT 2>/dev/null || \
    iptables -I INPUT 1 -m mark --mark "${MARK}/${MARK}" -j ACCEPT

  # Block external access to the redirect port — but ONLY from non-loopback.
  # Redirected traffic from outline (uid-owner) lands here too, we must accept it.
  iptables -C INPUT -p tcp --dport "$REDIRECT_PORT" ! -i lo -j REJECT 2>/dev/null || \
    iptables -I INPUT 2 -p tcp --dport "$REDIRECT_PORT" ! -i lo -j REJECT

  echo "anysda-iptables up (user=$OWNER_USER, redirect=$REDIRECT_PORT, tproxy=$TPROXY_PORT)"

elif [[ "$ACTION" == "down" ]]; then
  iptables -t nat -D OUTPUT -m owner --uid-owner "$OWNER_USER" -p tcp -j ANYSDA_OUTLINE_TCP 2>/dev/null || true
  iptables -t nat -F ANYSDA_OUTLINE_TCP 2>/dev/null || true
  iptables -t nat -X ANYSDA_OUTLINE_TCP 2>/dev/null || true

  iptables -t mangle -D OUTPUT -m owner --uid-owner "$OWNER_USER" -p udp -j ANYSDA_OUTLINE_UDP 2>/dev/null || true
  iptables -t mangle -F ANYSDA_OUTLINE_UDP 2>/dev/null || true
  iptables -t mangle -X ANYSDA_OUTLINE_UDP 2>/dev/null || true

  iptables -t mangle -D PREROUTING -m mark --mark "$MARK" -p udp -j TPROXY --tproxy-mark "${MARK}/${MARK}" --on-port "$TPROXY_PORT" 2>/dev/null || true
  iptables -D INPUT -m mark --mark "${MARK}/${MARK}" -j ACCEPT 2>/dev/null || true
  iptables -D INPUT -p tcp --dport "$REDIRECT_PORT" -j REJECT 2>/dev/null || true
  ip rule del fwmark "$MARK" lookup "$TABLE" 2>/dev/null || true
  ip route flush table "$TABLE" 2>/dev/null || true
  echo "anysda-iptables down"
fi
IPTSEOF
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

MANUAL_ROUTES = '/etc/anysda/manual-routes.json'
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
Description=anysda-vpn2 — apply manual sing-box routes from panel
After=sing-box.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/anysda-apply-routes.py
EOF

cat > /etc/systemd/system/anysda-apply-routes.path << 'EOF'
[Unit]
Description=Watch /etc/anysda/manual-routes.json for changes

[Path]
PathModified=/etc/anysda/manual-routes.json
Unit=anysda-apply-routes.service

[Install]
WantedBy=multi-user.target
EOF

# Touch empty file so the path-watcher is happy on first boot
[[ -f /etc/anysda/manual-routes.json ]] || echo '[]' > /etc/anysda/manual-routes.json

systemctl daemon-reload
systemctl enable anysda-apply-routes.path >/dev/null 2>&1
systemctl start  anysda-apply-routes.path

# Derive config.json = fresh config-base.json + current manual routes and
# restart sing-box, so a re-run immediately reflects the regenerated base.
/usr/local/sbin/anysda-apply-routes.py || true

echo "[$HOST_TAG] iptables OUTPUT --uid-owner outline:"
iptables -t nat -L OUTPUT -n -v --line-numbers | grep outline | sed "s/^/[$HOST_TAG]   /" || \
  echo "[$HOST_TAG]   (no outline owner rules yet — will apply on next outline-ss-server restart)"

mkdir -p "$STAMP_DIR"
touch "$STAMP_DIR/$STAGE"
