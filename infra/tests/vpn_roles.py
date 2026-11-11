"""VPN-серверы entry для тестов: скрипты маршрутизации ролей и шаги пакета vpn2."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROLES = {'28-wireguard': ('wireguard', 'anysda-wg-routing.sh')}


def routing(stage):
    """Скрипт маршрутизации протокола: файл роли, у стадии - её heredoc."""
    if stage in ROLES:
        role, name = ROLES[stage]
        return (ROOT / 'infra/ansible/roles' / role / 'files' / name).read_text()
    text = (ROOT / 'infra/scripts' / f'{stage}.sh').read_text()
    return text.split("<<'IPTSEOF'\n", 1)[1].split('\nIPTSEOF', 1)[0]


def role_tasks(role):
    return (ROOT / 'infra/ansible/roles' / role / 'tasks/main.yml').read_text()


def wg_identity(root, *, env=None, stub=None):
    """Шаг `wg-identity` роли wireguard на каталоге root; stub - тело функции wg вместо настоящего wg."""
    code = ('import sys\nfrom vpn2 import entryvpn, pki\n'
            'try:\n    entryvpn.wg_identity("51820", "10.66.66.1", root=sys.argv[1])\n'
            'except pki.PkiError as error:\n    sys.exit(str(error))\n')
    env = dict(env or os.environ, PYTHONPATH=str(ROOT / 'infra/vpn2/src'))
    with tempfile.TemporaryDirectory() as directory:
        if stub:
            fake = Path(directory) / 'wg'
            fake.write_text('#!/bin/bash\n' + stub + 'wg "$@"\n')
            fake.chmod(0o755)
            env['PATH'] = f'{directory}:{env["PATH"]}'
        return subprocess.run([sys.executable, '-c', code, str(root)], env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False)


def role_defaults(role):
    return (ROOT / 'infra/ansible/roles' / role / 'defaults/main.yml').read_text()


def ufw_groups(role, values):
    """Группы ufw из defaults роли; values - значения переменных вида `{{ имя }}`."""
    import re
    import yaml

    def fill(node):
        if isinstance(node, dict):
            return {key: fill(value) for key, value in node.items()}
        if isinstance(node, list):
            return [fill(value) for value in node]
        whole = re.fullmatch(r'\{\{ (\w+) \}\}', node)
        return values[whole[1]] if whole else re.sub(r'\{\{ (\w+) \}\}', lambda m: str(values[m[1]]), node)
    return fill(yaml.safe_load(role_defaults(role))[f'{role}_ufw'])


def ufw_apply(groups, rules, fake, *, times=2):
    """Шаг `ufw --only-rules` поверх правил rules с подставным ufw: правила после times прогонов."""
    import json
    with tempfile.TemporaryDirectory() as directory:
        state = Path(directory, 'rules.json')
        state.write_text(json.dumps(rules))
        ufw = Path(directory, 'ufw')
        ufw.write_text(fake)
        ufw.chmod(0o755)
        env = dict(os.environ, PATH=f'{directory}:{os.environ["PATH"]}', UFW_STATE=str(state),
                   PYTHONPATH=str(ROOT / 'infra/vpn2/src'))
        for _ in range(times):
            subprocess.run([sys.executable, '-B', '-m', 'vpn2.cli.host', 'ufw', '--only-rules', json.dumps(groups)],
                           env=env, check=True, capture_output=True, text=True, timeout=30)
        return json.loads(state.read_text())
