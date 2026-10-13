#!/usr/bin/env python3
"""
config2env.py — читает config.yaml, пишет infra/envs/*.env

Использование:
    python3 infra/lib/config2env.py [repo_root]
"""

import base64
import ipaddress
import json
import re
import sys
from pathlib import Path

# Модуль рядом со скриптом: так импорт работает и при загрузке по пути файла.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from hy2_bandwidth import bandwidth_fields  # noqa: E402


def shq(v):
    """Одна строка присваивания; Bash source вернёт значение байт в байт.

    Деплой удаляет SSH_PASS построчно перед передачей env удалённой стадии.
    Переводы строк поэтому записываются как ANSI-C литералы, а не буквально.
    """
    return "'" + str(v).replace("'", "'\\''").replace('\n', "'$'\\n''").replace('\r', "'$'\\r''") + "'"


def unquote_scalar(v, key='значение'):
    """Снимает YAML-кавычки: '..' ('' внутри = '), ".." (escape-последовательности).

    Хвостовой комментарий после закрывающей кавычки отбрасывается. Незакрытая
    кавычка — ошибка: молча обрезанный пароль хуже упавшего деплоя.
    """
    q = v[0]
    i, out = 1, []
    while i < len(v):
        c = v[i]
        if q == "'" and c == "'":
            if v[i + 1:i + 2] == "'":
                out.append("'")
                i += 2
                continue
            if v[i + 1:].strip() and not v[i + 1:].lstrip().startswith('#'):
                config_error(key, 'лишний текст после закрывающей кавычки')
            return ''.join(out)
        if q == '"' and c == '\\':
            nxt = v[i + 1:i + 2]
            # Полный набор escape-последовательностей YAML 1.1: их пишет
            # yaml.safe_dump для символов вне ASCII и управляющих.
            escapes = {'0': '\0', 'a': '\a', 'b': '\b', 't': '\t', '\t': '\t', 'n': '\n',
                       'v': '\v', 'f': '\f', 'r': '\r', 'e': '\x1b', ' ': ' ', '"': '"',
                       '/': '/', '\\': '\\', 'N': '\x85', '_': '\xa0', 'L': ' ',
                       'P': ' '}
            width = {'x': 2, 'u': 4, 'U': 8}.get(nxt)
            if width:
                code = v[i + 2:i + 2 + width]
                if len(code) != width or not all(c in '0123456789abcdefABCDEF' for c in code) \
                        or int(code, 16) > 0x10FFFF:
                    config_error(key, 'неподдерживаемая escape-последовательность')
                out.append(chr(int(code, 16)))
                i += 2 + width
                continue
            if nxt not in escapes:
                config_error(key, 'неподдерживаемая escape-последовательность')
            out.append(escapes[nxt])
            i += 2
            continue
        if q == '"' and c == '"':
            if v[i + 1:].strip() and not v[i + 1:].lstrip().startswith('#'):
                config_error(key, 'лишний текст после закрывающей кавычки')
            return ''.join(out)
        out.append(c)
        i += 1
    config_error(key, 'незакрытая кавычка (значение скрыто)')


def _key_line(line):
    """(колонка ключа, значение) для строки `ключ: значение` или `- ключ: ...`."""
    content = line.lstrip(' ')
    if not content or content.startswith('#'):
        return None
    column = len(line) - len(content)
    if content.startswith('- '):
        content, column = content[2:], column + 2
    name, sep, value = content.partition(':')
    if not sep or not name.strip():
        return None
    return column, value.strip()


def _fold(first, rest, quote):
    """Склеивает перенесённое значение по правилам YAML (перенос = пробел)."""
    out, pending = first.rstrip(' '), 0
    for line in rest:
        part = line.strip(' ')
        if not part:
            pending += 1
            continue
        if quote == '"' and (len(out) - len(out.rstrip('\\'))) % 2:
            out = out[:-1] + '\\n' * pending + part.rstrip(' ')
        elif pending:
            out += ('\\n' if quote == '"' else '\n') * pending + part.rstrip(' ')
        else:
            out += ' ' + part.rstrip(' ')
        pending = 0
    if '\n' in out:
        # Перевод строки держит на одной строке только двойная кавычка.
        value = unquote_scalar(out) if quote == "'" else out
        escaped = value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
        return '"' + escaped + '"'
    return out


