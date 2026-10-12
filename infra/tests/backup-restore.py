#!/usr/bin/env python3
"""Real SQLite/WAL + age round trips; run ONLY in a disposable container.

VPN2_BACKUP_TEST=1 python3 infra/tests/backup-restore.py /path/to/repository
Services and deploy stages are stubs; backup/restore, SQLite, tar and age are real.
"""

import os
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
        for path in (DB.parent, BACKUPS, Path('/test-output'), Path('/test-bin')):
            shutil.rmtree(path, ignore_errors=True)
            path.mkdir(parents=True)
        for path in (*FILES, *EXTRA):
            write(path, body(path, 'archived'))
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
        snapshot = self.safety()
        with sqlite3.connect(snapshot / str(DB).lstrip('/')) as saved:
            self.assertEqual(saved.execute('SELECT name FROM clients').fetchall(),
                             [('live-client',)])
        self.assertEqual(list(DB.parent.glob('.*-tmp.*')), [])
        print('backup_restore_sqlite_ikev2=PASS', flush=True)

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


if __name__ == '__main__':
    if os.environ.get('VPN2_BACKUP_TEST') != '1' or not Path('/.dockerenv').exists():
        sys.exit('Run only inside a disposable Docker container with VPN2_BACKUP_TEST=1')
    unittest.main(defaultTest=BackupRestore.__name__, verbosity=2)
