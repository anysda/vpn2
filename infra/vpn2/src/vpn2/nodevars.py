"""Переменные узлов из config.yaml: то же, что config2env.py пишет в infra/envs/*.env.

env_by_node отдаёт на каждый узел окружение, которое bash получает после
`source all.env; source <узел>.env`: те же имена и те же строки. Состояние
деплоя (mgmt-ips.txt, excluded-exits.txt) только читается; закрепляет октеты
по-прежнему config2env.py, пока он жив.
"""
import ipaddress
import json
import re
from pathlib import Path

from vpn2.config import STATE_DIR, TRUE_WORDS, Config, ConfigError
from vpn2.watchdog.hy2_bandwidth import HY2_HOP_DEFAULTS, bandwidth_fields

# Порты перебора hy2 после стадии 20: HY2_PORT_CANDIDATES в infra/deploy.sh.
HY2_PORT_CANDIDATES = (443, 4443, 4444, 8443, 8444, 33445)
RESERVED_TAGS = ('ru', 'all', 'exits', 'foreign')
OVPN_PROTOS = ('udp', 'udp4', 'udp6', 'tcp', 'tcp4', 'tcp6', 'tcp-server', 'tcp4-server', 'tcp6-server')
FALSE_WORDS = ('false', 'no', 'n', '0')
LABEL = r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
# Значения этих имён в выводе ansible-inventory закрыты (см. infra/ansible/inventory.py).
SECRET_NAMES = frozenset({'SSH_PASS', 'ADMIN_PASSWORD', 'TELEGRAM_BOT_TOKEN', 'SSO_CLIENT_SECRET',
                          'BACKUP_PASSPHRASE', 'BACKUP_S3_ACCESS_KEY', 'BACKUP_S3_SECRET_KEY'})

# Имена, которые деплой выводит из config.yaml (кроме имён с тегом экзита).
CONFIG_NAMES = frozenset("""
ENTRY_HOST AGH_PORT HY2_DIRECT_PORT HY2_WARP_PORT HY2_MGMT_PORT HY2_HOP_ENABLED HY2_HOP_DIRECT HY2_HOP_WARP
HY2_HOP_MGMT HY2_HOP_INTERVAL ADMIN_SSH_PUBKEY EXIT_TAGS WG_PUBLIC_HOST OVPN_PUBLIC_HOST MGMT_IP_RU
HOST_TAG SSH_USER SSH_HOST SSH_PASS PUB_IP PUB_IP_OUT MGMT_IP HY2_UP_MBPS HY2_DOWN_MBPS
ADMIN_USER ADMIN_PASSWORD PANEL_DOMAIN ENTRY_RESOURCE_OVERRIDES PANEL_IMAGE TGBOT_IMAGE
WG_LISTEN_PORT OVPN_PORT OVPN_PROTO WG_MTU WG_SPLIT_LOCAL RESTART_AFTER_CHECKS RESTART_MIN_INTERVAL_S
TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID TELEGRAM_ADMIN_USERNAME ALERT_CPU_PCT ALERT_RAM_PCT ALERT_WINDOW_SEC
YT_ROUTE YT_QUIC YT_MARK YT_ZAPRET_OFFLOAD YT_ZAPRET_WINDOW_SCALE YT_ZAPRET_STRATEGY YT_ZAPRET_REF YT_DOMAINS
SSO_REQUESTED SSO_ENABLED SSO_DISCOVERY_URL SSO_CLIENT_ID SSO_CLIENT_SECRET SSO_ALLOWED_SUBS SSO_LABEL
SSO_AUTO_REDIRECT SSO_PASSWORD_LOGIN BACKUP_ENABLED BACKUP_BACKEND BACKUP_PASSPHRASE BACKUP_RETENTION
BACKUP_SCHEDULE BACKUP_LOCAL_DIR BACKUP_S3_ENDPOINT BACKUP_S3_BUCKET BACKUP_S3_REGION BACKUP_S3_ACCESS_KEY
BACKUP_S3_SECRET_KEY
""".split())
# Имена с тегом экзита: DOMAIN_<ТЕГ>, <ТЕГ>_HY2_UP_MBPS.
EXIT_PREFIXES = ('DOMAIN', 'MGMT_IP')
EXIT_SUFFIXES = ('HY2_UP_MBPS', 'HY2_DOWN_MBPS', 'HY2_INSECURE', 'HY2_DIRECT_PORT', 'HY2_MGMT_PORT')
# Настройки запуска мимо config.yaml: их ставит deploy.sh или оператор в окружении, роли читают.
RUN_NAMES = frozenset("""
VPN2_INFRA VPN2_RELEASE_MANIFEST VPN2_DEVELOPMENT VPN2_WHEEL VPN2_SOURCE_ROOT VPN2_PANEL_FOLLOWS VPN2_VERIFY_OUT
EMERGENCY_HY2_PORT EMERGENCY_IMAGE EMERGENCY_IMAGE_PULL TLS_ACME_ROOT TLS_ACME_URL TLS_INITIAL_WAIT
REALITY_PORT REALITY_SNI IKEV2_DNS IKEV2_SUBNET MGMT_PORT OVPN_SUBNET WG_SERVER_IP WG_SUBNET_PREFIX
PANEL_IMAGE_PULL TGBOT_IMAGE_PULL TGBOT_INVALID_TOKEN_PAUSE_SEC TGBOT_POLL_STALL_SEC
YT_ZAPRET_QUEUE YT_ZAPRET_REPO YT_ZAPRET_BASE YT_ZAPRET_WSCALE
SSH_PROBE_TIMEOUT VERIFY_SCRAPE_TIMEOUT VERIFY_SCRAPE_INTERVAL VERIFY_SNAPSHOT_WAIT VERIFY_REPORT VERIFY_TRAFFIC
""".split())


