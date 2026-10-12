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

С ключом --rule-sets печатает имена наборов правил, которые стадия 20 качает
в /var/lib/sing-box/rule-set (имя.srs).
"""

import ipaddress
import json
import os
from pathlib import Path
import sys

# Модуль рядом со скриптом: так импорт работает и при загрузке по пути файла.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hy2_bandwidth import bandwidth_fields  # noqa: E402

# ----------------------------------------------------------------------------
# YouTube: домены, которые уводятся с экзитов на РФ-выход (см. yt_block ниже).
# geosite:youtube ловит основную массу, но список нужен и сам по себе:
#   • geosite обновляется раз в неделю (стадия 20 качает .srs) и отстаёт;
#   • googlevideo.com — это САМО видео (GGC внутри РФ-провайдера), без него
#     уедет только морда, а видео пойдёт через экзит и с рекламой;
#   • youtubei.googleapis.com — API плеера, jnn-pa — аттестация плеера: если
#     они уходят за границу, YouTube считает клиента зарубежным и крутит
#     рекламу, даже когда видео идёт с РФ-IP;
#   • сюда же входят встроенный плеер, аватарки и yt.be.
#     Страновые youtube.<cc> не нужны:
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


# Российский резолвер для зон ru / рф / xn--p1ai / su.
RU_DNS = '77.88.8.8'

# Наборы правил sing-geoip/sing-geosite (geoip.db/geosite.db убраны в
# sing-box 1.12). Список и каталог общие со стадией 20, она их и качает:
# `gen-router-config.py --rule-sets` печатает имена.
RULE_SET_DIR = '/var/lib/sing-box/rule-set'
RULE_SETS = ['geoip-ru', 'geosite-youtube', 'geosite-category-gov-ru']

# Сети Telegram — https://core.telegram.org/resources/cidr.txt (2026-10-09).
TELEGRAM_CIDRS = [
    '91.105.192.0/23', '91.108.4.0/22', '91.108.8.0/22', '91.108.12.0/22',
    '91.108.16.0/22', '91.108.20.0/22', '91.108.56.0/22', '149.154.160.0/20',
    '185.76.151.0/24',
    '2001:67c:4e8::/48', '2001:b28:f23c::/48', '2001:b28:f23d::/48',
    '2001:b28:f23f::/48', '2a0a:f280::/32',
]


def yt_settings():
    """YT_ROUTE и список доменов маршрутизации из окружения."""
    yt_route = (os.environ.get('YT_ROUTE') or 'off').strip().lower()
    if yt_route not in ('off', 'zapret', 'direct'):
        print(f'ERROR: YT_ROUTE={yt_route!r} — допустимо off | zapret | direct',
              file=sys.stderr)
        sys.exit(1)
    yt_domains = (os.environ.get('YT_DOMAINS') or '').split() or YT_DOMAIN_SUFFIXES
    return yt_route, yt_domains


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
    # мимикрирует под TLS.
    hy2_mgmt_port_global = int(require('HY2_MGMT_PORT'))
    wg_out_iface = require('WG_OUT_IFACE')
    ru_clash_secret = require('RU_CLASH_SECRET')
    mgmt_ip = os.environ.get('MGMT_IP', '10.99.0.1')

    outbounds = [
        {
            'type': 'direct',
            'tag': 'direct-ru',
            'bind_interface': wg_out_iface,
        },
        # AdGuard слушает mgmt-IP на этой же ноде (lo). Через direct-ru
        # (bind net0) до него не достучаться, а auto_detect_interface
        # прибил бы к net0 и голый direct - поэтому явно lo.
        {
            'type': 'direct',
            'tag': 'local-dns',
            'bind_interface': 'lo',
        },
        # Отдельный AdGuard без фильтров (127.0.0.2:53). Адрес подменяет
        # правило route ниже: поля override_* у direct sing-box 1.13 убрал.
        {
            'type': 'direct', 'tag': 'dns-unfiltered',
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
        T = tag.upper().replace('-', '_')
        domain = require(f'DOMAIN_{T}')
        direct_port = int(os.environ.get(f'{T}_HY2_DIRECT_PORT') or hy2_direct_port_global)
        mgmt_port = int(os.environ.get(f'{T}_HY2_MGMT_PORT') or hy2_mgmt_port_global)
        pwd_direct = require(f'{T}_DIRECT')
        pwd_warp = require(f'{T}_WARP')
        pwd_mgmt = require(f'{T}_MGMT')
        pwd_obfs = require(f'{T}_OBFS')
        mon_port = 10100 + int(require(f'MGMT_IP_{T}').split('.')[-1])
        try:
            bandwidth = bandwidth_fields(os.environ.get(f'{T}_HY2_UP_MBPS'),
                                         os.environ.get(f'{T}_HY2_DOWN_MBPS'),
                                         label=f'Hysteria2 {tag}', client=True)
        except ValueError as error:
            sys.exit(f'ERROR: {error}')

        # Self-signed cert на exit-нодах. Если orchestrator опубликовал
        # PEM-сертификат экзита и
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
            **bandwidth,
            'server': domain,
            'server_port': direct_port,
            'password': pwd_direct,
            'obfs': {'type': 'salamander', 'password': pwd_obfs},
            'tls': tls_direct,
        })
        outbounds.append({
            'type': 'hysteria2',
            'tag': f'hy2-{tag}-warp',
            **bandwidth,
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
    pools_file = Path(__file__).with_name('vpn-pools.json')
    if not pools_file.exists():
        pools_file = Path(__file__).resolve().parents[2] / 'app/shared/vpn-pools.json'
    pools = json.loads(pools_file.read_text())
    # Старая LANE_SUBNETS не должна исключить расширение после обновления.
    lane_subnets = list(dict.fromkeys([
        *(n for key in ('wg', 'ovpn', 'ikev2') for n in pools[key]['subnets']),
        *os.environ.get('LANE_SUBNETS', '').split(),
    ]))
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
        {
            # Выход бота (inbound tg-proxy). Отдельно от foreign-best: Bot API
            # (api.telegram.org) режется не на всех экзитах — вотчдог держит
            # tg-best на экзите, с которого Bot API отвечает.
            'type': 'selector',
            'tag': 'tg-best',
            'outbounds': all_hy2,
            'default': all_hy2[0],
            'interrupt_exist_connections': False,
        },
    ] + lane_outbounds + [
        # pin-<выход> — для ручных маршрутов панели (anysda-apply-routes
        # подменяет hy2-X на pin-hy2-X). Пока узел выключен или выход мёртв,
        # вотчдог переводит pin на foreign-best: маршрут не рвётся.
        {
            'type': 'selector',
            'tag': f'pin-{t}',
            'outbounds': [t, 'foreign-best'],
            'default': t,
            'interrupt_exist_connections': False,
        } for t in all_hy2
    ]

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
                'action': 'reject',
            })
            yt_route_rules.append({
                'rule_set': ['geosite-youtube'], 'network': 'udp', 'port': [443],
                'action': 'reject',
            })
        yt_route_rules.append({'domain_suffix': yt_domains, 'outbound': yt_tag})
        yt_route_rules.append({'rule_set': ['geosite-youtube'], 'outbound': yt_tag})
        # DNS делится по российским зонам независимо от маршрута YouTube.

    # Telegram может маскировать TLS чужим SNI. Не только .ru: имя YouTube
    # тоже не должно отправлять его в РФ (или в блок UDP/443). GeoIP/geosite
    # также не должны перебивать известные сети Telegram. Исключаем их из
    # всех автоматических РФ-правил, сохраняя дальнейший выбор выхода:
    # предпочтение клиента -> дорожка -> foreign-best. Ручные маршруты
    # стадии 20 по-прежнему выше этих правил; tg-best обслуживает только бота.
    # Действие (outbound или reject) остаётся на логическом правиле, внутри
    # только условия.
    action_keys = ('action', 'outbound')
    domestic_route_rules = [{
        'type': 'logical',
        'mode': 'and',
        'rules': [
            {key: value for key, value in rule.items() if key not in action_keys},
            {'ip_cidr': TELEGRAM_CIDRS, 'invert': True},
        ],
        **{key: value for key, value in rule.items() if key in action_keys},
    } for rule in yt_route_rules + [
        # SNI/Host передают IDN как A-label (punycode).
        {'domain_suffix': ['.ru', '.рф', '.xn--p1ai', '.su'], 'outbound': 'direct-ru'},
        {'rule_set': ['geosite-category-gov-ru'], 'outbound': 'direct-ru'},
        {'rule_set': ['geoip-ru'], 'outbound': 'direct-ru'},
        {'ip_is_private': True, 'outbound': 'direct-ru'},
    ]]

    cfg = {
        'log': {'level': 'error', 'timestamp': True},

        'dns': {
            'servers': [
                {'type': 'udp', 'tag': 'ru-dns', 'server': RU_DNS, 'detour': 'direct-ru'},
                {'type': 'udp', 'tag': 'bootstrap-dns', 'server': '8.8.8.8', 'detour': 'direct-ru'},
                # Этот DNS нужен только самому sing-box: имена из tg-proxy
                # (бот). Клиенты туннелей спрашивают AdGuard напрямую, и их
                # ответы эти правила не трогают; для бота .ru идёт через
                # Яндекс, остальное через AdGuard.
                {'type': 'udp', 'tag': 'adguard', 'server': mgmt_ip, 'detour': 'local-dns'},
            ],
            'rules': [
                {'domain_suffix': ['.ru', '.рф', '.xn--p1ai', '.su'], 'server': 'ru-dns'},
            ],
            'final': 'adguard',
            'strategy': 'ipv4_only',
        },

        'inbounds': [
            # NAT OUTPUT стадии 22 заводит сюда только DoH-апстримы.
            {'type': 'redirect', 'tag': 'agh-doh-v4',
             'listen': '127.0.0.1', 'listen_port': 7899},
            {'type': 'redirect', 'tag': 'agh-doh-v6',
             'listen': '::1', 'listen_port': 7899},
            {
                # Локальный прокси (HTTP CONNECT + SOCKS) для Telegram-бота:
                # из Москвы api.telegram.org напрямую не открывается, поэтому
                # бот ходит в Telegram через экзит. Весь трафик этого входа
                # уходит в tg-best — экзит, на котором вотчдог проверил Bot API.
                # Слушает только loopback.
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
                # Сниффинг - действием sniff первым правилом route ниже.
            },
        ] + mon_inbounds,

        'outbounds': outbounds,

        'route': {
            # Наборы sing-geoip/sing-geosite, их качает стадия 20. Локальные
            # файлы sing-box перечитывает сам, когда стадия их обновит.
            'rule_set': [
                {'type': 'local', 'tag': t, 'format': 'binary',
                 'path': f'{RULE_SET_DIR}/{t}.srs'}
                for t in RULE_SETS
            ],
            # Маршрутизация:
            #   1. YouTube (если YT_ROUTE != off) → youtube-ru, РФ-выход + десинк
            #   2. .ru/.рф/.su по домену → direct-ru (даже если сайт хостится за рубежом)
            #   3. category-gov-ru → direct-ru (госуслуги и т.п.)
            #   4. geoip-ru / частные сети → direct-ru (физически в РФ)
            #   5. клиентские VPN-IP → lane-NN (selector; экзит дорожки выбирает watchdog)
            #   6. всё остальное → foreign-best (selector; экзит выбирает failover-watchdog)
            # Telegram пропускает пункты 1–4 и доходит до выбора экзита клиента.
            # warp-best / hy2-*-warp используются ТОЛЬКО через ручные правила (UI).
            'rules': [
                # Только sniff, БЕЗ подмены назначения (override_destination).
                # Правила по домену матчат сниффнутый домен (SNI/Host/QUIC) и
                # без подмены, а подмена стирала исходный IP: ip_cidr из панели
                # и geoip ru/private не матчились ни на одном HTTP/HTTPS/QUIC
                # (VPN2-61). Назначение остаётся тем IP, куда шёл клиент, и
                # tproxy отвечает на UDP с него же. Сниффинг обязан стоять до
                # любого правила по домену, в том числе ручных маршрутов.
                {'inbound': ['wg-tproxy-in'], 'action': 'sniff'},
            ] + mon_route_rules + [
                # mon-{tag} матчатся по inbound-тегу ВЫШЕ блока 127.0.0.0/8:
                # цель служебного запроса — 127.0.0.1:9100 (loopback экзита),
                # иначе его срезало бы правило ниже.
                {'ip_cidr': ['127.0.0.0/8', '0.0.0.0/8'], 'action': 'reject'},
                {
                    'port': [853],
                    'ip_cidr': ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8'],
                    'action': 'reject',
                },
                # Выход бота — только ПОСЛЕ блокировок выше: прокси бота слушает
                # loopback роутера, и любой локальный процесс через него дошёл
                # бы до loopback экзита (sshd, node_exporter, clash-api).
                {'inbound': ['tg-proxy'], 'outbound': 'tg-best'},
                # Правила со своим inbound остаются в служебной голове при
                # наложении ручных маршрутов (включая 0.0.0.0/0 из панели).
                {'inbound': ['agh-doh-v4', 'agh-doh-v6'], 'port': [443],
                 'ip_cidr': ['1.1.1.1/32', '8.8.8.8/32', '8.8.4.4/32',
                             '2001:4860:4860::8888/128', '2001:4860:4860::8844/128'],
                 'outbound': 'foreign-best'},
                {'inbound': ['agh-doh-v4', 'agh-doh-v6'], 'action': 'reject'},
                # TPROXY сохраняет для клиента исходный адрес DNS, поэтому
                # ранее выданные конфиги тоже работают.
                {'inbound': ['wg-tproxy-in'], 'protocol': 'dns',
                 'action': 'route', 'outbound': 'dns-unfiltered',
                 'override_address': '127.0.0.2', 'override_port': 53},
            ] + domestic_route_rules + lane_route_rules,
            'final': 'foreign-best',
            'auto_detect_interface': True,
            # Имена в адресах экзитов (DOMAIN_{TAG} может быть доменом)
            # резолвит bootstrap-dns, а не AdGuard: DoH-апстримы AdGuard
            # сами идут через экзиты. При двух DNS-серверах sing-box 1.14
            # без явного резолвера не стартует.
            'default_domain_resolver': {'server': 'bootstrap-dns'},
        },

        'experimental': {
            # Выбор foreign-best, сделанный сторожем через clash API, иначе
            # терялся при каждом перезапуске sing-box: selector вставал на
            # первый тег, даже если тот мёртв. Выбор селекторов хранится в
            # cache_file сам, отдельного store_selected нет.
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
    if sys.argv[1:] == ['--rule-sets']:
        print(' '.join(RULE_SETS))
    else:
        main()
