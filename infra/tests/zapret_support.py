"""Роль zapret в тестах: модуль шагов и шаблоны без jinja2."""

import os
import re
from pathlib import Path
import sys

ROOT = Path(os.environ.get('INFRA_TEST_ROOT', Path(__file__).resolve().parents[2]))
ROLE = ROOT / 'infra/ansible/roles/zapret'
sys.path.insert(0, str(ROOT / 'infra/vpn2/src'))
from vpn2.zapret import stage  # noqa: E402

VALUES = {'mark': '256', 'queue': '200', 'base': '/opt/zapret2', 'bin': '/opt/zapret2/binaries/my/nfqws2',
          'strategy': '--lua-desync=wssize:wsize=1:scale=1 ' + stage.SPLIT, 'lua_init': ' --lua-init=@/x.lua',
          'wan': 'eth0', 'offload': 'keep', 'diag_uid': 42, 'diag_gid': 42, 'window_rules': ''}
# Команда vpn2 скрипта проверки в тестах - пакет из дерева.
VPN2_MOCK = f'vpn2() {{ PYTHONPATH={ROOT}/infra/vpn2/src {sys.executable} -m vpn2.cli "$@"; }}\n'


def render(name, **values):
    """Шаблон роли с подставленными значениями; в шаблонах только {{ zapret_v.ИМЯ }}."""
    known = {**VALUES, **values}
    known.update(stage.shell_values(known))
    text = (ROLE / 'templates' / f'{name}.j2').read_text()
    text = re.sub(r'\{\{ zapret_v\.(\w+) \}\}', lambda match: str(known[match[1]]), text)
    assert '{{' not in text and '{%' not in text, name
    return text