def _flag(value):
    return str(value).lower() in TRUE_WORDS


def env_tag(tag):
    """Тег экзита в именах переменных: дефис bash в именах не допускает."""
    return tag.upper().replace('-', '_')


def exit_names(tags):
    """Имена переменных с тегами этих экзитов."""
    names = [env_tag(tag) for tag in tags]
    return ({f'{prefix}_{name}' for prefix in EXIT_PREFIXES for name in names}
            | {f'{name}_{suffix}' for name in names for suffix in EXIT_SUFFIXES})


def _check_host(value, key):
    try:
        ipaddress.ip_address(value)
        return
    except ValueError:
        pass
    if (not value or len(value) > 253 or re.fullmatch(r'[0-9.]+', value)
            or not all(re.fullmatch(LABEL, label) for label in value.rstrip('.').split('.'))):
        raise ConfigError(key, 'ожидается IP-адрес или DNS-имя')


def _check_port(value, key):
    if not re.fullmatch(r'[0-9]+', value) or not 1 <= int(value) <= 65535:
        raise ConfigError(key, 'порт должен быть целым числом от 1 до 65535')


def _check_int(section, name, low, high, reason):
    value = section.get(name)
    if value is not None and (not re.fullmatch(r'[0-9]+', value) or not low <= int(value) <= high):
        raise ConfigError(reason[0], reason[1])


def normalize_domain(value, key):
    """Каноническое DNS-имя для Caddy и OIDC redirect_uri."""
    try:
        value = value.encode('idna').decode('ascii').lower().rstrip('.')
    except UnicodeError:
        raise ConfigError(key, 'неверное DNS-имя') from None
    labels = value.split('.')
    if (len(value) > 253 or len(labels) < 2 or not re.search(r'[a-z]', labels[-1])
            or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)):
        raise ConfigError(key, 'ожидается DNS-имя без схемы, порта и пробелов')
    return value


