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
  echo "[$HOST_TAG] stage 28-wireguard: skipped (only RU)"
  exit 0
fi

WG_PORT="${WG_LISTEN_PORT:-51820}"
WG_SERVER_IP="${WG_SERVER_IP:-10.66.66.1}"
WG_SUBNETS=$(vpn_pool_subnets wg)

mkdir -p /var/anysda/.stamps /etc/wireguard
chmod 0700 /etc/wireguard

# ── 1. wireguard-tools ──────────────────────────────────────────────────────
echo "[$HOST_TAG] [1/5] wireguard-tools"
if ! command -v wg >/dev/null 2>&1; then
  apt_get update -qq
  apt_get install -y -qq wireguard-tools >/dev/null
fi
modprobe wireguard 2>/dev/null || true

# ── 2. Server keypair (idempotent — survives reruns) ────────────────────────
echo "[$HOST_TAG] [2/5] server keys"
wireguard_keys_build() {
  local work=$1
  # Миграция плоской раскладки: приватный ключ не меняется.
  if [[ ! -s "$work/server.priv" && -s /etc/wireguard/server.priv ]]; then
    cp /etc/wireguard/server.priv "$work/server.priv"
  fi
  if [[ ! -s "$work/server.priv" && -s /etc/wireguard/wg0.conf ]]; then
    sed -n 's/^[[:space:]]*[Pp][Rr][Ii][Vv][Aa][Tt][Ee][Kk][Ee][Yy][[:space:]]*=[[:space:]]*//p' \
      /etc/wireguard/wg0.conf > "$work/server.priv"
  fi
  # Сбой вычисления public key не означает, что нужно ротировать private.
  # WG private key — 32 байта в base64; пустой/оборванный старый файл можно
  # восстановить только если ещё нет признаков действующей идентичности.
  if [[ ! $(cat "$work/server.priv" 2>/dev/null) =~ ^[A-Za-z0-9+/]{43}=$ ]]; then
    if [[ -s /etc/wireguard/server.pub || -s /etc/wireguard/wg0.conf ]]; then
      echo 'PKI: cannot replace an active WireGuard private key' >&2
      return 1
    fi
    wg genkey > "$work/server.priv"
  fi
  wg pubkey < "$work/server.priv" > "$work/server.pub"
  [[ -s "$work/server.pub" ]]
  chmod 600 "$work/server.priv" "$work/server.pub"
}
# Не заменяем /etc/wireguard: это bind mount панели, содержащий wg0.conf.
pki_transaction /etc/wireguard/keys wireguard_keys_build
for key in server.priv server.pub; do
  ln -sfn "keys/$key" "/etc/wireguard/.$key.link"
  mv -Tf "/etc/wireguard/.$key.link" "/etc/wireguard/$key"
done
chmod 0600 /etc/wireguard/server.priv /etc/wireguard/server.pub
SERVER_PRIV=$(cat /etc/wireguard/server.priv)

# ULA fast-fail: единый источник fd66:66::1 в паре с 10.66.66.1, чтобы
# v6-пакет клиента получал ICMPv6 unreachable за один RTT вместо таймаута
# (см. wgUlaAddress в server/utils/wireguard.ts).
WG_SERVER_ULA="fd66:66::1"

# Initial wg0.conf — panel rewrites it later with real peers
if [[ ! -f /etc/wireguard/wg0.conf ]]; then
  cat > /etc/wireguard/wg0.conf <<EOF
# Initial config — panel will overwrite via syncWireguardConfig().
[Interface]
Address = ${WG_SERVER_IP}/24, ${WG_SERVER_ULA}/64
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
# Узкий wg0.forwarding=1 сам по себе не форвардит, на стенде подтверждено,
# что ядро форвардит только когда all.forwarding=1 (интерфейсный флаг его не
# заменяет). Разрешённый диапазон держит не sysctl, а ip6tables FORWARD
# (policy DROP + REJECT только для wg0, см. anysda-wg-routing.sh).
net.ipv6.conf.all.forwarding=1
EOF
sysctl -p /etc/sysctl.d/98-wg-forward.conf >/dev/null

ufw_sync_rules 'wireguard listener' "${WG_PORT}/udp"
# WireGuard clients (10.66.66.0/24) resolve via AdGuard on the entry mgmt IP
# shellcheck disable=SC2086
ufw_sync_dns 'wireguard → AdGuard DNS' 10.99.0.1 $WG_SUBNETS

# ── 4. wg-quick@wg0 ────────────────────────────────────────────────────────
echo "[$HOST_TAG] [4/5] wg-quick@wg0 service"
systemctl enable wg-quick@wg0 >/dev/null 2>&1 || true

