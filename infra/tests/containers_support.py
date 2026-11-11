"""Пакет vpn2.containers для тестов настроек: окружение служб без Docker."""

import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(os.environ.get('INFRA_TEST_ROOT', Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT / 'infra/vpn2/src'))
from vpn2.containers import compose, panel, telegram  # noqa: E402,F401

# Секреты и решения стадии, которых нет в файлах окружения.
FACTS = {'session': 's', 'clash': 'c', 'tgbot': 't', 'admin_user': 'admin', 'agh_password': 'p',
         'sso_enabled': 'false', 'sso_auto_redirect': 'false', 'sso_password_login': 'true',
         'sso_redirect_url': ''}

PANEL_TLS = '''
import os, sys
from vpn2.containers import panel
from vpn2.containers.transaction import Transaction
root, lib, fail = sys.argv[1:4]
tx = Transaction(None, root + '/panel-state')
tx.lock()
tx.changed = True
tx.files_restored = panel.Openvpn(print, root + panel.OVPN_CONF).restored
try:
    panel.host_tls(os.environ, print, tx, root, lib)
    if fail:
        raise SystemExit(1)
except BaseException:
    tx.failure()
    raise
'''


def panel_tls(directory, domain, env=None, fail=False, entry='192.0.2.1'):
    """Шаг TLS стадии панели под её транзакцией; /etc и /var перенесены в каталог теста."""
    Path(directory, 'etc/caddy').mkdir(parents=True, exist_ok=True)
    env = dict(env or os.environ, HOST_TAG='ru', ENTRY_HOST=entry, PANEL_DOMAIN=domain,
               PYTHONPATH=str(ROOT / 'infra/vpn2/src'))
    return subprocess.run([sys.executable, '-c', PANEL_TLS, str(directory), str(ROOT / 'infra/ansible/roles/containers/files/vpn2_tls.py'),
                           'fail' if fail else ''], env=env, capture_output=True, text=True, timeout=60)