def hop_settings(hop):
    """ports.hy2_hop с умолчаниями: (включено, {канал: (от, до)}, интервал)."""
    enabled = _flag(hop.get('enabled', HY2_HOP_DEFAULTS['enabled']))
    interval = hop.get('interval', HY2_HOP_DEFAULTS['interval'])
    if not re.fullmatch(r'[1-9][0-9]*[smh]', interval):
        raise ConfigError('ports.hy2_hop.interval', 'ожидается длительность вида 30s, 5m')
    ranges = {}
    for channel in ('direct', 'warp', 'mgmt'):
        key = f'ports.hy2_hop.{channel}'
        match = re.fullmatch(r'([0-9]+)-([0-9]+)', hop.get(channel, HY2_HOP_DEFAULTS[channel]))
        if not match or not 1 <= int(match[1]) <= int(match[2]) <= 32767:
            raise ConfigError(key, 'ожидается диапазон UDP вида 20000-24999 в пределах 1..32767')
        low, high = int(match[1]), int(match[2])
        for other, (start, end) in ranges.items():
            if low <= end and start <= high:
                raise ConfigError(key, f'пересекается с ports.hy2_hop.{other}')
        taken = [port for port in HY2_PORT_CANDIDATES if low <= port <= high]
        if taken:
            raise ConfigError(key, f'задевает порт перебора hy2 {taken[0]} (HY2_PORT_CANDIDATES)')
        ranges[channel] = (low, high)
    return enabled, ranges, interval


def _sections(cfg):
    """Разделы config.yaml в раскладке старого разбора: hy2_hop отдельно от ports."""
    raw = cfg.raw
    ports = dict(raw.get('ports', {}))
    hop = ports.pop('hy2_hop', None)
    out = {name: raw.get(name, {}) for name in (
        'entry', 'telegram', 'admin', 'panel', 'tgbot', 'youtube', 'sso', 'backup', 'entry_resources',
        'wireguard', 'openvpn', 'failover', 'alerts')}
    out.update(exits=[dict(item) for item in raw.get('exits', [])], ports=ports, hy2_hop=hop)
    return out


def validate(cfg: Config) -> dict:
    """Проверки config2env до записи env; возвращает разделы с каноническим panel.domain."""
    sec = _sections(cfg)
    entry = sec['entry']
    _check_host(entry.get('host', ''), 'entry.host')
    if not entry.get('password'):
        raise ConfigError('entry.password', 'обязательное непустое значение')
    if entry.get('public_host'):
        _check_host(entry['public_host'], 'entry.public_host')
    if not sec['exits']:
        raise ConfigError('exits', 'нужна хотя бы одна выходная нода')
    tags = set()
    for index, item in enumerate(sec['exits']):
        key = f'exits[{index}]'
        tag = item.get('tag', '')
        if not re.fullmatch(r'[a-z](?:[a-z0-9-]*[a-z0-9])?', tag) or tag in RESERVED_TAGS or tag in tags:
            raise ConfigError(key + '.tag', 'нужен уникальный незарезервированный тег: [a-z](?:[a-z0-9-]*[a-z0-9])?')
        tags.add(tag)
        _check_host(item.get('host', ''), key + '.host')
        if not item.get('password'):
            raise ConfigError(key + '.password', 'обязательное непустое значение')
        if 'hy2_direct_port' in item:
            _check_port(item['hy2_direct_port'], key + '.hy2_direct_port')
    for name, value in sec['ports'].items():
        _check_port(value, 'ports.' + name)
    if sec['hy2_hop'] is not None:
        hop_settings(sec['hy2_hop'])
    if 'proto' in sec['openvpn'] and sec['openvpn']['proto'] not in OVPN_PROTOS:
        raise ConfigError('openvpn.proto', 'допустимо ' + ', '.join(OVPN_PROTOS))
    _check_int(sec['wireguard'], 'mtu', 1280, 9000, ('wireguard.mtu', 'ожидается целое число от 1280 до 9000'))
    for name in ('cpu_pct', 'ram_pct'):
        _check_int(sec['alerts'], name, 1, 100, ('alerts.' + name, 'ожидается целое число от 1 до 100'))
    _check_int(sec['alerts'], 'window', 30, float('inf'), ('alerts.window', 'ожидается число секунд не меньше 30'))
    if sec['panel'].get('domain'):
        sec['panel'] = {**sec['panel'], 'domain': normalize_domain(sec['panel']['domain'], 'panel.domain')}
    for item in sec['exits']:
        try:
            bandwidth_fields(item.get('hy2_up_mbps'), item.get('hy2_down_mbps'))
        except ValueError:
            raise ConfigError(f'exits[{item["tag"]}]', 'нужны обе скорости hy2_up_mbps/hy2_down_mbps, целые Мбит/с '
                              'от 1 до 1000000, либо обе пустые (BBR)') from None
    if _flag(sec['backup'].get('enabled', '')) and not sec['backup'].get('passphrase', ''):
        raise ConfigError('backup.passphrase', 'пустой при включённом бэкапе - задай его (или запусти '
                          './setup.sh), либо выключи backup.enabled')
    return sec