def normalize_yaml(text):
    """Сводит равнозначные формы YAML к одной; номера строк не меняются.

    yaml.safe_dump пишет список без отступа (`exits:` и `- tag:` в одной
    колонке) и переносит длинные значения на 80 символах. Такой список
    сдвигается на два пробела, перенесённое значение склеивается в строку
    ключа, а строки продолжения становятся пустыми, чтобы ошибки указывали
    на строку исходного файла. Отступ блоков по-прежнему ровно два пробела.
    """
    lines = text.split('\n')
    changed = True
    while changed:
        changed = False
        for index, line in enumerate(lines):
            key = _key_line(line)
            if not key or (key[1] and not key[1].startswith('#')):
                continue
            column = key[0]
            following = [i for i in range(index + 1, len(lines))
                         if lines[i].strip() and not lines[i].lstrip(' ').startswith('#')]
            if not following:
                continue
            first = lines[following[0]]
            if len(first) - len(first.lstrip(' ')) != column or not first.lstrip(' ').startswith('-'):
                continue
            for i in range(index + 1, len(lines)):
                content = lines[i].lstrip(' ')
                indent = len(lines[i]) - len(content)
                if content and not content.startswith('#') and (
                        indent < column or (indent == column and not content.startswith('-'))):
                    break
                if content and indent >= column:
                    lines[i] = '  ' + lines[i]
            changed = True
            break

    index = 0
    while index < len(lines):
        key = _key_line(lines[index])
        index += 1
        if not key or not key[1] or key[1].startswith('#'):
            continue
        column, value = key
        quote = value[0] if value[0] in ('"', "'") else ''
        if not quote and re.search(r'\s#', value):
            continue
        rest, end = [], index
        while end < len(lines):
            content = lines[end].lstrip(' ')
            indent = len(lines[end]) - len(content)
            if content and indent <= column:
                break
            if not quote and content and (content.startswith('#') or re.search(r':(\s|$)|\s#', content)):
                break   # в простом значении это уже не продолжение: пусть отвергнет схема
            rest.append(lines[end])
            end += 1
        while rest and not rest[-1].strip():
            rest.pop()
            end -= 1
        if not rest:
            continue
        head = lines[index - 1]
        start = head.index(':', len(head) - len(head.lstrip(' '))) + 1
        lines[index - 1] = head[:start] + ' ' + _fold(value, rest, quote)
        for i in range(index, index + len(rest)):
            lines[i] = ''
        index += len(rest)
    return '\n'.join(lines)


def parse_yaml(text):
    """Минимальный парсер для нашего фиксированного формата config.yaml."""
    text = normalize_yaml(text)
    validate_yaml(text)
    cfg = {'exits': [], 'ports': {}, 'entry': {}, 'telegram': {}, 'admin': {},
           'panel': {}, 'tgbot': {}, 'backup': {}, 'youtube': {}, 'sso': {},
           'entry_resources': {}, 'wireguard': {}, 'openvpn': {}}
    context = None
    current_exit = None
    backup_sub = None    # 's3' или 'local' внутри backup: блока

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith('#'):
            continue

        indent = len(line) - len(line.lstrip())
        content = line.lstrip()

        def kv(s):
            k, _, v = s.partition(':')
            v = v.strip()
            if v[:1] in ('"', "'"):
                v = unquote_scalar(v)
            else:
                # `us   # короткий тег` — комментарий после значения не часть значения
                v = re.sub(r'\s+#.*$', '', v)
                if v.startswith('#'):
                    v = ''
            return k.strip(), v

        if indent == 0:
            if content.startswith('exits:'):
                context = 'exits'
                current_exit = None
            elif content.startswith('entry:'):
                context = 'entry'
            elif content.startswith('ports:'):
                context = 'ports'
            elif content.startswith('telegram:'):
                context = 'telegram'
            elif content.startswith('admin:'):
                context = 'admin'
            elif content.startswith('panel:'):
                context = 'panel'
            elif content.startswith('tgbot:'):
                context = 'tgbot'
            elif content.startswith('backup:'):
                context = 'backup'
                backup_sub = None
            elif content.startswith('youtube:'):
                context = 'youtube'
            elif content.startswith('sso:'):
                context = 'sso'
            elif content.startswith('entry_resources:'):
                context = 'entry_resources'
            elif content.startswith('wireguard:'):
                context = 'wireguard'
            elif content.startswith('openvpn:'):
                context = 'openvpn'
            else:
                k, v = kv(content)
                if v:
                    cfg[k] = v
        elif indent == 2:
            if context == 'exits':
                if content.startswith('- '):
                    k, v = kv(content[2:])
                    current_exit = {k: v}
                    cfg['exits'].append(current_exit)
                elif current_exit is not None:
                    k, v = kv(content)
                    current_exit[k] = v
            elif context == 'entry':
                k, v = kv(content)
                cfg['entry'][k] = v
            elif context == 'ports':
                k, v = kv(content)
                cfg['ports'][k] = v
            elif context == 'telegram':
                k, v = kv(content)
                cfg['telegram'][k] = v
            elif context == 'admin':
                k, v = kv(content)
                cfg['admin'][k] = v
            elif context == 'panel':
                k, v = kv(content)
                cfg['panel'][k] = v
            elif context == 'tgbot':
                k, v = kv(content)
                cfg['tgbot'][k] = v
            elif context == 'youtube':
                k, v = kv(content)
                cfg['youtube'][k] = v
            elif context == 'sso':
                k, v = kv(content)
                cfg['sso'][k] = v
            elif context == 'entry_resources':
                k, v = kv(content)
                cfg['entry_resources'][k] = v
            elif context in ('wireguard', 'openvpn'):
                k, v = kv(content)
                cfg[context][k] = v
            elif context == 'backup':
                # `local:` / `s3:` — суб-блок; иначе обычный ключ верхнего уровня
                k, v = kv(content)
                if k in ('local', 's3'):
                    backup_sub = k
                    cfg['backup'].setdefault(backup_sub, {})
                else:
                    cfg['backup'][k] = v
        elif indent == 4:
            if context == 'exits' and current_exit is not None:
                k, v = kv(content)
                current_exit[k] = v
            elif context == 'backup' and backup_sub is not None:
                k, v = kv(content)
                cfg['backup'][backup_sub][k] = v

    return cfg


