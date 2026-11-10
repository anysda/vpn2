"""vpn2 watchdog failover-env - env-файл сторожа экзитов на entry.

Настройки сторожа правятся руками в env-файле: передеплой переносит текущие
значения, а где строки нет или значение битое, ставит умолчание. Адрес и
секрет clash-api, группу и файл стоянки задаёт сам деплой.
"""

import argparse
import os
import re
import sys
from pathlib import Path

from vpn2 import fsutil

ENV_FILE = '/etc/anysda/failover-watchdog.env'
# Секрет clash-api пишет стадия 20-ru-router.
SECRET_FILE = '/etc/anysda/clash-secret.txt'
INT = r'[0-9]+'
NUM = r'[0-9]+(?:\.[0-9]+)?'
# имя, умолчание, допустимое значение
SETTINGS = (
    ('PROBE_URL', 'http://www.gstatic.com/generate_204', r'https?://\S+'),
    ('INTERVAL', '2', NUM),
    ('PROBE_TIMEOUT_MS', '3000', INT),
    ('TOLERANCE_MS', '120', INT),
    ('DEAD_AFTER', '3', INT),
    ('CONFIRM_GAP', '0.4', NUM),
    ('LATENCY_HOLD', '4', INT),
    ('COOLDOWN_S', '60', NUM),
    ('MAX_COOLDOWN_S', '600', NUM),
    ('PENALTY_RESET_S', '300', NUM),
    ('BALANCE', 'on', 'on|off'),
    ('BALANCE_MARGIN_MS', '150', INT),
    ('ACTIVE_BPS', '20000', NUM),
    ('REBALANCE_INTERVAL_S', '300', NUM),
    ('IMBALANCE_SHARE', '0.35', NUM),
    ('IMBALANCE_MIN_MBPS', '5', NUM),
    ('MIN_DWELL_S', '1800', NUM),
    ('IDLE_DWELL_S', '600', NUM),
    ('RATE_TAU_S', '300', NUM),
    ('WARP_RETURN_S', '600', NUM),
    ('LAT_TAU_S', '30', NUM),
    ('SLOW_HOLD_S', '60', NUM),
    ('TG_GROUP', 'tg-best', r'[A-Za-z0-9_-]+'),
    ('TG_PROBE_URL', 'https://api.telegram.org', r'https://\S+'),
    ('TG_INTERVAL_S', '30', NUM),
    ('RESTART_AFTER_CHECKS', '3', INT),
    ('RESTART_MIN_INTERVAL_S', '600', INT),
)
# Эти два живут только в config.yaml (блок failover:, деплой кладёт их в
# окружение стадии): есть ключ - его значение, нет - умолчание. Прошлое
# значение env-файла не читается, иначе удалённый из config.yaml 0 выключил бы
# перезапуск навсегда.
FROM_CONFIG = ('RESTART_AFTER_CHECKS', 'RESTART_MIN_INTERVAL_S')


def render(previous, secret, environ):
    """Текст env-файла: previous - прежний файл, environ - окружение стадии."""
    lines = [f'CLASH_API=http://{environ.get("MGMT_IP") or "10.99.0.1"}:9090',
             f'CLASH_SECRET={secret}',
             'WATCH_GROUP=foreign-best',
             'LANE_STATE_FILE=/var/lib/anysda-failover-watchdog/lanes.json']
    for key, default, pattern in SETTINGS:
        if key in FROM_CONFIG:
            value = environ.get(key)
        else:
            found = re.findall(rf'^{key}=({pattern})$', previous, re.MULTILINE | re.ASCII)
            value = found[-1] if found else ''
        lines.append(f'{key}={value or default}')
    return '\n'.join(lines) + '\n'


def main(argv):
    parser = argparse.ArgumentParser(prog='vpn2 watchdog failover-env')
    parser.add_argument('--env-file', default=ENV_FILE)
    parser.add_argument('--secret-file', default=SECRET_FILE)
    args = parser.parse_args(argv)
    path = Path(args.env_file)
    try:
        secret = Path(args.secret_file).read_text().rstrip('\n')
    except OSError:
        secret = ''
    if not secret:
        sys.stderr.write(f'{args.secret_file} пуст - сначала 20-ru-router\n')
        return 1
    try:
        previous = path.read_bytes().decode(errors='replace')
    except FileNotFoundError:
        previous = None
    body = render(previous or '', secret, os.environ)
    changed = body != previous
    if changed:
        fsutil.atomic(path, body, mode=0o600)
    elif path.stat().st_mode & 0o777 != 0o600:
        path.chmod(0o600)
    print(f'changed={int(changed)}')
    return 0
