#!/opt/vpn2/venv/bin/python3
"""Динамический инвентарь ansible из config.yaml: entry (ru) и экзиты по тегам."""
import json
import os
import sys
from pathlib import Path

from vpn2.config import STATE_DIR, Config, ConfigError, load


def inventory(cfg: Config, state: Path) -> dict:
    """Инвентарь в формате ansible --list; те же опции ssh, что у bash-деплоя."""
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
            'ansible_password': password,
            'ansible_ssh_private_key_file': '~/.ssh/id_ed25519',
            'ansible_ssh_common_args': common_args,
        }

    meta = {'ru': hostvars(cfg.entry.host, cfg.entry.password)}
    for node in cfg.exits:
        meta[node.tag] = hostvars(node.host, node.password)
    return {
        '_meta': {'hostvars': meta},
        'all': {'children': ['entry', 'exits']},
        'entry': {'hosts': ['ru']},
        'exits': {'hosts': [node.tag for node in cfg.exits]},
    }


def main(argv):
    if argv[:1] == ['--list']:
        infra = Path(__file__).resolve().parents[1]
        path = os.environ.get('VPN2_CONFIG') or str(infra.parent / 'config.yaml')
        try:
            cfg = load(path)
        except (OSError, ConfigError) as error:
            print(f'inventory.py: {error}', file=sys.stderr)
            return 1
        print(json.dumps(inventory(cfg, STATE_DIR), indent=2, sort_keys=True))
        return 0
    if len(argv) == 2 and argv[0] == '--host':
        print('{}')
        return 0
    print('usage: inventory.py --list | --host <name>', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
