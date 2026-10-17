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
# Перезапуск служб только при смене их входов (infra/lib/anysda-svc.sh): деплой
# кладёт библиотеку рядом со стадией, anysda-restore зовёт стадию из клона репо.
SVC_LIB=$(dirname "$0")/anysda-svc.sh
[[ -f "$SVC_LIB" ]] || SVC_LIB=$(dirname "$0")/../lib/anysda-svc.sh
# shellcheck source=infra/lib/anysda-svc.sh
source "$SVC_LIB"
PKI_LIB=$(dirname "$SVC_LIB")/anysda-pki.sh
# shellcheck source=infra/lib/anysda-pki.sh
source "$PKI_LIB"

if [[ "$HOST_TAG" != "ru" ]]; then
  echo "[$HOST_TAG] stage 29-openvpn: skipped (only RU)"
  exit 0
fi

OVPN_PORT="${OVPN_PORT:-1194}"
OVPN_PROTO="${OVPN_PROTO:-udp}"
case "$OVPN_PROTO" in
  udp|udp4|udp6) OVPN_SERVER_PROTO=$OVPN_PROTO ;;
  tcp|tcp4|tcp6) OVPN_SERVER_PROTO=${OVPN_PROTO}-server ;;
  tcp-server|tcp4-server|tcp6-server) OVPN_SERVER_PROTO=$OVPN_PROTO ;;
  *) echo "[$HOST_TAG] unsupported OpenVPN protocol: $OVPN_PROTO" >&2; exit 1 ;;
esac
OVPN_SUBNET="${OVPN_SUBNET:-10.67.67.0}"
OVPN_SUBNETS=$(vpn_pool_subnets ovpn)
OVPN_DIR=/etc/openvpn/server
PKI=$OVPN_DIR/pki

mkdir -p /var/anysda/.stamps "$PKI" "$PKI/newcerts" "$OVPN_DIR/ccd"

# ── 1. openvpn + openssl ────────────────────────────────────────────────────
echo "[$HOST_TAG] [1/6] openvpn"
if ! command -v openvpn >/dev/null 2>&1; then
  # Списки обновлял 00-bootstrap этого же деплоя минуты назад - второй update
  # пропускаем только при свежем индексе. Пакет не нашёлся — обновляем и ставим ещё раз.
  apt_install() {
    apt_update_if_stale
    apt_get install -y -qq "$@" >/dev/null 2>&1 && return 0
    apt_get update -qq && apt_get install -y -qq "$@" >/dev/null
  }
  apt_install openvpn openssl
fi

# ── 2. openssl.cnf for `openssl ca` (used by the panel to sign/revoke) ──────
openvpn_pki_build() {
  local PKI=$1
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

  pki_require_key "$PKI/ca.key" "$PKI/ca.crt"
  if ! pki_cert_valid "$PKI/ca.crt"; then
    echo "[$HOST_TAG]   generating CA"
    openssl req -x509 -new -key "$PKI/ca.key" -days 3650 -sha256 \
      -out "$PKI/ca.crt" -subj "/CN=anysda-vpn2 CA" \
      -config "$PKI/openssl.cnf" -extensions v3_ca
  fi
  pki_check_pair "$PKI/ca.key" "$PKI/ca.crt"
  openssl verify -CAfile "$PKI/ca.crt" "$PKI/ca.crt" >/dev/null
  # openssl ca должен менять только временную копию базы, не рабочую PKI.
  sed "s|^dir .*|dir = $PKI|" "$PKI/openssl.cnf" > "$PKI/build.cnf"

  pki_require_key "$PKI/server.key" "$PKI/server.crt"
  if ! pki_pair_valid "$PKI/server.key" "$PKI/server.crt" || \
      ! openssl verify -CAfile "$PKI/ca.crt" "$PKI/server.crt" >/dev/null 2>&1; then
    echo "[$HOST_TAG]   generating server cert"
    openssl req -new -key "$PKI/server.key" -out "$PKI/server.csr" \
      -subj "/CN=anysda-vpn2-server" -config "$PKI/openssl.cnf"
    openssl ca -batch -notext -config "$PKI/build.cnf" \
      -extensions server_ext -days 3650 -in "$PKI/server.csr" -out "$PKI/server.crt"
    rm -f "$PKI/server.csr"
  fi

  # Проверяем формат статического ключа, а не только наличие файла.
  if ! pki_tls_crypt_valid "$PKI/tls-crypt.key"; then
    openvpn --genkey secret "$PKI/tls-crypt.key"
  fi
  if ! openssl crl -in "$PKI/crl.pem" -noout -verify -CAfile "$PKI/ca.crt" >/dev/null 2>&1; then
    openssl ca -config "$PKI/build.cnf" -gencrl -out "$PKI/crl.pem"
  fi
  pki_check_pair "$PKI/server.key" "$PKI/server.crt"
  openssl verify -CAfile "$PKI/ca.crt" -purpose sslserver "$PKI/server.crt" >/dev/null
  openssl crl -in "$PKI/crl.pem" -noout -verify -CAfile "$PKI/ca.crt"
  pki_tls_crypt_valid "$PKI/tls-crypt.key"
  rm -f "$PKI/build.cnf"

  chmod 600 "$PKI/ca.key" "$PKI/server.key" "$PKI/tls-crypt.key"
  chmod 644 "$PKI/ca.crt" "$PKI/server.crt" "$PKI/crl.pem"
}
pki_transaction "$PKI" openvpn_pki_build

