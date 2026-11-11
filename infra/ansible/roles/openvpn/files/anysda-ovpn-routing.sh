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
