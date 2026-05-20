#!/usr/bin/env bash
# Stage 29 — OpenVPN server on RU.
#
# Seeds an EC PKI (CA + server cert + tls-crypt key) under
# /etc/openvpn/server/pki, runs openvpn-server@server on udp/1194 (dev tun0,
# 10.67.67.0/24), and diverts forwarded tun0 traffic into the same sing-box
# TPROXY as WireGuard — so OpenVPN clients get the same exit routing.
#
# The panel (stage 30) issues/revokes per-client certs via `openssl ca`
# against this CA and toggles client-config-dir disable flags.

set -euo pipefail
[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}"

if [[ "$HOST_TAG" != "ru" ]]; then
  echo "[$HOST_TAG] stage 29-openvpn: skipped (only RU)"
  exit 0
fi

OVPN_PORT="${OVPN_PORT:-1194}"
OVPN_PROTO="${OVPN_PROTO:-udp}"
OVPN_SUBNET="${OVPN_SUBNET:-10.67.67.0}"
OVPN_DIR=/etc/openvpn/server
PKI=$OVPN_DIR/pki

mkdir -p /var/anysda/.stamps "$PKI" "$PKI/newcerts" "$OVPN_DIR/ccd"

# ── 1. openvpn + openssl ────────────────────────────────────────────────────
echo "[$HOST_TAG] [1/6] openvpn"
if ! command -v openvpn >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq openvpn openssl >/dev/null
fi

# ── 2. openssl.cnf for `openssl ca` (used by the panel to sign/revoke) ──────
echo "[$HOST_TAG] [2/6] openssl.cnf"
cat > "$PKI/openssl.cnf" <<'EOF'
[ca]
default_ca = CA_default

[CA_default]
dir              = /etc/openvpn/server/pki
database         = $dir/index.txt
new_certs_dir    = $dir/newcerts
certificate      = $dir/ca.crt
private_key      = $dir/ca.key
serial           = $dir/serial
crlnumber        = $dir/crlnumber
crl              = $dir/crl.pem
default_md       = sha256
default_days     = 3650
default_crl_days = 3650
policy           = policy_anything
unique_subject   = no
copy_extensions  = none

[policy_anything]
commonName             = supplied
countryName            = optional
stateOrProvinceName    = optional
localityName           = optional
organizationName       = optional
organizationalUnitName = optional
emailAddress           = optional

[req]
distinguished_name = req_dn
prompt             = no

[req_dn]
CN = anysda-vpn2

[v3_ca]
basicConstraints     = critical,CA:TRUE
keyUsage             = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash

[server_ext]
basicConstraints       = CA:FALSE
keyUsage               = critical,digitalSignature,keyEncipherment
extendedKeyUsage       = serverAuth
subjectKeyIdentifier   = hash
authorityKeyIdentifier = keyid,issuer

[client_ext]
basicConstraints       = CA:FALSE
keyUsage               = critical,digitalSignature
extendedKeyUsage       = clientAuth
subjectKeyIdentifier   = hash
authorityKeyIdentifier = keyid,issuer
EOF

# ── 3. CA + server cert + tls-crypt (idempotent — keep keys across reruns) ──
echo "[$HOST_TAG] [3/6] PKI"
[[ -f "$PKI/index.txt" ]] || : > "$PKI/index.txt"
[[ -f "$PKI/serial"    ]] || echo 01 > "$PKI/serial"
[[ -f "$PKI/crlnumber" ]] || echo 01 > "$PKI/crlnumber"

if [[ ! -f "$PKI/ca.key" ]]; then
  echo "[$HOST_TAG]   generating CA"
  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:prime256v1 -out "$PKI/ca.key"
  openssl req -x509 -new -key "$PKI/ca.key" -days 3650 -sha256 \
    -out "$PKI/ca.crt" -subj "/CN=anysda-vpn2 CA" \
    -config "$PKI/openssl.cnf" -extensions v3_ca
fi

if [[ ! -f "$PKI/server.key" ]]; then
  echo "[$HOST_TAG]   generating server cert"
  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:prime256v1 -out "$PKI/server.key"
  openssl req -new -key "$PKI/server.key" -out "$PKI/server.csr" \
    -subj "/CN=anysda-vpn2-server" -config "$PKI/openssl.cnf"
  openssl ca -batch -notext -config "$PKI/openssl.cnf" \
    -extensions server_ext -days 3650 -in "$PKI/server.csr" -out "$PKI/server.crt"
  rm -f "$PKI/server.csr"
fi

[[ -f "$PKI/tls-crypt.key" ]] || openvpn --genkey secret "$PKI/tls-crypt.key"
[[ -f "$PKI/crl.pem" ]] || openssl ca -config "$PKI/openssl.cnf" -gencrl -out "$PKI/crl.pem"

chmod 600 "$PKI/ca.key" "$PKI/server.key" "$PKI/tls-crypt.key"
chmod 644 "$PKI/ca.crt" "$PKI/server.crt" "$PKI/crl.pem"