# ── 4. server.conf ──────────────────────────────────────────────────────────
echo "[$HOST_TAG] [4/6] server.conf"
OVPN_FASTIO=''
[[ "$OVPN_PROTO" == udp* ]] && OVPN_FASTIO=fast-io
cat > "$OVPN_DIR/server.conf" <<EOF
port ${OVPN_PORT}
proto ${OVPN_SERVER_PROTO}
dev tun0
topology subnet
# Постоянные адреса устройств .2-.239 панель раздаёт через ccd
# (ifconfig-push). Общий пул только .240-.253 для устройств, которым панель
# ещё не выдала адрес, чтобы он не пересекался с закреплёнными (VPN2-115).
server ${OVPN_SUBNET} 255.255.255.0 nopool
ifconfig-pool ${OVPN_SUBNET%.*}.240 ${OVPN_SUBNET%.*}.253 255.255.255.0
ca   ${PKI}/ca.crt
cert ${PKI}/server.crt
key  ${PKI}/server.key
dh none
tls-crypt ${PKI}/tls-crypt.key
crl-verify ${PKI}/crl.pem
client-config-dir ${OVPN_DIR}/ccd
# keepalive 5 30: пуш клиентам ping 5 / ping-restart 30 — детект мёртвого
# entry за ~30с и быстрый реконнект (было 10/60 → ~62с простоя после ребута).
keepalive 5 30
cipher AES-256-GCM
data-ciphers AES-256-GCM:CHACHA20-POLY1305
auth SHA256
persist-key
persist-tun
# Буферы сокета системные (0 - не трогать, после 00-bootstrap это 8 МиБ);
# fast-io - запись в UDP без лишнего poll, на TCP опции нет.
sndbuf 0
rcvbuf 0
${OVPN_FASTIO}
user nobody
group nogroup
push "redirect-gateway def1 bypass-dhcp"
# DNS is NOT pushed from the server — each client's .ovpn carries its own
# dhcp-option DNS (AdGuard or public, per the panel's filterTraffic flag).
# status-version 2 → машиночитаемый CSV (CLIENT_LIST,...); файл лежит в
# /etc/openvpn/server (этот путь смонтирован в контейнер панели) — сборщик
# трафика панели читает отсюда per-client байты.
status /etc/openvpn/server/status-server.log
status-version 2
# Management на unix-сокете, не TCP. Через него панель рвёт уже установленные
# сессии при заморозке, истечении, удалении и перевыпуске: ccd-директиву disable и CRL
# OpenVPN проверяет только на НОВОМ подключении (VPN2-39). Сокет OpenVPN
# создаёт с правами 0777, поэтому пускаем только root (панель в контейнере
# тоже root) - иначе любой локальный пользователь ноды рвал бы сессии.
management /etc/openvpn/server/mgmt.sock unix
management-client-user root
verb 3
EOF

