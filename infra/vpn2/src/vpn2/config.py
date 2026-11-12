"""Разбор config.yaml: PyYAML и типизированный доступ к разделам.

Старый разбор (infra/lib/config2env.py) читает конфиг построчно и всё отдаёт
строками. Здесь формат тот же: схема ключей, булевы значения, целые в
entry_resources и failover проверяются так же, текстовые значения остаются
строками в той же форме. Значения с секретами в тексты ошибок не попадают.
"""
import os
import re
from pathlib import Path
from typing import NamedTuple

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
KEY_NAMES = FREE_TOP.union(*SCHEMA.values(), (name for name in SCHEMA if '.' not in name))
KEY_LINE = re.compile(r'^( *(?:- +)?)([a-z0-9_]+):(.*)$')
# Простое значение, которое старый построчный разбор брал как есть, а YAML читает как разметку.
MARKUP = re.compile(r'[@%`,\]}:]|[-?]( |$)|(=|<<)$|[^&*!\[{].*(:( |$)|\t)')
QUOTED = re.compile(r"""('(?:[^']|'')*'|"(?:[^"\\]|\\.)*")(.*)$""")
FOREIGN = (yaml.AnchorToken, yaml.AliasToken, yaml.TagToken, yaml.DirectiveToken,
           yaml.DocumentStartToken, yaml.DocumentEndToken)


class ConfigError(Exception):
    """Ошибка config.yaml: key - путь ключа, reason - причина."""

    def __init__(self, key, reason):
        super().__init__(f'config.yaml: {key}: {reason}')
        self.key = key
        self.reason = reason


class Node(NamedTuple):
    tag: str
    host: str
    password: str


class Config(NamedTuple):
    """Подключение к узлам и разделы файла как есть: все значения строками."""
    entry: Node
    exits: tuple
    raw: dict


def _plain_forms(text):
    """Строки `ключ: значение` в форме, которую YAML читает так же, как старый построчный разбор.

    Пробелы и табуляция вокруг значения и комментарий после него снимаются; простое значение из
    знаков разметки (`@p`, `%p`, `a: b`, `- p`) берётся в одинарные кавычки. Число строк прежнее.
    """
    lines = text.split('\n')
    for index, line in enumerate(lines):
        found = KEY_LINE.match(line.rstrip('\r'))
        if not found or found.group(2) not in KEY_NAMES:
            continue
        head, name, value = found.group(1), found.group(2), found.group(3).strip()
        if not value or value[0] in '"\'#|>':
            closed = QUOTED.match(value)
            if closed and closed.group(2).lstrip()[:1] in ('', '#'):
                value = closed.group(1)
            lines[index] = f'{head}{name}: {value}'.rstrip()
            continue
        value = re.sub(r'\s+#.*$', '', value)
        if MARKUP.match(value):
            value = "'" + value.replace("'", "''") + "'"
        lines[index] = f'{head}{name}: {value}'
    return '\n'.join(lines)


def _reject_foreign(text):
    """Якоря, теги, директивы, границы документа и `-` отдельной строкой: старый разбор их не брал."""
    if text.startswith('\ufeff'):
        raise ConfigError('yaml', 'файл начинается с BOM; сохраните его в UTF-8 без BOM')
    tokens = list(yaml.scan(text, Loader=yaml.SafeLoader))
    for token, after in zip(tokens, tokens[1:]):
        if isinstance(token, FOREIGN):
            raise ConfigError('yaml', f'сложный YAML не поддерживается, строка {token.start_mark.line + 1}')
        if isinstance(token, yaml.BlockEntryToken) and after.start_mark.line != token.start_mark.line:
            raise ConfigError('yaml', f'элемент списка начинается в строке с `-`, строка {token.start_mark.line + 1}')


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
        if not isinstance(knode, yaml.ScalarNode) or knode.style is not None:
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
        name = knode.value if isinstance(knode, yaml.ScalarNode) and knode.style is None else '?'
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


def loads(text):
    """Config из текста config.yaml; неверный файл - ConfigError.

    Config.raw - разделы как в файле, все значения строками (секреты и числа с ведущим нулём целы).
    """
    text = _plain_forms(text)
    try:
        _reject_foreign(text)
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as error:
        mark = getattr(error, 'problem_mark', None)
        where = f', строка {mark.line + 1}' if mark else ''
        raise ConfigError('yaml', f'{getattr(error, "problem", None) or "ошибка разбора"}{where}') from None
    if root is None:
        raise ConfigError('<корень>', 'пустой файл или не словарь ключей')
    sections = _sections(root)
    entry = sections.get('entry', {})
    exits = tuple(Node(item.get('tag', ''), item.get('host', ''), item.get('password', ''))
                  for item in sections.get('exits', []))
    return Config(Node('ru', entry.get('host', ''), entry.get('password', '')), exits, sections)


def load(path='/etc/anysda/config.yaml'):
    """Config из файла."""
    with open(path, encoding='utf-8') as handle:
        return loads(handle.read())

