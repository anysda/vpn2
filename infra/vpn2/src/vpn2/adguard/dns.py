"""Правки конфигов AdGuard Home: учётная запись, политика DNS, экземпляр без фильтров.

Функции меняют разобранный YAML на месте и ничего не пишут на диск: что из
результата публиковать и когда перезапускать службу, решает vpn2.adguard.stage.
"""

import copy
import ipaddress
import json

# Значения по умолчанию для полей, которые админ меняет в панели.
# aaaa_disabled: на entry нет IPv6-форвардинга, AAAA уводит клиента мимо
# manual-routes. ratelimit 0: до v0.107.80 включительно whitelist не доходит до
# dnsproxy, лимит душил бы туннельных клиентов.
DEFAULTS = {
    'upstream_dns': ['[/ru/рф/xn--p1ai/su/]77.88.8.8', 'https://1.1.1.1/dns-query',
                     'https://dns.google/dns-query'],
    'bootstrap_dns': ['8.8.8.8', '8.8.4.4'],
    'upstream_mode': 'parallel',
    'cache_optimistic': True,
    'ratelimit': 0,
    'aaaa_disabled': True,
}
# Хаб без файла состояния ставили прежние выпуски. Своими считаем только
# значения, которые они писали сами (или AdGuard, пока поле не задавалось).
LEGACY = {'bootstrap_dns': [['77.88.8.8', '8.8.8.8']], 'cache_optimistic': [False], 'ratelimit': [20]}
LEGACY_UPSTREAMS = {'77.88.8.8', 'https://1.1.1.1/dns-query', 'https://dns.google/dns-query',
                    '[/ru/рф/xn--p1ai/su/]77.88.8.8'}
# Что экземпляр без фильтров берёт у основного: только транспорт DNS и кеш.
PLAIN_DNS_KEYS = ('upstream_dns', 'upstream_dns_file', 'bootstrap_dns', 'fallback_dns', 'upstream_mode',
                  'cache_size', 'cache_ttl_min', 'cache_ttl_max', 'cache_optimistic', 'aaaa_disabled',
                  'edns_client_subnet', 'use_http3_upstreams')

FRESH = '''http:
  address: 127.0.0.1:{port}

users:
  - name: {user}
    password: {password}

auth_attempts: 5
block_auth_min: 15

dns:
  bind_hosts:
    - 127.0.0.1
    - {bind}
  port: 53
  upstream_dns:
    - '[/ru/рф/xn--p1ai/su/]77.88.8.8'
    - https://1.1.1.1/dns-query
    - https://dns.google/dns-query
  upstream_dns_file: ''
  bootstrap_dns:
    - 8.8.8.8
    - 8.8.4.4
  fallback_dns: []
  upstream_mode: parallel
  cache_optimistic: true
  use_http3_upstreams: false
  cache_size: 4194304
  cache_ttl_min: 0
  cache_ttl_max: 0
  enable_dnssec: false
  blocked_response_ttl: 10
  filtering_enabled: true
  filters_update_interval: 24
  # На entry нет IPv6-форвардинга: с AAAA клиент идёт по v6 мимо manual-routes.
  # Режем AAAA на стороне DNS, клиент получает только A.
  aaaa_disabled: true
  # ECS не добавляем: апстримам незачем знать подсеть клиента. ECS, который
  # клиент прислал сам, AdGuard всё равно пробрасывает как есть.
  edns_client_subnet:
    custom_ip: ""
    enabled: false
    use_custom: false

filters:
  - enabled: true
    url: https://adguardteam.github.io/HostlistsRegistry/assets/filter_1.txt
    name: AdGuard DNS filter
    id: 1
  - enabled: {adaway}
    url: https://adguardteam.github.io/HostlistsRegistry/assets/filter_2.txt
    name: AdAway Default Blocklist
    id: 2

querylog:
  size_memory: {querylog}

log:
  file: ''
  max_backups: 0
  max_size: 100
  max_age: 3
  compress: false
  local_time: false
  verbose: false

schema_version: 28
'''


def fresh_text(user, password_hash, port, bind, adaway, querylog):
    """Конфиг новой установки. JSON-строка - YAML-скаляр: тип и кавычки сохраняются."""
    return FRESH.format(port=port, bind=bind, adaway=adaway, querylog=querylog,
                        user=json.dumps(user), password=json.dumps(password_hash))


