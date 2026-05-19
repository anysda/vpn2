#!/usr/bin/env python3
"""
config-set-port.py — записывает hy2_direct_port для указанного exit-тега в config.yaml.

Использование: config-set-port.py <repo_root> <tag> <port>

Если для тега уже есть hy2_direct_port — обновляет, иначе вставляет после `tag:`.
Сохраняет форматирование, комментарии, прочие поля.
"""
import sys
from pathlib import Path


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

    lines = cfg.read_text().splitlines()
    out = []
    in_target = False
    inserted = False

    for line in lines:
        stripped = line.lstrip()

        if stripped.startswith('- tag:'):
            # Конец предыдущего блока — если был target и порт не вставлен, вставим
            if in_target and not inserted:
                out.append(f'    hy2_direct_port: {port}')
                inserted = True
            current_tag = stripped.split(':', 1)[1].strip()
            in_target = (current_tag == tag)
            out.append(line)
            continue

        # Внутри блока ноды (indent 4) — ищем существующий hy2_direct_port
        if in_target and stripped.startswith('hy2_direct_port:'):
            out.append(f'    hy2_direct_port: {port}')
            inserted = True
            continue

        # Выход из блока exits (новый top-level ключ или пусто на корне)
        if in_target and line and not line.startswith(' '):
            if not inserted:
                out.append(f'    hy2_direct_port: {port}')
                inserted = True
            in_target = False

        out.append(line)

    # EOF и мы всё ещё были в блоке нужной ноды
    if in_target and not inserted:
        out.append(f'    hy2_direct_port: {port}')
        inserted = True

    if not inserted:
        print(f'ERROR: тег "{tag}" не найден в config.yaml', file=sys.stderr)
        sys.exit(1)

    cfg.write_text('\n'.join(out) + '\n')
    cfg.chmod(0o600)
    print(f'config.yaml: {tag} hy2_direct_port = {port}')


if __name__ == '__main__':
    main()
