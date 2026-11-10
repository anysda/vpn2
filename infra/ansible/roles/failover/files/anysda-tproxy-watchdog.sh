#!/usr/bin/env bash
# Сторож TPROXY на entry (anysda-tproxy-watchdog.timer, раз в минуту).
#
# Маршрутизация fwmark 0x42 → table 101 → local default. Её ставят
# anysda-{wg,ovpn,ikev2}-routing один раз при старте; перезапуск
# systemd-networkd её стирает, TPROXY метит пакеты, а доставить их в sing-box
# некуда. Возвращаем на место.
set -uo pipefail
MARK='0x42'
TABLE=101
PREF=32765

# ip rule переживает nft flush; отсутствие PREROUTING раньше выключало
# проверку целиком. Восстанавливаем цепочки только активных VPN-служб.
for spec in 'wg wg0 WG' 'ovpn tun0 OVPN' 'ikev2 xfrm0 IKEV2'; do
  read -r protocol iface chain <<<"$spec"
  unit="anysda-$protocol-routing"
  if systemctl is-active --quiet "$unit" \
     && ! iptables -t mangle -C PREROUTING -i "$iface" -j "ANYSDA_${chain}_TPROXY" 2>/dev/null; then
    echo "$unit: цепочка TPROXY пропала, восстанавливаю" >&2
    systemctl restart "$unit" || exit 1
  fi
done

if iptables -t mangle -S PREROUTING 2>/dev/null | grep -E -- '-j ANYSDA_[A-Z0-9]+_TPROXY' >/dev/null; then
  # pref тот же, что ставят anysda-{wg,ovpn,ikev2}-routing; без него правило
  # вставало на 219, выше strongSwan 220, и так там и оставалось.
  if ! ip rule list pref "$PREF" | grep "fwmark $MARK lookup $TABLE" >/dev/null; then
    ip rule add fwmark "$MARK" lookup "$TABLE" pref "$PREF" \
      && echo "правило fwmark $MARK lookup $TABLE пропало с приоритета $PREF, вернул"
  fi
  for p in $(ip rule list | awk -F: -v m="fwmark $MARK lookup $TABLE" -v k="$PREF" 'index($0, m) && $1 != k {print $1}'); do
    ip rule del pref "$p" fwmark "$MARK" lookup "$TABLE" \
      && echo "копия правила fwmark $MARK lookup $TABLE на приоритете $p, убрал"
  done
  if ! ip route show table "$TABLE" 2>/dev/null | grep 'local default' >/dev/null; then
    ip route add local 0.0.0.0/0 dev lo table "$TABLE" \
      && echo "маршрут local default в таблице $TABLE пропал, вернул"
  fi
fi
