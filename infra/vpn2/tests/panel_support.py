"""TLS-шаг стадии панели во временном корне и настоящий Caddy той версии, что ставит деплой."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / 'infra/vpn2/src'
CADDY_VER = re.search(r"^CADDY_VER='([^']+)'", (ROOT / 'infra/lib/anysda-svc.sh').read_text(), re.M)[1]

PANEL_TLS = '''
import os, sys
from pathlib import Path
from vpn2.containers import panel
root, fail = sys.argv[1:3]
for name in ('TLS_DIR', 'TLS_CONFIG', 'SECURE', 'MANAGED', 'OVPN_CONF'):
    setattr(panel, name, root + getattr(panel, name))
panel.CADDY = [root + path for path in panel.CADDY]
panel.STATE = Path(root, 'panel-state')
run = panel.subprocess.run


def tls_in_root(argv, **kwargs):
    if tuple(argv[:4]) == panel.TLS:
        argv = [*argv, '--caddy-dir', root + '/etc/caddy', '--storage', root + '/var/lib/caddy/.local/share/caddy',
                '--bot-secret', root + '/etc/anysda/tgbot-secret.txt']
    return run(argv, **kwargs)


panel.subprocess.run = tls_in_root
tx = panel.Transaction(None)
tx.lock()
tx.changed = True
tx.files_restored = panel.Openvpn().restored
try:
    panel.host_tls(os.environ, tx)
    caddyfile = Path(panel.CADDY[0])
    caddyfile.write_text(caddyfile.read_text().replace('import /etc/caddy/', f'import {root}/etc/caddy/'))
    if fail:
        raise SystemExit(1)
except BaseException:
    tx.failure()
    raise
'''


def caddy():
    """Caddy версии CADDY_VER: CADDY_BIN, PATH или кэш, куда его кладёт шаг CI."""
    cache = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / f'vpn2-tests/caddy-{CADDY_VER}/caddy'
    for candidate in (os.environ.get('CADDY_BIN'), shutil.which('caddy'), cache.exists() and str(cache)):
        if candidate and subprocess.run([candidate, 'version'], capture_output=True,
                                        text=True).stdout.startswith(f'v{CADDY_VER} '):
            return candidate
    pytest.skip(f'needs caddy {CADDY_VER} in CADDY_BIN, PATH or {cache}')


def panel_tls(directory, domain, env=None, fail=False, entry='192.0.2.1'):
    """Шаг TLS стадии панели под её транзакцией; /etc и /var перенесены в каталог теста."""
    Path(directory, 'etc/caddy').mkdir(parents=True, exist_ok=True)
    env = dict(env or os.environ, HOST_TAG='ru', ENTRY_HOST=entry, PANEL_DOMAIN=domain, PYTHONPATH=str(SRC))
    return subprocess.run([sys.executable, '-c', PANEL_TLS, str(directory), 'fail' if fail else ''],
                          env=env, capture_output=True, text=True, timeout=60)