def validate_yaml(text):
    """Не угадывать смысл YAML за пределами поддерживаемого формата.

    Схема также не даёт опечаткам и неправильному отступу выключать функции.
    Все ошибки проверяются до разбора и до изменения env/state/SSH-ключей.
    """
    schema = {
        'entry': {'host', 'password', 'public_host'},
        'exits': {'tag', 'host', 'password', 'hy2_direct_port', 'hy2_up_mbps', 'hy2_down_mbps'},
        'ports': {'hy2_direct', 'hy2_warp', 'hy2_mgmt', 'wireguard', 'openvpn'},
        'wireguard': {'mtu', 'split_local'},
        'openvpn': {'proto'},
        'admin': {'user', 'password', 'ssh_pubkey'},
        'panel': {'domain', 'image'}, 'tgbot': {'image'},
        'telegram': {'bot_token', 'chat_id', 'admin_username'},
        'youtube': {'route', 'quic', 'mark', 'offload', 'strategy', 'zapret_ref', 'domains',
                    'window_scale'},
        'sso': {'enabled', 'base_url', 'app_slug', 'client_id', 'client_secret',
                'allowed_subs', 'auto_redirect', 'password_login', 'label', 'discovery_url'},
        'backup': {'enabled', 'backend', 'passphrase', 'retention', 'schedule', 'local', 's3'},
        'backup.local': {'dir'},
        'backup.s3': {'endpoint', 'bucket', 'region', 'access_key', 'secret_key'},
        # Значения проверяет entry_resources.py на целевой машине.
        'entry_resources': {'panel_memory_mib', 'panel_old_space_mib', 'bot_memory_mib',
                            'vm_memory_mib', 'vm_cache_mib', 'vm_query_mib',
                            'vm_concurrent_requests', 'agh_memory_mib', 'agh_high_mib',
                            'agh_go_mib', 'retention_days', 'adaway_enabled', 'querylog_size'},
    }
    booleans = {'backup.enabled', 'sso.enabled', 'sso.auto_redirect', 'sso.password_login',
                'entry_resources.adaway_enabled', 'wireguard.split_local'}
    boolean_values = {'true', 'false', 'yes', 'no', 'y', 'n', '1', '0'}
    seen = set()
    context = ''
    sub = ''
    exit_index = -1
    for number, raw in enumerate(text.splitlines(), 1):
        content = raw.lstrip(' ')
        if not content or content.startswith('#'):
            continue
        indent = len(raw) - len(content)
        item = content.startswith('- ')
        if item:
            content = content[2:]
        name, sep, value = content.partition(':')
        name, value = name.strip(), value.strip()
        if indent == 0:
            key = name
            if name not in schema and name not in ('orchestrator_key', 'orchestrator_pubkey'):
                config_error(key, f'неизвестный ключ, строка {number}')
            context, sub = name, ''
            exit_index = -1
        else:
            if context == 'exits':
                if item and indent == 2:
                    exit_index += 1
                key = f'exits[{max(exit_index, 0)}].{name}'
                valid_indent = (item and indent == 2) or (not item and indent == 4 and exit_index >= 0)
                parent = 'exits'
            else:
                if indent == 2:
                    sub = ''
                parent = context + ('.' + sub if sub else '')
                key = parent + '.' + name
                valid_indent = indent == (4 if sub else 2) and not item
            if not valid_indent:
                config_error(key, f'неверный отступ или структура, строка {number}')
            if name not in schema.get(parent, set()):
                config_error(key, f'неизвестный ключ, строка {number}')
        if not sep or '\t' in raw[:len(raw) - len(raw.lstrip())] or (item and indent == 0):
            config_error(key, f'неверный формат, строка {number}')
        if key in seen:
            config_error(key, f'повтор ключа, строка {number}')
        seen.add(key)
        if key in schema:
            if value and not value.startswith('#'):
                config_error(key, 'ожидается блок с отступами, как в config.example.yaml')
            if key.startswith('backup.'):
                sub = name
            continue
        quoted = value[:1] in ('"', "'")
        if quoted:
            value = unquote_scalar(value, key)
        else:
            value = re.sub(r'\s+#.*$', '', value)
            if value.startswith('#'):
                value = ''
            if value[:1] in ('[', '{', '|', '>', '!', '&', '*') or value.lower() in ('null', '~'):
                config_error(key, 'ожидается скаляр; сложный YAML не поддерживается')
            if key not in booleans and value.lower() in ('true', 'false', 'yes', 'no'):
                config_error(key, 'ожидается строка; заключите значение в кавычки')
        if '\0' in value:
            config_error(key, 'нулевой байт недопустим в env')
        if key in booleans and value.lower() not in boolean_values:
            config_error(key, 'ожидается true или false')
        # Тот же контракт, что в entry_resources.py: ошибка до записи env.
        if key == 'entry_resources.adaway_enabled' and value.lower() not in ('true', 'false'):
            config_error(key, 'ожидается true или false')
        if key.startswith('entry_resources.') and key != 'entry_resources.adaway_enabled' and (
                not value.isdigit() or (key == 'entry_resources.retention_days' and int(value) == 0)):
            config_error(key, 'ожидается целое число' + (' > 0' if key.endswith('retention_days') else ' >= 0'))


def prepare_private(path):
    """Новый файл сразу 0600; существующий закрываем до записи секретов."""
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)


