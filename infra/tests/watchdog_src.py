"""Откуда тесты берут сторожей: файлы infra/lib (их ставят стадии) или пакет vpn2.

VPN2_WATCHDOG_IMPL=package гонит те же сценарии по vpn2.watchdog: поведение
пакета сверяется со старыми файлами одним набором тестов.
"""
import os
from pathlib import Path
import sys

ROOT = Path(os.environ.get('VPN2_AUDIT_ROOT', Path(__file__).resolve().parents[2]))
SRC = ROOT / 'infra/vpn2/src'
PACKAGE = os.environ.get('VPN2_WATCHDOG_IMPL') == 'package'
FILES = {
    'failover': ('infra/lib/failover-watchdog.py', 'failover.py'),
    'warp': ('infra/lib/warp-watchdog.py', 'warp.py'),
    'yt': ('infra/lib/yt-failover.py', 'yt.py'),
    'hy2': ('infra/lib/hy2_bandwidth.py', 'hy2_bandwidth.py'),
}

if PACKAGE:
    sys.path.insert(0, str(SRC))
    os.environ['PYTHONPATH'] = os.pathsep.join(
        filter(None, [str(SRC), os.environ.get('PYTHONPATH')]))


def path(kind):
    """Файл сторожа для загрузки модулем."""
    lib, module = FILES[kind]
    return SRC / 'vpn2/watchdog' / module if PACKAGE else ROOT / lib


def command(kind):
    """Запуск сторожа отдельным процессом."""
    if PACKAGE:
        return [sys.executable, '-m', 'vpn2.cli', 'watchdog', kind]
    return ['python3', str(path(kind))]
