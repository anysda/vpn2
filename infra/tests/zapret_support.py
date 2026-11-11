"""Роль zapret в тестах: модуль шагов, файлы роли и шаблоны без jinja2."""

import os
import re
from pathlib import Path
import sys

import yaml

ROOT = Path(os.environ.get('INFRA_TEST_ROOT', Path(__file__).resolve().parents[2]))
ROLE = ROOT / 'infra/ansible/roles/zapret'
sys.path.insert(0, str(ROOT / 'infra/vpn2/src'))
from vpn2.zapret import stage  # noqa: E402

VALUES = {'mark': '256', 'queue': '200', 'base': '/opt/zapret2', 'bin': '/opt/zapret2/binaries/my/nfqws2',
          'strategy': '--lua-desync=wssize:wsize=1:scale=1 ' + stage.SPLIT, 'lua_init': ' --lua-init=@/x.lua',
          'wan': 'eth0', 'offload': 'keep', 'diag_uid': 42, 'diag_gid': 42, 'window_rules': ''}


def role_file(name):
    return (ROLE / 'files' / name).read_text()


def render(name, **values):
    """Шаблон роли с подставленными значениями; в шаблонах только {{ zapret_v.ИМЯ }}."""
    known = {**VALUES, **values}
    known.update(stage.shell_values(known))
    text = (ROLE / 'templates' / f'{name}.j2').read_text()
    text = re.sub(r'\{\{ zapret_v\.(\w+) \}\}', lambda match: str(known[match[1]]), text)
    assert '{{' not in text and '{%' not in text, name
    return text


def tasks():
    """Задачи роли подряд: общий файл, затем включение десинка, блоки раскрыты."""
    found = []

    def walk(items):
        for task in items:
            found.append(task)
            walk(task.get('block', []))

    for name in ('main.yml', 'desync.yml'):
        walk(yaml.safe_load((ROLE / 'tasks' / name).read_text()))
    return found
