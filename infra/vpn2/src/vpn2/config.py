"""Разбор config.yaml: PyYAML и типизированный доступ к разделам.

Старый разбор (infra/lib/config2env.py) читает конфиг построчно и всё отдаёт
строками. Здесь формат тот же: схема ключей, булевы значения, целые в
entry_resources и failover проверяются так же, текстовые значения остаются
строками в той же форме. Значения с секретами в тексты ошибок не попадают.
"""
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

# Состояние деплоя на entry; тот же путь, что в infra/lib/state-dir.sh.
STATE_DIR = Path(os.environ.get('VPN2_STATE_DIR') or '/var/lib/anysda-vpn2/state')

BOOL_WORDS = {'true', 'false', 'yes', 'no', 'y', 'n', '1', '0'}
TRUE_WORDS = {'true', 'yes', 'y', '1'}
STRICT_STR_WORDS = {'true', 'false', 'yes', 'no'}

SCHEMA = {
    'entry': {'host', 'password', 'public_host'},
    'exits': {'tag', 'host', 'password', 'hy2_direct_port', 'hy2_up_mbps', 'hy2_down_mbps',
              'hy2_insecure'},
    'ports': {'hy2_direct', 'hy2_warp', 'hy2_mgmt', 'wireguard', 'openvpn', 'hy2_hop'},
    'ports.hy2_hop': {'enabled', 'direct', 'warp', 'mgmt', 'interval'},
    'wireguard': {'mtu', 'split_local'},
    'openvpn': {'proto'},
    'failover': {'restart_after_checks', 'restart_min_interval'},
    'alerts': {'cpu_pct', 'ram_pct', 'window'},
    'admin': {'user', 'password', 'ssh_pubkey'},
    'panel': {'domain', 'image'},
    'tgbot': {'image'},
    'telegram': {'bot_token', 'chat_id', 'admin_username'},
    'youtube': {'route', 'quic', 'mark', 'offload', 'strategy', 'zapret_ref', 'domains',
                'window_scale'},
    'sso': {'enabled', 'base_url', 'app_slug', 'client_id', 'client_secret', 'allowed_subs',
            'auto_redirect', 'password_login', 'label', 'discovery_url'},
    'backup': {'enabled', 'backend', 'passphrase', 'retention', 'schedule', 'local', 's3'},
    'backup.local': {'dir'},
    'backup.s3': {'endpoint', 'bucket', 'region', 'access_key', 'secret_key'},
    'entry_resources': {'panel_memory_mib', 'panel_old_space_mib', 'bot_memory_mib',
                        'vm_memory_mib', 'vm_cache_mib', 'vm_query_mib',
                        'vm_concurrent_requests', 'agh_memory_mib', 'agh_high_mib',
                        'agh_go_mib', 'retention_days', 'adaway_enabled', 'querylog_size'},
}
# Вложенные блоки: родитель.ключ -> путь в SCHEMA.
SUBBLOCKS = {'ports.hy2_hop', 'backup.local', 'backup.s3'}
BOOLEANS = {'backup.enabled', 'sso.enabled', 'sso.auto_redirect', 'sso.password_login',
            'entry_resources.adaway_enabled', 'wireguard.split_local', 'exits[].hy2_insecure',
            'ports.hy2_hop.enabled'}
FREE_TOP = {'orchestrator_key', 'orchestrator_pubkey'}


class ConfigError(Exception):
    """Ошибка config.yaml: key - путь ключа, reason - причина."""

    def __init__(self, key, reason):
        super().__init__(f'config.yaml: {key}: {reason}')
        self.key = key
        self.reason = reason


@dataclass(frozen=True)
class Entry:
    host: str = ''
    password: str = ''
    public_host: str = ''


@dataclass(frozen=True)
class Exit:
    tag: str = ''
    host: str = ''
    password: str = ''
    hy2_direct_port: str = ''
    hy2_up_mbps: str = ''
    hy2_down_mbps: str = ''
    hy2_insecure: bool = False


@dataclass(frozen=True)
class Hy2Hop:
    enabled: bool = True
    direct: str = '20000-24999'
    warp: str = '25000-29999'
    mgmt: str = '30000-30999'
    interval: str = '5s'


@dataclass(frozen=True)
class Ports:
    hy2_direct: str = ''
    hy2_warp: str = ''
    hy2_mgmt: str = ''
    wireguard: str = ''
    openvpn: str = ''
    hy2_hop: Hy2Hop | None = None


@dataclass(frozen=True)
class Telegram:
    bot_token: str = ''
    chat_id: str = ''
    admin_username: str = ''


@dataclass(frozen=True)
class Sso:
    enabled: bool = False
    base_url: str = ''
    app_slug: str = ''
    client_id: str = ''
    client_secret: str = ''
    allowed_subs: str = ''
    auto_redirect: bool = True
    password_login: bool = True
    label: str = ''
    discovery_url: str = ''


@dataclass(frozen=True)
class Config:
    entry: Entry
    exits: tuple[Exit, ...]
    ports: Ports
    telegram: Telegram
    sso: Sso
    raw: dict[str, Any] = field(default_factory=dict)


