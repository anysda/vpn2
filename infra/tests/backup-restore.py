#!/usr/bin/env python3
"""Real SQLite/WAL + age round trips; run ONLY in a disposable container.

VPN2_BACKUP_TEST=1 python3 infra/tests/backup-restore.py /path/to/repository
Services and deploy stages are stubs; backup/restore, SQLite, tar and age are real.
"""

import os
import json
import pwd
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import unittest


REPO = Path(sys.argv.pop(1)).resolve()
DB = Path('/var/lib/anysda-vpn2/db.sqlite')
BACKUPS = Path('/var/backups/anysda-vpn2')
CADDY = Path('/var/lib/caddy/.local/share/caddy')
FILES = (
    '/etc/anysda/admin-password.txt', '/etc/wireguard/wg0.conf',
    '/etc/openvpn/server/pki/ca.key', '/etc/strongswan/pki/ca.key',
    '/etc/swanctl/private/server.key', '/etc/swanctl/conf.d/anysda-clients.conf',
    '/etc/strongswan.conf', '/etc/strongswan.d/anysda-swanctl-load.conf',
    '/etc/ipsec.conf', '/etc/ipsec.secrets',
)
EXTRA = ('/etc/caddy/Caddyfile', '/opt/AdGuardHome/AdGuardHome.yaml',
         '/var/anysda/.stamps/30-frontend')


# restore выводит server.pub из PrivateKey wg0.conf, поэтому ключ должен быть
# настоящим; метка состояния остаётся в комментарии.
WG_KEYS = {'archived': 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
           'live': 'AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE='}


def body(path, state):
    if path.endswith('/wg0.conf'):
        return f'[Interface]\nPrivateKey = {WG_KEYS[state]}\n# {state}:{path}\n'
    return f'{state}:{path}'


def write(path, content, mode=0o600):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    target.chmod(mode)


def run(*args, **kwargs):
    return subprocess.run(args, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, check=False, **kwargs)


def decrypt(archive, destination):
    env = dict(os.environ, AGE_SOURCE=str(archive), AGE_TARGET=str(destination))
    result = run('expect', env=env, input='''
log_user 0
set timeout 60
spawn age -d -o $env(AGE_TARGET) $env(AGE_SOURCE)
expect {
    "Enter passphrase*" { send -- "test-backup-passphrase\\r"; exp_continue }
    eof
}
catch wait result
exit [lindex $result 3]
''')
    if result.returncode:
        raise AssertionError(result.stdout)


