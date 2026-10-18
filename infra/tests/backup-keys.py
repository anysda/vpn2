"""Disposable container only: real age/tar/sqlite/WG/OpenSSL backup and restore.

Run with VPN2_BACKUP_TEST_CONTAINER=1; dependencies: bash, age, expect, sqlite3,
wireguard-tools, openssl, python3, Node 24. SOURCE_ROOT selects scripts under test;
LEGACY_ROOT optionally selects an older checkout for the legacy archive case.
Service/network commands are stubbed; no tunnel traffic is claimed here.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import unittest


ROOT = Path(os.environ.get('SOURCE_ROOT', '/repo'))
LEGACY = Path(os.environ.get('LEGACY_ROOT', str(ROOT)))
DATA = Path('/var/lib/anysda-vpn2')
WG = Path('/etc/wireguard')
OVPN = Path('/etc/openvpn/server/pki')
IKE = Path('/etc/strongswan/pki')
BACKUPS = Path('/test-backups')


def run(*args, input_text=None, check=True):
    return subprocess.run(args, input=input_text, text=True, capture_output=True, check=check)


def put(path, body, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(mode)


def fixture():
    DATA.mkdir(parents=True, exist_ok=True)
    put('/etc/anysda/backup.env', 'BACKUP_BACKEND=local\n'
        f'BACKUP_LOCAL_DIR={BACKUPS}\nANYSDA_BACKUP_PASSPHRASE=disposable-test-only\n')
    put(WG / 'server.priv', run('wg', 'genkey').stdout)
    put(WG / 'server.pub', run('wg', 'pubkey', input_text=(WG / 'server.priv').read_text()).stdout)
    put(WG / 'wg0.conf', '[Interface]\nAddress = 192.0.2.1/24\nListenPort = 51820\n'
        f'PrivateKey = {(WG / "server.priv").read_text()}')
    for pki in (OVPN, IKE):
        pki.mkdir(parents=True, exist_ok=True)
        run('openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt',
            'ec_paramgen_curve:prime256v1', '-nodes', '-subj', '/CN=backup-test-ca',
            '-days', '1', '-keyout', str(pki / 'ca.key'), '-out', str(pki / 'ca.crt'))
        run('openssl', 'req', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
            '-nodes', '-subj', '/CN=vpn.example.test', '-keyout', str(pki / 'server.key'),
            '-out', str(pki / 'server.csr'))
        run('openssl', 'x509', '-req', '-in', str(pki / 'server.csr'),
            '-CA', str(pki / 'ca.crt'), '-CAkey', str(pki / 'ca.key'),
            '-set_serial', '1', '-days', '1', '-out', str(pki / 'server.crt'))
    put(OVPN / 'tls-crypt.key', run('openssl', 'rand', '-hex', '256').stdout)
    for name, source in (('x509ca/anysda-ca.crt', 'ca.crt'),
                         ('x509/anysda-server.crt', 'server.crt'),
                         ('private/anysda-server.key', 'server.key')):
        put(Path('/etc/swanctl') / name, (IKE / source).read_text())
    put('/etc/swanctl/conf.d/anysda-clients.conf', 'secrets { eap-test { secret = test } }\n')
    run('sqlite3', str(DATA / 'db.sqlite'),
        'CREATE TABLE clients (id INTEGER); INSERT INTO clients VALUES (1);')
    # История миграций как у панели: restore сверяет её с журналом версии.
    migrations = Path('/opt/anysda-vpn2/app/server/database/migrations')
    shutil.rmtree(migrations, ignore_errors=True)
    shutil.copytree(ROOT / 'app/server/database/migrations', migrations)
    entries = json.loads((migrations / 'meta/_journal.json').read_text())['entries']
    with sqlite3.connect(DATA / 'db.sqlite') as db:
        db.execute('CREATE TABLE __drizzle_migrations '
                   '(id INTEGER PRIMARY KEY AUTOINCREMENT, hash TEXT NOT NULL, created_at NUMERIC)')
        db.executemany('INSERT INTO __drizzle_migrations (hash, created_at) VALUES (?, ?)', [
            (hashlib.sha256((migrations / (e['tag'] + '.sql')).read_bytes()).hexdigest(), e['when'])
            for e in entries])


def clean_host():
    for path in (DATA, WG, OVPN.parent.parent, IKE.parent,
                 Path('/etc/swanctl'), Path('/etc/anysda')):
        shutil.rmtree(path, ignore_errors=True)


def identity():
    paths = [WG / 'server.pub', WG / 'server.priv', OVPN / 'tls-crypt.key',
             Path('/etc/swanctl/conf.d/anysda-clients.conf')]
    for pki in (OVPN, IKE):
        paths.extend(pki / name for name in ('ca.key', 'ca.crt', 'server.key', 'server.crt'))
    paths.extend(Path('/etc/swanctl') / name for name in
                 ('x509ca/anysda-ca.crt', 'x509/anysda-server.crt', 'private/anysda-server.key'))
    return {p: p.read_bytes() for p in paths}


class BackupRestore(unittest.TestCase):
    def setUp(self):
        clean_host()
        shutil.rmtree(BACKUPS, ignore_errors=True)
        fixture()
        for path in ('/etc/sysctl.d', '/etc/systemd/system', '/usr/local/sbin'):
            Path(path).mkdir(parents=True, exist_ok=True)
        # Реальные стадии и скрипты бэкапа; только внешние службы изолированы.
        for cmd in ('systemctl', 'docker', 'swanctl', 'sysctl', 'ufw', 'modprobe'):
            put(f'/test-bin/{cmd}', '#!/bin/sh\nexit 0\n', 0o755)
        Path('/test-panel-started').unlink(missing_ok=True)
        put('/test-bin/docker', '#!/bin/sh\nif [ "$1" = start ]; then\n'
            '  node /repo/infra/tests/backup-panel.mjs "${SOURCE_ROOT:-/repo}" '
            '> /test-panel.log 2>&1 && touch /test-panel-started\nfi\n', 0o755)
        put('/test-bin/ip', '#!/bin/sh\nexit 1\n', 0o755)
        put('/test-bin/curl', '#!/bin/sh\necho \'{"version":"test"}\'\n', 0o755)
        for stage in ('22-adguard', '30-frontend', '35-telegram', '99-verify'):
            put(f'/opt/anysda-vpn2/infra/scripts/{stage}.sh', '#!/bin/sh\nexit 0\n', 0o755)
        put('/opt/anysda-vpn2/infra/envs/all.env', 'HOST_TAG=ru\n')
        put('/opt/anysda-vpn2/infra/envs/ru.env', 'ENTRY_HOST=vpn.example.test\n')
        put('/opt/anysda-vpn2/infra/configs/anysda-config.yaml.tpl',
            (ROOT / 'infra/configs/anysda-config.yaml.tpl').read_text())

    def roundtrip(self, backup_root, bare=False, legacy=False):
        original = identity()
        put('/test-expected.pub', original[WG / 'server.pub'].decode())
        env = Path('/etc/anysda/backup.env').read_text()
        if legacy:
            # Формат старого архива: только wg0.conf, ключ содержит padding '='.
            (WG / 'server.priv').unlink()
            (WG / 'server.pub').unlink()
        result = run('bash', str(backup_root / 'infra/lib/anysda-backup.sh'))
        self.assertIn('backup:', result.stdout)
        clean_host()
        if not bare:
            fixture()  # B после deploy: все ключи другие, БД уже существует.
            self.assertNotEqual(original[WG / 'server.pub'], (WG / 'server.pub').read_bytes())
        else:
            DATA.mkdir(parents=True)
            put('/etc/anysda/backup.env', env)
        result = run('bash', str(ROOT / 'infra/lib/anysda-restore.sh'), 'latest', '--force', check=False)
        print(f'\n{self.id()}: restore exit={result.returncode}\n'
              + '\n'.join((result.stdout + result.stderr).splitlines()[-8:]), flush=True)
        self.assertEqual(result.returncode, 0)
        # Все идентификаторы проверяем до следующего деплоя/запуска панели.
        for path, body in original.items():
            with self.subTest(path=str(path)):
                self.assertTrue(path.exists(), f'missing {path}')
                if path.exists():
                    self.assertTrue(path.read_bytes() == body, f'identity changed: {path}')
        self.assertTrue(Path('/test-panel-started').exists(), Path('/test-panel.log').read_text())
        panel = run('node', '/repo/infra/tests/backup-panel.mjs', str(ROOT))
        print(panel.stdout.strip(), flush=True)
        result = run('bash', str(ROOT / 'infra/scripts/28-wireguard.sh'),
                     '/opt/anysda-vpn2/infra/envs/all.env', check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('28-wireguard done', result.stdout)
        self.assertEqual((WG / 'server.pub').read_bytes(), original[WG / 'server.pub'])
        self.assertEqual(run('wg', 'pubkey', input_text=(WG / 'server.priv').read_text()).stdout.encode(),
                         original[WG / 'server.pub'])
        for name in ('server.priv', 'server.pub', 'wg0.conf'):
            self.assertEqual((WG / name).stat().st_mode & 0o777, 0o600)
        for pki in (OVPN, IKE):
            run('openssl', 'verify', '-CAfile', str(pki / 'ca.crt'), str(pki / 'server.crt'))
            print(f'{pki}: ' + run('openssl', 'x509', '-in', str(pki / 'ca.crt'),
                                   '-noout', '-fingerprint', '-sha256').stdout.strip())
        print('PASS: WG keypair + stage 28; OpenVPN CA/server/tls-crypt; IKEv2 CA/server/swanctl', flush=True)

    def test_deployed_host(self):
        self.roundtrip(ROOT)

    def test_keys_without_rendered_config(self):
        (WG / 'wg0.conf').unlink()
        self.roundtrip(ROOT)

    def test_bare_host(self):
        self.roundtrip(ROOT, bare=True)

    def test_legacy_archive(self):
        self.roundtrip(LEGACY, legacy=True)

    def test_invalid_legacy_key_does_not_modify_host(self):
        (WG / 'server.priv').unlink()
        (WG / 'server.pub').unlink()
        put(WG / 'wg0.conf', '[Interface]\nPrivateKey = invalid\n')
        run('bash', str(ROOT / 'infra/lib/anysda-backup.sh'))
        clean_host()
        fixture()
        original = identity()
        result = run('bash', str(ROOT / 'infra/lib/anysda-restore.sh'), 'latest', '--force', check=False)
        self.assertEqual(result.returncode, 5)
        self.assertNotIn('останавливаю сервисы', result.stdout)
        self.assertFalse(Path('/test-panel-started').exists())
        self.assertTrue(identity() == original)

    def test_ikev2_le_archive_without_caddy_storage(self):
        put('/etc/anysda/ikev2-mode', 'letsencrypt')
        put('/etc/anysda/ikev2-server-host', 'vpn.example.test')
        put('/etc/swanctl/x509ca/anysda-le-chain-1.crt', (IKE / 'ca.crt').read_text())
        paths = [Path('/etc/swanctl') / name for name in
                 ('x509/anysda-server.crt', 'private/anysda-server.key', 'x509ca/anysda-le-chain-1.crt')]
        original = {p: p.read_bytes() for p in paths}
        put('/test-expected.pub', (WG / 'server.pub').read_text())
        run('bash', str(ROOT / 'infra/lib/anysda-backup.sh'))
        clean_host()
        fixture()
        result = run('bash', str(ROOT / 'infra/lib/anysda-restore.sh'), 'latest', '--force')
        self.assertIn('anysda-restore: SUCCESS', result.stdout)
        # Настоящая стадия 27 после restore; службы/сеть/наличие пакетов — заглушки.
        Path('/etc/strongswan.d').mkdir(exist_ok=True)
        put('/test-bin/dpkg', '#!/bin/sh\n[ "$2" = strongswan-starter ]\n', 0o755)
        put('/test-bin/swanctl', '#!/bin/sh\necho anysda-ikev2\n', 0o755)
        put('/test-bin/ss', '#!/bin/sh\nexit 0\n', 0o755)
        put('/test-bin/ip', '#!/bin/sh\necho "default via 192.0.2.1 dev eth0"\n', 0o755)
        put('/test-stage.env', 'HOST_TAG=ru\nENTRY_HOST=192.0.2.10\nPANEL_DOMAIN=vpn.example.test\n')
        self.assertFalse(Path('/var/lib/caddy/.local/share/caddy/certificates').exists())
        result = run('bash', str(ROOT / 'infra/scripts/27-ikev2.sh'), '/test-stage.env', check=False)
        self.assertEqual(Path('/etc/anysda/ikev2-mode').read_text(), 'letsencrypt', result.stdout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for path, body in original.items():
            self.assertTrue(path.read_bytes() == body, f'LE identity changed: {path}')
        self.assertIn('27-ikev2 done', result.stdout)
        print('PASS: real stage 27 preserves restored LE cert/key/chain without Caddy storage', flush=True)


if __name__ == '__main__':
    if os.environ.get('VPN2_BACKUP_TEST_CONTAINER') != '1' or not Path('/.dockerenv').exists():
        raise SystemExit('Only run in a disposable Docker container with VPN2_BACKUP_TEST_CONTAINER=1')
    os.environ['PATH'] = '/test-bin:' + os.environ['PATH']
    unittest.main(defaultTest=BackupRestore.__name__, verbosity=2)
