"""VPN-серверы entry для тестов: скрипты маршрутизации ролей и шаги пакета vpn2."""
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROLES = {'28-wireguard': ('wireguard', 'anysda-wg-routing.sh'), '29-openvpn': ('openvpn', 'anysda-ovpn-routing.sh')}


def routing(stage):
    """Скрипт маршрутизации протокола: файл роли, у стадии - её heredoc."""
    if stage in ROLES:
        role, name = ROLES[stage]
        return (ROOT / 'infra/ansible/roles' / role / 'files' / name).read_text()
    text = (ROOT / 'infra/scripts' / f'{stage}.sh').read_text()
    return text.split("<<'IPTSEOF'\n", 1)[1].split('\nIPTSEOF', 1)[0]


def package(name):
    """Модуль пакета vpn2, например package('entryvpn')."""
    import importlib
    src = str(ROOT / 'infra/vpn2/src')
    if src not in sys.path:
        sys.path.insert(0, src)
    return importlib.import_module(f'vpn2.{name}')


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


def ovpn_pki(target, *, env=None):
    """Шаг `ovpn-pki` роли openvpn: комплект PKI в каталоге target."""
    code = ('import sys\nfrom vpn2 import entryvpn, pki\n'
            'try:\n    entryvpn.ovpn_pki(target=sys.argv[1], dirs=())\n'
            'except pki.PkiError as error:\n    sys.exit(str(error))\n')
    env = dict(env or os.environ, PYTHONPATH=str(ROOT / 'infra/vpn2/src'))
    return subprocess.run([sys.executable, '-c', code, str(target)], env=env, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, check=False)


OVPN_STAGE = '''import subprocess
import sys
from vpn2 import entryvpn
from vpn2.cli import host
root, applied, apply, port = sys.argv[1:]
directory = root + entryvpn.OVPN_DIR
try:
    entryvpn.ovpn_write_conf(port, 'udp', '10.67.67.0', ['10.67.67.0/24'], directory=directory,
                             marker=root + entryvpn.OVPN_API_MANAGED)
    host.ovpn_restart(directory=directory, applied=applied, apply=apply)
except (RuntimeError, subprocess.CalledProcessError) as error:
    sys.exit(str(error))
'''


def ovpn_stage(root, applied, apply, port=1194):
    """Строка bash: шаги `ovpn-server-conf` и `ovpn-restart` роли openvpn на дереве root.

    systemctl и vpn2-apply берутся подставные: первый из PATH, второй путём apply.
    """
    return shlex.join(['env', f'PYTHONPATH={ROOT / "infra/vpn2/src"}', sys.executable, '-B', '-c', OVPN_STAGE,
                       str(root), str(applied), str(apply), str(port)]) + '\n'


def ikev2_pki(target, *, env=None):
    """Свой CA и сертификат сервера роли ikev2 в каталоге target, как шаг `ikev2-server`."""
    code = ('import sys\nfrom vpn2 import pki\n'
            'try:\n    with pki.transaction(sys.argv[1]) as work:\n'
            '        pki.build_ikev2(work, "203.0.113.10", "203.0.113.10", say=print)\n'
            'except pki.PkiError as error:\n    sys.exit(str(error))\n')
    env = dict(env or os.environ, PYTHONPATH=str(ROOT / 'infra/vpn2/src'))
    return subprocess.run([sys.executable, '-c', code, str(target)], env=env, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, check=False)


def ikev2_routing(wan='wan', subnets=('10.68.68.0/24',), subnet6='fd68:68::/112'):
    """Скрипт маршрутизации роли ikev2: шаблон с теми же подстановками, что делает ansible."""
    text = (ROOT / 'infra/ansible/roles/ikev2/templates/anysda-ikev2-routing.sh.j2').read_text()
    values = {"{{ ikev2_wan_if | quote }}": shlex.quote(wan),
              "{{ ikev2_subnets | map('quote') | join('\\\\ ') }}": '\\ '.join(shlex.quote(net) for net in subnets),
              "{{ ikev2_subnet6 | quote }}": shlex.quote(subnet6)}
    for mark, value in values.items():
        if mark not in text:
            raise AssertionError(f'в шаблоне нет {mark}')
        text = text.replace(mark, value)
    if '{{' in text or '{%' in text:
        raise AssertionError('в шаблоне осталась неизвестная подстановка')
    return text


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
