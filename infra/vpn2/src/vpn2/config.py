"""Разбор config.yaml: PyYAML и типизированный доступ к разделам.

Схема ключей, булевы значения, целые в entry_resources и failover проверяются;
текстовые значения остаются строками в той же форме, что в файле. Значения с секретами в тексты ошибок не попадают.
"""
import base64
import os
import re
from pathlib import Path
from typing import NamedTuple

import yaml
# Состояние деплоя на entry; тот же путь, что в корневом deploy.sh.
STATE_DIR = Path(os.environ.get('VPN2_STATE_DIR') or '/var/lib/anysda-vpn2/state')

BOOL_WORDS = {'true', 'false', 'yes', 'no', 'y', 'n', '1', '0'}
TRUE_WORDS = {'true', 'yes', 'y', '1'}
STRICT_STR_WORDS = {'true', 'false', 'yes', 'no'}
# Порты hy2-direct, которые перебирает проверка после 20-ru-router; диапазоны hy2_hop их не задевают.
HY2_PORT_CANDIDATES = (443, 4443, 4444, 8443, 8444, 33445)

# password у entry и экзитов - пароль root из конфигов 1.2.0: файл принимается, деплой его не берёт и
# после успешного деплоя стирает (drop_passwords).
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
    'secrets': {'telegram_bot_token', 's3_access_key', 's3_secret_key', 'sso_client_secret',
                'backup_passphrase', 'admin_password'},
}
# Места секретов в config.yaml 1.2.0: читаются как область secrets и стираются после деплоя.
LEGACY_SECRETS = {('telegram', 'bot_token'): 'telegram_bot_token', ('backup', 's3', 'access_key'): 's3_access_key',
                  ('backup', 's3', 'secret_key'): 's3_secret_key', ('sso', 'client_secret'): 'sso_client_secret',
                  ('backup', 'passphrase'): 'backup_passphrase', ('admin', 'password'): 'admin_password',
                  ('orchestrator_key',): 'orchestrator_key'}
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


class Config(NamedTuple):
    """Подключение к узлам и разделы файла как есть: все значения строками."""
    entry: Node
    exits: tuple
    raw: dict


def _plain_forms(text):
    """Строки `ключ: значение` в форме, которую YAML читает так же, как старый построчный разбор.

    Пробелы и табуляция вокруг значения и комментарий после него снимаются; простое значение из
    знаков разметки (`@p`, `%p`, `a: b`, `- p`) берётся в одинарные кавычки. Число строк прежнее.
    Значение с `[ { | > ! & *` в начале или с текстом после кавычки YAML прочёл бы разметкой:
    отказ с ключом и строкой, как в v1.1.5.
    """
    lines = text.split('\n')
    for index, line in enumerate(lines):
        found = KEY_LINE.match(line.rstrip('\r'))
        if not found or found.group(2) not in KEY_NAMES:
            continue
        head, name, value = found.group(1), found.group(2), found.group(3).strip()
        where = f'строка {index + 1}; заключите значение в кавычки'
        if value[:1] in tuple('[{|>!&*'):
            raise ConfigError(_key_at(text, index, len(line)), f'значение начинается с «{value[0]}», {where}')
        if not value or value[0] in '"\'#':
            closed = QUOTED.match(value)
            if closed and closed.group(2).lstrip()[:1] not in ('', '#'):
                raise ConfigError(_key_at(text, index, len(line)), f'текст после закрывающей кавычки, {where} целиком')
            if closed:
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
    if plain and not boolean and value.lower() in STRICT_STR_WORDS | {'null', '~'}:
        raise ConfigError(key, f'ожидается строка, строка {node.start_mark.line + 1}; заключите значение в кавычки')
    if plain and value.lower() in ('null', '~'):
        raise ConfigError(key, 'ожидается true или false')
    if '\0' in value:
        raise ConfigError(key, 'нулевой байт недопустим в env')
    if boolean and value.lower() not in BOOL_WORDS:
        raise ConfigError(key, 'ожидается true или false')
    if key == 'entry_resources.adaway_enabled' and value.lower() not in ('true', 'false'):
        raise ConfigError(key, 'ожидается true или false')
    if key.startswith('entry_resources.') and key != 'entry_resources.adaway_enabled' and (
            not value.isdigit() or (key.endswith('.retention_days') and int(value) == 0)):
        raise ConfigError(key, 'ожидается целое число' + (' > 0' if key.endswith('retention_days') else ' >= 0'))
    # backup.retention пустой - по умолчанию 10; 0 - остаётся только новый архив.
    if (key.startswith('failover.') or key == 'backup.retention' and value) and not re.fullmatch(r'[0-9]+', value):
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
        found = _mapping(item, f'exits[{index}]', 'exits', item.start_mark.column)
        if 'host' in found:
            found['host'] = _exit_host(found['host'], f'exits[{index}].host')
        out.append(found)
    return out