def merge_setup_config(previous, answers):
    """Обновляет только ответы мастера, сохраняя остальные строки YAML.

    Работаем с тем же форматом, что parse_yaml: блоки с отступом в два
    пробела и список exits. Неподдерживаемый YAML отклоняем ДО записи.
    Экзиты сопоставляем по тегу, а не по позиции; удалённые в мастере
    экзиты удаляются, дополнительные поля оставшихся сохраняются.
    """
    previous, answers = normalize_yaml(previous), normalize_yaml(answers)
    old_config = parse_yaml(previous)
    new_config = parse_yaml(answers)
    tags = [entry.get('tag') for entry in old_config['exits']]
    if any(not tag for tag in tags) or len(tags) != len(set(tags)):
        config_error('exits', 'для сохранения настроек нужны уникальные непустые теги')

    def blocks(text, pattern):
        matches = list(re.finditer(pattern, text, re.MULTILINE))
        prefix = text[:matches[0].start()] if matches else text
        parts = [text[match.start():matches[index + 1].start()
                      if index + 1 < len(matches) else len(text)]
                 for index, match in enumerate(matches)]
        return prefix, parts

    def merge_mapping(old, new, indent):
        pattern = r'^' + ' ' * indent + r'[^ #\n][^:\n]*:'
        prefix, old_parts = blocks(old, pattern)
        _, new_parts = blocks(new, pattern)
        keyed = {part.split(':', 1)[0].strip(): part for part in old_parts}
        for part in new_parts:
            name = part.split(':', 1)[0].strip()
            if name not in keyed:
                keyed[name] = part
                continue
            old_head, _, old_body = keyed[name].partition('\n')
            new_head, _, new_body = part.partition('\n')
            if indent == 0 and name == 'exits':
                old_prefix, old_exits = blocks(old_body, r'^  - ')
                _, new_exits = blocks(new_body, r'^  - ')
                by_tag = {entry['tag']: text for entry, text in zip(old_config['exits'], old_exits)}
                merged = []
                for entry, text in zip(new_config['exits'], new_exits):
                    # Первый ключ элемента может быть не tag. Превращаем
                    # обе записи в обычные mapping-блоки перед слиянием.
                    old_text = by_tag.get(entry['tag'], '')
                    result = merge_mapping(old_text.replace('  - ', '    ', 1),
                                           text.replace('  - ', '    ', 1), 4)
                    merged.append(result.replace('    ', '  - ', 1))
                keyed[name] = old_head + '\n' + old_prefix + ''.join(merged)
            elif (not new_head.partition(':')[2].strip()
                    or new_head.partition(':')[2].lstrip().startswith('#')):
                keyed[name] = old_head + '\n' + merge_mapping(old_body, new_body, indent + 2)
            else:
                # У скаляра old_body содержит только комментарии и пустые строки.
                # Комментарий в строке значения тоже сохраняем; # внутри
                # кавычек (в том числе с '' или \" внутри) — часть значения.
                value = old_head.partition(':')[2].lstrip()
                match = re.fullmatch(r'''(?:'(?:[^']|'')*'|"(?:[^"\\]|\\.)*"|.*?)([ \t]+\#.*)?''', value)
                comment = (' ' + value) if value.startswith('#') else (match[1] or '')
                keyed[name] = new_head + comment + '\n' + old_body
        return prefix + ''.join(keyed.values())

    result = merge_mapping(previous.rstrip() + '\n', answers, 0)
    validate_yaml(result)
    return result


def config_error(key, reason):
    # Значения могут содержать секреты: в диагностике только путь ключа.
    sys.exit(f'ERROR: config.yaml: {key}: {reason}')


def normalize_domain(value, key):
    """Каноническое DNS-имя для Caddy и OIDC redirect_uri."""
    try:
        value = value.encode('idna').decode('ascii').lower().rstrip('.')
    except UnicodeError:
        config_error(key, 'неверное DNS-имя')
    labels = value.split('.')
    if (len(value) > 253 or len(labels) < 2 or not re.search(r'[a-z]', labels[-1]) or
            not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)):
        config_error(key, 'ожидается DNS-имя без схемы, порта и пробелов')
    return value


def validate_network(cfg):
    """Проверить идентификаторы и адреса до любых записей на диск."""
    def host(value, key):
        try:
            ipaddress.ip_address(value)
            return
        except ValueError:
            pass
        if (not value or len(value) > 253 or
                not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
                        for label in value.rstrip('.').split('.')) or
                re.fullmatch(r'[0-9.]+', value)):
            config_error(key, 'ожидается IP-адрес или DNS-имя')

    def port(value, key):
        if not re.fullmatch(r'[0-9]+', value) or not 1 <= int(value) <= 65535:
            config_error(key, 'порт должен быть целым числом от 1 до 65535')

    entry = cfg['entry']
    host(entry.get('host', ''), 'entry.host')
    if not entry.get('password'):
        config_error('entry.password', 'обязательное непустое значение')
    if entry.get('public_host'):
        host(entry['public_host'], 'entry.public_host')
    if not cfg['exits']:
        config_error('exits', 'нужна хотя бы одна выходная нода')
    tags = set()
    for index, ex in enumerate(cfg['exits']):
        key = f'exits[{index}]'
        tag = ex.get('tag', '')
        if (not re.fullmatch(r'[a-z](?:[a-z0-9-]*[a-z0-9])?', tag) or
                tag in ('ru', 'all', 'exits', 'foreign') or tag in tags):
            config_error(key + '.tag', 'нужен уникальный незарезервированный тег: [a-z](?:[a-z0-9-]*[a-z0-9])?')
        tags.add(tag)
        host(ex.get('host', ''), key + '.host')
        if not ex.get('password'):
            config_error(key + '.password', 'обязательное непустое значение')
        if 'hy2_direct_port' in ex:
            port(ex['hy2_direct_port'], key + '.hy2_direct_port')
    for name, value in cfg['ports'].items():
        port(value, 'ports.' + name)
    if 'proto' in cfg['openvpn'] and cfg['openvpn']['proto'] not in (
            'udp', 'udp4', 'udp6', 'tcp', 'tcp4', 'tcp6',
            'tcp-server', 'tcp4-server', 'tcp6-server'):
        config_error('openvpn.proto', 'допустимо udp, udp4, udp6, tcp, tcp4, tcp6, '
                     'tcp-server, tcp4-server, tcp6-server')
    if 'mtu' in cfg['wireguard']:
        mtu = cfg['wireguard']['mtu']
        if not re.fullmatch(r'[0-9]+', mtu) or not 1280 <= int(mtu) <= 9000:
            config_error('wireguard.mtu', 'ожидается целое число от 1280 до 9000')
    if cfg['panel'].get('domain'):
        cfg['panel']['domain'] = normalize_domain(cfg['panel']['domain'], 'panel.domain')