# Рестарт wg-quick рвёт туннель всем WG-клиентам, а на повторном деплое
# wg0.conf обычно прежний: wg0_apply применяет ключи и пиры `wg syncconf`.
wg0_apply

# ── 5. TPROXY: forwarded wg0 traffic → sing-box :7898 ──────────────────────
echo "[$HOST_TAG] [5/5] anysda-wg-routing"
# MSS под MTU, который панель пишет в конфиг клиента (NUXT_WG_MTU в 30-frontend).
WG_MSS=$(( ${WG_MTU:-1420} - 40 ))
cat > /etc/systemd/system/anysda-wg-routing.service <<EOF
[Unit]
Description=anysda-vpn2 — wg0 forwarded traffic → sing-box TPROXY
After=network-online.target sing-box.service wg-quick@wg0.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=WG_MSS=$WG_MSS
Environment="WG_SUBNETS=$WG_SUBNETS"
ExecStart=/usr/local/sbin/anysda-wg-routing.sh up
ExecStop=/usr/local/sbin/anysda-wg-routing.sh down

[Install]
WantedBy=multi-user.target
EOF

cat > /usr/local/sbin/anysda-wg-routing.sh <<'IPTSEOF'
#!/usr/bin/env bash
# Divert wg0 forwarded TCP+UDP into sing-box TPROXY on :7898 so WG clients
# pick the same geoip routing as OpenVPN clients. Also: ULA fast-fail для
# v6 (см. 28-wireguard.sh): FORWARD ставится в DROP и v6 с wg0 отбивается
# REJECT, чтобы клиент получал icmp6-addr-unreachable, а не таймаут.
set -euo pipefail
ACTION=${1:-up}
# Все VPN-службы меняют общие mark/table и INPUT. Проверка и добавление
# правила должны идти под одним замком, включая параллельный старт при boot.
exec 9>/run/anysda-vpn-routing.lock
flock -x 9
WG_IF='wg0'
MARK='0x42'
TABLE=101
PREF=32765
TPROXY_PORT=7898
MSS=${WG_MSS:-1380}
MSS_OUT=(-p tcp --tcp-flags SYN,RST SYN -m tcpmss --mss "$((MSS + 1)):65535" -j TCPMSS --set-mss "$MSS")
PING_REJECT=(-p icmp --icmp-type echo-request -m comment --comment 'vpn2: Hysteria2 has no ICMP' -j REJECT --reject-with icmp-proto-unreachable)

