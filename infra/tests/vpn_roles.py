"""VPN-серверы entry для тестов: скрипты маршрутизации ролей и шаги пакета vpn2."""
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROLES = ROOT / 'infra/ansible/roles'
# Окружение юнитов маршрутизации, как его пишут роли.
TUNNELS = {
    '28-wireguard': {'IFACE': 'wg0', 'CHAIN': 'ANYSDA_WG_TPROXY', 'MSS': '1380', 'V6_REJECT': '1'},
    '29-openvpn': {'IFACE': 'tun0', 'CHAIN': 'ANYSDA_OVPN_TPROXY', 'MSS': '1340'},
    '27-ikev2': {'IFACE': 'xfrm0', 'CHAIN': 'ANYSDA_IKEV2_TPROXY', 'MSS': '1360', 'V6_REJECT': '1',
                 'XFRM_DEV': 'wan', 'SUBNET6': 'fd68:68::/112', 'ROUTES': '10.68.68.0/24'},
}
PLAY = """- hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - ansible.builtin.include_vars: {defaults}
    - ansible.builtin.template: {{src: {src}, dest: {out}}}
"""


def ansible_playbook():
    """ansible-playbook из PATH, иначе из venv тестов на пинах деплоя."""
    found = shutil.which('ansible-playbook')
    if found:
        return found
    pins = ROOT / 'infra/ansible/constraints.txt'
    cache = Path.home() / '.cache/vpn2-tests'
    venv = cache / f'ansible-{hashlib.sha256(pins.read_bytes()).hexdigest()[:12]}'
    cache.mkdir(parents=True, exist_ok=True)
    with open(cache / 'ansible.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Venv строится на своём месте: скрипты venv помнят путь интерпретатора в shebang.
        # Оборванная установка не оставляет ansible-playbook, следующий запуск строит заново.
        if not (venv / 'bin/ansible-playbook').exists():
            shutil.rmtree(venv, ignore_errors=True)
            subprocess.run([sys.executable, '-m', 'venv', str(venv)], check=True)
            subprocess.run([f'{venv}/bin/pip', 'install', '-q', '-c', str(pins), 'ansible-core', 'mitogen'], check=True)
    return str(venv / 'bin/ansible-playbook')


def routing(stage, **values):
    """Общий скрипт маршрутизации с окружением юнита роли; values - замены, routes='...'."""
    env = {**TUNNELS[stage], **{name.upper(): value for name, value in values.items()}}
    head = ''.join(f'{name}={shlex.quote(value)}\n' for name, value in env.items())
    text = (ROLES / 'wireguard/files/anysda-vpn-routing.sh').read_text()
    return text.replace('set -euo pipefail\n', 'set -euo pipefail\n' + head, 1)


def template(role, src, variables):
    """Шаблон роли с её defaults и переменными variables, как его отрисует ansible."""
    with tempfile.TemporaryDirectory() as work:
        work = Path(work)
        (work / 'vars.json').write_text(json.dumps(variables))
        (work / 'play.yml').write_text(PLAY.format(defaults=ROLES / role / 'defaults/main.yml',
                                                   src=ROLES / role / 'templates' / src, out=work / 'out'))
        done = subprocess.run([ansible_playbook(), '-i', 'localhost,', str(work / 'play.yml'),
                               '-e', '@' + str(work / 'vars.json')], text=True, capture_output=True, check=False)
        if done.returncode:
            raise AssertionError(done.stdout + done.stderr)
        return (work / 'out').read_text()


def server_conf(port='1194', proto='udp', subnet='10.67.67.0', subnets=('10.67.67.0/24',), managed=False):
    """server.conf роли openvpn."""
    return template('openvpn', 'server.conf.j2', {
        'vpn2_env': {}, 'openvpn_infra': str(ROOT / 'infra'), 'openvpn_port': str(port), 'openvpn_proto': proto,
        'openvpn_subnet': subnet, 'openvpn_subnets': list(subnets), 'openvpn_api_managed': {'stat': {'exists': managed}}})


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
root, applied, apply = sys.argv[1:]
try:
    host.ovpn_restart(directory=root + entryvpn.OVPN_DIR, applied=applied, apply=apply)
except (RuntimeError, subprocess.CalledProcessError) as error:
    sys.exit(str(error))
'''


def ovpn_stage(root, applied, apply, port=1194):
    """Строка bash: server.conf роли openvpn в дереве root уже записан, шаг `ovpn-restart`.

    systemctl и vpn2-apply берутся подставные: первый из PATH, второй путём apply.
    """
    directory = Path(str(root) + '/etc/openvpn/server')
    directory.mkdir(parents=True, exist_ok=True)
    managed = Path(str(root) + '/etc/vpn2-control/api-managed').is_file()
    text = server_conf(port, managed=managed).replace('/etc/openvpn/server', str(directory))
    (directory / 'server.conf').write_text(text)
    return shlex.join(['env', f'PYTHONPATH={ROOT / "infra/vpn2/src"}', sys.executable, '-B', '-c', OVPN_STAGE,
                       str(root), str(applied), str(apply)]) + '\n'


def ikev2_pki(target, *, env=None):
    """Свой CA и сертификат сервера роли ikev2 в каталоге target, как шаг `ikev2-server`."""
    code = ('import sys\nfrom vpn2 import pki\n'
            'try:\n    with pki.transaction(sys.argv[1]) as work:\n'
            '        pki.build_ikev2(work, "203.0.113.10", "203.0.113.10", say=print)\n'
            'except pki.PkiError as error:\n    sys.exit(str(error))\n')
    env = dict(env or os.environ, PYTHONPATH=str(ROOT / 'infra/vpn2/src'))
    return subprocess.run([sys.executable, '-c', code, str(target)], env=env, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, check=False)


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