def _exit_host(value, key):
    """Адрес экзита из `root@адрес` или из голого адреса (вход тоже root)."""
    user, at, host = value.rpartition('@')
    if at and user != 'root':
        raise ConfigError(key, 'вход на экзит только root: root@адрес или адрес')
    return host


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


def _key_at(text, line, column):
    """Ключ через точку, в значении которого ошибка разбора; вне значений ключей - yaml."""
    path = []
    for number, row in enumerate(text.splitlines()[:line + 1]):
        if not row.strip() or row.lstrip().startswith('#'):
            continue
        match = re.match(r'((?: *- )* *)([A-Za-z_][\w-]*)? *(:)?', row)
        depth = len(match[1])
        path = [(level, key) for level, key in path if level < depth]
        if match[2] and match[3] and (number < line or column >= match.end(3)):
            path.append((depth, match[2]))
    return '.'.join(key for _, key in path) or 'yaml'


def loads(text):
    """Config из текста config.yaml; неверный файл - ConfigError.

    Config.raw - разделы как в файле, все значения строками (секреты и числа с ведущим нулём целы);
    exits[].host - адрес без `root@`.
    """
    text = _plain_forms(text)
    try:
        _reject_foreign(text)
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as error:
        # Сломанная строка в кавычках: начало значения, а не место, где сканер упёрся в конец.
        scanner = isinstance(error, yaml.scanner.ScannerError) and getattr(error, 'context_mark', None)
        mark = scanner or getattr(error, 'problem_mark', None)
        where = f', строка {mark.line + 1}' if mark else ''
        raise ConfigError(_key_at(text, mark.line, mark.column) if mark else 'yaml',
                          f'{getattr(error, "problem", None) or "ошибка разбора"}{where}') from None
    if root is None:
        raise ConfigError('<корень>', 'пустой файл или не словарь ключей')
    sections = _sections(root)
    entry = sections.get('entry', {})
    exits = tuple(Node(item.get('tag', ''), item.get('host', '')) for item in sections.get('exits', []))
    return Config(Node('ru', entry.get('host', '')), exits, sections)


def load(path='/etc/anysda/config.yaml'):
    """Config из файла."""
    with open(path, encoding='utf-8') as handle:
        return loads(handle.read())


def set_exit_port(path, tag, port):
    """hy2_direct_port экзита в config.yaml; остальные строки и комментарии не трогаются."""
    path = Path(path)
    lines = path.read_text().splitlines()
    root = yaml.compose(_plain_forms('\n'.join(lines)))
    exits = next((value for key, value in root.value if key.value == 'exits'), None)
    for item in exits.value if exits else ():
        fields = {key.value: (key, value) for key, value in item.value}
        if 'tag' not in fields or fields['tag'][1].value != tag:
            continue
        if 'hy2_direct_port' in fields:
            mark = fields['hy2_direct_port'][0].start_mark
            lines[mark.line] = lines[mark.line][:mark.column] + f'hy2_direct_port: {port}'
        else:
            last, value = item.value[-1]
            lines.insert(value.start_mark.line + 1, ' ' * last.start_mark.column + f'hy2_direct_port: {port}')
        replace(path, '\n'.join(lines) + '\n')
        path.chmod(0o600)
        return
    raise ConfigError('exits', f'тег "{tag}" не найден в config.yaml')