if [[ "$ACTION" == "up" ]]; then
  # Нужны и при syncconf, и после reboot до появления первого нового пира.
  for subnet in ${WG_SUBNETS:?}; do
    ip route replace "$subnet" dev "$WG_IF"
  done
  # Явный pref: без него ядро ставит правило выше самого верхнего (219 перед
  # strongSwan 220), а юниты wg/ovpn/ikev2 на загрузке в гонке добавляют копии.
  ip rule list pref "$PREF" | grep "fwmark $MARK lookup $TABLE" >/dev/null || \
    ip rule add fwmark "$MARK" lookup "$TABLE" pref "$PREF" 2>/dev/null || \
    ip rule list pref "$PREF" | grep "fwmark $MARK lookup $TABLE" >/dev/null
  for p in $(ip rule list | awk -F: -v m="fwmark $MARK lookup $TABLE" -v k="$PREF" 'index($0, m) && $1 != k {print $1}'); do
    ip rule del pref "$p" fwmark "$MARK" lookup "$TABLE" 2>/dev/null || true
  done
  # replace, не add: на загрузке wg/ovpn/ikev2-routing стартуют разом, и
  # проигравший гонку add падал с File exists, не поставив свой TPROXY.
  ip route show table "$TABLE" 2>/dev/null | grep 'local default' >/dev/null || \
    ip route replace local 0.0.0.0/0 dev lo table "$TABLE"

  iptables -t mangle -N ANYSDA_WG_TPROXY 2>/dev/null || true
  iptables -t mangle -F ANYSDA_WG_TPROXY
  # DNS to AdGuard (entry mgmt IP) must be delivered locally, not TPROXY'd.
  iptables -t mangle -A ANYSDA_WG_TPROXY -d 10.99.0.1 -j RETURN
  # MSS режем в обе стороны, как у OpenVPN: TCP клиента заканчивается в
  # sing-box, и сегменты к клиенту он режет по MSS из его SYN. Клиент с MTU
  # больше, чем держит его путь (PPPoE 1492 и т.п.), иначе получает
  # туннельный UDP крупнее пути, фрагменты теряются, HTTPS виснет.
  iptables -t mangle -A ANYSDA_WG_TPROXY "${MSS_OUT[@]}"
  iptables -t mangle -C POSTROUTING -o "$WG_IF" "${MSS_OUT[@]}" 2>/dev/null || \
    iptables -t mangle -A POSTROUTING -o "$WG_IF" "${MSS_OUT[@]}"
  iptables -t mangle -A ANYSDA_WG_TPROXY -p tcp -j TPROXY --tproxy-mark "${MARK}/${MARK}" --on-port "$TPROXY_PORT" --on-ip 127.0.0.1
  iptables -t mangle -A ANYSDA_WG_TPROXY -p udp -j TPROXY --tproxy-mark "${MARK}/${MARK}" --on-port "$TPROXY_PORT" --on-ip 127.0.0.1

  iptables -t mangle -C PREROUTING -i "$WG_IF" -j ANYSDA_WG_TPROXY 2>/dev/null || \
    iptables -t mangle -I PREROUTING 1 -i "$WG_IF" -j ANYSDA_WG_TPROXY

  iptables -C INPUT -m mark --mark "${MARK}/${MARK}" -j ACCEPT 2>/dev/null || \
    iptables -I INPUT 1 -m mark --mark "${MARK}/${MARK}" -j ACCEPT

  # Hysteria2 переносит только TCP/UDP. Отказ до ufw/Docker ACCEPT: ни
  # тихого DROP, ни обхода выбранного exit через WAN entry. INPUT (ping
  # самого entry) и ICMP-ошибки, включая PMTU, этим правилом не затронуты.
  iptables -C FORWARD -i "$WG_IF" "${PING_REJECT[@]}" 2>/dev/null || \
    iptables -I FORWARD 1 -i "$WG_IF" "${PING_REJECT[@]}"

  # v6: FORWARD по умолчанию DROP, форвардинг с любого другого интерфейса
  # (WAN, exit) остаётся закрыт независимо от sysctl. wg0 получает явный
  # REJECT первой строкой: перед ним у ufw уже стоят терминальные ACCEPT
  # для ICMPv6, append в конец эти ACCEPT не обгонит. Снимаем все копии
  # (в т.ч. дописанные append'ом раньше) и вставляем заново на позицию 1.
  ip6tables -P FORWARD DROP
  while ip6tables -C FORWARD -i "$WG_IF" -j REJECT --reject-with icmp6-addr-unreachable 2>/dev/null; do
    ip6tables -D FORWARD -i "$WG_IF" -j REJECT --reject-with icmp6-addr-unreachable
  done
  ip6tables -I FORWARD 1 -i "$WG_IF" -j REJECT --reject-with icmp6-addr-unreachable

  echo "anysda-wg-routing up (iface=$WG_IF, mark=$MARK, tproxy=$TPROXY_PORT, mss=$MSS, v6-fastfail=on)"

elif [[ "$ACTION" == "down" ]]; then
  iptables -D FORWARD -i "$WG_IF" "${PING_REJECT[@]}" 2>/dev/null || true
  iptables -t mangle -D PREROUTING -i "$WG_IF" -j ANYSDA_WG_TPROXY 2>/dev/null || true
  iptables -t mangle -D POSTROUTING -o "$WG_IF" "${MSS_OUT[@]}" 2>/dev/null || true
  iptables -t mangle -F ANYSDA_WG_TPROXY 2>/dev/null || true
  iptables -t mangle -X ANYSDA_WG_TPROXY 2>/dev/null || true
  # INPUT по mark и правило таблицы 101 общие с OpenVPN/IKEv2: остановка
  # одного wg0 не должна отрезать остальные туннели.
  ip6tables -D FORWARD -i "$WG_IF" -j REJECT --reject-with icmp6-addr-unreachable 2>/dev/null || true
  # Policy DROP на FORWARD осознанно не откатываем, так безопаснее и вне
  # активного окна службы.
fi
IPTSEOF

chmod +x /usr/local/sbin/anysda-wg-routing.sh
systemctl daemon-reload
systemctl enable anysda-wg-routing >/dev/null 2>&1 || true
svc_restart_if_changed anysda-wg-routing /etc/systemd/system/anysda-wg-routing.service \
  /usr/local/sbin/anysda-wg-routing.sh
vpn_routing_ensure anysda-wg-routing wg0 ANYSDA_WG_TPROXY

touch /var/anysda/.stamps/28-wireguard
echo "[$HOST_TAG] 28-wireguard done"