def _state_file(state, repo_root, name):
    """Файл состояния для чтения: до первого деплоя нового выпуска он ещё в checkout (infra/state)."""
    current = Path(state) / name
    if not current.exists() and repo_root is not None:
        legacy = Path(repo_root) / 'infra/state' / name
        if legacy.exists():
            return legacy
    return current


def mgmt_octets(tags, state=STATE_DIR, repo_root=None):
    """{тег: последний октет MGMT_IP}: закреплённые в mgmt-ips.txt, новым - наименьший свободный.

    Файла нет - прежняя формула «позиция в списке + 2». Ничего не пишет.
    """
    source = _state_file(state, repo_root, 'mgmt-ips.txt')
    if source.exists():
        pinned = {}
        for line in source.read_text(encoding='utf-8').splitlines():
            parts = line.split()
            if len(parts) == 2 and not parts[0].startswith('#') and parts[1].isdigit():
                pinned[parts[0]] = int(parts[1])
    else:
        pinned = {tag: index for index, tag in enumerate(tags, start=2)}
    for tag in tags:
        if tag not in pinned:
            used = set(pinned.values())
            pinned[tag] = next(octet for octet in range(2, 255) if octet not in used)
    return {tag: pinned[tag] for tag in tags}


def excluded_exits(state=STATE_DIR, repo_root=None):
    """Теги из excluded-exits.txt: экзиты, недостижимые по UDP, деплой не настраивает."""
    source = _state_file(state, repo_root, 'excluded-exits.txt')
    if not source.exists():
        return set()
    lines = (line.strip() for line in source.read_text(encoding='utf-8').splitlines())
    return {line for line in lines if line and not line.startswith('#')}


def _common(sec, exits, octets):
    """Переменные all.env: одинаковые для всех узлов."""
    ports = sec['ports']
    direct = ports.get('hy2_direct', '443')
    env = {'ENTRY_HOST': sec['entry']['host'], 'AGH_PORT': '3000', 'HY2_DIRECT_PORT': direct,
           'HY2_WARP_PORT': ports.get('hy2_warp', '8443'), 'HY2_MGMT_PORT': ports.get('hy2_mgmt', '8444')}
    if sec['hy2_hop'] is not None:
        enabled, ranges, interval = hop_settings(sec['hy2_hop'])
        env['HY2_HOP_ENABLED'] = str(int(enabled))
        for channel, (low, high) in ranges.items():
            env[f'HY2_HOP_{channel.upper()}'] = f'{low}-{high}'
        env['HY2_HOP_INTERVAL'] = interval
    env['ADMIN_SSH_PUBKEY'] = (sec['admin'].get('ssh_pubkey') or '').strip()
    env['EXIT_TAGS'] = ' '.join(item['tag'] for item in exits)
    # Домен вместо IP в клиентских WG/OVPN-конфигах; без public_host - домен панели.
    public_host = (sec['entry'].get('public_host') or '').strip() or sec['panel'].get('domain') or ''
    if public_host:
        env['WG_PUBLIC_HOST'] = env['OVPN_PUBLIC_HOST'] = public_host
    env['MGMT_IP_RU'] = '10.99.0.1'
    for item in exits:
        tag = env_tag(item['tag'])
        env[f'DOMAIN_{tag}'] = item['host']
        env[f'{tag}_HY2_UP_MBPS'] = item.get('hy2_up_mbps', '')
        env[f'{tag}_HY2_DOWN_MBPS'] = item.get('hy2_down_mbps', '')
        if _flag(item.get('hy2_insecure', '')):
            env[f'{tag}_HY2_INSECURE'] = '1'
        env[f'MGMT_IP_{tag}'] = f'10.99.0.{octets[item["tag"]]}'
        env[f'{tag}_HY2_DIRECT_PORT'] = item.get('hy2_direct_port', direct)
    return env


