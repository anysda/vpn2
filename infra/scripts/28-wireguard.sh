#!/usr/bin/env bash
# Stage 28 — WireGuard server on RU.
#
# Brings up kernel WireGuard on wg0 (10.66.66.1/24, udp/51820), generates
# the server keypair on first run, and wires forwarded wg0 traffic into
# the existing sing-box TPROXY chain so WG clients pick the same exit
# routing as OpenVPN clients (geoip rules → direct-ru / hy2-* outbounds).
#
# The panel (stage 30) writes /etc/wireguard/wg0.conf with the enabled
# clients' WG peers and hot-reloads via `wg syncconf`.

set -euo pipefail
[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}"

if [[ "$HOST_TAG" != "ru" ]]; then
  echo "[$HOST_TAG] stage 28-wireguard: skipped (only RU)"
  exit 0
fi

WG_PORT="${WG_LISTEN_PORT:-51820}"
WG_SERVER_IP="${WG_SERVER_IP:-10.66.66.1}"

mkdir -p /var/anysda/.stamps /etc/wireguard
chmod 0700 /etc/wireguard

# ── 1. wireguard-tools ──────────────────────────────────────────────────────
echo "[$HOST_TAG] [1/5] wireguard-tools"
if ! command -v wg >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq wireguard-tools >/dev/null
fi
modprobe wireguard 2>/dev/null || true

# ── 2. Server keypair (idempotent — survives reruns) ────────────────────────
echo "[$HOST_TAG] [2/5] server keys"
if [[ ! -f /etc/wireguard/server.priv ]]; then
  umask 077
  wg genkey > /etc/wireguard/server.priv
  wg pubkey < /etc/wireguard/server.priv > /etc/wireguard/server.pub
  echo "[$HOST_TAG]   keys generated"
fi
chmod 0600 /etc/wireguard/server.priv /etc/wireguard/server.pub
SERVER_PRIV=$(cat /etc/wireguard/server.priv)

# Initial wg0.conf — panel rewrites it later with real peers
if [[ ! -f /etc/wireguard/wg0.conf ]]; then
  cat > /etc/wireguard/wg0.conf <<EOF
# Initial config — panel will overwrite via syncWireguardConfig().
[Interface]
Address = ${WG_SERVER_IP}/24
ListenPort = ${WG_PORT}
PrivateKey = ${SERVER_PRIV}
EOF
  chmod 0600 /etc/wireguard/wg0.conf
fi

# ── 3. ip forward + ufw ─────────────────────────────────────────────────────
echo "[$HOST_TAG] [3/5] sysctl + ufw"
cat > /etc/sysctl.d/98-wg-forward.conf <<'EOF'
net.ipv4.ip_forward=1
net.ipv4.conf.all.forwarding=1
EOF
sysctl -p /etc/sysctl.d/98-wg-forward.conf >/dev/null

ufw allow "${WG_PORT}/udp" comment 'wireguard listener' >/dev/null 2>&1 || true
# WireGuard clients (10.66.66.0/24) resolve via AdGuard on the entry mgmt IP
ufw allow proto udp from 10.66.66.0/24 to 10.99.0.1 port 53 comment 'wireguard → AdGuard DNS' >/dev/null 2>&1 || true
ufw allow proto tcp from 10.66.66.0/24 to 10.99.0.1 port 53 comment 'wireguard → AdGuard DNS' >/dev/null 2>&1 || true
ufw reload >/dev/null 2>&1 || true

# ── 4. wg-quick@wg0 ────────────────────────────────────────────────────────
echo "[$HOST_TAG] [4/5] wg-quick@wg0 service"
systemctl enable wg-quick@wg0 >/dev/null 2>&1 || true
systemctl restart wg-quick@wg0
sleep 1
systemctl status wg-quick@wg0 --no-pager -n 4 | head -6 | sed "s/^/[$HOST_TAG]   /"

# ── 5. TPROXY: forwarded wg0 traffic → sing-box :7898 ──────────────────────
echo "[$HOST_TAG] [5/5] anysda-wg-routing"
cat > /etc/systemd/system/anysda-wg-routing.service <<'EOF'
[Unit]
Description=anysda-vpn2 — wg0 forwarded traffic → sing-box TPROXY
After=network-online.target sing-box.service wg-quick@wg0.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/anysda-wg-routing.sh up
ExecStop=/usr/local/sbin/anysda-wg-routing.sh down

[Install]
WantedBy=multi-user.target
EOF

cat > /usr/local/sbin/anysda-wg-routing.sh <<'IPTSEOF'
#!/usr/bin/env bash
# Divert wg0 forwarded TCP+UDP into sing-box TPROXY on :7898 so WG clients
# pick the same geoip routing as OpenVPN clients.
set -euo pipefail
ACTION=${1:-up}
WG_IF='wg0'
MARK='0x42'
TABLE=101
TPROXY_PORT=7898

if [[ "$ACTION" == "up" ]]; then
  ip rule list | grep -q "fwmark $MARK lookup $TABLE" || \
    ip rule add fwmark "$MARK" lookup "$TABLE"
  ip route show table "$TABLE" 2>/dev/null | grep -q 'local default' || \
    ip route add local 0.0.0.0/0 dev lo table "$TABLE"

  iptables -t mangle -N ANYSDA_WG_TPROXY 2>/dev/null || true
  iptables -t mangle -F ANYSDA_WG_TPROXY
  # DNS to AdGuard (entry mgmt IP) must be delivered locally, not TPROXY'd.
  iptables -t mangle -A ANYSDA_WG_TPROXY -d 10.99.0.1 -j RETURN
  iptables -t mangle -A ANYSDA_WG_TPROXY -p tcp -j TPROXY --tproxy-mark "${MARK}/${MARK}" --on-port "$TPROXY_PORT" --on-ip 127.0.0.1
  iptables -t mangle -A ANYSDA_WG_TPROXY -p udp -j TPROXY --tproxy-mark "${MARK}/${MARK}" --on-port "$TPROXY_PORT" --on-ip 127.0.0.1

  iptables -t mangle -C PREROUTING -i "$WG_IF" -j ANYSDA_WG_TPROXY 2>/dev/null || \
    iptables -t mangle -I PREROUTING 1 -i "$WG_IF" -j ANYSDA_WG_TPROXY

  iptables -C INPUT -m mark --mark "${MARK}/${MARK}" -j ACCEPT 2>/dev/null || \
    iptables -I INPUT 1 -m mark --mark "${MARK}/${MARK}" -j ACCEPT

  echo "anysda-wg-routing up (iface=$WG_IF, mark=$MARK, tproxy=$TPROXY_PORT)"

elif [[ "$ACTION" == "down" ]]; then
  iptables -t mangle -D PREROUTING -i "$WG_IF" -j ANYSDA_WG_TPROXY 2>/dev/null || true
  iptables -t mangle -F ANYSDA_WG_TPROXY 2>/dev/null || true
  iptables -t mangle -X ANYSDA_WG_TPROXY 2>/dev/null || true
  iptables -D INPUT -m mark --mark "${MARK}/${MARK}" -j ACCEPT 2>/dev/null || true
  ip rule del fwmark "$MARK" lookup "$TABLE" 2>/dev/null || true
fi
IPTSEOF

chmod +x /usr/local/sbin/anysda-wg-routing.sh
systemctl daemon-reload
systemctl enable anysda-wg-routing >/dev/null 2>&1 || true
systemctl restart anysda-wg-routing

touch /var/anysda/.stamps/28-wireguard
echo "[$HOST_TAG] 28-wireguard done"