# ── 4. server.conf ──────────────────────────────────────────────────────────
echo "[$HOST_TAG] [4/6] server.conf"
cat > "$OVPN_DIR/server.conf" <<EOF
port ${OVPN_PORT}
proto ${OVPN_PROTO}
dev tun0
topology subnet
server ${OVPN_SUBNET} 255.255.255.0
ca   ${PKI}/ca.crt
cert ${PKI}/server.crt
key  ${PKI}/server.key
dh none
tls-crypt ${PKI}/tls-crypt.key
crl-verify ${PKI}/crl.pem
client-config-dir ${OVPN_DIR}/ccd
keepalive 10 60
cipher AES-256-GCM
data-ciphers AES-256-GCM:CHACHA20-POLY1305
auth SHA256
persist-key
persist-tun
user nobody
group nogroup
push "redirect-gateway def1 bypass-dhcp"
push "dhcp-option DNS 10.99.0.1"
status /run/openvpn-server/status-server.log
verb 3
EOF

mkdir -p /run/openvpn-server
sysctl -w net.ipv4.ip_forward=1 >/dev/null

ufw allow "${OVPN_PORT}/${OVPN_PROTO}" comment 'openvpn listener' >/dev/null 2>&1 || true
# OpenVPN clients (10.67.67.0/24) resolve via AdGuard on the entry mgmt IP
ufw allow proto udp from 10.67.67.0/24 to 10.99.0.1 port 53 comment 'openvpn → AdGuard DNS' >/dev/null 2>&1 || true
ufw allow proto tcp from 10.67.67.0/24 to 10.99.0.1 port 53 comment 'openvpn → AdGuard DNS' >/dev/null 2>&1 || true
ufw reload >/dev/null 2>&1 || true

# ── 5. openvpn-server@server ────────────────────────────────────────────────
echo "[$HOST_TAG] [5/6] openvpn-server@server"
systemctl enable openvpn-server@server >/dev/null 2>&1 || true
systemctl restart openvpn-server@server
sleep 2
systemctl status openvpn-server@server --no-pager -n 4 | head -6 | sed "s/^/[$HOST_TAG]   /"

# ── 6. tun0 → sing-box TPROXY (same mark/table/port as wg0) ─────────────────
echo "[$HOST_TAG] [6/6] anysda-ovpn-routing"
cat > /etc/systemd/system/anysda-ovpn-routing.service <<'EOF'
[Unit]
Description=anysda-vpn2 — tun0 forwarded traffic → sing-box TPROXY
After=network-online.target sing-box.service openvpn-server@server.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/anysda-ovpn-routing.sh up
ExecStop=/usr/local/sbin/anysda-ovpn-routing.sh down

[Install]
WantedBy=multi-user.target
EOF

cat > /usr/local/sbin/anysda-ovpn-routing.sh <<'IPTSEOF'
#!/usr/bin/env bash
# Divert tun0 forwarded TCP+UDP into sing-box TPROXY :7898 — shares the
# fwmark/table with wg0 so OpenVPN clients get the same geoip exit routing.
set -euo pipefail
ACTION=${1:-up}
TUN_IF='tun0'
MARK='0x42'
TABLE=101
TPROXY_PORT=7898

if [[ "$ACTION" == "up" ]]; then
  ip rule list | grep -q "fwmark $MARK lookup $TABLE" || \
    ip rule add fwmark "$MARK" lookup "$TABLE"
  ip route show table "$TABLE" 2>/dev/null | grep -q 'local default' || \
    ip route add local 0.0.0.0/0 dev lo table "$TABLE"

  iptables -t mangle -N ANYSDA_OVPN_TPROXY 2>/dev/null || true
  iptables -t mangle -F ANYSDA_OVPN_TPROXY
  # DNS to AdGuard (entry mgmt IP) must be delivered locally, not TPROXY'd.
  iptables -t mangle -A ANYSDA_OVPN_TPROXY -d 10.99.0.1 -j RETURN
  iptables -t mangle -A ANYSDA_OVPN_TPROXY -p tcp -j TPROXY --tproxy-mark "${MARK}/${MARK}" --on-port "$TPROXY_PORT" --on-ip 127.0.0.1
  iptables -t mangle -A ANYSDA_OVPN_TPROXY -p udp -j TPROXY --tproxy-mark "${MARK}/${MARK}" --on-port "$TPROXY_PORT" --on-ip 127.0.0.1

  iptables -t mangle -C PREROUTING -i "$TUN_IF" -j ANYSDA_OVPN_TPROXY 2>/dev/null || \
    iptables -t mangle -I PREROUTING 1 -i "$TUN_IF" -j ANYSDA_OVPN_TPROXY

  iptables -C INPUT -m mark --mark "${MARK}/${MARK}" -j ACCEPT 2>/dev/null || \
    iptables -I INPUT 1 -m mark --mark "${MARK}/${MARK}" -j ACCEPT

  echo "anysda-ovpn-routing up (iface=$TUN_IF, mark=$MARK, tproxy=$TPROXY_PORT)"

elif [[ "$ACTION" == "down" ]]; then
  iptables -t mangle -D PREROUTING -i "$TUN_IF" -j ANYSDA_OVPN_TPROXY 2>/dev/null || true
  iptables -t mangle -F ANYSDA_OVPN_TPROXY 2>/dev/null || true
  iptables -t mangle -X ANYSDA_OVPN_TPROXY 2>/dev/null || true
fi
IPTSEOF

chmod +x /usr/local/sbin/anysda-ovpn-routing.sh
systemctl daemon-reload
systemctl enable anysda-ovpn-routing >/dev/null 2>&1 || true
systemctl restart anysda-ovpn-routing

touch /var/anysda/.stamps/29-openvpn
echo "[$HOST_TAG] 29-openvpn done"