def _scalar(node, key):
    """Строка из скалярного узла; сложный YAML и неверные значения - ConfigError."""
    if not isinstance(node, yaml.ScalarNode) or node.style in ('|', '>'):
        raise ConfigError(key, 'ожидается скаляр; сложный YAML не поддерживается')
    value, plain = node.value, node.style is None
    shape = re.sub(r'^exits\[\d+\]', 'exits[]', key)
    boolean = shape in BOOLEANS
    if plain and value.lower() in ('null', '~'):
        raise ConfigError(key, 'ожидается скаляр; сложный YAML не поддерживается')
    if plain and not boolean and value.lower() in STRICT_STR_WORDS:
        raise ConfigError(key, 'ожидается строка; заключите значение в кавычки')
    if '\0' in value:
        raise ConfigError(key, 'нулевой байт недопустим в env')
    if boolean and value.lower() not in BOOL_WORDS:
        raise ConfigError(key, 'ожидается true или false')
    if key == 'entry_resources.adaway_enabled' and value.lower() not in ('true', 'false'):
        raise ConfigError(key, 'ожидается true или false')
    if key.startswith('entry_resources.') and key != 'entry_resources.adaway_enabled' and (
            not value.isdigit() or (key.endswith('.retention_days') and int(value) == 0)):
        raise ConfigError(key, 'ожидается целое число' + (' > 0' if key.endswith('retention_days') else ' >= 0'))
    if key.startswith('failover.') and not re.fullmatch(r'[0-9]+', value):
        raise ConfigError(key, 'ожидается целое число >= 0' + (', секунды' if key.endswith('interval') else ''))
    return value


def _is_empty(node):
    return isinstance(node, yaml.ScalarNode) and node.style is None and not node.value


def _mapping(node, path, schema_key, column):
    """Словарь раздела по схеме; вложенные блоки (backup.local, ...) - вложенными словарями."""
    if _is_empty(node):
        return {}
    if not isinstance(node, yaml.MappingNode) or node.flow_style:
        raise ConfigError(path, 'ожидается блок с отступами, как в config.example.yaml')
    out = {}
    for knode, vnode in node.value:
        if not isinstance(knode, yaml.ScalarNode):
            raise ConfigError(path, 'неверный формат ключа')
        name = knode.value
        key = f'{path}.{name}' if path else name
        if knode.start_mark.column != column:
            raise ConfigError(key, 'неверный отступ или структура')
        child = f'{schema_key}.{name}' if schema_key else name
        if name not in SCHEMA.get(schema_key, set()):
            raise ConfigError(key, 'неизвестный ключ')
        if name in out:
            raise ConfigError(key, 'повтор ключа')
        sub = child in SUBBLOCKS
        out[name] = _mapping(vnode, key, child, column + 2) if sub else _scalar(vnode, key)
    return out


def _exits(node):
    if _is_empty(node):
        return []
    if not isinstance(node, yaml.SequenceNode) or node.flow_style:
        raise ConfigError('exits', 'ожидается список экзитов, как в config.example.yaml')
    out = []
    for index, item in enumerate(node.value):
        if not isinstance(item, yaml.MappingNode) or item.start_mark.column not in (2, 4):
            raise ConfigError(f'exits[{index}]', 'неверный отступ или структура')
        out.append(_mapping(item, f'exits[{index}]', 'exits', item.start_mark.column))
    return out


def _sections(root):
    if not isinstance(root, yaml.MappingNode) or root.flow_style:
        raise ConfigError('<корень>', 'ожидается словарь ключей')
    out = {}
    for knode, vnode in root.value:
        name = knode.value if isinstance(knode, yaml.ScalarNode) else '?'
        if name in out:
            raise ConfigError(name, 'повтор ключа')
        if knode.start_mark.column != 0:
            raise ConfigError(name, 'неверный отступ или структура')
        if name in FREE_TOP:
            out[name] = _scalar(vnode, name)
        elif name in SCHEMA and '.' not in name:
            out[name] = _exits(vnode) if name == 'exits' else _mapping(vnode, name, name, 2)
        else:
            raise ConfigError(name, 'неизвестный ключ')
    return out


def _flag(value):
    return str(value).lower() in TRUE_WORDS


def _typed(cls, data):
    """Датакласс из словаря раздела; булевы поля приводятся к bool."""
    args = {}
    for item in cls.__dataclass_fields__.values():
        if item.name in data and not isinstance(data[item.name], dict):
            args[item.name] = _flag(data[item.name]) if item.type is bool else data[item.name]
    return cls(**args)


def _build(sections, raw):
    ports = dict(sections.get('ports', {}))
    hop = ports.pop('hy2_hop', None)
    return Config(
        entry=_typed(Entry, sections.get('entry', {})),
        exits=tuple(_typed(Exit, item) for item in sections.get('exits', [])),
        ports=replace(_typed(Ports, ports), hy2_hop=None if hop is None else _typed(Hy2Hop, hop)),
        telegram=_typed(Telegram, sections.get('telegram', {})),
        sso=_typed(Sso, sections.get('sso', {})),
        raw=raw,
    )


def loads(text):
    """Config из текста config.yaml; неверный файл - ConfigError."""
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
        raw = yaml.safe_load(text)
    except yaml.YAMLError as error:
        mark = getattr(error, 'problem_mark', None)
        where = f', строка {mark.line + 1}' if mark else ''
        raise ConfigError('yaml', f'{getattr(error, "problem", None) or "ошибка разбора"}{where}') from None
    if root is None or not isinstance(raw, dict):
        raise ConfigError('<корень>', 'пустой файл или не словарь ключей')
    return _build(_sections(root), raw)


def load(path='/etc/anysda/config.yaml'):
    """Config из файла."""
    with open(path, encoding='utf-8') as handle:
        return loads(handle.read())


def versions(path: str | Path) -> dict[str, str]:
    """Пины внешних версий из infra/versions.yml; неверный файл - ConfigError."""
    try:
        with open(path, encoding='utf-8') as handle:
            raw = yaml.safe_load(handle.read())
    except yaml.YAMLError as error:
        raise ConfigError('yaml', getattr(error, 'problem', None) or 'ошибка разбора') from None
    if not isinstance(raw, dict):
        raise ConfigError('<корень>', 'пустой файл или не словарь ключей')
    for key, value in raw.items():
        if not isinstance(value, str):
            raise ConfigError(str(key), 'значение не строка')
    return raw
