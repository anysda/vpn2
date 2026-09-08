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

Опциональные (YouTube мимо экзитов, см. стадию 19-yt-zapret):
  YT_ROUTE               — off | zapret | direct   (дефолт off)
  YT_MARK                — SO_MARK для YouTube-трафика (дефолт 256 = 0x100)
  YT_QUIC                — block | allow           (дефолт block)
  YT_DOMAINS             — пробельный список доменных суффиксов (переопределяет дефолт)
"""

import json
import os
import sys

# ----------------------------------------------------------------------------
# YouTube: домены, которые уводятся с экзитов на РФ-выход (см. yt_block ниже).
# geosite:youtube ловит основную массу, но список нужен и сам по себе:
#   • geosite обновляется раз в неделю (стадия 20 качает *.db) и отстаёт;
#   • googlevideo.com — это САМО видео (GGC внутри РФ-провайдера), без него
#     уедет только морда, а видео пойдёт через экзит и с рекламой;
#   • youtubei.googleapis.com — API плеера, jnn-pa — аттестация плеера: если
#     они уходят за границу, YouTube считает клиента зарубежным и крутит
#     рекламу, даже когда видео идёт с РФ-IP.
# ----------------------------------------------------------------------------
YT_DOMAIN_SUFFIXES = [
    'youtube.com',
    'youtu.be',
    'youtube-nocookie.com',
    'youtubekids.com',
    'ytimg.com',
    'ggpht.com',
    'googlevideo.com',
    'youtubei.googleapis.com',
    'jnn-pa.googleapis.com',
]


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

    # ── YouTube: отдельный выход мимо экзитов ────────────────────────────────
    # YouTube НЕ крутит рекламу на российских IP, поэтому гнать его через
    # зарубежный экзит — значит своими руками включить рекламу. Выводим его на
    # тот же WAN, что и direct-ru, но ОТДЕЛЬНЫМ outbound'ом: у него свой
    # routing_mark, по которому nfqws2 (стадия 19-yt-zapret) забирает в очередь
    # ровно YouTube и ничего больше. DPI провайдера entry-ноды режет TLS к
    # youtube.com — десинк это и обходит.
    #   zapret — РФ-выход + метка → десинк nfqws2 (штатный режим);
    #   direct — РФ-выход без метки (десинк не нужен / диагностика);
    #   off    — как раньше, YouTube уезжает на экзит вместе со всем остальным.
    yt_route = (os.environ.get('YT_ROUTE') or 'off').strip().lower()
    if yt_route not in ('off', 'zapret', 'direct'):
        print(f'ERROR: YT_ROUTE={yt_route!r} — допустимо off | zapret | direct',
              file=sys.stderr)
        sys.exit(1)
    yt_quic = (os.environ.get('YT_QUIC') or 'block').strip().lower()
    if yt_quic not in ('block', 'allow'):
        print(f'ERROR: YT_QUIC={yt_quic!r} — допустимо block | allow', file=sys.stderr)
        sys.exit(1)
    try:
        yt_mark = int(os.environ.get('YT_MARK') or 256)
    except ValueError:
        print('ERROR: YT_MARK должен быть числом', file=sys.stderr)
        sys.exit(1)
    yt_domains = (os.environ.get('YT_DOMAINS') or '').split() or YT_DOMAIN_SUFFIXES
    yt_tag = 'youtube-ru'

    if yt_route != 'off':
        yt_outbound = {
            'type': 'direct',
            'tag': yt_tag,
            'bind_interface': wg_out_iface,
            'domain_strategy': 'ipv4_only',
        }
        if yt_route == 'zapret':
            # SO_MARK на сокете. Единственный признак, по которому nft-цепочки
            # стадии 19 отличают YouTube от остального исходящего трафика ноды —
            # никаких списков IP вести не нужно, метка едет по факту решения
            # роутера. Значение обязано не пересекаться битами с 0x42 (TPROXY
            # WG/OpenVPN) и с 0x40000000 (пакеты, которые генерит сам nfqws2).
            yt_outbound['routing_mark'] = yt_mark
        outbounds.append(yt_outbound)

    direct_tags = []
    warp_tags = []

    for tag in exit_tags:
        T = tag.upper()
        domain = require(f'DOMAIN_{T}')
        direct_port = int(os.environ.get(f'{T}_HY2_DIRECT_PORT') or hy2_direct_port_global)
        pwd_direct = require(f'{T}_DIRECT')
        pwd_warp = require(f'{T}_WARP')
        pwd_obfs = require(f'{T}_OBFS')

        # Self-signed cert на exit-нодах. Если orchestrator опубликовал
        # PEM-сертификат экзита (см. SECURITY-AUDIT-2026-06-01.md H5) и
        # 20-ru-router положил его в /etc/sing-box/exit-certs/{tag}.pem,
        # пинимся к нему через `certificate_path` (sing-box принимает ТОЛЬКО
        # этот cert). Иначе — fallback к insecure=true (backward-compat).
        cert_path = os.environ.get(f'{T}_HY2_CERT_PATH', '').strip()
        if cert_path:
            tls_common = {
                'enabled': True,
                'server_name': domain,
                'certificate_path': cert_path,
            }
        else:
            tls_common = {'enabled': True, 'server_name': domain, 'insecure': True}
        tls_direct = dict(tls_common)
        tls_warp   = dict(tls_common)

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
            # false: живые соединения (в т.ч. идущая загрузка) дорабатывают на
            # старом экзите, на новый уходят только НОВЫЕ соединения. Иначе
            # каждое переключение рвёт всё сразу (infra/reports/05-09-2026
            # -vpn2-audit-problemy.md, п. 3).
            'interrupt_exist_connections': False,
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
            'interrupt_exist_connections': False,
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
            'interrupt_exist_connections': False,
        },
        {'type': 'block', 'tag': 'block-out'},
    ]

    # ── YouTube: правила маршрута и DNS ──────────────────────────────────────
    # Порядок в route.rules критичен: YouTube обязан стоять ВЫШЕ правила
    # `geoip: ru`. GGC-хосты (rrN---sn-*.googlevideo.com) физически стоят у
    # российских провайдеров, и geoip отправил бы их в direct-ru — выход тот же
    # самый, но БЕЗ метки, то есть без десинка. Симптом был бы «морда
    # открывается, видео виснет» — ровно тот, что уже ловили дома.
    yt_route_rules = []
    yt_dns_rules = []
    if yt_route != 'off':
        if yt_quic == 'block':
            # QUIC (UDP/443) режем: десинк проверен на TCP-TLS, а QUIC-поток
            # DPI режет так же, и клиент без явного отказа висит на нём до
            # таймаута. Блок заставляет плеер откатиться на TCP сразу.
            yt_route_rules.append({
                'domain_suffix': yt_domains, 'network': 'udp', 'port': [443],
                'outbound': 'block-out',
            })
            yt_route_rules.append({
                'geosite': ['youtube'], 'network': 'udp', 'port': [443],
                'outbound': 'block-out',
            })
        yt_route_rules.append({'domain_suffix': yt_domains, 'outbound': yt_tag})
        yt_route_rules.append({'geosite': ['youtube'], 'outbound': yt_tag})
        # Резолвим YouTube российским резолвером: он отдаёт РФ-овский GGC,
        # то есть кеш видео внутри страны. Через AdGuard (upstream_mode:
        # parallel, среди upstream'ов DoH Cloudflare/Google) ответ мог бы
        # прийти от зарубежного узла — и «российский выход» терял бы смысл.
        yt_dns_rules.append({'domain_suffix': yt_domains, 'server': 'ru-dns'})
        yt_dns_rules.append({'geosite': ['youtube'], 'server': 'ru-dns'})

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
            'rules': yt_dns_rules + [
                {'domain_suffix': ['.ru', '.рф', '.su'], 'server': 'ru-dns'},
                {'geosite': ['category-gov-ru'], 'server': 'ru-dns'},
            ],
            'final': 'adguard',
            'strategy': 'ipv4_only',
        },

        'inbounds': [
            {
                # Локальный forward-прокси (HTTP CONNECT + SOCKS) для
                # Telegram-бота: RU-нода в Москве api.telegram.org напрямую
                # не достаёт, поэтому бот ходит в Telegram через этот прокси.
                # Трафик маршрутизируется как обычный заграничный →
                # foreign-best → экзит. Слушает только loopback.
                'type': 'mixed',
                'tag': 'tg-proxy',
                'listen': '127.0.0.1',
                'listen_port': 7897,
            },
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
            #   1. YouTube (если YT_ROUTE != off) → youtube-ru, РФ-выход + десинк
            #   2. .ru/.рф/.su по домену → direct-ru (даже если сайт хостится за рубежом)
            #   3. category-gov-ru → direct-ru (госуслуги и т.п.)
            #   4. geoip:ru / private → direct-ru (физически в РФ)
            #   5. всё остальное → foreign-best (selector; экзит выбирает failover-watchdog)
            # warp-best / hy2-*-warp используются ТОЛЬКО через ручные правила (UI).
            'rules': [
                {'ip_cidr': ['127.0.0.0/8', '0.0.0.0/8'], 'outbound': 'block-out'},
                {
                    'port': [853],
                    'ip_cidr': ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8'],
                    'outbound': 'block-out',
                },
                {'protocol': 'dns',                          'outbound': 'direct-ru'},
            ] + yt_route_rules + [
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
