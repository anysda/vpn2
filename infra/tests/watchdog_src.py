"""Откуда тесты берут сторожей и генератор роутера: пакет vpn2."""
import os
from pathlib import Path
import sys

ROOT = Path(os.environ.get('VPN2_AUDIT_ROOT', Path(__file__).resolve().parents[2]))
SRC = ROOT / 'infra/vpn2/src'
FILES = {'failover': 'failover.py', 'warp': 'warp.py', 'yt': 'yt.py', 'hy2': 'hy2_bandwidth.py'}

sys.path.insert(0, str(SRC))
os.environ['PYTHONPATH'] = os.pathsep.join(
    filter(None, [str(SRC), os.environ.get('PYTHONPATH')]))


def path(kind):
    """Файл сторожа для загрузки модулем."""
    return SRC / 'vpn2/watchdog' / FILES[kind]


def command(kind):
    """Запуск сторожа отдельным процессом."""
    return [sys.executable, '-m', 'vpn2.cli', 'watchdog', kind]


def shim(directory, body=None):
    """Команда vpn2 для стадии в песочнице: пакет из дерева или подменное тело."""
    path = Path(directory) / 'vpn2'
    path.write_text(body or f'#!/bin/sh\nPYTHONPATH={SRC} exec {sys.executable} -m vpn2.cli "$@"\n')
    path.chmod(0o755)
    return path


VENV_VPN2 = '/opt/vpn2/venv/bin/vpn2'