def _youtube(yt):
    route = (yt.get('route', '') or 'off').lower()
    if route not in ('off', 'zapret', 'direct'):
        raise ConfigError('youtube.route', 'допустимо off | zapret | direct')
    quic = (yt.get('quic', '') or 'block').lower()
    if quic not in ('block', 'allow'):
        raise ConfigError('youtube.quic', 'допустимо block | allow')
    env = {'YT_ROUTE': route, 'YT_QUIC': quic, 'YT_MARK': yt.get('mark', '256') or '256',
           'YT_ZAPRET_OFFLOAD': (yt.get('offload', '') or 'keep').lower()}
    if yt.get('window_scale'):
        if not re.fullmatch(r'native|[0-9]|1[0-4]', yt['window_scale']):
            raise ConfigError('youtube.window_scale', 'допустимо native или 0..14')
        env['YT_ZAPRET_WINDOW_SCALE'] = yt['window_scale']
    for key, name in (('strategy', 'YT_ZAPRET_STRATEGY'), ('zapret_ref', 'YT_ZAPRET_REF'), ('domains', 'YT_DOMAINS')):
        if yt.get(key):
            env[name] = yt[key]
    return env


def _sso(sso):
    requested = _flag(sso.get('enabled', '') or '')
    secret = sso.get('client_secret', '') or ''
    enabled = requested and bool(secret.strip())
    env = {'SSO_REQUESTED': 'true' if requested else 'false', 'SSO_ENABLED': 'true' if enabled else 'false'}
    if not requested:
        return env
    discovery = sso.get('discovery_url', '')
    base = (sso.get('base_url', '') or '').rstrip('/')
    if not discovery and base:
        discovery = f'{base}/application/o/{sso.get("app_slug", "") or "vpn2"}/.well-known/openid-configuration'
    redirect_off = not enabled or (sso.get('auto_redirect', '') or '').lower() in FALSE_WORDS
    password_off = enabled and (sso.get('password_login', '') or '').lower() in FALSE_WORDS
    env.update(SSO_DISCOVERY_URL=discovery, SSO_CLIENT_ID=sso.get('client_id', '') or 'vpn2',
               SSO_CLIENT_SECRET=secret, SSO_ALLOWED_SUBS=sso.get('allowed_subs', ''),
               SSO_LABEL=sso.get('label', '') or 'Authentik',
               SSO_AUTO_REDIRECT='false' if redirect_off else 'true',
               SSO_PASSWORD_LOGIN='false' if password_off else 'true')
    return env


def _backup(backup):
    if not _flag(backup.get('enabled', '')):
        return {}
    env = {'BACKUP_ENABLED': 'true', 'BACKUP_BACKEND': backup.get('backend', 'local'),
           'BACKUP_PASSPHRASE': backup.get('passphrase', ''), 'BACKUP_RETENTION': backup.get('retention', '10'),
           'BACKUP_SCHEDULE': backup.get('schedule', 'daily'),
           'BACKUP_LOCAL_DIR': backup.get('local', {}).get('dir', '/var/backups/anysda-vpn2')}
    s3 = backup.get('s3', {})
    if backup.get('backend') == 's3' and s3:
        for key in ('endpoint', 'bucket', 'region', 'access_key', 'secret_key'):
            env['BACKUP_S3_' + key.upper()] = s3.get(key, '')
    return env