def user_password(cfg, user):
    return next((item.get('password') or '' for item in (cfg or {}).get('users') or []
                 if item.get('name') == user), '')


def sync_credentials(cfg, user, password_hash):
    """Учётная запись панели и выключенный ECS; aaaa_disabled ведёт patch_policy."""
    before = copy.deepcopy(cfg)
    cfg['users'] = [{'name': user, 'password': password_hash}]
    cfg.setdefault('dns', {}).setdefault('edns_client_subnet', {})['enabled'] = False
    return cfg != before


def _youtube_upstream(upstream):
    return (upstream.startswith('[/') and '/]' in upstream
            and 'youtube.com' in upstream[2:upstream.index('/]')].split('/'))


def _ours(dns, key, state):
    if key not in dns:
        return True
    value = dns[key]
    if key in state:
        return value == state[key]
    if value == DEFAULTS[key] or value in LEGACY.get(key, []):
        return True
    return (key == 'upstream_dns' and isinstance(value, list)
            and all(isinstance(item, str) and (item in LEGACY_UPSTREAMS or _youtube_upstream(item))
                    for item in value))


def patch_policy(cfg, state, pools, bind, port):
    """Политика DNS поверх живого конфига: (что записано стадией, поля из панели, новых адресов).

    Поле из DEFAULTS обновляется, только если в нём всё ещё значение деплоя;
    иначе его поменяли в панели, и оно остаётся. Список ratelimit_whitelist
    хранится ради совместимости: лимит на закрытом слушателе выключен целиком.
    """
    # Веб-интерфейс и API - только для панели на этом же хосте.
    cfg.setdefault('http', {})['address'] = f'127.0.0.1:{port}'
    dns = cfg.setdefault('dns', {})
    # Панели этих полей нет: глобальный fallback и файл апстримов обходили
    # разделение зон; OUTPUT перехватывает TCP, HTTP/3 обошёл бы его.
    dns['upstream_dns_file'] = ''
    dns['fallback_dns'] = []
    dns['use_http3_upstreams'] = False
    dns['bind_hosts'] = ['127.0.0.1', bind]
    written, kept = {}, []
    for key, value in DEFAULTS.items():
        if _ours(dns, key, state):
            dns[key] = copy.deepcopy(value)
            written[key] = value
        else:
            kept.append(key)
            written[key] = state.get(key, value)
    have = [str(address) for address in dns.get('ratelimit_whitelist') or []]
    need = dict.fromkeys(str(host) for pool in pools
                         for host in ipaddress.ip_network(pool, strict=False).hosts())
    known = set(have)
    missing = [address for address in need if address not in known]
    if missing:
        dns['ratelimit_whitelist'] = have + missing
    return written, kept, len(missing)


def plain_config(filtered, before, port):
    """Конфиг экземпляра без фильтров на 127.0.0.2 поверх того, что дописал сам AdGuard."""
    want = {
        'http': {'address': f'127.0.0.2:{port}'},
        'users': filtered['users'],
        'dns': {key: filtered['dns'][key] for key in PLAIN_DNS_KEYS if key in filtered['dns']},
        'filtering': {'protection_enabled': False, 'filtering_enabled': False,
                      'parental_enabled': False, 'safebrowsing_enabled': False,
                      'safe_search': {'enabled': False}},
        'filters': [], 'whitelist_filters': [], 'user_rules': [],
        'clients': {'persistent': []},
        'schema_version': 29,
    }
    # Журнал в памяти - по тому же ресурсному профилю entry, что и основной.
    if 'size_memory' in (filtered.get('querylog') or {}):
        want['querylog'] = {'size_memory': filtered['querylog']['size_memory']}
    want['dns'].update(bind_hosts=['127.0.0.2'], port=53, ratelimit=0)
    # Не откатываем миграцию схемы, которую выполнил сам AdGuard.
    want['schema_version'] = max(want['schema_version'], before.get('schema_version', 0))
    after = copy.deepcopy(before)
    _merge(after, want)
    return after


def _merge(have, want):
    # Сравниваются только управляемые поля: остальное AdGuard дописывает сам,
    # и повторный деплой иначе всегда давал бы перезапуск.
    for key, value in want.items():
        if isinstance(value, dict):
            _merge(have.setdefault(key, {}), value)
        else:
            have[key] = value
