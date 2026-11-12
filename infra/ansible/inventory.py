#!/opt/vpn2/venv/bin/python3
"""Динамический инвентарь ansible из config.yaml: entry (ru) и экзиты по тегам.

Кроме подключения, каждому узлу отдаются его переменные деплоя:
  vpn2_env   - окружение узла строками; роль читает `vpn2_env.ИМЯ | default('')`.
  vpn2_node  - кто этот узел: tag, role (entry|exit), host, mgmt_ip.
  vpn2_exits - экзиты деплоя списком: tag, env_tag, host, mgmt_ip, hy2_direct_port (число),
               hy2_up_mbps, hy2_down_mbps (число или null), hy2_insecure (логическое).
Секреты лежат в vpn2_env открыто: ansible-playbook они нужны. Под ansible-inventory
(его вывод попадает в логи) и при VPN2_INVENTORY_SECRETS=hide значения закрыты.
"""
import base64
import json
import os
import shlex
import sys
from pathlib import Path

from vpn2 import nodevars
from vpn2.config import STATE_DIR, Config, ConfigError, load

HIDDEN = '***'
TEMPLATE_MARKS = ('{{', '{%', '{#')


def raw(value):
    """Строки из конфига ansible шаблонизирует; значение с разметкой jinja отдаём как есть."""
    if isinstance(value, dict):
        return {name: raw(item) for name, item in value.items()}
    if isinstance(value, list):
        return [raw(item) for item in value]
    if isinstance(value, str) and any(mark in value for mark in TEMPLATE_MARKS):
        return {'__ansible_unsafe': value}
    return value


def passed_through(environ, tags, entry=()):
    """Переменные запуска, которые оператор задаёт ролям мимо config.yaml (его значения сильнее).

    entry - имена, что конфиг задаёт entry: экзиту их не отдаём, среди них секреты entry.
    """
    names = nodevars.RUN_NAMES | nodevars.CONFIG_NAMES | nodevars.exit_names(tags)
    return {name: environ[name] for name in names if name in environ and name not in entry}


def exits_list(env):
    """Типизированный список экзитов из общих переменных узла."""
    def number(value):
        return int(value) if value else None

    out = []
    for tag in env['EXIT_TAGS'].split():
        name = nodevars.env_tag(tag)
        out.append({'tag': tag, 'env_tag': name, 'host': env[f'DOMAIN_{name}'], 'mgmt_ip': env[f'MGMT_IP_{name}'],
                    'hy2_direct_port': int(env[f'{name}_HY2_DIRECT_PORT']),
                    'hy2_up_mbps': number(env[f'{name}_HY2_UP_MBPS']),
                    'hy2_down_mbps': number(env[f'{name}_HY2_DOWN_MBPS']),
                    'hy2_insecure': env.get(f'{name}_HY2_INSECURE') == '1'})
    return out


def inventory(cfg: Config, state: Path, *, repo_root=None, environ=None, hide_secrets=False) -> dict:
    """Инвентарь в формате ansible --list; те же опции ssh, что у bash-деплоя."""
    environ = os.environ if environ is None else environ
    common_args = ' '.join((
        f'-o UserKnownHostsFile={state}/known_hosts',
        '-o StrictHostKeyChecking=accept-new',
        '-o PreferredAuthentications=publickey,password',
        '-o ConnectTimeout=10',
    ))

    def hostvars(host, password):
        return {
            'ansible_host': host,
            'ansible_user': 'root',
            'ansible_password': HIDDEN if hide_secrets else password,
            'ansible_ssh_private_key_file': '~/.ssh/id_ed25519',
            'ansible_ssh_common_args': common_args,
        }

    meta = {'ru': hostvars(cfg.entry.host, cfg.entry.password)}
    for node in cfg.exits:
        meta[node.tag] = hostvars(node.host, node.password)
    result = {
        '_meta': {'hostvars': meta},
        'all': {'children': ['entry', 'exits']},
        'entry': {'hosts': ['ru']},
        'exits': {'hosts': [node.tag for node in cfg.exits]},
    }
    try:
        nodes = nodevars.env_by_node(cfg, state=state, repo_root=repo_root,
                                     development=environ.get('VPN2_DEVELOPMENT') == 'true')
    except ConfigError as error:
        # Такой конфиг деплой отвергает раньше (--prepare); узлы остаются видны, переменных нет.
        result['all']['vars'] = {'vpn2_env_error': error.key}
        return raw(result)
    tags = [tag for tag in nodes if tag != 'ru']
    for tag, env in nodes.items():
        env = {**passed_through(environ, tags, () if tag == 'ru' else nodes['ru']), **env}
        if tag == 'ru':
            env.pop('VPN2_WHEEL', None)  # wheel нужен узлу без исходников, entry ставит пакет из дерева
        if environ.get('VPN2_PRESERVE_ADMIN') == '1':
            env.pop('ADMIN_PASSWORD', None)
        exits = exits_list(env)
        if hide_secrets:
            env = {name: HIDDEN if name in nodevars.SECRET_NAMES and value else value for name, value in env.items()}
        meta[tag].update(vpn2_env=env, vpn2_exits=exits, vpn2_node={
            'tag': tag, 'role': 'entry' if tag == 'ru' else 'exit', 'host': env['SSH_HOST'], 'mgmt_ip': env['MGMT_IP']})
    return raw(result)


