"""Команда `vpn2 snapshot`: слепки стенда CI снимает и сверяет infra/tools/snapshot.py из checkout.

В пакете самого инструмента нет; команда осталась для workflow стенда, который зовёт её из корня дерева.
"""
import subprocess
import sys
from pathlib import Path

TOOL = Path('infra/tools/snapshot.py')


def main(argv):
    if not TOOL.is_file():
        print(f'vpn2 snapshot: нет {TOOL} - запускайте из корня checkout', file=sys.stderr)
        return 2
    return subprocess.run([sys.executable, '-B', str(TOOL), *argv], check=False).returncode