# tun0 и адреса прежних клиентов остаются /24. Дополнительные подсети
# маршрутизируются в тот же tun0; новые CCD получают маску /21 и старый gateway.
for subnet in $OVPN_SUBNETS; do
  [[ "$subnet" == "$OVPN_SUBNET/24" ]] && continue
  printf 'route %s 255.255.255.0\n' "${subnet%/*}" >> "$OVPN_DIR/server.conf"
done

mkdir -p /run/openvpn-server
sysctl -w net.ipv4.ip_forward=1 >/dev/null

# OpenVPN различает udp4/udp6 и tcp[46]-server; UFW принимает только udp/tcp.
case "$OVPN_PROTO" in
  udp*) OVPN_FW_PROTO=udp ;;
  tcp*) OVPN_FW_PROTO=tcp ;;
  *) echo "[$HOST_TAG] unsupported OpenVPN protocol: $OVPN_PROTO" >&2; exit 1 ;;
esac
ufw_sync_rules 'openvpn listener' "${OVPN_PORT}/${OVPN_FW_PROTO}"
# OpenVPN clients (10.67.67.0/24) resolve via AdGuard on the entry mgmt IP
# shellcheck disable=SC2086
ufw_sync_dns 'openvpn → AdGuard DNS' 10.99.0.1 $OVPN_SUBNETS

# ── 5. openvpn-server@server ────────────────────────────────────────────────
echo "[$HOST_TAG] [5/6] openvpn-server@server"
systemctl enable openvpn-server@server >/dev/null 2>&1 || true
# Рестарт рвёт всех OpenVPN-клиентов. CRL и ccd OpenVPN перечитывает сам на
# каждом подключении, их в отпечатке нет.
svc_restart_if_changed openvpn-server@server /usr/sbin/openvpn \
  /usr/lib/systemd/system/openvpn-server@.service "$OVPN_DIR/server.conf" \
  "$PKI/ca.crt" "$PKI/server.crt" "$PKI/server.key" "$PKI/tls-crypt.key"
# Type=notify: restart вернулся после READY=1, проба только ловит падение.
svc_settle 2 systemctl is-active --quiet openvpn-server@server
systemctl status openvpn-server@server --no-pager -n 4 | sed -n '1,6p' | HOST_TAG="$HOST_TAG" awk '{print "[" ENVIRON["HOST_TAG"] "]   " $0}'

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
# Общий замок с WG и IKEv2: ip rule add допускает одинаковые правила.
exec 9>/run/anysda-vpn-routing.lock
flock -x 9
TUN_IF='tun0'
MARK='0x42'
TABLE=101
PREF=32765
TPROXY_PORT=7898
MSS=1340
MSS_OUT=(-p tcp --tcp-flags SYN,RST SYN -m tcpmss --mss "$((MSS + 1)):65535" -j TCPMSS --set-mss "$MSS")
PING_REJECT=(-p icmp --icmp-type echo-request -m comment --comment 'vpn2: Hysteria2 has no ICMP' -j REJECT --reject-with icmp-proto-unreachable)