def prepare(cfg: Config, state: Path, repo_root: Path):
    """Перед деплоем: ключ оркестратора из config.yaml в ~/.ssh, октеты mesh закреплены за тегами."""
    nodevars.env_by_node(cfg, state=state, repo_root=repo_root)
    if cfg.raw.get('orchestrator_key'):
        ssh_dir = Path.home() / '.ssh'
        ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        private = ssh_dir / 'id_ed25519'
        private.touch(mode=0o600, exist_ok=True)
        private.chmod(0o600)
        private.write_bytes(base64.b64decode(cfg.raw['orchestrator_key']))
        if cfg.raw.get('orchestrator_pubkey'):
            (ssh_dir / 'id_ed25519.pub').write_text(cfg.raw['orchestrator_pubkey'].rstrip() + '\n')
        print('orchestrator-ключ восстановлен из config.yaml')
    tags = [node.tag for node in cfg.exits]
    path = state / 'mgmt-ips.txt'
    source = path if path.exists() else repo_root / 'infra/state/mgmt-ips.txt'
    old = dict(line.split() for line in (source.read_text().splitlines() if source.exists() else ())
               if len(line.split()) == 2 and not line.startswith('#'))
    pinned = {**old, **{tag: str(octet) for tag, octet in nodevars.mgmt_octets(tags, state=state, repo_root=repo_root).items()}}
    if pinned != old or not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text('# тег → последний октет MGMT_IP (10.99.0.N); номера не меняются: на них метки метрик и mon-порт.\n'
                        + ''.join(f'{tag} {octet}\n' for tag, octet in sorted(pinned.items(), key=lambda row: int(row[1]))))
    skipped = sorted(nodevars.excluded_exits(state, repo_root) & set(tags))
    if skipped:
        print(f'исключены по UDP-фильтру: {", ".join(skipped)} ({state}/excluded-exits.txt) — '
              'их MGMT_IP-индексы зарезервированы и не переиспользуются')


def set_port(path: Path, tag, port):
    """hy2_direct_port экзита в config.yaml; остальные строки и комментарии не трогаются."""
    import yaml
    from vpn2.config import _plain_forms
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
        path.write_text('\n'.join(lines) + '\n')
        path.chmod(0o600)
        return print(f'config.yaml: {tag} hy2_direct_port = {port}')
    sys.exit(f'ERROR: тег "{tag}" не найден в config.yaml')


def ancestors():
    """Командные строки процессов-предков, от ближайшего."""
    pid = os.getppid()
    while pid > 1:
        try:
            words = [os.fsdecode(word) for word in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0') if word]
            pid = int(Path(f'/proc/{pid}/stat').read_text(encoding='utf-8', errors='replace').rpartition(')')[2].split()[1])
        except (OSError, ValueError, IndexError):
            return
        yield words


def asked_by_ansible_inventory():
    """Инвентарь спросил ansible-inventory: как бы его ни запустили - файлом, обёрткой или модулем."""
    for words in ancestors():
        names = [Path(word).name for word in words]
        if 'ansible-inventory' in names or 'ansible.cli.inventory' in names:
            return True
        if any(pair == ('ansible', 'inventory') for pair in zip(names, names[1:])):
            return True
    return False


def main(argv):
    path = Path(os.environ.get('VPN2_CONFIG') or Path(__file__).resolve().parents[2] / 'config.yaml')
    repo = path.parent
    if argv[:1] in (['--prepare'], ['--env'], ['--set-port']):
        try:
            if argv[0] == '--set-port':
                return set_port(path, *argv[1:3])
            cfg = load(path)
            if argv[0] == '--prepare':
                return prepare(cfg, STATE_DIR, repo)
            found = inventory(cfg, STATE_DIR, repo_root=repo)
            if 'vpn2_env_error' in found['all'].get('vars', {}):
                nodevars.validate(cfg)
            if argv[1:] and argv[1] not in found['_meta']['hostvars']:
                sys.exit(f'ERROR: узла {argv[1]} нет в config.yaml')
            env = found['_meta']['hostvars'][argv[1]]['vpn2_env']
        except (OSError, ConfigError) as error:
            sys.exit(f'ERROR: {error}')
        for name, value in env.items():
            print(f'{name}={shlex.quote(value.get("__ansible_unsafe", "") if isinstance(value, dict) else value)}')
        return 0
    if argv[:1] == ['--list']:
        try:
            cfg = load(path)
        except (OSError, ConfigError) as error:
            print(f'inventory.py: {error}', file=sys.stderr)
            return 1
        mode = os.environ.get('VPN2_INVENTORY_SECRETS', '')
        hide = mode == 'hide' or (mode != 'show' and asked_by_ansible_inventory())
        print(json.dumps(inventory(cfg, STATE_DIR, repo_root=repo, hide_secrets=hide),
                         indent=2, sort_keys=True))
        return 0
    if len(argv) == 2 and argv[0] == '--host':
        print('{}')
        return 0
    print('usage: inventory.py --list | --host <name> | --prepare | --env <node> | --set-port <tag> <port>', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
