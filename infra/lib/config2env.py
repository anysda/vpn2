#!/usr/bin/env python3
"""config.yaml → infra/envs/*.env и закреплённые октеты служебной сети.

Разбор, проверку и переменные узлов считает пакет vpn2 (config, nodevars): те же значения
ansible получает через inventory.py. Файлы env читает восстановление из бэкапа и оператор.

    config2env.py <корень>                           записать envs
    config2env.py <корень> --set-port <тег> <порт>   hy2_direct_port экзита в config.yaml
"""
import base64
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'vpn2/src'))
try:
    import yaml
    from vpn2 import config, nodevars
except ImportError as error:
    sys.exit(f'ERROR: {error}. Нужен пакет python3-yaml: apt install python3-yaml')

normalize_domain = nodevars.normalize_domain


def shq(value):
    """Одна строка присваивания; Bash source вернёт значение байт в байт."""
    return "'" + str(value).replace("'", "'\\''").replace('\n', "'$'\\n''").replace('\r', "'$'\\r''") + "'"


def checked(text, validate=True):
    """Разобранный конфиг; отказ - одной строкой с путём ключа, без значений."""
    try:
        cfg = config.loads(text)
        if validate:
            nodevars.validate(cfg)
    except config.ConfigError as error:
        sys.exit(f'ERROR: {error}')
    return cfg


def parse_yaml(text):
    raw = checked(text, validate=False).raw
    for section in ('entry', 'panel', 'admin', 'alerts', 'entry_resources', 'failover', 'openvpn', 'ports',
                    'sso', 'tgbot', 'wireguard', 'backup', 'telegram'):
        raw.setdefault(section, {})
    return raw


def state_dir():
    return Path(os.environ.get('VPN2_STATE_DIR') or '/var/lib/anysda-vpn2/state')


def materialize_orchestrator_key(raw):
    """Ключ оркестратора из config.yaml в ~/.ssh: переживает пересоздание entry."""
    if not raw.get('orchestrator_key'):
        return
    ssh_dir = Path.home() / '.ssh'
    ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    private = ssh_dir / 'id_ed25519'
    private.touch(mode=0o600, exist_ok=True)
    private.chmod(0o600)
    private.write_bytes(base64.b64decode(raw['orchestrator_key']))
    if raw.get('orchestrator_pubkey'):
        (ssh_dir / 'id_ed25519.pub').write_text(raw['orchestrator_pubkey'].rstrip() + '\n')
    print('orchestrator-ключ восстановлен из config.yaml')


def pin_octets(tags, repo_root):
    """Последний октет MGMT_IP закрепляется за тегом: убрали экзит - адреса остальных те же."""
    path = state_dir() / 'mgmt-ips.txt'
    source = path if path.exists() else repo_root / 'infra/state/mgmt-ips.txt'
    old = dict(line.split() for line in (source.read_text().splitlines() if source.exists() else ())
               if len(line.split()) == 2 and not line.startswith('#'))
    octets = nodevars.mgmt_octets(tags, state=state_dir(), repo_root=repo_root)
    pinned = {**old, **{tag: str(octet) for tag, octet in octets.items()}}
    if pinned == old and path.exists():
        return
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except PermissionError:
        sys.exit(f'config2env: нет прав на каталог состояния деплоя {path.parent}: запускать от root')
    path.write_text('# тег → последний октет MGMT_IP (10.99.0.N), пишет config2env.py.\n'
                    '# Не перенумеровывать: на октет завязаны метки метрик и mon-порт.\n'
                    + ''.join(f'{tag} {octet}\n' for tag, octet in sorted(pinned.items(), key=lambda row: int(row[1]))))


def set_port(path, tag, port):
    """Пишет hy2_direct_port экзита, не трогая остальные строки и комментарии."""
    lines = path.read_text().splitlines()
    root = yaml.compose(config._plain_forms('\n'.join(lines)))
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
        path.write_text('\n'.join(lines) + '\n')
        path.chmod(0o600)
        return print(f'config.yaml: {tag} hy2_direct_port = {port}')
    sys.exit(f'ERROR: тег "{tag}" не найден в config.yaml')


def _blocks(text, pattern):
    matches = list(re.finditer(pattern, text, re.MULTILINE))
    ends = [match.start() for match in matches[1:]] + [len(text)]
    return (text[:matches[0].start()] if matches else text,
            [text[match.start():end] for match, end in zip(matches, ends)])


