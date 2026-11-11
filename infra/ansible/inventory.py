#!/opt/vpn2/venv/bin/python3
"""Динамический инвентарь ansible из config.yaml: entry (ru) и экзиты по тегам.

Кроме подключения, каждому узлу отдаются его переменные деплоя:
  vpn2_env   - то же окружение, что bash получает из all.env и <узел>.env:
               имена и строки один в один, чего нет в env - нет и здесь.
               Роль читает `vpn2_env.ИМЯ | default('')` вместо lookup('env').
  vpn2_node  - кто этот узел: tag, role (entry|exit), host, mgmt_ip.
  vpn2_exits - экзиты деплоя списком: tag, env_tag, host, mgmt_ip, hy2_direct_port (число),
               hy2_up_mbps, hy2_down_mbps (число или null), hy2_insecure (логическое).
Секреты лежат в vpn2_env открыто: ansible-playbook они нужны. Под ansible-inventory
(его вывод попадает в логи) и при VPN2_INVENTORY_SECRETS=hide значения закрыты.
"""
import json
import os
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

    entry - имена, которые entry задаёт конфиг: deploy.sh держит их в своём окружении (load_env ru)
    и с ним же запускает плеи экзитов. Экзиту они не нужны и среди них секреты entry.
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
        # Такой конфиг деплой отвергает раньше (config2env.py); узлы остаются видны, переменных нет.
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
    if argv[:1] == ['--list']:
        infra = Path(__file__).resolve().parents[1]
        path = os.environ.get('VPN2_CONFIG') or str(infra.parent / 'config.yaml')
        try:
            cfg = load(path)
        except (OSError, ConfigError) as error:
            print(f'inventory.py: {error}', file=sys.stderr)
            return 1
        mode = os.environ.get('VPN2_INVENTORY_SECRETS', '')
        hide = mode == 'hide' or (mode != 'show' and asked_by_ansible_inventory())
        print(json.dumps(inventory(cfg, STATE_DIR, repo_root=infra.parent, hide_secrets=hide),
                         indent=2, sort_keys=True))
        return 0
    if len(argv) == 2 and argv[0] == '--host':
        print('{}')
        return 0
    print('usage: inventory.py --list | --host <name>', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
