#!/usr/bin/env python3
"""
gen-router-config.py — генерирует sing-box конфиг роутера для RU-ноды.

Читает переменные из окружения, пишет JSON в stdout.
Вызывается из 20-ru-router.sh после того, как все переменные экспортированы.

Обязательные переменные:
  EXIT_TAGS              — пробельный список тегов выходных нод (us se ...)
  DOMAIN_{TAG}           — домен выходной ноды (DOMAIN_US, DOMAIN_SE, ...)
  {TAG}_HY2_DIRECT_PORT  — порт direct (или HY2_DIRECT_PORT — глобальный дефолт)
  HY2_WARP_PORT          — порт warp (общий для всех нод)
  {TAG}_DIRECT           — пароль Hysteria2 direct
  {TAG}_WARP             — пароль Hysteria2 warp
  {TAG}_OBFS             — пароль salamander obfs
  WG_OUT_IFACE           — WAN-интерфейс для direct-ru (привязка сокета)
  RU_CLASH_SECRET        — секрет clash API
"""

import json
import os
import sys


def require(key):
    v = os.environ.get(key)
    if not v:
        print(f'ERROR: {key} не задан', file=sys.stderr)
        sys.exit(1)
    return v


def main():
    exit_tags = require('EXIT_TAGS').split()
    if not exit_tags:
        print('ERROR: EXIT_TAGS пустой', file=sys.stderr)
        sys.exit(1)

    hy2_direct_port_global = int(require('HY2_DIRECT_PORT'))
    hy2_warp_port = int(require('HY2_WARP_PORT'))
    wg_out_iface = require('WG_OUT_IFACE')
    ru_clash_secret = require('RU_CLASH_SECRET')
    mgmt_ip = os.environ.get('MGMT_IP', '10.99.0.1')

    outbounds = [
        {
            'type': 'direct',
            'tag': 'direct-ru',
            'bind_interface': wg_out_iface,
            'domain_strategy': 'ipv4_only',
        }
    ]

    direct_tags = []
    warp_tags = []

    for tag in exit_tags:
        T = tag.upper()
        domain = require(f'DOMAIN_{T}')
        direct_port = int(os.environ.get(f'{T}_HY2_DIRECT_PORT') or hy2_direct_port_global)
        pwd_direct = require(f'{T}_DIRECT')
        pwd_warp = require(f'{T}_WARP')
        pwd_obfs = require(f'{T}_OBFS')

        # Self-signed cert на exit-нодах → insecure (своя инфраструктура)
        tls_direct = {'enabled': True, 'server_name': domain, 'insecure': True}
        tls_warp   = {'enabled': True, 'server_name': domain, 'insecure': True}

        outbounds.append({
            'type': 'hysteria2',
            'tag': f'hy2-{tag}-direct',
            'server': domain,
            'server_port': direct_port,
            'password': pwd_direct,
            'obfs': {'type': 'salamander', 'password': pwd_obfs},
            'tls': tls_direct,
        })
        outbounds.append({
            'type': 'hysteria2',
            'tag': f'hy2-{tag}-warp',
            'server': domain,
            'server_port': hy2_warp_port,
            'password': pwd_warp,
            'obfs': {'type': 'salamander', 'password': pwd_obfs},
            'tls': tls_warp,
        })
        direct_tags.append(f'hy2-{tag}-direct')
        warp_tags.append(f'hy2-{tag}-warp')

    all_hy2 = direct_tags + warp_tags

    outbounds += [
        {
            'type': 'urltest',
            'tag': 'direct-best',
            'outbounds': direct_tags,
            # Пробный URL — НЕ Cloudflare: до cp.cloudflare.com WARP (сеть
            # Cloudflare) добирается быстрее direct, и замеры врут. gstatic
            # нейтрален — direct закономерно не медленнее warp.
            'url': 'http://www.gstatic.com/generate_204',
            'interval': '10s',
            'tolerance': 50,
            'idle_timeout': '5m',
            'interrupt_exist_connections': True,
        },
        {
            'type': 'urltest',
            'tag': 'warp-best',
            'outbounds': warp_tags,
            # Пробный URL — НЕ Cloudflare: до cp.cloudflare.com WARP (сеть
            # Cloudflare) добирается быстрее direct, и замеры врут. gstatic
            # нейтрален — direct закономерно не медленнее warp.
            'url': 'http://www.gstatic.com/generate_204',
            'interval': '10s',
            'tolerance': 50,
            'idle_timeout': '5m',
            'interrupt_exist_connections': True,
        },
        {
            # foreign-best — selector, а НЕ urltest: им управляет
            # failover-watchdog (стейдж 21), активно опрашивая экзиты через
            # clash-api и переключаясь за ~3-5с. urltest сюда не годится — он
            # снимается с мёртвой ноды ~33с (зависшее QUIC-соединение висит
            # ~30с до признания дохлым). `default` — стартовый выбор до
            # первого тика вотчдога. См. lib/failover-watchdog.py.
            'type': 'selector',
            'tag': 'foreign-best',
            'outbounds': all_hy2,
            'default': all_hy2[0],
            'interrupt_exist_connections': True,
        },
        {'type': 'block', 'tag': 'block-out'},
    ]

    cfg = {
        'log': {'level': 'error', 'timestamp': True},

        'dns': {
            'servers': [
                {'tag': 'ru-dns', 'address': '77.88.8.8', 'detour': 'direct-ru'},
                # AdGuard Home on the entry mgmt IP — sing-box resolves through
                # it so clients get ad/tracker filtering. .ru stays on Yandex
                # DNS for correct Russian CDN IPs (geoip routing depends on it).
                {'tag': 'adguard', 'address': '10.99.0.1', 'detour': 'direct-ru'},
            ],
            'rules': [
                {'domain_suffix': ['.ru', '.рф', '.su'], 'server': 'ru-dns'},
                {'geosite': ['category-gov-ru'], 'server': 'ru-dns'},
            ],
            'final': 'adguard',
            'strategy': 'ipv4_only',
        },

        'inbounds': [
            {
                # Forwarded wg0/tun0 traffic lands here via TPROXY (PREROUTING).
                # `network` is omitted on purpose — sing-box rejects "tcp,udp"
                # and accepting a single side wouldn't cover WG clients;
                # omitting the filter accepts both protocols.
                'type': 'tproxy',
                'tag': 'wg-tproxy-in',
                'listen': '127.0.0.1',
                'listen_port': 7898,
                'sniff': True,
                'sniff_override_destination': True,
            },
        ],

        'outbounds': outbounds,

        'route': {
            'geoip':   {'path': '/var/lib/sing-box/geoip.db'},
            'geosite': {'path': '/var/lib/sing-box/geosite.db'},
            # Маршрутизация:
            #   1. .ru/.рф/.su по домену → direct-ru (даже если сайт хостится за рубежом)
            #   2. category-gov-ru → direct-ru (госуслуги и т.п.)
            #   3. geoip:ru / private → direct-ru (физически в РФ)
            #   4. всё остальное → foreign-best (selector; экзит выбирает failover-watchdog)
            # warp-best / hy2-*-warp используются ТОЛЬКО через ручные правила (UI).
            'rules': [
                {'ip_cidr': ['127.0.0.0/8', '0.0.0.0/8'], 'outbound': 'block-out'},
                {
                    'port': [853],
                    'ip_cidr': ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8'],
                    'outbound': 'block-out',
                },
                {'protocol': 'dns',                          'outbound': 'direct-ru'},
                {'domain_suffix': ['.ru', '.рф', '.su'],     'outbound': 'direct-ru'},
                {'geosite': ['category-gov-ru'],             'outbound': 'direct-ru'},
                {'geoip':   ['ru', 'private'],               'outbound': 'direct-ru'},
            ],
            'final': 'foreign-best',
            'auto_detect_interface': True,
        },

        'experimental': {
            'clash_api': {
                'external_controller': f'{mgmt_ip}:9090',
                'secret': ru_clash_secret,
            },
        },
    }

    print(json.dumps(cfg, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