def _entry(sec, development):
    """Переменные ru.env."""
    host, ports = sec['entry']['host'], sec['ports']
    env = {'HOST_TAG': 'ru', 'SSH_USER': 'root', 'SSH_HOST': host, 'SSH_PASS': sec['entry']['password'],
           'PUB_IP_OUT': host, 'MGMT_IP': '10.99.0.1',
           'ADMIN_USER': sec['admin'].get('user', 'anysda') or 'anysda',
           'ADMIN_PASSWORD': sec['admin'].get('password', ''), 'PANEL_DOMAIN': sec['panel'].get('domain', ''),
           'ENTRY_RESOURCE_OVERRIDES': json.dumps(sec['entry_resources'])}
    # Образы из конфига - только для --development: проверенный деплой берёт их из манифеста.
    if development:
        for section, name in (('panel', 'PANEL_IMAGE'), ('tgbot', 'TGBOT_IMAGE')):
            if sec[section].get('image', ''):
                env[name] = sec[section]['image']
    # Только явные настройки: умолчания принадлежат ролям.
    for key, name in (('wireguard', 'WG_LISTEN_PORT'), ('openvpn', 'OVPN_PORT')):
        if key in ports:
            env[name] = str(int(ports[key]))
    if 'proto' in sec['openvpn']:
        env['OVPN_PROTO'] = sec['openvpn']['proto']
    if 'mtu' in sec['wireguard']:
        env['WG_MTU'] = str(int(sec['wireguard']['mtu']))
    if 'split_local' in sec['wireguard']:
        env['WG_SPLIT_LOCAL'] = 'true' if _flag(sec['wireguard']['split_local']) else 'false'
    for key, name in (('restart_after_checks', 'RESTART_AFTER_CHECKS'),
                      ('restart_min_interval', 'RESTART_MIN_INTERVAL_S')):
        if key in sec['failover']:
            env[name] = str(int(sec['failover'][key]))
    telegram = sec['telegram']
    if telegram.get('bot_token') and telegram.get('chat_id'):
        env.update(TELEGRAM_BOT_TOKEN=telegram['bot_token'], TELEGRAM_CHAT_ID=telegram['chat_id'])
        if telegram.get('admin_username'):
            env['TELEGRAM_ADMIN_USERNAME'] = telegram['admin_username']
    for key, name in (('cpu_pct', 'ALERT_CPU_PCT'), ('ram_pct', 'ALERT_RAM_PCT'), ('window', 'ALERT_WINDOW_SEC')):
        if key in sec['alerts']:
            env[name] = str(int(sec['alerts'][key]))
    return {**env, **_youtube(sec['youtube']), **_sso(sec['sso']), **_backup(sec['backup'])}


def _exit(item, octet, direct):
    """Переменные <тег>.env."""
    env = {'HOST_TAG': item['tag'], 'SSH_USER': 'root', 'SSH_HOST': item['host'], 'SSH_PASS': item['password'],
           'PUB_IP': item['host'], 'MGMT_IP': f'10.99.0.{octet}',
           'HY2_UP_MBPS': item.get('hy2_up_mbps', ''), 'HY2_DOWN_MBPS': item.get('hy2_down_mbps', '')}
    if item.get('hy2_direct_port', direct) != direct:
        env['HY2_DIRECT_PORT'] = item['hy2_direct_port']
    return env


def env_by_node(cfg: Config, *, state=STATE_DIR, repo_root=None, development=False) -> dict:
    """{узел: {имя: строка}} для entry (ru) и экзитов, которые деплой настраивает."""
    sec = validate(cfg)
    octets = mgmt_octets([item['tag'] for item in sec['exits']], state, repo_root)
    skipped = excluded_exits(state, repo_root)
    exits = [item for item in sec['exits'] if item['tag'] not in skipped]
    if not exits:
        raise ConfigError('exits', 'нужна хотя бы одна выходная нода')
    common = _common(sec, exits, octets)
    nodes = {'ru': {**common, **_entry(sec, development)}}
    for item in exits:
        nodes[item['tag']] = {**common, **_exit(item, octets[item['tag']], common['HY2_DIRECT_PORT'])}
    return nodes


def node_env(cfg: Config, node, **options) -> dict:
    """Переменные одного узла; узла нет в конфиге (или он исключён) - ConfigError."""
    nodes = env_by_node(cfg, **options)
    if node not in nodes:
        raise ConfigError(node, 'такого узла нет в config.yaml: ' + ', '.join(nodes))
    return nodes[node]