def materialize_orchestrator_key(cfg):
    """Раскладывает orchestrator SSH-ключ из config.yaml в ~/.ssh/id_ed25519.

    Ключ хранится в config.yaml (поля orchestrator_key / orchestrator_pubkey),
    поэтому переживает пересоздание entry-ноды: новый entry с тем же config.yaml
    получит тот же ключ, и забутстрапленные ранее экзиты его уже знают.
    Если ключа в config.yaml нет — ничего не делаем, его сгенерит deploy.sh.
    """
    key_b64 = cfg.get('orchestrator_key', '')
    if not key_b64:
        return
    ssh_dir = Path.home() / '.ssh'
    ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    priv = ssh_dir / 'id_ed25519'
    prepare_private(priv)
    priv.write_bytes(base64.b64decode(key_b64))
    pub_line = cfg.get('orchestrator_pubkey', '')
    if pub_line:
        pub = ssh_dir / 'id_ed25519.pub'
        pub.write_text(pub_line.rstrip() + '\n')
        pub.chmod(0o644)
    print('orchestrator-ключ восстановлен из config.yaml')


def load_excluded_exits(repo_root):
    """Читает infra/state/excluded-exits.txt — список тегов экзитов, которые
    probe_udp_or_exclude() (в deploy.sh) пометил как недостижимые по UDP с
    RU направления. Эти экзиты исключаются из всех env-файлов на каждой
    регенерации envs (одиночные стадии тоже подхватывают)."""
    state_file = repo_root / 'infra' / 'state' / 'excluded-exits.txt'
    if not state_file.exists():
        return set()
    excluded = set()
    for line in state_file.read_text().splitlines():
        tag = line.strip()
        if tag and not tag.startswith('#'):
            excluded.add(tag)
    return excluded


def assign_mgmt_octets(repo_root, exits):
    """Закрепляет за тегом экзита последний октет MGMT_IP (10.99.0.N).

    Октет хранится в infra/state/mgmt-ips.txt (`тег октет`) и от порядка
    exits в config.yaml больше не зависит: убрали экзит из середины — у
    остальных адреса и mon-порты (10100+N) те же. Файла нет (первый деплой
    или обновление со старой версии) — засеваем прежней формулой «позиция в
    списке + 2», поэтому уже поставленные установки адресов не меняют.
    Новый тег берёт наименьший свободный октет; октет убранного тега
    остаётся за ним и никому не переходит.
    """
    state_file = repo_root / 'infra' / 'state' / 'mgmt-ips.txt'
    pinned = {}
    if state_file.exists():
        for line in state_file.read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and not parts[0].startswith('#') and parts[1].isdigit():
                pinned[parts[0]] = int(parts[1])
    else:
        pinned = {e['tag']: i for i, e in enumerate(exits, start=2)}

    changed = not state_file.exists()
    for e in exits:
        if e['tag'] not in pinned:
            used = set(pinned.values())
            pinned[e['tag']] = next(n for n in range(2, 255) if n not in used)
            changed = True
        e['_mgmt_idx'] = pinned[e['tag']]

    if changed:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            '# тег → последний октет MGMT_IP (10.99.0.N), пишет config2env.py.\n'
            '# Не перенумеровывать: на октет завязаны метки метрик и mon-порт.\n'
            + ''.join(f'{t} {n}\n' for t, n in sorted(pinned.items(), key=lambda kv: kv[1])))


