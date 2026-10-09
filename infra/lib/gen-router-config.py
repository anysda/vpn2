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
  {TAG}_MGMT             — пароль служебного Hysteria2 (скрейп node_exporter)
  {TAG}_OBFS             — пароль salamander obfs
  HY2_MGMT_PORT          — порт служебного hy2 (или {TAG}_HY2_MGMT_PORT на ноду)
  MGMT_IP_{TAG}          — служебный IP экзита; последний октет задаёт mon-порт
  WG_OUT_IFACE           — WAN-интерфейс для direct-ru (привязка сокета)
  RU_CLASH_SECRET        — секрет clash API

Опциональные (YouTube мимо экзитов, см. стадию 19-yt-zapret):
  YT_ROUTE               — off | zapret | direct   (дефолт off)
  YT_MARK                — SO_MARK для YouTube-трафика (дефолт 256 = 0x100)
  YT_QUIC                — block | allow           (дефолт block)
  YT_DOMAINS             — пробельный список доменных суффиксов (переопределяет дефолт)

С ключом --agh-yt-upstream печатает строку апстрима AdGuard для YouTube-доменов
(`[/домены/]<RU_DNS>`) или пустоту при YT_ROUTE=off - её берёт стадия 22, чтобы
список доменов и российский резолвер жили только здесь.
"""

import ipaddress
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
#     рекламу, даже когда видео идёт с РФ-IP;
#   • тот же список — апстрим AdGuard на RU_DNS, а AdGuard сверяет с ним имя
#     из запроса: имя, которого тут нет, резолвит кто угодно из parallel.
#     Поэтому сюда же youtube.googleapis.com, встроенный плеер, аватарки
#     yt3.googleusercontent.com и yt.be. Страновые youtube.<cc> не нужны:
#     они только редиректят на www.youtube.com.
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
    'youtube.googleapis.com',
    'youtubeembeddedplayer.googleapis.com',
    'yt3.googleusercontent.com',
    'yt.be',
]


# Российский резолвер: отдаёт YouTube адреса GGC внутри РФ-провайдеров.
RU_DNS = '77.88.8.8'


def yt_settings():
    """YT_ROUTE и список доменов из окружения; общий для main и AdGuard."""
    yt_route = (os.environ.get('YT_ROUTE') or 'off').strip().lower()
    if yt_route not in ('off', 'zapret', 'direct'):
        print(f'ERROR: YT_ROUTE={yt_route!r} — допустимо off | zapret | direct',
              file=sys.stderr)
        sys.exit(1)
    yt_domains = (os.environ.get('YT_DOMAINS') or '').split() or YT_DOMAIN_SUFFIXES
    return yt_route, yt_domains


def agh_yt_upstream():
    """Апстрим AdGuard: YouTube-домены резолвит только RU_DNS.

    Клиенты туннелей спрашивают DNS у AdGuard напрямую, а у него
    upstream_mode: parallel и среди апстримов DoH Cloudflare/Google: ответ мог
    прийти от зарубежного узла и увести видео с GGC внутри РФ на далёкий кеш.
    """
    yt_route, yt_domains = yt_settings()
    if yt_route != 'off':
        print('[/' + '/'.join(d.strip('.') for d in yt_domains) + '/]' + RU_DNS)


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
    # Служебный Hysteria2-туннель до экзитов: по нему (и только по нему) идёт
    # заграничный скрейп node_exporter. WG-mesh через границу блокируется, hy2
    # мимикрирует под TLS — см. docs/mgmt-over-hysteria2-design.md.
    hy2_mgmt_port_global = int(require('HY2_MGMT_PORT'))
    wg_out_iface = require('WG_OUT_IFACE')
    ru_clash_secret = require('RU_CLASH_SECRET')
    mgmt_ip = os.environ.get('MGMT_IP', '10.99.0.1')

    outbounds = [
        {
            'type': 'direct',
            'tag': 'direct-ru',
            'bind_interface': wg_out_iface,
            'domain_strategy': 'ipv4_only',
        },
        # AdGuard слушает mgmt-IP на этой же ноде (lo). Через direct-ru
        # (bind net0) до него не достучаться, а auto_detect_interface
        # прибил бы к net0 и голый direct - поэтому явно lo.
        {
            'type': 'direct',
            'tag': 'local-dns',
            'bind_interface': 'lo',
        },
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
    yt_route, yt_domains = yt_settings()
    yt_quic = (os.environ.get('YT_QUIC') or 'block').strip().lower()
    if yt_quic not in ('block', 'allow'):
        print(f'ERROR: YT_QUIC={yt_quic!r} — допустимо block | allow', file=sys.stderr)
        sys.exit(1)
    try:
        yt_mark = int(os.environ.get('YT_MARK') or 256)
    except ValueError:
        print('ERROR: YT_MARK должен быть числом', file=sys.stderr)
        sys.exit(1)
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
    # Служебный слой: на каждый экзит — свой hy2-{tag}-mgmt outbound и локальный
    # SOCKS-инбаунд mon-{tag}, через который VictoriaMetrics скребёт node_exporter
    # экзита. Порт mon-инбаунда выводится детерминированно из последнего октета
    # MGMT_IP_{TAG} — та же формула в 25-monitoring.sh, чтобы стороны сошлись
    # без общего конфига.
    mon_inbounds = []
    mon_route_rules = []

    for tag in exit_tags:
        T = tag.upper()
        domain = require(f'DOMAIN_{T}')
        direct_port = int(os.environ.get(f'{T}_HY2_DIRECT_PORT') or hy2_direct_port_global)
        mgmt_port = int(os.environ.get(f'{T}_HY2_MGMT_PORT') or hy2_mgmt_port_global)
        pwd_direct = require(f'{T}_DIRECT')
        pwd_warp = require(f'{T}_WARP')
        pwd_mgmt = require(f'{T}_MGMT')
        pwd_obfs = require(f'{T}_OBFS')
        mon_port = 10100 + int(require(f'MGMT_IP_{T}').split('.')[-1])

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
        # Служебный туннель до экзита. Тот же self-signed cert/obfs, что и у
        # рабочих hy2, но отдельный порт/пароль и своя пара outbound↔inbound,
        # чтобы служебный трафик не делил сессию с пользовательским.
        outbounds.append({
            'type': 'hysteria2',
            'tag': f'hy2-{tag}-mgmt',
            'server': domain,
            'server_port': mgmt_port,
            'password': pwd_mgmt,
            'obfs': {'type': 'salamander', 'password': pwd_obfs},
            'tls': dict(tls_common),
        })
        # Локальный SOCKS/HTTP-инбаунд: VictoriaMetrics ходит сюда как через
        # прокси (proxy_url: socks5://127.0.0.1:mon_port), запрос уезжает в
        # hy2-{tag}-mgmt и на экзите бьёт в node_exporter 127.0.0.1:9100.
        mon_inbounds.append({
            'type': 'mixed',
            'tag': f'mon-{tag}',
            'listen': '127.0.0.1',
            'listen_port': mon_port,
        })
        mon_route_rules.append({'inbound': f'mon-{tag}', 'outbound': f'hy2-{tag}-mgmt'})

        direct_tags.append(f'hy2-{tag}-direct')
        warp_tags.append(f'hy2-{tag}-warp')

    all_hy2 = direct_tags + warp_tags

    # Дорожки (lanes) — балансировка активных устройств по экзитам. Устройство
    # попадает в lane-NN по VPN-IP: последний октет mod LANES (подсети WG/OVPN/
    # IKEv2). Каждая дорожка — selector со всеми hy2-выходами; на какой экзит
    # её направить, решает failover-watchdog (clash-api PUT, без рестарта
    # sing-box). Списки статические: новые устройства раскладываются сами.
    lanes = int(os.environ.get('LANES') or 32)
    lane_subnets = (os.environ.get('LANE_SUBNETS')
                    or '10.66.66.0/24 10.67.67.0/24 10.68.68.0/24').split()
    lane_ips = [[] for _ in range(lanes)]
    for net in map(ipaddress.ip_network, lane_subnets):
        for host in net.hosts():
            lane_ips[int(host) % 256 % lanes].append(f'{host}/32')
    lane_tags = [f'lane-{i:02d}' for i in range(lanes)]
    lane_outbounds = [{
        'type': 'selector',
        'tag': t,
        'outbounds': all_hy2,
        # стартовый выбор до первого тика вотчдога — разносим по direct-выходам
        'default': direct_tags[i % len(direct_tags)],
        'interrupt_exist_connections': False,
    } for i, t in enumerate(lane_tags)]
    lane_route_rules = [
        {'source_ip_cidr': ips, 'outbound': t}
        for t, ips in zip(lane_tags, lane_ips) if ips
    ]

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
    ] + lane_outbounds

    # ── YouTube: правила маршрута ──────────────────────────────────────
    # Порядок в route.rules критичен: YouTube обязан стоять ВЫШЕ правила
    # `geoip: ru`. GGC-хосты (rrN---sn-*.googlevideo.com) физически стоят у
    # российских провайдеров, и geoip отправил бы их в direct-ru — выход тот же
    # самый, но БЕЗ метки, то есть без десинка. Симптом был бы «морда
    # открывается, видео виснет» — ровно тот, что уже ловили дома.
    yt_route_rules = []
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
        # Адрес YouTube клиент туннеля берёт у AdGuard, а не у sing-box:
        # инбаунд tproxy только сниффит домен и не резолвит его. Поэтому
        # YouTube -> RU_DNS задан апстримом AdGuard (стадия 22,
        # agh_yt_upstream), а не DNS-правилом здесь.

    cfg = {
        'log': {'level': 'error', 'timestamp': True},

        'dns': {
            'servers': [
                {'tag': 'ru-dns', 'address': RU_DNS, 'detour': 'direct-ru'},
                # Этот DNS нужен только самому sing-box: имена из tg-proxy
                # (бот). Клиенты туннелей спрашивают AdGuard напрямую, и их
                # ответы эти правила не трогают; для бота .ru идёт через
                # Яндекс, остальное через AdGuard.
                {'tag': 'adguard', 'address': mgmt_ip, 'detour': 'local-dns'},
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
                # Только sniff, БЕЗ sniff_override_destination. Правила по
                # домену матчат сниффнутый домен (SNI/Host/QUIC) и без подмены,
                # а подмена стирала исходный IP: ip_cidr из панели и
                # geoip ru/private не матчились ни на одном HTTP/HTTPS/QUIC
                # (VPN2-61). Назначение остаётся тем IP, куда шёл клиент, и
                # tproxy отвечает на UDP с него же - udp_disable_domain_unmapping
                # больше не нужен.
                'sniff': True,
            },
        ] + mon_inbounds,

        'outbounds': outbounds,

        'route': {
            'geoip':   {'path': '/var/lib/sing-box/geoip.db'},
            'geosite': {'path': '/var/lib/sing-box/geosite.db'},
            # Маршрутизация:
            #   1. YouTube (если YT_ROUTE != off) → youtube-ru, РФ-выход + десинк
            #   2. .ru/.рф/.su по домену → direct-ru (даже если сайт хостится за рубежом)
            #   3. category-gov-ru → direct-ru (госуслуги и т.п.)
            #   4. geoip:ru / private → direct-ru (физически в РФ)
            #   5. клиентские VPN-IP → lane-NN (selector; экзит дорожки выбирает watchdog)
            #   6. всё остальное → foreign-best (selector; экзит выбирает failover-watchdog)
            # warp-best / hy2-*-warp используются ТОЛЬКО через ручные правила (UI).
            'rules': mon_route_rules + [
                # mon-{tag} матчатся по inbound-тегу ВЫШЕ блока 127.0.0.0/8:
                # цель служебного запроса — 127.0.0.1:9100 (loopback экзита),
                # иначе его срезало бы правило ниже.
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
            ] + lane_route_rules,
            'final': 'foreign-best',
            'auto_detect_interface': True,
        },

        'experimental': {
            # Выбор foreign-best, сделанный сторожем через clash API, иначе
            # терялся при каждом перезапуске sing-box: selector вставал на
            # первый тег, даже если тот мёртв. В 1.10 выбор селекторов
            # хранится в cache_file сам, отдельного store_selected нет.
            'cache_file': {
                'enabled': True,
                'path': '/var/lib/sing-box/cache.db',
            },
            'clash_api': {
                'external_controller': f'{mgmt_ip}:9090',
                'secret': ru_clash_secret,
            },
        },
    }

    print(json.dumps(cfg, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    if sys.argv[1:] == ['--agh-yt-upstream']:
        agh_yt_upstream()
    else:
        main()