def merge_setup_config(previous, answers):
    """Обновляет в прежнем config.yaml только ответы мастера; остальные строки и комментарии целы.

    Экзиты сопоставляются по тегу: удалённые в мастере уходят, лишние поля оставшихся остаются.
    """
    previous, answers = config._plain_forms(previous), config._plain_forms(answers)
    old_exits, new_exits = parse_yaml(previous)['exits'], parse_yaml(answers)['exits']
    tags = [item.get('tag') for item in old_exits]
    if any(not tag for tag in tags) or len(tags) != len(set(tags)):
        sys.exit('ERROR: config.yaml: exits: для сохранения настроек нужны уникальные непустые теги')

    def merge(old, new, indent):
        pattern = '^' + ' ' * indent + r'[^ #\n][^:\n]*:'
        prefix, old_parts = _blocks(old, pattern)
        keyed = {part.split(':', 1)[0].strip(): part for part in old_parts}
        for part in _blocks(new, pattern)[1]:
            name = part.split(':', 1)[0].strip()
            if name not in keyed:
                keyed[name] = part
                continue
            (old_head, _, old_body), (new_head, _, new_body) = keyed[name].partition('\n'), part.partition('\n')
            new_value = new_head.partition(':')[2].strip()
            if indent == 0 and name == 'exits':
                lead, old_items = _blocks(old_body, '^  - ')
                by_tag = {item['tag']: text for item, text in zip(old_exits, old_items)}
                merged = [merge(by_tag.get(item['tag'], '').replace('  - ', '    ', 1),
                                text.replace('  - ', '    ', 1), 4).replace('    ', '  - ', 1)
                          for item, text in zip(new_exits, _blocks(new_body, '^  - ')[1])]
                keyed[name] = old_head + '\n' + lead + ''.join(merged)
            elif not new_value or new_value.startswith('#'):
                keyed[name] = old_head + '\n' + merge(old_body, new_body, indent + 2)
            else:
                # Комментарий в строке прежнего значения остаётся; # внутри кавычек - часть значения.
                value = old_head.partition(':')[2].lstrip()
                found = re.fullmatch(r'''(?:'(?:[^']|'')*'|"(?:[^"\\]|\\.)*"|.*?)([ \t]+\#.*)?''', value)
                keyed[name] = new_head + ((' ' + value) if value.startswith('#') else (found[1] or '')) + '\n' + old_body
        return prefix + ''.join(keyed.values())

    result = merge(previous.rstrip() + '\n', answers, 0)
    checked(result)
    return result


def main(argv):
    repo_root = Path(argv[0]).resolve() if argv else Path.cwd()
    path = repo_root / 'config.yaml'
    if not path.exists():
        sys.exit('ERROR: config.yaml не найден. Запусти ./setup.sh')
    if argv[1:2] == ['--set-port']:
        return set_port(path, *argv[2:4])
    cfg = checked(path.read_text())
    materialize_orchestrator_key(cfg.raw)
    pin_octets([node.tag for node in cfg.exits], repo_root)
    skipped = sorted(nodevars.excluded_exits(state_dir(), repo_root) & {node.tag for node in cfg.exits})
    if skipped:
        print(f'config2env: исключены по UDP-фильтру: {", ".join(skipped)} ({state_dir()}/excluded-exits.txt) — '
              'их MGMT_IP-индексы зарезервированы и не переиспользуются')
    development = os.environ.get('VPN2_DEVELOPMENT') == 'true'
    for key in ('panel', 'tgbot'):
        image = cfg.raw.get(key, {}).get('image', '')
        if image and not development:
            print(f'config2env: {key}.image: {image} не используется - образ берётся из манифеста '
                  'сборки по digest; строку можно удалить (нужна только для --development)')
    try:
        nodes = nodevars.env_by_node(cfg, state=state_dir(), repo_root=repo_root, development=development)
    except config.ConfigError as error:
        sys.exit(f'ERROR: {error}')
    envs = repo_root / 'infra/envs'
    envs.mkdir(parents=True, exist_ok=True)
    # all.env - общее для всех узлов; файл узла полон сам по себе и читается после него.
    shared = {name: value for name, value in nodes['ru'].items() if all(name in env for env in nodes.values())}
    tags = [tag for tag in nodes if tag != 'ru']
    files = {'all': shared, **nodes, 'exits': {'EXIT_TAGS': ' '.join(tags)}}
    for name, env in files.items():
        target = envs / f'{name}.env'
        target.touch(mode=0o600, exist_ok=True)
        target.chmod(0o600)
        target.write_text(''.join(f'{key}={value if value.isdigit() else shq(value)}\n' for key, value in env.items()))
    print(f'Сгенерировано: all.env, ru.env, {", ".join(tag + ".env" for tag in tags)}, exits.env')


if __name__ == '__main__':
    main(sys.argv[1:])