def main():
    repo_root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    config_path = repo_root / 'config.yaml'

    if not config_path.exists():
        print('ERROR: config.yaml не найден. Запусти ./setup.sh', file=sys.stderr)
        sys.exit(1)

    cfg = parse_yaml(config_path.read_text())
    validate_network(cfg)
    materialize_orchestrator_key(cfg)

    entry = cfg.get('entry', {})
    exits = cfg.get('exits', [])
    ports = cfg.get('ports', {})

    # Проверяем до записи env/state: неполная пара не должна молча включать
    # разные алгоритмы в двух направлениях. Скорости заданы со стороны экзита.
    for ex in exits:
        try:
            bandwidth_fields(ex.get('hy2_up_mbps'), ex.get('hy2_down_mbps'),
                             label=f'exits[{ex.get("tag", "?")}]')
        except ValueError as error:
            sys.exit(f'ERROR: {error}')

    # MGMT_IP закреплён за тегом (до исключения по UDP-фильтру), а не за
    # позицией в списке: см. assign_mgmt_octets.
    assign_mgmt_octets(repo_root, exits)

    # Применить persistent UDP-фильтр от verify_and_rotate_ports. Эти экзиты
    # деплой не настраивает ни в одной стадии, пока verify не подтвердит
    # обратное (для re-include — удалить тег из excluded-exits.txt и
    # перезапустить ./deploy.sh; verify прогонится заново при do_all).
    excluded = load_excluded_exits(repo_root)
    if excluded:
        kept = [e for e in exits if e.get('tag') not in excluded]
        skipped = [e['tag'] for e in exits if e.get('tag') in excluded]
        if skipped:
            print(f'config2env: исключены по UDP-фильтру: {", ".join(skipped)} '
                  f'(infra/state/excluded-exits.txt) — '
                  f'их MGMT_IP-индексы зарезервированы и не переиспользуются')
        exits = kept

    if not entry.get('host'):
        print('ERROR: entry.host не задан в config.yaml', file=sys.stderr)
        sys.exit(1)
    if not exits:
        print('ERROR: exits пустой — нужна хотя бы одна выходная нода', file=sys.stderr)
        sys.exit(1)

    entry_host = entry['host']

    # Дефолтные порты
    hy2_direct = ports.get('hy2_direct', '443')
    hy2_warp   = ports.get('hy2_warp',   '8443')
    # Служебный Hysteria2 (скрейп node_exporter из-за границы вместо WG-mesh).
    hy2_mgmt   = ports.get('hy2_mgmt',   '8444')

    # Ключ человека-админа: если задан, 00-bootstrap отключает парольный вход;
    # если пусто — пароль остаётся (VPN2-31, чтобы не запереть себя без ключа).
    admin_cfg = cfg.get('admin', {}) or {}
    admin_ssh_pubkey = (admin_cfg.get('ssh_pubkey') or '').strip()

    envs_dir = repo_root / 'infra' / 'envs'
    envs_dir.mkdir(parents=True, exist_ok=True)

    exit_tags = ' '.join(e['tag'] for e in exits)

    # ------------------------------------------------------------------
    # all.env
    # ------------------------------------------------------------------
    lines = [
        f'ENTRY_HOST={shq(entry_host)}',
        '',
        'AGH_PORT=3000',
        '',
        f'HY2_DIRECT_PORT={hy2_direct}',
        f'HY2_WARP_PORT={hy2_warp}',
        f'HY2_MGMT_PORT={hy2_mgmt}',
        '',
        f'ADMIN_SSH_PUBKEY={shq(admin_ssh_pubkey)}',
        '',
        f'EXIT_TAGS={shq(exit_tags)}',
    ]

    # Адрес сервера в клиентских WG/OVPN-конфигах. Домен вместо IP: при смене
    # IP entry хватает A-записи, конфиги у клиентов не перевыпускаются.
    # Без public_host адресом служит домен панели: он уже указывает на entry.
    public_host = (str(entry.get('public_host') or '').strip()
                   or str(cfg['panel'].get('domain') or ''))
    if public_host:
        lines += ['', f'WG_PUBLIC_HOST={shq(public_host)}', f'OVPN_PUBLIC_HOST={shq(public_host)}']

    # В именах env дефис тега заменяем на _: shell не допускает дефисы.
    # Сам тег (имена файлов, outbound, метки) сохраняем без изменений.
    # Per-exit server IP (используется как адрес для подключения Hysteria2)
    for ex in exits:
        tag = ex['tag'].upper().replace('-', '_')
        lines.append(f"DOMAIN_{tag}={shq(ex['host'])}")
        for key in ('up', 'down'):
            lines.append(f"{tag}_HY2_{key.upper()}_MBPS={shq(ex.get(f'hy2_{key}_mbps', ''))}")

    # Per-host MGMT IPs (октет закреплён за тегом, см. assign_mgmt_octets).
    lines.append('')
    lines.append('MGMT_IP_RU=10.99.0.1')
    for ex in exits:
        tag = ex['tag'].upper().replace('-', '_')
        lines.append(f"MGMT_IP_{tag}=10.99.0.{ex['_mgmt_idx']}")

    # Per-exit Hysteria2 direct port
    lines.append('')
    for ex in exits:
        tag = ex['tag'].upper().replace('-', '_')
        port = ex.get('hy2_direct_port', hy2_direct)
        lines.append(f'{tag}_HY2_DIRECT_PORT={port}')

    (envs_dir / 'all.env').write_text('\n'.join(lines) + '\n')

    # ------------------------------------------------------------------
    # ru.env (entry node)
    # ------------------------------------------------------------------
    entry_pass = entry.get('password', '')
    if not entry_pass:
        print('ERROR: entry.password не задан в config.yaml — перезапусти ./setup.sh',
              file=sys.stderr)
        sys.exit(1)

    admin = cfg.get('admin', {})
    admin_user = admin.get('user', 'anysda') or 'anysda'
    admin_password = admin.get('password', '')  # пусто → auto-gen в 22-adguard

    panel_domain = cfg.get('panel', {}).get('domain', '')
    # Образ панели. Пусто → дефолт стадии 30 (…/panel:dev). Пин нужен, когда на
    # проде осознанно крутится не dev-линия: без него обычный прогон 30-frontend
    # молча утащит панель обратно на :dev.
    panel_image = cfg.get('panel', {}).get('image', '')
    # Образ бота — то же для стадии 35-telegram (…/tgbot:dev).
    tgbot_image = cfg.get('tgbot', {}).get('image', '')

    lines = [
        "HOST_TAG='ru'",
        '',
        "SSH_USER='root'",
        f"SSH_HOST={shq(entry_host)}",
        f"SSH_PASS={shq(entry_pass)}",
        '',
        f"PUB_IP_OUT={shq(entry_host)}",
        '',
        "MGMT_IP='10.99.0.1'",
        '',
        f"ADMIN_USER={shq(admin_user)}",
        f"ADMIN_PASSWORD={shq(admin_password)}",
        f"PANEL_DOMAIN={shq(panel_domain)}",
        # Только явные значения. Характеристики читают стадии на entry.
        f"ENTRY_RESOURCE_OVERRIDES={shq(json.dumps(cfg['entry_resources']))}",
    ]
    if panel_image:
        lines.append(f"PANEL_IMAGE={shq(panel_image)}")
    if tgbot_image:
        lines.append(f"TGBOT_IMAGE={shq(tgbot_image)}")
    # Только явные настройки: без новых ключей env остаются байт в байт
    # прежними, а умолчания по-прежнему принадлежат стадиям 28/29/30.
    for key, variable in (('wireguard', 'WG_LISTEN_PORT'), ('openvpn', 'OVPN_PORT')):
        if key in ports:
            lines.append(f'{variable}={shq(int(ports[key]))}')
    if 'proto' in cfg['openvpn']:
        lines.append(f"OVPN_PROTO={shq(cfg['openvpn']['proto'])}")
    if 'mtu' in cfg['wireguard']:
        lines.append(f"WG_MTU={shq(int(cfg['wireguard']['mtu']))}")
    if 'split_local' in cfg['wireguard']:
        split_local = cfg['wireguard']['split_local'].lower() in ('true', 'yes', 'y', '1')
        lines.append(f"WG_SPLIT_LOCAL={shq('true' if split_local else 'false')}")
    tg = cfg.get('telegram', {})
    if tg.get('bot_token') and tg.get('chat_id'):
        lines += [
            '',
            f"TELEGRAM_BOT_TOKEN={shq(tg['bot_token'])}",
            f"TELEGRAM_CHAT_ID={shq(tg['chat_id'])}",
        ]
        if tg.get('admin_username'):
            lines.append(f"TELEGRAM_ADMIN_USERNAME={shq(tg['admin_username'])}")


    # YouTube-секция. Читают обе стадии: 20-ru-router (gen-router-config.py
    # строит outbound youtube-ru и правила) и 19-yt-zapret (десинк). Пустая
    # секция → YT_ROUTE=off, поведение ровно как до появления фичи.
    yt = cfg.get('youtube', {})
    yt_route = (yt.get('route', '') or 'off').lower()
    if yt_route not in ('off', 'zapret', 'direct'):
        print(f"ERROR: youtube.route='{yt_route}' — допустимо off | zapret | direct",
              file=sys.stderr)
        sys.exit(1)
    yt_quic = (yt.get('quic', '') or 'block').lower()
    if yt_quic not in ('block', 'allow'):
        print(f"ERROR: youtube.quic='{yt_quic}' — допустимо block | allow", file=sys.stderr)
        sys.exit(1)
    lines += [
        '',
        f"YT_ROUTE={shq(yt_route)}",
        f"YT_QUIC={shq(yt_quic)}",
        f"YT_MARK={shq(yt.get('mark', '256') or '256')}",
        f"YT_ZAPRET_OFFLOAD={shq((yt.get('offload', '') or 'keep').lower())}",
    ]
    # Необязательные переопределения — пишем только если заданы, чтобы дефолты
    # жили в одном месте (в стадии 19), а не размножались по конфигам.
    if yt.get('window_scale'):
        window_scale = yt['window_scale']
        if not re.fullmatch(r'native|[0-9]|1[0-4]', window_scale):
            print('ERROR: youtube.window_scale — допустимо native или 0..14', file=sys.stderr)
            sys.exit(1)
        lines.append(f"YT_ZAPRET_WINDOW_SCALE={shq(window_scale)}")
    if yt.get('strategy'):
        lines.append(f"YT_ZAPRET_STRATEGY={shq(yt['strategy'])}")
    if yt.get('zapret_ref'):
        lines.append(f"YT_ZAPRET_REF={shq(yt['zapret_ref'])}")
    if yt.get('domains'):
        lines.append(f"YT_DOMAINS={shq(yt['domains'])}")

    # SSO-секция (Authentik / OIDC). Читает стадия 30-frontend. Пустая секция
    # или enabled=false → SSO_ENABLED='false', панель ведёт себя ровно как до
    # появления фичи (форма с логином и паролем).
    sso = cfg.get('sso', {})
    sso_requested = (sso.get('enabled', '') or '').lower() in ('true', 'yes', 'y', '1')
    client_secret = sso.get('client_secret', '') or ''
    sso_on = sso_requested and bool(client_secret.strip())
    # Намерение нужно только для итоговой подсказки деплоя; засев всегда безопасен.
    lines += ['', f"SSO_REQUESTED={shq('true' if sso_requested else 'false')}",
              f"SSO_ENABLED={shq('true' if sso_on else 'false')}"]
    if sso_requested:
        # discovery_url можно задать целиком; иначе собираем из адреса IdP и
        # slug'а приложения — у Authentik ручка ВСЕГДА такая.
        discovery = sso.get('discovery_url', '')
        if not discovery:
            base = (sso.get('base_url', '') or '').rstrip('/')
            slug = sso.get('app_slug', '') or 'vpn2'
            if base:
                discovery = f'{base}/application/o/{slug}/.well-known/openid-configuration'
        client_id = sso.get('client_id', '') or 'vpn2'
        allowed_subs = sso.get('allowed_subs', '')
        # Проверять полноту засева можно только на entry: стадия 30 знает,
        # сохранены ли уже приоритетные настройки в профиле панели.
        lines += [
            f'SSO_DISCOVERY_URL={shq(discovery)}',
            f'SSO_CLIENT_ID={shq(client_id)}',
            f'SSO_CLIENT_SECRET={shq(client_secret)}',
            f'SSO_ALLOWED_SUBS={shq(allowed_subs)}',
            f"SSO_LABEL={shq(sso.get('label', '') or 'Authentik')}",
            # Бесшовность (форму не видно вообще). false — форма остаётся, но с
            # кнопкой входа через IdP.
            f"SSO_AUTO_REDIRECT={shq('false' if not sso_on or (sso.get('auto_redirect', '') or '').lower() in ('false', 'no', 'n', '0') else 'true')}",
            # Парольный вход. Стандарт флота — «спрятан, но жив», поэтому по
            # умолчанию true; false рубит его наглухо (и break-glass тоже!).
            f"SSO_PASSWORD_LOGIN={shq('false' if sso_on and (sso.get('password_login', '') or '').lower() in ('false', 'no', 'n', '0') else 'true')}",
        ]

    # Backup-секция. Выгружаем как BACKUP_* переменные; пустая backup секция /
    # enabled=false → ничего не пишем, 26-backup увидит отсутствие и пропустится.
    backup = cfg.get('backup', {})
    if backup.get('enabled', '').lower() in ('true', 'yes', 'y', '1'):
        lines += [
            '',
            "BACKUP_ENABLED='true'",
            f"BACKUP_BACKEND={shq(backup.get('backend', 'local'))}",
            f"BACKUP_PASSPHRASE={shq(backup.get('passphrase', ''))}",
            f"BACKUP_RETENTION={shq(backup.get('retention', '10'))}",
            f"BACKUP_SCHEDULE={shq(backup.get('schedule', 'daily'))}",
            f"BACKUP_LOCAL_DIR={shq(backup.get('local', {}).get('dir', '/var/backups/anysda-vpn2'))}",
        ]
        s3 = backup.get('s3', {})
        if backup.get('backend') == 's3' and s3:
            lines += [
                f"BACKUP_S3_ENDPOINT={shq(s3.get('endpoint', ''))}",
                f"BACKUP_S3_BUCKET={shq(s3.get('bucket', ''))}",
                # region — обязателен для cloud.ru/yandex/selectel и т.п. (SigV4).
                # Пустое значение оставит дефолт aws-cli (us-east-1).
                f"BACKUP_S3_REGION={shq(s3.get('region', ''))}",
                f"BACKUP_S3_ACCESS_KEY={shq(s3.get('access_key', ''))}",
                f"BACKUP_S3_SECRET_KEY={shq(s3.get('secret_key', ''))}",
            ]

    ru_env = envs_dir / 'ru.env'
    prepare_private(ru_env)
    ru_env.write_text('\n'.join(lines) + '\n')

    # ------------------------------------------------------------------
    # Per-exit env files (октет закреплён за тегом, см. assign_mgmt_octets).
    # ------------------------------------------------------------------
    for ex in exits:
        tag      = ex['tag']
        host     = ex['host']
        password = ex.get('password', '')
        mgmt_ip  = f"10.99.0.{ex['_mgmt_idx']}"
        hy2_port = ex.get('hy2_direct_port', hy2_direct)

        if not password:
            print(f'ERROR: exits[{tag}].password не задан — перезапусти ./setup.sh',
                  file=sys.stderr)
            sys.exit(1)

        lines = [
            f"HOST_TAG={shq(tag)}",
            '',
            "SSH_USER='root'",
            f"SSH_HOST={shq(host)}",
            f"SSH_PASS={shq(password)}",
            '',
            f"PUB_IP={shq(host)}",
            '',
            f"MGMT_IP={shq(mgmt_ip)}",
            f"HY2_UP_MBPS={shq(ex.get('hy2_up_mbps', ''))}",
            f"HY2_DOWN_MBPS={shq(ex.get('hy2_down_mbps', ''))}",
        ]
        if str(hy2_port) != str(hy2_direct):
            lines.append(f'HY2_DIRECT_PORT={hy2_port}')

        ex_env = envs_dir / f'{tag}.env'
        prepare_private(ex_env)
        ex_env.write_text('\n'.join(lines) + '\n')

    # ------------------------------------------------------------------
    # exits.env
    # ------------------------------------------------------------------
    (envs_dir / 'exits.env').write_text(f'EXIT_TAGS={shq(exit_tags)}\n')

    print(f'Сгенерировано: all.env, ru.env, '
          f'{", ".join(e["tag"] + ".env" for e in exits)}, exits.env')


if __name__ == '__main__':
    main()