def rewrite(path, edit):
    """Правка config.yaml по строкам: комментарии, порядок, права и владелец те же.

    edit(root, lines) правит строки файла по узлам YAML (root - корневой блок); строка None - удалена.
    Файл подменяется через rename, только если текст сменился. Вернуть, сменился ли он.
    """
    path = Path(path)
    text = path.read_text()
    root = yaml.compose(_plain_forms(text), Loader=yaml.SafeLoader)
    lines = text.split('\n')
    edit(root if isinstance(root, yaml.MappingNode) else yaml.MappingNode('tag:yaml.org,2002:map', []), lines)
    fresh = '\n'.join(line for line in lines if line is not None)
    if fresh == text:
        return False
    replace(path, fresh)
    return True


def set_exits(path, exits):
    """Состав экзитов config.yaml - exits, список (тег, адрес): оставшиеся экзиты со своими строками и
    комментариями как есть, лишние убираются вместе с комментариями внутри своего блока, новые - в конец
    списка `- tag:` + `host: root@адрес`. Остальной файл, права и владелец те же."""
    path = Path(path)
    text = path.read_text()
    lines = text.split('\n')
    root = yaml.compose(_plain_forms(text), Loader=yaml.SafeLoader)
    node = next((value for key, value in root.value if key.value == 'exits'), None)
    if not isinstance(node, yaml.SequenceNode) or not exits:
        raise ConfigError('exits', 'нужен список экзитов, как в config.example.yaml')
    items, keep, drop = node.value, {tag for tag, _host in exits}, set()
    spans = []
    for index, item in enumerate(items):
        start = item.start_mark.line
        if index + 1 < len(items):
            end = items[index + 1].start_mark.line
        else:
            end = item.value[-1][1].end_mark.line + (item.value[-1][1].end_mark.column > 0)
            # Комментарии глубже тире под последним ключом - его же блок.
            while end < len(lines) and lines[end].strip().startswith('#') and \
                    len(lines[end]) - len(lines[end].lstrip()) > item.start_mark.column - 2:
                end += 1
        spans.append(end)
        tag = next((value.value for key, value in item.value if key.value == 'tag'), '')
        if tag not in keep:
            drop.update(range(start, end))
    column = items[0].start_mark.column if items else 4
    known = {next((value.value for key, value in item.value if key.value == 'tag'), '') for item in items}
    added = [line for tag, host in exits if tag not in known
             for line in (f'{" " * (column - 2)}- tag: {tag}', f'{" " * column}host: root@{host}')]
    at = spans[-1] if spans else node.start_mark.line + 1
    lines[at:at] = added
    drop = {number + (len(added) if number >= at else 0) for number in drop}
    replace(path, '\n'.join(line for number, line in enumerate(lines) if number not in drop))


def replace(path, text):
    """Файл подменяется через rename с правами и владельцем прежнего."""
    stat = path.stat()
    tmp = path.with_name(f'.{path.name}.tmp')
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.st_mode & 0o7777)
    with os.fdopen(fd, 'w') as out:
        os.fchmod(out.fileno(), stat.st_mode & 0o7777)
        os.fchown(out.fileno(), stat.st_uid, stat.st_gid)
        out.write(text)
    os.replace(tmp, path)


def _find(node, keys):
    """(блок, номер) ключа по пути keys; нет такого - None."""
    for depth, key in enumerate(keys):
        if not isinstance(node, yaml.MappingNode):
            return None
        index = next((number for number, (name, _) in enumerate(node.value) if name.value == key), None)
        if index is None:
            return None
        if depth == len(keys) - 1:
            return node, index
        node = node.value[index][1]
    return None


