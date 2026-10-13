#!/usr/bin/env python3
"""
config-set-port.py — записывает hy2_direct_port для указанного exit-тега в config.yaml.

Использование: config-set-port.py <repo_root> <tag> <port>

Если для тега уже есть hy2_direct_port — обновляет, иначе дописывает в конец его записи.
Сохраняет форматирование, комментарии, прочие поля.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config2env import normalize_yaml, unquote_scalar  # noqa: E402


def scalar(value):
    value = value.strip()
    if value[:1] in ('"', "'"):
        return unquote_scalar(value)
    return '' if value.startswith('#') else re.sub(r'\s+#.*$', '', value)


def exit_items(lines):
    """Диапазоны строк [начало, конец) каждого элемента списка exits."""
    items, inside = [], False
    for index, line in enumerate(lines):
        content = line.lstrip(' ')
        if not content or content.startswith('#'):
            continue
        indent = len(line) - len(content)
        if indent == 0:
            inside = content.startswith('exits:')
            if items and items[-1][1] is None:
                items[-1][1] = index
        elif inside and indent == 2 and content.startswith('- '):
            if items and items[-1][1] is None:
                items[-1][1] = index
            items.append([index, None])
    if items and items[-1][1] is None:
        items[-1][1] = len(lines)
    return [tuple(item) for item in items]


def main():
    if len(sys.argv) != 4:
        print('usage: config-set-port.py <repo_root> <tag> <port>', file=sys.stderr)
        sys.exit(2)
    repo_root = Path(sys.argv[1])
    tag = sys.argv[2]
    port = sys.argv[3]

    cfg = repo_root / 'config.yaml'
    if not cfg.exists():
        print(f'ERROR: {cfg} не найден', file=sys.stderr)
        sys.exit(1)

    text = cfg.read_text()
    lines = text.splitlines()
    # Та же форма, что видит config2env: список с отступом, значения в одну
    # строку. Номера строк совпадают, правка идёт по исходному тексту.
    normal = normalize_yaml(text).splitlines()
    inserted = False
    for start, end in exit_items(normal):
        fields = {}
        for index in range(start, end):
            content = normal[index].lstrip(' ')
            if content.startswith('- '):
                content = content[2:]
            name, sep, value = content.partition(':')
            if sep and not content.startswith('#'):
                fields[name.strip()] = (index, scalar(value))
        if fields.get('tag', (0, None))[1] != tag:
            continue
        if 'hy2_direct_port' in fields:
            index = fields['hy2_direct_port'][0]
            line = lines[index]
            lines[index] = line[:line.index('hy2_direct_port')] + f'hy2_direct_port: {port}'
        else:
            item = lines[start]
            column = len(item) - len(item.lstrip(' ')) + 2
            last = max(i for i in range(start, end)
                       if lines[i].strip() and not lines[i].lstrip().startswith('#'))
            lines.insert(last + 1, ' ' * column + f'hy2_direct_port: {port}')
        inserted = True
        break

    if not inserted:
        print(f'ERROR: тег "{tag}" не найден в config.yaml', file=sys.stderr)
        sys.exit(1)

    cfg.write_text('\n'.join(lines) + '\n')
    cfg.chmod(0o600)
    print(f'config.yaml: {tag} hy2_direct_port = {port}')


if __name__ == '__main__':
    main()
