"""Роли уходят с lookup('env') на hostvars: число обращений не растёт, каждое имя есть в инвентаре."""
import re

from nodevars_support import ROOT
from vpn2 import nodevars

ANSIBLE = ROOT / 'infra/ansible'
# Столько обращений к окружению осталось в каждой роли. Число только уменьшают: убрал lookup - снизь
# его, дошёл до нуля - убери строку. Роли стоят группами шага 2: правки групп не задевают друг друга.
LOOKUPS_LEFT = {
}
CALL = re.compile(r"(?:lookup|query)\(\s*'(?:ansible\.builtin\.)?env'\s*,\s*([^)]*)\)")
NAME = r'[A-Z][A-Z0-9_]*'
LIST_KEY = re.compile(r'^([a-z0-9_]+_env_(?:names|common)):\s*(.*)$')
# Другое имя того же словаря: router_env: "{{ vpn2_env }}" в defaults, {%- set env = router_env -%} в шаблоне.
ALIAS = re.compile(r'^([a-z0-9_]+):\s*"\{\{\s*vpn2_env\s*\}\}"\s*$|\{%-?\s*set\s+(\w+)\s*=\s*(\w+)\s*-?%\}', re.M)


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


def aliases():
    found, pairs = {'vpn2_env'}, []
    for path in sources():
        for match in ALIAS.finditer(path.read_text(encoding='utf-8')):
            if match[1]:
                found.add(match[1])
            else:
                pairs.append((match[2], match[3]))
    while new := {name for name, source in pairs if source in found} - found:
        found |= new
    return found


def reads():
    """Имена, которые роли берут из vpn2_env: (простые, приставки экзита, окончания экзита)."""
    plain, prefixes, suffixes = set(), set(), set()
    names = '|'.join(sorted(aliases()))
    for path in sources():
        text = path.read_text(encoding='utf-8')
        plain |= set(re.findall(rf'\b(?:{names})\.({NAME})\b', text))
        for key in re.findall(rf'\b(?:{names})\[([^\]]*)\]', text):
            plain |= set(re.findall(rf"^\s*'({NAME})'\s*$", key))
            prefixes |= set(re.findall(rf"'({NAME})_'\s*~", key))
            suffixes |= set(re.findall(rf"~\s*'_({NAME})'", key))
        # verify собирает <ТЕГ>_HY2_INSECURE из тегов: map('regex_replace', '$', '_HY2_INSECURE').
        suffixes |= set(re.findall(rf"regex_replace',\s*'\$',\s*'_({NAME})'", text))
    return plain, prefixes, suffixes


def owner(path):
    """Роль, которой принадлежит файл; плеи и общие файлы считаются под своим путём."""
    parts = path.relative_to(ANSIBLE).parts
    return parts[1] if parts[0] == 'roles' else '/'.join(parts)


def test_lookup_count_does_not_grow():
    found = {}
    for path, _ in calls():
        found[owner(path)] = found.get(owner(path), 0) + 1
    grown = {name: (LOOKUPS_LEFT.get(name, 0), count) for name, count in found.items()
             if count > LOOKUPS_LEFT.get(name, 0)}
    assert grown == {}, (
        f'обращений к окружению стало больше (было, стало): {grown}. '
        "Новое значение бери из hostvars (vpn2_env.ИМЯ | default('')), а не через lookup('env')")
    assert found == LOOKUPS_LEFT, 'обращений стало меньше: снизь числа LOOKUPS_LEFT в этом тесте'


def test_counter_sees_every_spelling():
    text = ("{{ lookup('env', 'A') }} {{ lookup('ansible.builtin.env', 'B') }} {{ query('ansible.builtin.env', *names) }}"
            " {{ lookup( 'env' , 'C', default='') }} {{ lookup('ansible.builtin.file', 'D') }}")
    assert [match[1] for match in CALL.finditer(text)] == ["'A'", "'B'", '*names', "'C', default=''"]


def test_every_name_roles_read_is_in_hostvars():
    lists = listed()
    assert {'adguard_env_names', 'verify_env_names',
            'zapret_env_names'} <= set(lists), 'списки имён ролей не найдены: разбор defaults устарел'
    plain, prefixes, suffixes = reads()
    assert {'env', 'router_env'} <= aliases(), 'другие имена vpn2_env не найдены: разбор устарел'
    assert {'MGMT_IP'} <= prefixes and {'HY2_UP_MBPS', 'HY2_INSECURE'} <= suffixes, 'имена с тегом не найдены'
    # Приставка без тега экзита (HY2_HOP_ ~ канал) - начало готовых имён в hostvars.
    known = nodevars.CONFIG_NAMES | nodevars.RUN_NAMES
    stray = {p for p in prefixes - set(nodevars.EXIT_PREFIXES) if not any(n.startswith(f'{p}_') for n in known)}
    assert stray == set(), sorted(stray)
    assert suffixes <= set(nodevars.EXIT_SUFFIXES), sorted(suffixes - set(nodevars.EXIT_SUFFIXES))
    plain |= {name for _, args in calls() for name in re.findall(rf"'({NAME})'", args)}
    for names in lists.values():
        plain |= set(names)
    assert len(plain) >= 75
    unknown = plain - known
    assert unknown == set(), f'этих имён в hostvars (vpn2_env) нет, добавь в vpn2.nodevars: {sorted(unknown)}'
