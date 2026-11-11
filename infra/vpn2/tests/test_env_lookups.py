"""Роли уходят с lookup('env') на hostvars: число обращений не растёт, каждое имя есть в инвентаре."""
import re

from nodevars_support import ROOT
from vpn2 import nodevars

ANSIBLE = ROOT / 'infra/ansible'
# Столько обращений к окружению осталось в ролях. Число только уменьшают: убрал lookup - снизь его.
LOOKUPS_LEFT = 115
CALL = re.compile(r"(?:lookup|query)\(\s*'(?:ansible\.builtin\.)?env'\s*,\s*([^)]*)\)")
NAME = r'[A-Z][A-Z0-9_]*'
LIST_KEY = re.compile(r'^([a-z0-9_]+_env_(?:names|common|exit_prefix|exit_suffix)):\s*(.*)$')


def sources():
    return [path for path in sorted(ANSIBLE.rglob('*')) if path.is_file() and path.suffix in ('.yml', '.yaml', '.j2')]


def calls():
    return [(path, match[1]) for path in sources() for match in CALL.finditer(path.read_text(encoding='utf-8'))]


def listed():
    """Списки имён, которые роли читают одним query: {ключ defaults: [имена]}."""
    out = {}
    for path in sorted(ANSIBLE.glob('roles/*/defaults/main.yml')):
        current = None
        for line in path.read_text(encoding='utf-8').splitlines():
            head = LIST_KEY.match(line)
            if head:
                current = head[1]
                out.setdefault(current, []).extend(re.findall(NAME, head[2]) if head[2].startswith('[') else [])
                continue
            item = re.match(rf'^\s+-\s+({NAME})\s*$', line)
            if current and item:
                out[current].append(item[1])
            elif line.strip() and not line.startswith((' ', '#')):
                current = None
    return out


def test_lookup_count_does_not_grow():
    found = len(calls())
    assert found <= LOOKUPS_LEFT, (
        f'обращений к окружению в infra/ansible стало {found}, было {LOOKUPS_LEFT}: '
        "новое значение бери из hostvars (vpn2_env.ИМЯ | default('')), а не через lookup('env')")
    assert found == LOOKUPS_LEFT, f'обращений стало меньше ({found}): снизь LOOKUPS_LEFT в этом тесте'


def test_counter_sees_every_spelling():
    text = ("{{ lookup('env', 'A') }} {{ lookup('ansible.builtin.env', 'B') }} {{ query('ansible.builtin.env', *names) }}"
            " {{ lookup( 'env' , 'C', default='') }} {{ lookup('ansible.builtin.file', 'D') }}")
    assert [match[1] for match in CALL.finditer(text)] == ["'A'", "'B'", '*names', "'C', default=''"]


def test_every_name_roles_read_is_in_hostvars():
    lists = listed()
    assert {'adguard_env_names', 'containers_env_common', 'router_env_exit_suffix', 'verify_env_names',
            'zapret_env_names'} <= set(lists), 'списки имён ролей не найдены: разбор defaults устарел'
    plain = {name for _, args in calls() for name in re.findall(rf"'({NAME})'", args)}
    for key, names in lists.items():
        if key.endswith('_exit_prefix'):
            assert set(names) <= set(nodevars.EXIT_PREFIXES), key
        elif key.endswith('_exit_suffix'):
            assert set(names) <= set(nodevars.EXIT_SUFFIXES), key
        else:
            plain |= set(names)
    assert len(plain) >= 90
    # <ТЕГ>_HY2_INSECURE verify собирает сам: имя с тегом экзита.
    unknown = plain - nodevars.CONFIG_NAMES - nodevars.RUN_NAMES
    assert unknown == set(), f'этих имён в hostvars (vpn2_env) нет, добавь в vpn2.nodevars: {sorted(unknown)}'