def _drop_field(lines, item, index):
    """Ключ блока со значением - строки в None; `- ключ:` первым в элементе списка отдаёт тире следующему."""
    name, field = item.value[index]
    end = field.end_mark.line - (field.end_mark.column == 0)
    head = lines[name.start_mark.line][:name.start_mark.column]
    if head.strip() == '-' and index + 1 < len(item.value):
        following = item.value[index + 1][0].start_mark.line
        lines[following] = head + lines[following][len(head):]
    lines[name.start_mark.line:end + 1] = [None] * (end + 1 - name.start_mark.line)


def drop_passwords(path):
    """Пароли root entry и экзитов из config.yaml 1.2.0; без паролей файл не трогается. True - пароли были."""
    def edit(root, lines):
        for key, value in root.value:
            items = [value] if key.value == 'entry' else value.value if key.value == 'exits' else ()
            for item in (found for found in items if isinstance(found, yaml.MappingNode)):
                for index in [number for number, (name, _) in enumerate(item.value) if name.value == 'password']:
                    _drop_field(lines, item, index)
    return rewrite(path, edit)


AREA_NAMES = ('telegram_bot_token', 's3_access_key', 's3_secret_key', 'sso_client_secret', 'admin_password')
AREA = ['# Секреты. Деплой переносит их в зашифрованное хранилище entry и стирает здесь;',
        '# пустое значение - секрет прежний, новое значение - применится при следующем ./deploy.sh.',
        'secrets:', *(f'  {name}:' for name in AREA_NAMES)]
# Блок, который прежний деплой дописывал к ключу оркестратора.
KEY_NOTE = ('# orchestrator SSH-ключ — НЕ удалять: нужен для повторного деплоя',
            '# с пересозданной entry-ноды. config.yaml надо сохранять/переносить.')


def secrets(cfg):
    """Секреты из файла: область secrets, без значения в ней - место 1.2.0. {имя: значение}, пустых нет."""
    out = {}
    for keys, name in LEGACY_SECRETS.items():
        node = cfg.raw
        for key in keys:
            node = node.get(key, '') if isinstance(node, dict) else ''
        if isinstance(node, str) and node:
            out[name] = node
    out.update({name: value for name, value in cfg.raw.get('secrets', {}).items() if value})
    if 'orchestrator_key' in out:
        try:
            out['orchestrator_key'] = base64.b64decode(out['orchestrator_key'], validate=True).decode()
        except ValueError:
            raise ConfigError('orchestrator_key', 'не ключ ssh в base64 - верни строку из прежнего config.yaml') from None
    return out


def wipe_secrets(path, keep=()):
    """После успешного деплоя: значения области secrets пустеют, места 1.2.0 уходят из файла; keep - имена,
    что этот прогон не применил и оставляет до следующего.

    Убрано место 1.2.0, а области в файле нет - она дописывается пустой в конец. Вернуть, сменился ли файл.
    """
    def edit(root, lines):
        legacy = False
        for keys in (keys for keys, name in (*LEGACY_SECRETS.items(), (('orchestrator_pubkey',), '')) if name not in keep):
            if found := _find(root, keys):
                _drop_field(lines, *found)
                legacy = True
        for number, line in enumerate(lines):
            if line in KEY_NOTE:
                lines[number] = None
        area = _find(root, ('secrets',))
        block = area[0].value[area[1]][1] if area else None
        for name, value in block.value if isinstance(block, yaml.MappingNode) else ():
            if value.value and name.value not in keep:
                first, last = name.start_mark.line, value.end_mark.line
                comment = re.search(r'\s#[^\'"]*$', lines[last])
                lines[first:last + 1] = [lines[first][:name.start_mark.column] + f'{name.value}:'
                                         + (comment[0] if comment else '')] + [None] * (last - first)
        if legacy and not area:
            tail = 1 if lines and lines[-1] == '' else 0
            lines[len(lines) - tail:len(lines) - tail] = ['', *AREA]
    return rewrite(path, edit)