if [[ "$ACTION" == "up" ]]; then
  # Явный pref: без него ядро ставит правило выше самого верхнего (219 перед
  # strongSwan 220), а юниты wg/ovpn/ikev2 на загрузке в гонке добавляют копии.
  ip rule list pref "$PREF" | grep "fwmark $MARK lookup $TABLE" >/dev/null || \
    ip rule add fwmark "$MARK" lookup "$TABLE" pref "$PREF" 2>/dev/null || \
    ip rule list pref "$PREF" | grep "fwmark $MARK lookup $TABLE" >/dev/null
  for p in $(ip rule list | awk -F: -v m="fwmark $MARK lookup $TABLE" -v k="$PREF" 'index($0, m) && $1 != k {print $1}'); do
    ip rule del pref "$p" fwmark "$MARK" lookup "$TABLE" 2>/dev/null || true
  done
  ip route show table "$TABLE" 2>/dev/null | grep 'local default' >/dev/null || \
    ip route replace local 0.0.0.0/0 dev lo table "$TABLE"

  iptables -t mangle -N ANYSDA_OVPN_TPROXY 2>/dev/null || true
  iptables -t mangle -F ANYSDA_OVPN_TPROXY
  # DNS to AdGuard (entry mgmt IP) must be delivered locally, not TPROXY'd.
  iptables -t mangle -A ANYSDA_OVPN_TPROXY -d 10.99.0.1 -j RETURN
  # MSS режем сами: при DCO (ядро ovpn) mssfix не работает, клиент на
  # DCO шлёт MSS 1460, sing-box отвечает полными сегментами, туннельный
  # UDP выходит за 1500, фрагменты теряются в пути - HTTPS виснет.
  # 1340 = путь с MTU 1440 минус оверхед OpenVPN и TCP/IP.
  iptables -t mangle -A ANYSDA_OVPN_TPROXY -p tcp --tcp-flags SYN,RST SYN \
    -m tcpmss --mss "$((MSS + 1)):65535" -j TCPMSS --set-mss "$MSS"
  iptables -t mangle -C POSTROUTING -o "$TUN_IF" "${MSS_OUT[@]}" 2>/dev/null || \
    iptables -t mangle -A POSTROUTING -o "$TUN_IF" "${MSS_OUT[@]}"
  iptables -t mangle -A ANYSDA_OVPN_TPROXY -p tcp -j TPROXY --tproxy-mark "${MARK}/${MARK}" --on-port "$TPROXY_PORT" --on-ip 127.0.0.1
  iptables -t mangle -A ANYSDA_OVPN_TPROXY -p udp -j TPROXY --tproxy-mark "${MARK}/${MARK}" --on-port "$TPROXY_PORT" --on-ip 127.0.0.1

  iptables -t mangle -C PREROUTING -i "$TUN_IF" -j ANYSDA_OVPN_TPROXY 2>/dev/null || \
    iptables -t mangle -I PREROUTING 1 -i "$TUN_IF" -j ANYSDA_OVPN_TPROXY

  iptables -C INPUT -m mark --mark "${MARK}/${MARK}" -j ACCEPT 2>/dev/null || \
    iptables -I INPUT 1 -m mark --mark "${MARK}/${MARK}" -j ACCEPT

  # Hysteria2 не переносит ICMP: явный отказ перед ufw/Docker, без выхода
  # ping через WAN entry. Локальный ping и ICMP-ошибки (PMTU) не меняем.
  iptables -C FORWARD -i "$TUN_IF" "${PING_REJECT[@]}" 2>/dev/null || \
    iptables -I FORWARD 1 -i "$TUN_IF" "${PING_REJECT[@]}"

  echo "anysda-ovpn-routing up (iface=$TUN_IF, mark=$MARK, tproxy=$TPROXY_PORT)"

elif [[ "$ACTION" == "down" ]]; then
  iptables -D FORWARD -i "$TUN_IF" "${PING_REJECT[@]}" 2>/dev/null || true
  iptables -t mangle -D PREROUTING -i "$TUN_IF" -j ANYSDA_OVPN_TPROXY 2>/dev/null || true
  iptables -t mangle -D POSTROUTING -o "$TUN_IF" "${MSS_OUT[@]}" 2>/dev/null || true
  iptables -t mangle -F ANYSDA_OVPN_TPROXY 2>/dev/null || true
  iptables -t mangle -X ANYSDA_OVPN_TPROXY 2>/dev/null || true
fi
IPTSEOF

chmod +x /usr/local/sbin/anysda-ovpn-routing.sh
systemctl daemon-reload
systemctl enable anysda-ovpn-routing >/dev/null 2>&1 || true
svc_restart_if_changed anysda-ovpn-routing /etc/systemd/system/anysda-ovpn-routing.service \
  /usr/local/sbin/anysda-ovpn-routing.sh
vpn_routing_ensure anysda-ovpn-routing tun0 ANYSDA_OVPN_TPROXY

touch /var/anysda/.stamps/29-openvpn
echo "[$HOST_TAG] 29-openvpn done"
