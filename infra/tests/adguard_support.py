"""Шаг «config» роли adguard на каталогах во временной папке."""

import contextlib
import io
import os
import re
from pathlib import Path
import sys
from unittest import mock

import yaml

ROOT = Path(os.environ.get('INFRA_TEST_ROOT', Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT / 'infra/vpn2/src'))
from vpn2 import resources  # noqa: E402
from vpn2.adguard import stage  # noqa: E402


class Hub:
    """Каталоги /opt/AdGuardHome и /etc/anysda во временной папке."""

    def __init__(self, directory, port=3001, user='anysda'):
        self.root = Path(directory)
        self.agh, self.plain = self.root / 'agh', self.root / 'plain'
        self.etc, self.work = self.root / 'etc-anysda', self.root / 'work'
        for path in (self.agh, self.plain, self.etc, self.work):
            path.mkdir(parents=True)
        self.config = self.agh / 'AdGuardHome.yaml'
        self.env = {'AGH_PORT': str(port), 'MGMT_IP': '192.0.2.53'}
        self.user = user
        self.changed = False

    def deploy(self):
        """Сборка и публикация обоих конфигов; возвращает stderr шага."""
        log = io.StringIO()
        with contextlib.redirect_stderr(log), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(stage, 'password_hash', lambda *_: 'hash'), \
                mock.patch.object(resources, 'hardware', lambda: (8000, 8192)), \
                mock.patch.object(stage.svc, 'active', lambda _unit: False), \
                mock.patch.object(stage.svc, 'forget', lambda _unit: None):
            staged = stage.build_configs(self.env, self.user, 'pw', self.work, agh_dir=self.agh, plain_dir=self.plain,
                                         etc=self.etc, pools=ROOT / 'app/shared/vpn-pools.json')
            published = [stage.publish(unit, path, directory / 'AdGuardHome.yaml')
                         for unit, path, directory in zip(stage.UNITS, staged, (self.agh, self.plain))]
        self.changed = published[0]
        return log.getvalue()

    def dns(self):
        return yaml.safe_load(self.config.read_text())['dns']

    def edit(self, **values):
        """Как AdGuard после POST /control/dns_config из панели."""
        config = yaml.safe_load(self.config.read_text())
        config['dns'].update(values)
        self.config.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))


def render_unit(name, limits):
    """Юнит роли с подставленными лимитами; в шаблоне только {{ adguard_limits.ИМЯ }}."""
    text = (ROOT / f'infra/ansible/roles/adguard/templates/{name}.service.j2').read_text()
    text = re.sub(r'\{\{ adguard_limits\.(\w+) \}\}', lambda match: str(limits[match[1]]), text)
    assert '{{' not in text and '{%' not in text, name
    return text
