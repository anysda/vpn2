#!/usr/bin/env python3
"""Генератор nft-таблицы, режущей QUIC (UDP/443) к Google из туннелей.

Зачем это нужно, если QUIC для YouTube уже режет sing-box правилом
`domain_suffix=[youtube...] network=udp port=443 => block-out`:

Правило sing-box работает по SNI, добытому сниффером из QUIC Initial. Это
верно ровно до тех пор, пока сниффер видит домен. Если ClientHello приедет
фрагментированным, придёт 0-RTT или клиент пойдёт на голый IP — домена нет,
правило не матчится, и QUIC утекает мимо zapret-десинка. Десинк работает
только по TCP, так что утечка означает YouTube без обхода: реклама и потеря
российского GGC.

Цена ошибки несимметрична: лишний раз отрезанный QUIC к Google означает лишь
откат на TCP ((работает штатно), а пропущенный — сломанный YouTube у всех
клиентов сразу. Поэтому вторым рубежом стоит блокировка по IP-принадлежности
Google, которая от сниффера не зависит вовсе.

Список берётся из официального https://www.gstatic.com/ipranges/goog.json,
снапшот лежит в репе как fallback — стадия не должна оставлять дыру, если
gstatic недоступен в момент прогона.
"""
import ipaddress
import json
import sys

TABLE = 'anysda_ytquic'
# Интерфейсы туннелей: WG, IKEv2 и OpenVPN. Клиент любого из них должен
# одинаково падать на TCP.
TUN_IFACES = ('wg0', 'xfrm0', 'tun0')


def load(path):
    with open(path) as f:
        data = json.load(f)
    v4, v6 = set(), set()
    for row in data.get('prefixes', []):
        if 'ipv4Prefix' in row:
            v4.add(str(ipaddress.IPv4Network(row['ipv4Prefix'], strict=False)))
        elif 'ipv6Prefix' in row:
            v6.add(str(ipaddress.IPv6Network(row['ipv6Prefix'], strict=False)))
    if not v4:
        raise SystemExit(f'{path}: не разобрано ни одного IPv4-префикса')
    return sorted(v4), sorted(v6)


def main():
    if len(sys.argv) != 2:
        raise SystemExit('usage: gen-yt-quic-nft.py <goog.json>')
    v4, v6 = load(sys.argv[1])

    lines = [
        '#!/usr/sbin/nft -f',
        '# Managed by anysda-vpn2 stage 19-yt-zapret. Не редактировать руками —',
        '# перезаписывается стадией и таймером anysda-yt-quic-refresh.timer.',
        '',
        f'table inet {TABLE}',
        f'delete table inet {TABLE}',
        '',
        f'table inet {TABLE} {{',
        '    set google_v4 {',
        '        type ipv4_addr',
        '        flags interval',
        '        elements = { ' + ', '.join(v4) + ' }',
        '    }',
        '    set google_v6 {',
        '        type ipv6_addr',
        '        flags interval',
        '        elements = { ' + ', '.join(v6) + ' }',
        '    }',
        '',
        '    # priority -200 — РАНЬШЕ mangle/prerouting (-150), где стоит TPROXY.',
        '    # Иначе пакет уйдёт в sing-box до того, как мы его увидим.',
        '    chain prerouting {',
        '        type filter hook prerouting priority -200; policy accept;',
    ]
    for iface in TUN_IFACES:
        lines.append(f'        iifname "{iface}" udp dport 443 ip daddr @google_v4 counter drop')
        lines.append(f'        iifname "{iface}" udp dport 443 ip6 daddr @google_v6 counter drop')
    lines += ['    }', '}', '']
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
