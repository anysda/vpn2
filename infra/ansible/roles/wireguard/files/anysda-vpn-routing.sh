#!/usr/bin/env bash
# Трафик, пересылаемый с туннеля IFACE, уходит в TPROXY sing-box :7898.
# Окружение юнита: IFACE, CHAIN, MSS; ROUTES - подсети пула на IFACE;
# V6_REJECT=1 - v6 с IFACE получает быстрый отказ; XFRM_DEV - IFACE как xfrm-интерфейс
# IPsec (if_id 42) поверх этого устройства, SUBNET6 - его v6-пул.
# Метка, таблица и правило INPUT общие у всех туннелей: остановка одного их не снимает.
set -euo pipefail
ACTION=${1:-up}
exec 9>/run/anysda-vpn-routing.lock
flock -x 9
MARK='0x42'
TABLE=101
PREF=32765
MSS_OUT=(-p tcp --tcp-flags SYN,RST SYN -m tcpmss --mss "$((MSS + 1)):65535" -j TCPMSS --set-mss "$MSS")
PING_REJECT=(-p icmp --icmp-type echo-request -m comment --comment 'vpn2: Hysteria2 has no ICMP' -j REJECT --reject-with icmp-proto-unreachable)
V6=(FORWARD -i "$IFACE" -j REJECT --reject-with icmp6-addr-unreachable)

if [[ "$ACTION" == up ]]; then
  if [[ -n "${XFRM_DEV:-}" ]]; then
    ip link show "$IFACE" >/dev/null 2>&1 || ip link add "$IFACE" type xfrm dev "$XFRM_DEV" if_id 42
    ip link set "$IFACE" mtu 1400 up
    ip -6 route replace "$SUBNET6" dev "$IFACE"
  fi
  # Обратные пакеты к клиентам: без маршрута на пул они дохнут на интерфейсе.
  for subnet in ${ROUTES:-}; do
    ip route replace "$subnet" dev "$IFACE"
  done
  # Явный pref: без него правило встаёт выше strongSwan (220), а юниты на загрузке добавляют копии.
  ip rule list pref "$PREF" | grep -q "fwmark $MARK lookup $TABLE" || \
    ip rule add fwmark "$MARK" lookup "$TABLE" pref "$PREF" 2>/dev/null || \
    ip rule list pref "$PREF" | grep -q "fwmark $MARK lookup $TABLE"
  for p in $(ip rule list | awk -F: -v m="fwmark $MARK lookup $TABLE" -v k="$PREF" 'index($0, m) && $1 != k {print $1}'); do
    ip rule del pref "$p" fwmark "$MARK" lookup "$TABLE" 2>/dev/null || true
  done
  ip route show table "$TABLE" 2>/dev/null | grep -q 'local default' || \
    ip route replace local 0.0.0.0/0 dev lo table "$TABLE"

  iptables -t mangle -N "$CHAIN" 2>/dev/null || true
  iptables -t mangle -F "$CHAIN"
  # DNS клиентов - AdGuard на mgmt-адресе entry, он локальный.
  iptables -t mangle -A "$CHAIN" -d 10.99.0.1 -j RETURN
  # MSS в обе стороны: TCP клиента кончается в sing-box, сегменты к клиенту он режет по его SYN.
  iptables -t mangle -A "$CHAIN" "${MSS_OUT[@]}"
  iptables -t mangle -C POSTROUTING -o "$IFACE" "${MSS_OUT[@]}" 2>/dev/null || \
    iptables -t mangle -A POSTROUTING -o "$IFACE" "${MSS_OUT[@]}"
  for proto in tcp udp; do
    iptables -t mangle -A "$CHAIN" -p "$proto" -j TPROXY --tproxy-mark "$MARK/$MARK" --on-port 7898 --on-ip 127.0.0.1
  done
  iptables -t mangle -C PREROUTING -i "$IFACE" -j "$CHAIN" 2>/dev/null || \
    iptables -t mangle -I PREROUTING 1 -i "$IFACE" -j "$CHAIN"
  iptables -C INPUT -m mark --mark "$MARK/$MARK" -j ACCEPT 2>/dev/null || \
    iptables -I INPUT 1 -m mark --mark "$MARK/$MARK" -j ACCEPT
  # Отказ ping до ufw/Docker ACCEPT; локальный ping и ICMP-ошибки (PMTU) не затронуты.
  iptables -C FORWARD -i "$IFACE" "${PING_REJECT[@]}" 2>/dev/null || \
    iptables -I FORWARD 1 -i "$IFACE" "${PING_REJECT[@]}"
  if [[ "${V6_REJECT:-}" == 1 ]]; then
    # Первой строкой, перед ACCEPT ICMPv6 у ufw; policy DROP закрывает остальные интерфейсы.
    ip6tables -P FORWARD DROP
    while ip6tables -C "${V6[@]}" 2>/dev/null; do ip6tables -D "${V6[@]}"; done
    ip6tables -I "${V6[@]:0:1}" 1 "${V6[@]:1}"
  fi
  echo "anysda-vpn-routing up (iface=$IFACE, chain=$CHAIN, mss=$MSS)"
elif [[ "$ACTION" == down ]]; then
  iptables -D FORWARD -i "$IFACE" "${PING_REJECT[@]}" 2>/dev/null || true
  iptables -t mangle -D PREROUTING -i "$IFACE" -j "$CHAIN" 2>/dev/null || true
  iptables -t mangle -D POSTROUTING -o "$IFACE" "${MSS_OUT[@]}" 2>/dev/null || true
  iptables -t mangle -F "$CHAIN" 2>/dev/null || true
  iptables -t mangle -X "$CHAIN" 2>/dev/null || true
  [[ "${V6_REJECT:-}" == 1 ]] && { ip6tables -D "${V6[@]}" 2>/dev/null || true; }
  [[ -n "${XFRM_DEV:-}" ]] && { ip link del "$IFACE" 2>/dev/null || true; }
fi
exit 0