class BackupRestore(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(CADDY, ignore_errors=True)
        for path in (DB.parent, BACKUPS, Path('/test-output'), Path('/test-bin')):
            shutil.rmtree(path, ignore_errors=True)
            path.mkdir(parents=True)
        for path in (*FILES, *EXTRA):
            write(path, body(path, 'archived'))
        # Как на entry: ключи лежат в keys/, верхний уровень — ссылки.
        for name, value in (('server.priv', WG_KEYS['archived']),
                            ('server.pub', run('wg', 'pubkey', input=WG_KEYS['archived']).stdout.strip())):
            key = Path('/etc/wireguard') / name
            key.unlink(missing_ok=True)
            write('/etc/wireguard/keys/' + name, value)
            key.symlink_to('keys/' + name)
        shutil.rmtree('/tmp/anysda', ignore_errors=True)
        write('/etc/anysda/backup.env', '''BACKUP_BACKEND=local
BACKUP_LOCAL_DIR=/var/backups/anysda-vpn2
BACKUP_RETENTION=2
ANYSDA_BACKUP_PASSPHRASE=test-backup-passphrase
''')
        write('/test-bin/systemctl', '#!/bin/sh\nexit 0\n', 0o755)
        write('/test-bin/docker', '''#!/bin/sh
if [ "$1" = stop ] && [ -f /test-output/stop-fails ]; then exit 1; fi
if [ "$1" = stop ] && [ -f /test-output/writer.pid ]; then
    kill -KILL "$(cat /test-output/writer.pid)"
fi
exit 0
''', 0o755)
        write('/test-bin/curl', '#!/bin/sh\necho \'{"version":"test"}\'\n', 0o755)
        for stage in ('22-adguard', '30-frontend', '35-telegram', '99-verify'):
            write('/opt/anysda-vpn2/infra/scripts/' + stage + '.sh',
                  '#!/bin/sh\nexit 0\n', 0o755)
        write('/opt/anysda-vpn2/infra/configs/anysda-config.yaml.tpl', 'template from installed tree')
        for name in ('all', 'ru'):
            write('/opt/anysda-vpn2/infra/envs/' + name + '.env', 'HOST_TAG=ru\n')
        self.env = dict(os.environ, PATH='/test-bin:' + os.environ['PATH'])
        self.conn = sqlite3.connect(DB)
        self.addCleanup(self.conn.close)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA wal_autocheckpoint=0')
        self.conn.execute('CREATE TABLE clients (name TEXT)')
        self.conn.commit()
        self.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.conn.execute("INSERT INTO clients VALUES ('archived-client')")
        self.conn.commit()
        self.assertGreater(Path(str(DB) + '-wal').stat().st_size, 0)

    def script(self, name, *args):
        return run('bash', str(REPO / 'infra/lib' / ('anysda-' + name + '.sh')),
                   *args, env=self.env)

    def backup(self):
        result = self.script('backup')
        self.assertEqual(result.returncode, 0, result.stdout)
        archive = next(BACKUPS.glob('*.age'))
        decrypt(archive, '/test-output/backup.tar.gz')
        with tarfile.open('/test-output/backup.tar.gz') as tar:
            tar.extractall('/test-output/backup', filter='data')
        return next(Path('/test-output/backup').glob('anysda-vpn2-*'))

    def safety(self):
        archive = next((BACKUPS / '.pre-restore').glob('*.age'))
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        decrypt(archive, '/test-output/safety.tar.gz')
        with tarfile.open('/test-output/safety.tar.gz') as tar:
            tar.extractall('/test-output/safety', filter='data')
        return Path('/test-output/safety')

    def test_safety_wal_and_all_overwritten_files(self):
        self.backup()
        for path in (*FILES, *EXTRA):
            write(path, body(path, 'live'))
        # Stop failure keeps the writer alive and lets us inspect the safety
        # snapshot made before any swap. Exactly one committed row is in WAL.
        write('/test-output/stop-fails', '1')
        result = self.script('restore', 'latest', '--force')
        snapshot = self.safety()
        with sqlite3.connect(snapshot / str(DB).lstrip('/')) as saved:
            count = saved.execute('SELECT COUNT(*) FROM clients').fetchone()[0]
            self.assertEqual(saved.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        print(f'live_clients=1 snapshot_clients={count}', flush=True)
        missing = [path for path in (*FILES, *EXTRA)
                   if not (snapshot / path.lstrip('/')).exists()]
        print('safety_missing=' + ','.join(missing), flush=True)
        self.assertEqual(count, 1)
        self.assertEqual(missing, [])
        for path in (*FILES, *EXTRA):
            self.assertEqual((snapshot / path.lstrip('/')).read_text(), body(path, 'live'))
        self.assertNotEqual(result.returncode, 0, 'must abort if writer cannot stop')
        self.assertEqual(Path(FILES[0]).read_text(), body(FILES[0], 'live'))

    def test_full_round_trip_and_stale_wal(self):
        bundle = self.backup()
        for name in ('server.priv', 'server.pub'):
            key = bundle / 'etc-wireguard' / name
            self.assertFalse(key.is_symlink(), f'{name}: backup must contain key bytes')
            self.assertTrue(key.read_text())
        with sqlite3.connect(bundle / 'db.sqlite') as saved:
            self.assertEqual(saved.execute('SELECT name FROM clients').fetchall(),
                             [('archived-client',)])
        self.conn.close()
        # Simulate a stopped/crashed writer which left a valid old WAL.
        writer = subprocess.Popen(['python3', '-c', '''import sqlite3, sys
c = sqlite3.connect('/var/lib/anysda-vpn2/db.sqlite')
c.execute('PRAGMA wal_autocheckpoint=0')
c.execute("UPDATE clients SET name='live-client'")
c.commit()
print('ready', flush=True)
sys.stdin.read()
'''], text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.addCleanup(writer.wait)
        self.addCleanup(writer.kill)
        self.addCleanup(writer.stdin.close)
        self.addCleanup(writer.stdout.close)
        self.assertEqual(writer.stdout.readline(), 'ready\n')
        write('/test-output/writer.pid', str(writer.pid))
        for path in FILES:
            write(path, body(path, 'live'))
        write('/etc/wireguard/keys/server.priv', WG_KEYS['live'])
        result = self.script('restore', 'latest', '--force')
        self.assertEqual(result.returncode, 0, result.stdout)
        with sqlite3.connect(DB) as restored:
            rows = restored.execute('SELECT name FROM clients').fetchall()
            integrity = restored.execute('PRAGMA integrity_check').fetchone()[0]
        print(f'round_trip_clients={rows} integrity_check={integrity}', flush=True)
        self.assertEqual(rows, [('archived-client',)])
        self.assertEqual(integrity, 'ok')
        for path in FILES:
            self.assertEqual(Path(path).read_text(), body(path, 'archived'))
        self.assertEqual(Path('/etc/wireguard/keys/server.priv').read_text().strip(), WG_KEYS['archived'])
        snapshot = self.safety()
        with sqlite3.connect(snapshot / str(DB).lstrip('/')) as saved:
            self.assertEqual(saved.execute('SELECT name FROM clients').fetchall(),
                             [('live-client',)])
        self.assertEqual(list(DB.parent.glob('.*-tmp.*')), [])
        print('backup_restore_sqlite_ikev2=PASS', flush=True)

    def test_legacy_dangling_wg_keys(self):
        # Воспроизводим старый формат с пропущенным keys/, реальным манифестом
        # и шифрованием. Работает и при прогоне теста против origin/dev.
        legacy = REPO / 'infra/lib/anysda-backup.sh'
        write('/test-output/legacy-backup.sh', legacy.read_text().replace('cp -aL', 'cp -a'))
        result = run('bash', '/test-output/legacy-backup.sh', env=self.env)
        self.assertEqual(result.returncode, 0, result.stdout)
        result = self.script('restore', 'latest', '--force')
        print(f'legacy_symlink_restore_rc={result.returncode}', flush=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(Path('/etc/wireguard/server.priv').read_text().strip(), WG_KEYS['archived'])
        expected = run('wg', 'pubkey', input=WG_KEYS['archived']).stdout.strip()
        self.assertEqual(Path('/etc/wireguard/server.pub').read_text().strip(), expected)

    def test_restore_after_tmp_cleanup_preserves_archived_password(self):
        # Изолируем зависимость от /tmp от отдельной регрессии ссылок WG.
        for name in ('server.priv', 'server.pub'):
            key = Path('/etc/wireguard') / name
            content = key.read_text()
            key.unlink()
            write(str(key), content)
        self.backup()
        # Стадия проверяет тот же обязательный вход, что настоящая стадия 30.
        write('/opt/anysda-vpn2/infra/scripts/30-frontend.sh', '''#!/bin/bash
set -eu
source "$1"
test -s /tmp/anysda/anysda-config.yaml.tpl
test -z "${ADMIN_PASSWORD:-}"
cmp /tmp/anysda/anysda-config.yaml.tpl /opt/anysda-vpn2/infra/configs/anysda-config.yaml.tpl
''', 0o755)
        write('/opt/anysda-vpn2/infra/envs/ru.env', 'HOST_TAG=ru\nADMIN_PASSWORD=current-deploy\n')
        self.assertFalse(Path('/tmp/anysda').exists())
        result = self.script('restore', 'latest', '--force')
        print(f'cold_restore_rc={result.returncode}', flush=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(Path(FILES[0]).read_text(), body(FILES[0], 'archived'))

    def test_snapshot_failure_preserves_live_data(self):
        self.backup()
        # Inject a tar failure only for safety archive creation.
        write('/test-bin/tar', '''#!/bin/sh
case "$*" in *pre-restore*) /usr/bin/tar "$@"; exit 42;; esac
exec /usr/bin/tar "$@"
''', 0o755)
        result = self.script('restore', 'latest', '--force')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list((BACKUPS / '.pre-restore').glob('*.age')), [])
        self.assertEqual(self.conn.execute('SELECT name FROM clients').fetchall(),
                         [('archived-client',)])
        self.assertEqual(list(DB.parent.glob('.*-tmp.*')), [])
        print('snapshot_failure_preserves_live_data=PASS', flush=True)

    def test_caddy_storage_round_trip(self):
        if run('id', 'caddy').returncode:
            self.assertEqual(run('useradd', '--system', 'caddy').returncode, 0)
        files = ('certificates/acme-v02.api.letsencrypt.org-directory/vpn.example.test/'
                 'vpn.example.test.crt',
                 'certificates/acme-v02.api.letsencrypt.org-directory/vpn.example.test/'
                 'vpn.example.test.key', 'acme/account/account.key', 'acme/account/account.json')
        for name in files:
            write(str(CADDY / name), 'archived:' + name, 0o644)
        write(str(CADDY / 'locks/test.lock'), 'runtime lock')
        bundle = self.backup()
        manifest = json.loads((bundle / 'manifest.json').read_text())
        print('caddy_storage_in_manifest=' + str('caddy-storage' in manifest['components']), flush=True)
        self.assertIn('caddy-storage', manifest['components'])
        self.assertFalse((bundle / 'caddy-storage/locks').exists())
        for name in files:
            self.assertEqual((bundle / 'caddy-storage' / name).read_text(), 'archived:' + name)
            write(str(CADDY / name), 'live:' + name)
        write(str(CADDY / 'stale.key'), 'must disappear')
        write('/etc/anysda/ikev2-domain', 'vpn.example.test')
        write('/etc/anysda/ikev2-cert.sha256', 'stale digest')
        write('/test-bin/systemctl', '''#!/bin/sh
echo "$*" >> /test-output/services
if [ "$1 $2" = 'start caddy' ]; then
    test ! -e /var/lib/caddy/.local/share/caddy/stale.key || exit 41
    test ! -e /etc/anysda/ikev2-cert.sha256 || exit 42
fi
''', 0o755)
        result = self.script('restore', 'latest', '--force')
        self.assertEqual(result.returncode, 0, result.stdout)
        owner = pwd.getpwnam('caddy')
        for name in files:
            path = CADDY / name
            self.assertEqual(path.read_text(), 'archived:' + name)
            self.assertEqual(path.stat().st_uid, owner.pw_uid)
            self.assertEqual(path.stat().st_gid, owner.pw_gid)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        services = Path('/test-output/services').read_text().splitlines()
        for unit in ('anysda-ikev2-cert-sync.path', 'anysda-ikev2-cert-sync.timer',
                     'anysda-ikev2-cert-sync.service'):
            self.assertLess(services.index('stop ' + unit), services.index('stop caddy'))
        self.assertLess(services.index('stop caddy'), services.index('start caddy'))
        self.assertLess(services.index('start strongswan-starter'),
                        services.index('start anysda-ikev2-cert-sync.service'))
        snapshot = self.safety() / str(CADDY).lstrip('/')
        for name in files:
            self.assertEqual((snapshot / name).read_text(), 'live:' + name)
        print('caddy_storage_restore_ownership_order_snapshot=PASS', flush=True)

    def test_without_caddy_storage(self):
        # То же содержимое бандла, что до появления Caddy в формате v1.
        bundle = self.backup()
        self.assertNotIn('caddy-storage', json.loads((bundle / 'manifest.json').read_text())['components'])
        for existing in (False, True):
            with self.subTest(existing_storage=existing):
                if existing:
                    write(str(CADDY / 'existing.key'), 'keep current storage')
                result = self.script('restore', 'latest', '--force')
                self.assertEqual(result.returncode, 0, result.stdout)
                if existing:
                    self.assertEqual((CADDY / 'existing.key').read_text(), 'keep current storage')
                else:
                    self.assertFalse(CADDY.exists())
        print('no_domain_and_legacy_caddy_restore=PASS', flush=True)


if __name__ == '__main__':
    if os.environ.get('VPN2_BACKUP_TEST') != '1' or not Path('/.dockerenv').exists():
        sys.exit('Run only inside a disposable Docker container with VPN2_BACKUP_TEST=1')
    unittest.main(defaultTest=BackupRestore.__name__, verbosity=2)
