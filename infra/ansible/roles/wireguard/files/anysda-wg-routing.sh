#!/usr/bin/env bash
# Divert wg0 forwarded TCP+UDP into sing-box TPROXY on :7898 so WG clients
# pick the same geoip routing as OpenVPN clients. Also: ULA fast-fail для
# v6 (адрес fd66:66::1 на wg0): FORWARD ставится в DROP и v6 с wg0 отбивается
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
