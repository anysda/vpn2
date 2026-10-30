#!/usr/bin/env python3
"""Real SQLite/WAL + age round trips; run ONLY in a disposable container.

VPN2_BACKUP_TEST=1 python3 infra/tests/backup-restore.py /path/to/repository
Services and deploy stages are stubs; backup/restore, SQLite, tar and age are real.
"""

import base64
import hashlib
import os
import json
import pwd
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
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
# Каталоги, в которые restore кладёт содержимое архива поверх живого.
LIVE_DIRS = tuple(Path(p) for p in ('/var/lib/anysda-vpn2', '/etc/anysda', '/etc/wireguard', '/etc/openvpn',
                                    '/etc/strongswan', '/etc/swanctl', '/etc/strongswan.d'))
EXTRA = ('/etc/caddy/Caddyfile', '/opt/AdGuardHome/AdGuardHome.yaml',
         '/var/anysda/.stamps/30-frontend')


# restore выводит server.pub из PrivateKey wg0.conf, поэтому ключ должен быть
# настоящим; метка состояния остаётся в комментарии.
MIGRATIONS = Path('/opt/anysda-vpn2/app/server/database/migrations')


def journal():
    entries = json.loads((MIGRATIONS / 'meta/_journal.json').read_text())['entries']
    return [(hashlib.sha256((MIGRATIONS / (e['tag'] + '.sql')).read_bytes()).hexdigest(), e['when'])
            for e in entries]


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


def encrypt(source, archive):
    env = dict(os.environ, AGE_SOURCE=str(source), AGE_TARGET=str(archive))
    result = run('expect', env=env, input='''
log_user 0
set timeout 60
spawn age -p -o $env(AGE_TARGET) $env(AGE_SOURCE)
expect {
    "Enter passphrase*" { send -- "test-backup-passphrase\\r"; exp_continue }
    "Confirm passphrase*" { send -- "test-backup-passphrase\\r"; exp_continue }
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
        # История миграций как у настоящей панели: restore сверяет её с журналом.
        shutil.rmtree(MIGRATIONS, ignore_errors=True)
        shutil.copytree(REPO / 'app/server/database/migrations', MIGRATIONS)
        self.conn.execute('CREATE TABLE __drizzle_migrations '
                          '(id INTEGER PRIMARY KEY AUTOINCREMENT, hash TEXT NOT NULL, created_at NUMERIC)')
        self.conn.executemany('INSERT INTO __drizzle_migrations (hash, created_at) VALUES (?, ?)', journal())
        self.conn.commit()
        self.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.conn.execute("INSERT INTO clients VALUES ('archived-client')")
        self.conn.commit()
        self.assertGreater(Path(str(DB) + '-wal').stat().st_size, 0)

    def script(self, name, *args):
        # Как на узле: 26-backup ставит wg_conf.py в /usr/local/lib/anysda, сверка берёт его оттуда.
        lib = Path('/usr/local/lib/anysda')
        if not (lib / 'wg_conf.py').exists():
            if not lib.exists():
                lib.mkdir(parents=True)
                self.addCleanup(shutil.rmtree, lib)
            shutil.copy(REPO / 'infra/lib/wg_conf.py', lib / 'wg_conf.py')
            self.addCleanup((lib / 'wg_conf.py').unlink, True)
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


    def use_s3(self):
        write('/etc/anysda/backup.env', '''BACKUP_BACKEND=s3
BACKUP_LOCAL_DIR=/var/backups/anysda-vpn2
BACKUP_RETENTION=2
BACKUP_S3_ENDPOINT=https://s3.example.test
BACKUP_S3_BUCKET=bucket
ANYSDA_S3_ACCESS_KEY=key
ANYSDA_S3_SECRET_KEY=secret
ANYSDA_BACKUP_PASSPHRASE=test-backup-passphrase
''')
        Path('/test-output/s3').mkdir(exist_ok=True)
        write('/test-bin/aws', '''#!/bin/sh
if [ -f /test-output/s3-down ]; then
    echo 'Could not connect to the endpoint URL' >&2; exit 255
fi
shift 3
case "$1" in
    ls) for f in /test-output/s3/*; do
            [ -e "$f" ] && echo "2026-01-01 00:00:00 $(stat -c %s "$f") ${f##*/}"
        done ;;
    cp) cp "/test-output/s3/${2#s3://bucket/}" "$3" ;;
esac
''', 0o755)

    def test_latest_prefers_newer_s3_archive(self):
        self.backup()
        local = next(BACKUPS.glob('*.age'))
        time.sleep(1.1)
        self.conn.execute("INSERT INTO clients VALUES ('s3-client')")
        self.conn.commit()
        self.assertEqual(self.script('backup').returncode, 0)
        fresh = next(p for p in BACKUPS.glob('*.age') if p != local)
        self.use_s3()
        shutil.move(fresh, Path('/test-output/s3') / fresh.name)
        # mtime не время создания: локальный старый архив "трогали" позже.
        os.utime(local, (time.time() + 3600, time.time() + 3600))
        result = self.script('restore', 'latest', '--force')
        print(result.stdout.strip().splitlines()[0], flush=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(f'latest = {fresh.name} (S3)', result.stdout)
        self.assertIn(f'самый свежий local: {local.name}', result.stdout)
        with sqlite3.connect(DB) as restored:
            rows = restored.execute('SELECT name FROM clients').fetchall()
        print(f'latest_restored_clients={rows}', flush=True)
        self.assertEqual(rows, [('archived-client',), ('s3-client',)])

    def test_latest_dry_run_and_unreachable_s3(self):
        self.backup()
        local = next(BACKUPS.glob('*.age'))
        self.use_s3()
        write('/test-output/s3-down', '1')
        self.conn.execute("INSERT INTO clients VALUES ('live-client')")
        self.conn.commit()
        result = self.script('restore', 'latest', '--force', '--dry-run')
        print(result.stdout, flush=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('S3 недоступен', result.stdout)
        self.assertIn(f'dry-run — восстановил бы {local.name} (local)', result.stdout)
        self.assertFalse((BACKUPS / '.pre-restore').exists())
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM clients').fetchone()[0], 2)

    def test_dry_run_without_archive_name(self):
        # Прямой вызов на entry: первый аргумент-флаг — это latest, не имя архива.
        self.backup()
        local = next(BACKUPS.glob('*.age'))
        for args in (('--dry-run',), ('--force', '--dry-run')):
            with self.subTest(args):
                result = self.script('restore', *args)
                print(result.stdout.strip(), flush=True)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn(f'dry-run — восстановил бы {local.name} (local), ничего не изменено', result.stdout)
        self.assertFalse((BACKUPS / '.pre-restore').exists())


    def repack(self, bundle):
        archive = next(BACKUPS.glob('*.age'))
        with tarfile.open('/test-output/repacked.tar.gz', 'w:gz') as tar:
            tar.add(bundle, arcname=bundle.name)
        archive.unlink()
        encrypt('/test-output/repacked.tar.gz', archive)

    def assert_untouched(self, result, reason):
        print(result.stdout.strip().splitlines()[-1], flush=True)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(reason, result.stdout)
        self.assertFalse((BACKUPS / '.pre-restore').exists())
        self.assertEqual(self.conn.execute('SELECT name FROM clients').fetchall(),
                         [('archived-client',), ('live-client',)])
        self.assertEqual(Path(FILES[0]).read_text(), body(FILES[0], 'live'))

    def test_unknown_spec_version_refused_before_changes(self):
        bundle = self.backup()
        manifest = json.loads((bundle / 'manifest.json').read_text())
        manifest['spec_version'] = 2
        (bundle / 'manifest.json').write_text(json.dumps(manifest))
        self.repack(bundle)
        self.conn.execute("INSERT INTO clients VALUES ('live-client')")
        self.conn.commit()
        write(FILES[0], body(FILES[0], 'live'))
        self.assert_untouched(self.script('restore', 'latest', '--force'), 'spec_version=2')
        result = self.script('restore', 'latest', '--force', '--allow-incompatible')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(Path(FILES[0]).read_text(), body(FILES[0], 'archived'))

    def test_schema_newer_or_diverged_refused_older_accepted(self):
        cases = (
            ('newer', "INSERT INTO __drizzle_migrations (hash, created_at) VALUES ('f' || hex(randomblob(31)), 9999999999999)",
             'схема БД архива новее'),
            ('diverged', "UPDATE __drizzle_migrations SET hash = 'other' "
                         "WHERE id = (SELECT MIN(id) + 1 FROM __drizzle_migrations)", 'расходится'),
            ('older', 'DELETE FROM __drizzle_migrations WHERE id = (SELECT MAX(id) FROM __drizzle_migrations)', None),
        )
        for name, change, reason in cases:
            with self.subTest(name):
                shutil.rmtree(BACKUPS, ignore_errors=True)
                shutil.rmtree('/test-output/backup', ignore_errors=True)
                # Успешный restore подменяет файл базы: открываем текущий.
                self.conn.close()
                self.conn = sqlite3.connect(DB)
                self.conn.execute('DELETE FROM clients')
                self.conn.execute('DELETE FROM __drizzle_migrations')
                self.conn.executemany('INSERT INTO __drizzle_migrations (hash, created_at) VALUES (?, ?)', journal())
                self.conn.execute("INSERT INTO clients VALUES ('archived-client')")
                self.conn.execute(change)
                self.conn.commit()
                write(FILES[0], body(FILES[0], 'archived'))
                self.backup()
                self.conn.execute("INSERT INTO clients VALUES ('live-client')")
                self.conn.commit()
                write(FILES[0], body(FILES[0], 'live'))
                result = self.script('restore', 'latest', '--force')
                if reason:
                    self.assert_untouched(result, reason)
                else:
                    print(next(line for line in result.stdout.splitlines() if 'схема БД' in line), flush=True)
                    self.assertEqual(result.returncode, 0, result.stdout)
                    self.assertIn('1 догонит стадия 30', result.stdout)
                    self.assertEqual(Path(FILES[0]).read_text(), body(FILES[0], 'archived'))


    def foreign_owner(self):
        """Живые каталоги с владельцем и режимом, которых нет ни в архиве, ни у WORK restore."""
        saved = {path: path.stat() for path in LIVE_DIRS}

        def put_back():
            for path, st in saved.items():
                if path.is_dir():
                    os.chown(path, st.st_uid, st.st_gid)
                    path.chmod(st.st_mode & 0o7777)
        self.addCleanup(put_back)
        for path in LIVE_DIRS:
            os.chown(path, 4242, 4242)
            path.chmod(0o750)

    @staticmethod
    def dir_state():
        return {str(p): f'{p.stat().st_uid}:{p.stat().st_gid} {p.stat().st_mode & 0o7777:o}' for p in LIVE_DIRS}

    def host_state(self):
        paths = [Path(p) for p in (*FILES, *EXTRA)] + [Path('/etc/wireguard/keys/server.priv')]
        paths += sorted(p for p in Path('/etc/openvpn').rglob('*') if p.is_file())
        return {**{str(p): p.read_bytes() for p in paths}, **self.dir_state()}

    def test_restore_keeps_live_directory_owner_and_mode(self):
        self.backup()
        self.foreign_owner()
        before = self.dir_state()
        result = self.script('restore', 'latest', '--force')
        self.assertEqual(result.returncode, 0, result.stdout)
        after = self.dir_state()
        print('changed:', {p: (before[p], after[p]) for p in before if before[p] != after[p]}, flush=True)
        self.assertEqual(after, before)
        self.assertEqual(Path(FILES[0]).read_text(), body(FILES[0], 'archived'))

    def test_failure_after_stop_rolls_back_and_starts_services(self):
        write('/etc/openvpn/server/ccd/archived-only', 'must not survive rollback')
        self.backup()
        Path('/etc/openvpn/server/ccd/archived-only').unlink()
        self.conn.execute("INSERT INTO clients VALUES ('live-client')")
        self.conn.commit()
        self.conn.close()
        for path in (*FILES, *EXTRA):
            write(path, body(path, 'live'))
        write('/etc/wireguard/keys/server.priv', WG_KEYS['live'])
        write('/test-bin/docker', '''#!/bin/sh
echo "docker $*" >> /test-output/services
exit 0
''', 0o755)
        # Владелец и режим каталога данных — как у панели, а не как у WORK restore.
        self.foreign_owner()
        failures = {
            'db': ('mv', 'case "$*" in *.db-restore.sqlite*) exit 1;; esac; exec /usr/bin/mv "$@"',
                   'установка БД'),
            'pki': ('cp', 'case "$*" in */etc-openvpn/.*) exit 1;; esac; exec /usr/bin/cp "$@"',
                    'распаковка PKI OpenVPN'),
            'signal': ('cp', 'case "$*" in */etc-openvpn/.*) kill -TERM $PPID;; esac; exec /usr/bin/cp "$@"',
                       'распаковка PKI OpenVPN'),
            'service': ('systemctl', 'echo "$*" >> /test-output/services\n'
                        'if [ "$*" = "start openvpn-server@server" ] && [ ! -e /test-output/failed-once ]; then\n'
                        '    touch /test-output/failed-once; exit 1\nfi\nexit 0', 'запуск службы openvpn-server@server'),
        }
        for name, (command, script, step) in failures.items():
            with self.subTest(name):
                for stub in ('mv', 'cp', 'failed-once', 'services'):
                    Path('/test-bin', stub).unlink(missing_ok=True)
                    Path('/test-output', stub).unlink(missing_ok=True)
                write('/test-bin/systemctl', '#!/bin/sh\necho "$*" >> /test-output/services\nexit 0\n', 0o755)
                write('/test-bin/' + command, '#!/bin/sh\n' + script + '\n', 0o755)
                shutil.rmtree(BACKUPS / '.pre-restore', ignore_errors=True)
                before = self.host_state()
                db_before = DB.read_bytes()
                result = self.script('restore', 'latest', '--force')
                print('\n'.join(line for line in result.stdout.splitlines() if 'FAILED' in line
                                 or 'возвращено' in line or 'запущены' in line), flush=True)
                self.assertEqual(result.returncode, 10, result.stdout)
                self.assertIn(f'на шаге «{step}»', result.stdout)
                self.assertEqual(self.host_state(), before)
                safety = self.safety() / str(DB).lstrip('/')
                self.assertIn(DB.read_bytes(), (db_before, safety.read_bytes()))
                shutil.rmtree('/test-output/safety')
                with sqlite3.connect(DB) as live:
                    self.assertEqual(live.execute('SELECT name FROM clients').fetchall(),
                                     [('archived-client',), ('live-client',)])
                services = Path('/test-output/services').read_text().splitlines()
                for started in ('start sing-box', 'start openvpn-server@server', 'start strongswan-starter',
                                'docker start anysda-vpn2', 'docker start anysda-tgbot'):
                    self.assertIn(started, services[services.index('docker stop anysda-tgbot'):])
                self.assertIn('службы и контейнеры прежнего состояния запущены', result.stdout)


    def pki(self):
        """CA, сертификаты устройства 0x10 и 0x11, CRL пустой и с отозванным 0x10."""
        pki = Path('/test-output/pki')
        pki.mkdir()
        (pki / 'cnf').write_text('[ca]\ndefault_ca = x\n[x]\ndatabase = index.txt\ncrlnumber = crlnumber\n'
                                 'default_md = sha256\ndefault_crl_days = 30\n')
        (pki / 'index.txt').write_text('')
        (pki / 'crlnumber').write_text('01\n')
        ec = ('-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes')
        ca = ('-config', 'cnf', '-keyfile', 'ca.key', '-cert', 'ca.crt')
        steps = [('req', '-x509', *ec, '-keyout', 'ca.key', '-out', 'ca.crt', '-subj', '/CN=test-ca', '-days', '30')]
        for serial in ('10', '11'):
            steps += [('req', '-new', *ec, '-keyout', serial + '.key', '-out', serial + '.csr', '-subj', '/CN=dev_1'),
                      ('x509', '-req', '-in', serial + '.csr', '-CA', 'ca.crt', '-CAkey', 'ca.key',
                       '-set_serial', '0x' + serial, '-days', '30', '-out', serial + '.pem')]
        steps += [('ca', '-batch', *ca, '-gencrl', '-out', 'empty.pem'),
                  ('ca', '-batch', *ca, '-revoke', '10.pem'),
                  ('ca', '-batch', *ca, '-gencrl', '-out', 'revoked.pem')]
        for step in steps:
            result = run('openssl', *step, cwd=pki)
            self.assertEqual(result.returncode, 0, result.stdout)
        return pki

    def revoked_active(self, bundle):
        """Серийники действующих сертификатов БД архива, отозванные в его CRL (по openssl)."""
        crl = run('openssl', 'crl', '-noout', '-text', '-in', str(bundle / 'etc-openvpn/server/pki/crl.pem')).stdout
        revoked = {int(n, 16) for n in re.findall(r'Serial Number: ([0-9A-F]+)', crl)}
        with sqlite3.connect(bundle / 'db.sqlite') as db:
            certs = [c for (c,) in db.execute('SELECT ovpn_cert FROM devices WHERE deleting = 0')]
        serials = {int(run('openssl', 'x509', '-noout', '-serial', input=c).stdout.split('=')[1], 16)
                   for c in certs}
        return serials & revoked, serials

    def test_reissue_during_backup_gives_consistent_archive(self):
        pki = self.pki()
        write('/etc/openvpn/server/pki/crl.pem', (pki / 'empty.pem').read_text(), 0o644)
        write('/etc/openvpn/server/.pki.lock', '', 0o644)
        self.conn.execute('CREATE TABLE devices (id INTEGER PRIMARY KEY, deleting INTEGER NOT NULL DEFAULT 0, '
                          'ovpn_cert TEXT)')
        self.conn.execute('INSERT INTO devices VALUES (1, 0, ?)', ((pki / '10.pem').read_text(),))
        self.conn.commit()
        # Перевыпуск посреди бэкапа: отзыв старого сертификата попадает в CRL,
        # а новый сертификат панель пишет в БД отдельным шагом чуть позже.
        write('/test-bin/sqlite3', f"""#!/bin/sh
case "$*" in *".backup "*)
  if [ ! -e /test-output/reissued ]; then
    touch /test-output/reissued
    if flock -n -x /etc/openvpn/server/.pki.lock true; then echo free; else echo held; fi > /test-output/lock
    cp {pki}/revoked.pem /etc/openvpn/server/pki/crl.pem
    /usr/bin/sqlite3 "$@" || exit
    (sleep 1; /usr/bin/sqlite3 {DB} ".timeout 10000" \\
      "UPDATE devices SET ovpn_cert = CAST(readfile('{pki}/11.pem') AS TEXT) WHERE id = 1") >/dev/null 2>&1 &
    exit 0
  fi;;
esac
exec /usr/bin/sqlite3 "$@"
""", 0o755)
        bundle = self.backup()
        bad, serials = self.revoked_active(bundle)
        lock = Path('/test-output/lock').read_text().strip()
        print(f'archive_active={sorted(serials)} revoked_active={sorted(bad)} lock_during_db_snapshot={lock}', flush=True)
        self.assertEqual(bad, set())
        self.assertEqual(serials, {0x11})
        self.assertEqual(lock, 'held')

        # Старый архив с расхождением: restore предупреждает и называет устройство.
        Path('/test-bin/sqlite3').unlink()
        shutil.copy(REPO / 'infra/lib/anysda-backup.sh', '/usr/local/bin/anysda-backup.sh')
        Path('/usr/local/bin/anysda-backup.sh').chmod(0o755)
        self.addCleanup(Path('/usr/local/bin/anysda-backup.sh').unlink)
        shutil.copy(pki / 'revoked.pem', bundle / 'etc-openvpn/server/pki/crl.pem')
        with sqlite3.connect(bundle / 'db.sqlite') as db:
            db.execute('UPDATE devices SET ovpn_cert = ? WHERE id = 1', ((pki / '10.pem').read_text(),))
        manifest = json.loads((bundle / 'manifest.json').read_text())
        for name in ('db.sqlite', 'etc-openvpn'):
            manifest['components'][name] = self.digest(bundle / name)
        (bundle / 'manifest.json').write_text(json.dumps(manifest))
        self.repack(bundle)
        result = self.script('restore', 'latest', '--force')
        print('\n'.join(line for line in result.stdout.splitlines() if 'CRL' in line or 'отозван' in line), flush=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('в архиве БД и CRL OpenVPN расходятся', result.stdout)
        self.assertIn('устройство 1: сертификат 10 отозван в CRL', result.stdout)

    @staticmethod
    def access_configs(devices):
        """wg0.conf и anysda-clients.conf так, как их пишет vpn2-control."""
        wg = f'[Interface]\nPrivateKey = {WG_KEYS["archived"]}\n' + ''.join(
            f'\n[Peer]\nPublicKey = {d["key"]}\nPresharedKey = {d["psk"]}\nAllowedIPs = {d["wg_ip"]}/32\n'
            for d in devices)
        conns = ''.join(f'  anysda-dev-{d["id"]} : connections.anysda-ikev2 {{\n'
                        f'    remote-client {{\n      eap_id = "{d["user"]}"\n    }}\n  }}\n' for d in devices)
        secrets = ''.join(f'  eap-{d["id"]} {{\n    id = "{d["user"]}"\n    secret = "{d["password"]}"\n  }}\n'
                          for d in devices)
        return wg, f'connections {{\n{conns}}}\nsecrets {{\n{secrets}}}\n'

    def test_issue_and_revoke_wg_ikev2_during_backup_give_consistent_archive(self):
        def device(n, client):
            return {'id': n, 'client': client, 'key': base64.b64encode(bytes([n]) * 32).decode(),
                    'psk': base64.b64encode(bytes([n + 100]) * 32).decode(), 'wg_ip': f'10.66.66.{n + 1}',
                    'user': f'dev{n}', 'password': f'secret{n}', 'ike_ip': f'10.99.1.{n + 1}'}
        kept, revoked, issued, freezing = device(1, 1), device(2, 1), device(3, 1), device(4, 2)
        self.conn.execute('DROP TABLE clients')
        self.conn.execute('CREATE TABLE clients (id INTEGER PRIMARY KEY, frozen_manual INTEGER NOT NULL DEFAULT 0, '
                          'expires_at INTEGER)')
        self.conn.execute('CREATE TABLE devices (id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL, '
                          'deleting INTEGER NOT NULL DEFAULT 0, wg_public_key TEXT, wg_preshared_key TEXT, wg_ip TEXT, '
                          'ikev2_username TEXT, ikev2_password TEXT, ikev2_ip TEXT)')
        self.conn.execute('CREATE TABLE access_operations (id TEXT PRIMARY KEY, client_id INTEGER NOT NULL, '
                          'status TEXT NOT NULL, payload TEXT NOT NULL)')
        # Клиент 2 замораживается: операция доступа ещё в очереди, пир пока на месте.
        self.conn.execute('INSERT INTO clients VALUES (1, 0, ?), (2, 1, NULL)', (int(time.time()) + 86400,))
        self.conn.execute("INSERT INTO access_operations VALUES ('op', 2, 'pending', '{\"retired\": []}')")
        self.conn.executemany('INSERT INTO devices VALUES (:id, :client, 0, :key, :psk, :wg_ip, :user, :password, '
                              ':ike_ip)', [kept, revoked, freezing])
        self.conn.commit()
        before_wg, before_ike = self.access_configs([kept, revoked, freezing])
        after_wg, after_ike = self.access_configs([kept, issued, freezing])
        write('/etc/wireguard/wg0.conf', before_wg)
        write('/etc/swanctl/conf.d/anysda-clients.conf', before_ike)
        write('/test-output/after-wg', after_wg)
        write('/test-output/after-ike', after_ike)
        write('/test-output/issue-revoke.sql', 'DELETE FROM devices WHERE id = 2;\n'
              f"INSERT INTO devices VALUES (3, 1, 0, '{issued['key']}', '{issued['psk']}', '{issued['wg_ip']}', "
              f"'dev3', 'secret3', '{issued['ike_ip']}');\n")
        # Посреди бэкапа панель выдаёт устройство 3 и удаляет 2: БД уже новая,
        # а конфиги WireGuard и IKEv2 панель применяет отдельным шагом позже.
        write('/test-bin/sqlite3', f"""#!/bin/sh
case "$*" in *".backup "*)
  if [ ! -e /test-output/applied ]; then
    touch /test-output/applied
    /usr/bin/sqlite3 {DB} ".timeout 10000" ".read /test-output/issue-revoke.sql" || exit
    /usr/bin/sqlite3 "$@" || exit
    (sleep 1; cp /test-output/after-wg /etc/wireguard/wg0.conf
     cp /test-output/after-ike /etc/swanctl/conf.d/anysda-clients.conf) >/dev/null 2>&1 &
    exit 0
  fi;;
esac
exec /usr/bin/sqlite3 "$@"
""", 0o755)
        bundle = self.backup()
        db = sqlite3.connect(bundle / 'db.sqlite')
        archived = sorted(n for (n,) in db.execute('SELECT id FROM devices'))
        db.close()
        wg = (bundle / 'etc-wireguard/wg0.conf').read_text()
        ike = (bundle / 'etc-swanctl/conf.d/anysda-clients.conf').read_text()
        peers = sorted(d['id'] for d in (kept, revoked, issued, freezing) if d['key'] in wg)
        accounts = sorted(int(n) for n in re.findall(r'anysda-dev-(\d+) :', ike))
        print(f'archive_devices={archived} wg_peers={peers} ikev2_accounts={accounts}', flush=True)
        self.assertEqual(archived, [1, 3, 4])
        self.assertEqual(peers, [1, 3, 4])
        self.assertEqual(accounts, [1, 3, 4])

        # Старый архив с расхождением: restore предупреждает и называет устройства.
        Path('/test-bin/sqlite3').unlink()
        shutil.copy(REPO / 'infra/lib/anysda-backup.sh', '/usr/local/bin/anysda-backup.sh')
        Path('/usr/local/bin/anysda-backup.sh').chmod(0o755)
        self.addCleanup(Path('/usr/local/bin/anysda-backup.sh').unlink)
        (bundle / 'etc-wireguard/wg0.conf').write_text(before_wg)
        (bundle / 'etc-swanctl/conf.d/anysda-clients.conf').write_text(before_ike)
        manifest = json.loads((bundle / 'manifest.json').read_text())
        for name in ('etc-wireguard', 'etc-swanctl'):
            manifest['components'][name] = self.digest(bundle / name)
        (bundle / 'manifest.json').write_text(json.dumps(manifest))
        self.repack(bundle)
        result = self.script('restore', 'latest', '--force')
        print('\n'.join(line for line in result.stdout.splitlines()
                        if 'WireGuard' in line or 'IKEv2' in line), flush=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('в архиве БД и пиры WireGuard, учётки IKEv2 расходятся', result.stdout)
        for line in ('устройство 3: нет пира WireGuard', 'устройство 3: нет учётки IKEv2',
                     f'пир WireGuard {revoked["key"]}: устройства нет в БД',
                     'устройство 2: учётка IKEv2 есть, устройства нет в БД'):
            self.assertIn(line, result.stdout)
        self.assertNotIn('устройство 4', result.stdout)

    def live_pki(self):
        """PKI как у стадии 29: openssl ca ведёт index, serial и CRL в /etc/openvpn/server/pki."""
        pki = Path('/etc/openvpn/server/pki')
        shutil.rmtree('/etc/openvpn', ignore_errors=True)
        self.addCleanup(shutil.rmtree, '/etc/openvpn', True)
        (pki / 'newcerts').mkdir(parents=True)
        (pki / 'openssl.cnf').write_text(
            f'[ca]\ndefault_ca = x\n[x]\ndir = {pki}\ndatabase = $dir/index.txt\nnew_certs_dir = $dir/newcerts\n'
            'certificate = $dir/ca.crt\nprivate_key = $dir/ca.key\nserial = $dir/serial\n'
            'crlnumber = $dir/crlnumber\ndefault_md = sha256\ndefault_days = 30\ndefault_crl_days = 30\n'
            'policy = any\nunique_subject = no\n[any]\ncommonName = supplied\n[client_ext]\nbasicConstraints = CA:FALSE\n')
        (pki / 'index.txt').write_text('')
        (pki / 'serial').write_text('01\n')
        (pki / 'crlnumber').write_text('01\n')
        ec = ('-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes')
        self.openssl('req', '-x509', *ec, '-keyout', 'ca.key', '-out', 'ca.crt', '-subj', '/CN=test-ca', '-days', '30')
        self.openssl('ca', '-batch', '-config', 'openssl.cnf', '-gencrl', '-out', 'crl.pem')
        return pki

    def openssl(self, *args):
        result = run('openssl', *args, cwd='/etc/openvpn/server/pki')
        self.assertEqual(result.returncode, 0, result.stdout)
        return result.stdout

    def issue(self, n):
        ec = ('-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes')
        self.openssl('req', '-new', *ec, '-keyout', f'/test-output/{n}.key', '-out', f'/test-output/{n}.csr',
                     '-subj', f'/CN=dev_{n}')
        self.openssl('ca', '-batch', '-notext', '-config', 'openssl.cnf', '-extensions', 'client_ext',
                     '-in', f'/test-output/{n}.csr', '-out', f'/test-output/{n}.pem')
        return Path(f'/test-output/{n}.pem').read_text()

    def revoke(self, n):
        self.openssl('ca', '-batch', '-config', 'openssl.cnf', '-revoke', f'/test-output/{n}.pem')
        self.openssl('ca', '-batch', '-config', 'openssl.cnf', '-gencrl', '-out', 'crl.pem')

    def revocation_fixture(self, archived=(1, 2)):
        """Устройства в архиве; WG, OpenVPN и IKEv2 у каждого, конфиги как у vpn2-control."""
        self.live_pki()
        self.conn.execute('CREATE TABLE devices (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER, '
                          'wg_private_key TEXT, wg_public_key TEXT, wg_preshared_key TEXT, wg_ip TEXT, '
                          'ovpn_cert TEXT, ovpn_key TEXT, ikev2_username TEXT, ikev2_password TEXT, ikev2_ip TEXT)')
        devices = {}
        for n in (1, 2, 3):
            devices[n] = {'id': n, 'key': base64.b64encode(bytes([n]) * 32).decode(),
                          'psk': base64.b64encode(bytes([n + 100]) * 32).decode(), 'wg_ip': f'10.66.66.{n + 1}',
                          'user': f'dev{n}', 'password': f'secret{n}', 'ike_ip': f'10.99.1.{n + 1}'}
        for n in archived:
            self.add_device(devices[n])
        self.apply_configs([devices[n] for n in archived])
        return devices

    def add_device(self, d):
        cert = self.issue(d['id'])
        self.conn.execute('INSERT INTO devices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                          (d['id'], 1000 + d['id'], 'private', d['key'], d['psk'], d['wg_ip'], cert, 'key',
                           d['user'], d['password'], d['ike_ip']))
        self.conn.commit()

    def apply_configs(self, devices):
        wg, ike = self.access_configs(devices)
        write('/etc/wireguard/wg0.conf', wg)
        write('/etc/swanctl/conf.d/anysda-clients.conf', ike)

    def test_restore_keeps_revocations_made_after_archive(self):
        devices = self.revocation_fixture()
        self.backup()
        # После архива: устройство 3 выдано и удалено, устройство 2 удалено,
        # у устройства 1 перевыпущен пароль IKEv2.
        self.add_device(devices[3])
        self.revoke(3)
        self.conn.execute('DELETE FROM devices WHERE id = 3')
        self.revoke(2)
        self.conn.execute('DELETE FROM devices WHERE id = 2')
        devices[1]['password'] = 'rotated1'
        self.conn.execute("UPDATE devices SET ikev2_password = 'rotated1' WHERE id = 1")
        self.conn.commit()
        self.apply_configs([devices[1]])
        live_serial = int(Path('/etc/openvpn/server/pki/serial').read_text(), 16)

        result = self.script('restore', 'latest', '--force')
        print('\n'.join(line for line in result.stdout.splitlines() if 'отозван' in line), flush=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        wg = Path('/etc/wireguard/wg0.conf').read_text()
        self.assertNotIn(devices[2]['key'], wg, 'удалённый пир WireGuard вернулся')
        self.assertIn(devices[1]['key'], wg)
        ike = Path('/etc/swanctl/conf.d/anysda-clients.conf').read_text()
        for gone in ('eap-2', 'secret2', 'secret1'):
            self.assertNotIn(gone, ike, 'отозванная учётка IKEv2 вернулась')
        index = {line.split('\t')[3]: line.split('\t')[0]
                 for line in Path('/etc/openvpn/server/pki/index.txt').read_text().splitlines()}
        self.assertEqual(index, {'01': 'V', '02': 'R', '03': 'R'})
        crl = self.openssl('crl', '-in', 'crl.pem', '-noout', '-text')
        self.assertEqual(sorted(re.findall(r'Serial Number: ([0-9A-F]+)', crl)), ['02', '03'])
        self.openssl('crl', '-in', 'crl.pem', '-noout', '-verify', '-CAfile', 'ca.crt')
        self.assertGreaterEqual(int(Path('/etc/openvpn/server/pki/serial').read_text(), 16), live_serial)
        with sqlite3.connect(DB) as db:
            rows = {r[0]: r[1:] for r in db.execute(
                'SELECT id, wg_public_key, ovpn_cert IS NOT NULL, ikev2_password FROM devices')}
        self.assertEqual(rows, {1: (devices[1]['key'], 1, None), 2: (None, 0, None)})
        self.assertIn('отозванных записей применено поверх архива: 5 (WireGuard 1, OpenVPN 2, IKEv2 2)',
                      result.stdout)
        self.assertIn('устройство 2: IKEv2, OpenVPN, WireGuard отозваны; удалено после архива', result.stdout)
        self.assertIn('устройство 1: IKEv2 отозваны; перевыпущено после архива', result.stdout)

    def test_foreign_install_revocations_do_not_touch_archive(self):
        devices = self.revocation_fixture()
        self.backup()
        # Перенос на новый узел: свой CA, свой ключ WireGuard, устройств нет.
        self.live_pki()
        self.conn.execute('DELETE FROM devices')
        self.conn.commit()
        write('/etc/wireguard/wg0.conf', body('/etc/wireguard/wg0.conf', 'live'))
        for name in ('server.priv', 'server.pub'):
            write('/etc/wireguard/keys/' + name, WG_KEYS['live'])
        result = self.script('restore', 'latest', '--force')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('отозванных записей применено поверх архива: 0 — архив другой установки', result.stdout)
        wg = Path('/etc/wireguard/wg0.conf').read_text()
        self.assertTrue(all(devices[n]['key'] in wg for n in (1, 2)))
        with sqlite3.connect(DB) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM devices WHERE wg_public_key IS NOT NULL '
                                        'AND ovpn_cert IS NOT NULL AND ikev2_password IS NOT NULL').fetchone(), (2,))

    # VPN2-556: устройство выдано после архива, в архиве его нет.
    NO_ACCESS = dict.fromkeys(('OpenVPN', 'WireGuard', 'IKEv2', 'ccd'), False)
    FULL_ACCESS = dict.fromkeys(NO_ACCESS, True)

    @staticmethod
    def ccd(d):
        write(f'/etc/openvpn/server/ccd/dev_{d["id"]}', f'ifconfig-push 10.67.67.{d["id"] + 1} 255.255.255.0\n', 0o644)

    def serial(self, n):
        return int(self.openssl('x509', '-noout', '-serial', '-in', f'/test-output/{n}.pem').strip().partition('=')[2], 16)

    def crl_serials(self):
        crl = self.openssl('crl', '-in', 'crl.pem', '-noout', '-text')
        return [int(s, 16) for s in re.findall(r'Serial Number: ([0-9A-F]+)', crl)]

    def access(self, d):
        """Чем устройство ещё может подключиться после restore."""
        ike = Path('/etc/swanctl/conf.d/anysda-clients.conf').read_text()
        return {'OpenVPN': self.serial(d['id']) not in self.crl_serials(),
                'WireGuard': d['key'] in Path('/etc/wireguard/wg0.conf').read_text(),
                'IKEv2': f'eap-{d["id"]} {{' in ike or f'"{d["password"]}"' in ike,
                'ccd': Path(f'/etc/openvpn/server/ccd/dev_{d["id"]}').exists()}

    def issued_after_archive(self, archived=(1, 2), new=3):
        devices = self.revocation_fixture(archived)
        for n in archived:
            self.ccd(devices[n])
        self.backup()
        self.add_device(devices[new])
        self.ccd(devices[new])
        self.apply_configs([devices[n] for n in (*archived, new)])
        return devices

    def restore(self, archive='latest'):
        result = self.script('restore', archive, '--force')
        print('\n'.join(line for line in result.stdout.splitlines() if 'отозван' in line), flush=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        return result

    def test_issued_after_archive_loses_access(self):
        devices = self.issued_after_archive()
        self.restore()
        self.assertEqual(self.access(devices[3]), self.NO_ACCESS, 'устройство 3 выдано после архива — доступ остался')

    def test_issued_after_archive_archived_devices_keep_access(self):
        devices = self.issued_after_archive()
        self.restore()
        for n in (1, 2):
            self.assertEqual(self.access(devices[n]), self.FULL_ACCESS, f'устройство {n} из архива потеряло доступ')
        with sqlite3.connect(DB) as db:
            rows = {r[0]: r[1:] for r in db.execute('SELECT id, wg_public_key IS NOT NULL, ovpn_cert IS NOT NULL, '
                                                    'ikev2_password IS NOT NULL FROM devices')}
        self.assertEqual(rows, {1: (1, 1, 1), 2: (1, 1, 1)})

    def test_issued_after_archive_id_not_reused(self):
        self.issued_after_archive()
        self.restore()
        with sqlite3.connect(DB) as db:
            new = db.execute('INSERT INTO devices (created_at) VALUES (9999)').lastrowid
        self.assertGreater(new, 3, 'новое устройство получило id устройства, выданного после архива')

    def test_issued_after_archive_and_deleted_stays_revoked(self):
        devices = self.issued_after_archive()
        self.revoke(3)
        self.conn.execute('DELETE FROM devices WHERE id = 3')
        self.conn.commit()
        Path('/etc/openvpn/server/ccd/dev_3').unlink()
        self.apply_configs([devices[1], devices[2]])
        self.restore()
        self.assertEqual(self.access(devices[3]), self.NO_ACCESS)
        self.assertEqual(self.crl_serials(), [self.serial(3)])
        for n in (1, 2):
            self.assertEqual(self.access(devices[n]), self.FULL_ACCESS, f'устройство {n} из архива потеряло доступ')

    def test_issued_after_archive_restore_twice(self):
        devices = self.issued_after_archive()
        archive = next(BACKUPS.glob('*.age')).name
        self.restore(archive)
        first = self.crl_serials()
        self.restore(archive)
        second = self.crl_serials()
        self.assertEqual(len(second), len(set(second)), f'дубли в CRL: {second}')
        self.assertEqual(sorted(second), sorted(first), 'повторный restore изменил набор отозванного')
        self.assertEqual(self.access(devices[3]), self.NO_ACCESS, 'после повторного restore у устройства 3 доступ')

    def test_issued_after_archive_without_devices(self):
        devices = self.issued_after_archive(archived=(), new=1)
        self.restore()
        self.assertEqual(self.access(devices[1]), self.NO_ACCESS, 'в архиве нет устройств — доступ у устройства 1 остался')

    def archive_name(self):
        """Снимок узла; имя нового архива (имя — время до секунды)."""
        before = {p.name for p in BACKUPS.glob('*.age')}
        time.sleep(1.1)
        result = self.script('backup')
        self.assertEqual(result.returncode, 0, result.stdout)
        (name,) = {p.name for p in BACKUPS.glob('*.age')} - before
        return name

    def issued_after_archive_then_newer(self):
        """Архив A (1, 2), выдано 3, архив B (1, 2, 3); restore A, затем restore B."""
        devices = self.revocation_fixture()
        for n in (1, 2):
            self.ccd(devices[n])
        older = self.archive_name()
        self.add_device(devices[3])
        self.ccd(devices[3])
        self.apply_configs([devices[n] for n in (1, 2, 3)])
        newer = self.archive_name()
        self.restore(older)
        return devices, self.restore(newer)

    def test_issued_after_archive_newer_archive_restores_access(self):
        devices, result = self.issued_after_archive_then_newer()
        self.assertEqual(self.access(devices[3]), self.FULL_ACCESS, 'устройство 3 есть в новом архиве — доступа нет')
        for n in (1, 2):
            self.assertEqual(self.access(devices[n]), self.FULL_ACCESS, f'устройство {n} из архива потеряло доступ')
        with sqlite3.connect(DB) as db:
            rows = {r[0]: r[1:] for r in db.execute('SELECT id, wg_public_key IS NOT NULL, ovpn_cert IS NOT NULL, '
                                                    'ikev2_password IS NOT NULL FROM devices')}
        self.assertEqual(rows, {1: (1, 1, 1), 2: (1, 1, 1), 3: (1, 1, 1)})
        self.assertNotIn('устройство 3:', result.stdout)

    def test_issued_after_archive_newer_archive_id_not_reused(self):
        self.issued_after_archive_then_newer()
        with sqlite3.connect(DB) as db:
            new = db.execute('INSERT INTO devices (created_at) VALUES (9999)').lastrowid
        self.assertGreater(new, 3, 'новое устройство получило уже выданный id')

    def test_issued_after_archive_same_id_other_device_revoked(self):
        devices = self.revocation_fixture()
        for n in (1, 2):
            self.ccd(devices[n])
        self.backup()
        # Под номером 2 в живой БД другое устройство: свои created_at, ключ, сертификат, пароль.
        other = {'id': 2, 'key': base64.b64encode(bytes([42]) * 32).decode(),
                 'psk': base64.b64encode(bytes([142]) * 32).decode(), 'wg_ip': '10.66.66.43',
                 'user': 'other2', 'password': 'othersecret2', 'ike_ip': '10.99.1.43'}
        cert = self.issue(42)
        self.conn.execute('UPDATE devices SET created_at = 5002, wg_public_key = ?, wg_preshared_key = ?, wg_ip = ?, '
                          'ovpn_cert = ?, ikev2_username = ?, ikev2_password = ?, ikev2_ip = ? WHERE id = 2',
                          (other['key'], other['psk'], other['wg_ip'], cert, other['user'], other['password'],
                           other['ike_ip']))
        self.conn.commit()
        self.apply_configs([devices[1], other])
        self.restore()
        self.assertIn(self.serial(42), self.crl_serials(), 'сертификат чужого устройства 2 не отозван')
        self.assertNotIn(other['key'], Path('/etc/wireguard/wg0.conf').read_text(), 'пир чужого устройства 2 остался')
        self.assertNotIn(f'"{other["password"]}"', Path('/etc/swanctl/conf.d/anysda-clients.conf').read_text(),
                         'секрет IKEv2 чужого устройства 2 остался')
        for n in (1, 2):
            self.assertEqual(self.access(devices[n]), self.FULL_ACCESS, f'устройство {n} из архива потеряло доступ')
        with sqlite3.connect(DB) as db:
            row = db.execute('SELECT created_at, wg_public_key, ikev2_password FROM devices WHERE id = 2').fetchone()
        self.assertEqual(row, (1002, devices[2]['key'], devices[2]['password']))

    def lose_live_db(self, corrupt=False):
        """Живая БД потеряна (файла нет) или испорчена (таблица устройств не читается), PKI и ccd узла целы."""
        self.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.conn.close()
        if not corrupt:
            for suffix in ('', '-wal', '-shm'):
                Path(str(DB) + suffix).unlink(missing_ok=True)
            return
        db = sqlite3.connect(DB)
        page = db.execute('PRAGMA page_size').fetchone()[0]
        (root,) = db.execute("SELECT rootpage FROM sqlite_master WHERE name = 'devices'").fetchone()
        db.close()
        with open(DB, 'r+b') as f:
            f.seek((root - 1) * page)
            f.write(b'\xff' * page)
        db = sqlite3.connect(DB)
        self.addCleanup(db.close)
        with self.assertRaises(sqlite3.DatabaseError):
            db.execute('SELECT * FROM devices').fetchall()

    def assert_only_archived_have_access(self, devices):
        self.assertEqual(self.access(devices[3]), self.NO_ACCESS, 'устройство 3 выдано после архива — доступ остался')
        for n in (1, 2):
            self.assertEqual(self.access(devices[n]), self.FULL_ACCESS, f'устройство {n} из архива потеряло доступ')
        with sqlite3.connect(DB) as db:
            new = db.execute('INSERT INTO devices (created_at) VALUES (9999)').lastrowid
        self.assertGreater(new, 3, 'новое устройство получило id устройства, выданного после архива')

    def test_issued_after_archive_live_db_lost(self):
        devices = self.issued_after_archive()
        self.lose_live_db()
        self.restore()
        self.assert_only_archived_have_access(devices)

    def test_issued_after_archive_live_db_corrupt(self):
        devices = self.issued_after_archive()
        self.lose_live_db(corrupt=True)
        result = self.restore()
        self.assertIn('WARN: живая БД не читается', result.stdout)
        self.assert_only_archived_have_access(devices)

    def issued_after_archive_reissued(self, live_db='live'):
        """Архив (1, 2), выдано 3; устройство 1 перевыпущено, как в панели: новые ключ WireGuard,
        пароль IKEv2 (учётка та же) и сертификат OpenVPN, старый сертификат отозван."""
        devices = self.issued_after_archive()
        old = devices[1]
        archived = self.serial(1)
        self.revoke(1)
        new = dict(old, key=base64.b64encode(bytes([11]) * 32).decode(),
                   psk=base64.b64encode(bytes([111]) * 32).decode(), password='reissued1')
        self.conn.execute('UPDATE devices SET wg_public_key = ?, wg_preshared_key = ?, ovpn_cert = ?, '
                          'ikev2_password = ? WHERE id = 1', (new['key'], new['psk'], self.issue(1), new['password']))
        self.conn.commit()
        self.apply_configs([new, devices[2], devices[3]])
        if live_db != 'live':
            self.lose_live_db(corrupt=live_db == 'corrupt')
        result = self.restore()
        # Как при живой БД: архивные реквизиты устройства 1 сняты панелью и доступа не
        # получают, выданных после архива в восстановленных конфигах нет. Устройство и
        # его адрес ccd остаются, креды в БД пустые — перевыпуск в панели выдаст новые.
        crl = self.crl_serials()
        self.assertIn(archived, crl, 'архивный сертификат устройства 1, отозванный панелью, снова действует')
        self.assertIn(self.serial(1), crl, 'сертификат устройства 1, выданный после архива, не отозван')
        wg = Path('/etc/wireguard/wg0.conf').read_text()
        ike = Path('/etc/swanctl/conf.d/anysda-clients.conf').read_text()
        self.assertNotIn(old['key'], wg, 'архивный пир WireGuard устройства 1, снятый перевыпуском, вернулся')
        self.assertNotIn(f'"{old["password"]}"', ike, 'архивный секрет IKEv2 устройства 1, снятый перевыпуском, вернулся')
        self.assertNotIn('eap-1 {', ike, 'учётка IKEv2 устройства 1 со снятым секретом вернулась')
        self.assertNotIn(new['key'], wg)
        self.assertNotIn(f'"{new["password"]}"', ike)
        self.assertTrue(Path('/etc/openvpn/server/ccd/dev_1').exists(), 'перевыпуск отнял у устройства 1 адрес ccd')
        with sqlite3.connect(DB) as db:
            row = db.execute('SELECT created_at, wg_public_key, ovpn_cert, ikev2_username, ikev2_password '
                             'FROM devices WHERE id = 1').fetchone()
        self.assertEqual(row, (1001, None, None, old['user'], None))
        self.assertIn('устройство 1: IKEv2, OpenVPN, WireGuard отозваны; перевыпущено после архива', result.stdout)
        self.assertNotRegex(result.stdout, r'устройство 1: [^\n]*удалено после архива')
        self.assertEqual(self.access(devices[2]), self.FULL_ACCESS, 'устройство 2 из архива потеряло доступ')
        self.assertEqual(self.access(devices[3]), self.NO_ACCESS, 'устройство 3 выдано после архива — доступ остался')
        return result

    def test_issued_after_archive_reissued_live_db(self):
        self.issued_after_archive_reissued()

    def test_issued_after_archive_reissued_live_db_lost(self):
        self.issued_after_archive_reissued('lost')

    def test_issued_after_archive_reissued_live_db_corrupt(self):
        result = self.issued_after_archive_reissued('corrupt')
        self.assertIn('WARN: живая БД не читается', result.stdout)

    def issued_after_archive_deleted_id_reused(self, corrupt=False):
        """Архив (1, 2), выдано 3; устройство 2 удалено, номер 2 занят новым устройством; живой БД нет."""
        devices = self.issued_after_archive()
        gone = devices[2]
        archived = self.serial(2)
        self.revoke(2)
        self.conn.execute('DELETE FROM devices WHERE id = 2')
        self.conn.commit()
        other = {'id': 2, 'key': base64.b64encode(bytes([42]) * 32).decode(),
                 'psk': base64.b64encode(bytes([142]) * 32).decode(), 'wg_ip': '10.66.66.43',
                 'user': 'other2', 'password': 'othersecret2', 'ike_ip': '10.99.1.43'}
        self.conn.execute('INSERT INTO devices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                          (2, 5002, 'private', other['key'], other['psk'], other['wg_ip'], self.issue(2), 'key',
                           other['user'], other['password'], other['ike_ip']))
        self.conn.commit()
        self.ccd(other)
        self.apply_configs([devices[1], other, devices[3]])
        self.lose_live_db(corrupt)
        result = self.restore()
        crl = self.crl_serials()
        self.assertIn(archived, crl, 'сертификат удалённого устройства 2 снова действует')
        self.assertIn(self.serial(2), crl, 'сертификат нового устройства 2, выданного после архива, не отозван')
        wg = Path('/etc/wireguard/wg0.conf').read_text()
        ike = Path('/etc/swanctl/conf.d/anysda-clients.conf').read_text()
        for d, who in ((gone, 'удалённого'), (other, 'нового')):
            self.assertNotIn(d['key'], wg, f'пир WireGuard {who} устройства 2 остался')
            self.assertNotIn(f'"{d["password"]}"', ike, f'секрет IKEv2 {who} устройства 2 остался')
        self.assertNotIn('eap-2 {', ike, 'учётка IKEv2 удалённого устройства 2 вернулась')
        self.assertIn('устройство 2: IKEv2, OpenVPN, WireGuard отозваны; удалено после архива', result.stdout)
        self.assertNotRegex(result.stdout, r'устройство 2: [^\n]*перевыпущено после архива')
        self.assertEqual(self.access(devices[1]), self.FULL_ACCESS, 'устройство 1 из архива потеряло доступ')
        self.assertEqual(self.access(devices[3]), self.NO_ACCESS, 'устройство 3 выдано после архива — доступ остался')
        with sqlite3.connect(DB) as db:
            new = db.execute('INSERT INTO devices (created_at) VALUES (9999)').lastrowid
        self.assertGreater(new, 3, 'новое устройство получило уже выданный id')
        return result

    def test_issued_after_archive_deleted_id_reused_live_db_lost(self):
        self.issued_after_archive_deleted_id_reused()

    def test_issued_after_archive_deleted_id_reused_live_db_corrupt(self):
        result = self.issued_after_archive_deleted_id_reused(corrupt=True)
        self.assertIn('WARN: живая БД не читается', result.stdout)

    def issued_after_archive_suspended(self, how, live_db='live'):
        """Архив (1, 2), выдано 3; клиент устройства 2 заморожен в панели или просрочен по таймеру
        vpn2-control. Оба пути снимают пир и секрет с узла и ставят disable в ccd, сертификат действует."""
        devices = self.issued_after_archive()
        old = devices[2]
        archived = self.serial(2)
        ccd = Path('/etc/openvpn/server/ccd/dev_2')
        if how == 'frozen':
            # Панель: openvpn.apply с action deny переписывает ccd целиком.
            write(str(ccd), 'ifconfig-push 10.67.67.3 255.255.255.0\ndisable\n', 0o644)
        else:
            # Таймер: тот же deny поверх действующего ccd, срок в журнале expired.json.
            write(str(ccd), ccd.read_text() + 'disable\n', 0o644)
        self.apply_configs([devices[1], devices[3]])
        if live_db != 'live':
            self.lose_live_db(corrupt=live_db == 'corrupt')
        result = self.restore()
        # Заморозка и истечение сертификат не отзывают: устройство 2 не удалено и не
        # перевыпущено, реквизиты архива — его, после разморозки розданные конфиги работают.
        self.assertNotIn(archived, self.crl_serials(), f'{how}: сертификат устройства 2 отозван')
        self.assertEqual(self.access(old), self.FULL_ACCESS, f'{how}: устройство 2 из архива потеряло доступ')
        with sqlite3.connect(DB) as db:
            row = db.execute('SELECT created_at, wg_public_key, wg_preshared_key, ovpn_cert IS NOT NULL, '
                             'ikev2_username, ikev2_password FROM devices WHERE id = 2').fetchone()
        self.assertEqual(row, (1002, old['key'], old['psk'], 1, old['user'], old['password']),
                         f'{how}: реквизиты устройства 2 не как в архиве')
        self.assertNotRegex(result.stdout, r'устройство 2: [^\n]*(перевыпущено|удалено) после архива')
        self.assertEqual(self.access(devices[1]), self.FULL_ACCESS, 'устройство 1 из архива потеряло доступ')
        self.assertEqual(self.access(devices[3]), self.NO_ACCESS, 'устройство 3 выдано после архива — доступ остался')
        return result

    def test_issued_after_archive_frozen_live_db(self):
        self.issued_after_archive_suspended('frozen')

    def test_issued_after_archive_frozen_live_db_lost(self):
        self.issued_after_archive_suspended('frozen', 'lost')

    def test_issued_after_archive_frozen_live_db_corrupt(self):
        result = self.issued_after_archive_suspended('frozen', 'corrupt')
        self.assertIn('WARN: живая БД не читается', result.stdout)

    def test_issued_after_archive_expired_live_db(self):
        self.issued_after_archive_suspended('expired')

    def test_issued_after_archive_expired_live_db_lost(self):
        self.issued_after_archive_suspended('expired', 'lost')

    def test_issued_after_archive_expired_live_db_corrupt(self):
        result = self.issued_after_archive_suspended('expired', 'corrupt')
        self.assertIn('WARN: живая БД не читается', result.stdout)

    WG_CONF = '/etc/wireguard/wg0.conf'
    IKE_CONF = '/etc/swanctl/conf.d/anysda-clients.conf'

    def issued_after_archive_taken(self, how, live_conf):
        """Архив (1, 2), выдано 3; без живой БД. Устройство 1 перевыпущено или устройство 2 удалено и
        его номер занят новым, как в панели. live_conf: какой живой конфиг пропал или испорчен."""
        devices = self.issued_after_archive()
        n = 1 if how == 'reissued' else 2
        old = devices[n]
        archived = self.serial(n)
        self.revoke(n)
        if how == 'reissued':
            new = dict(old, key=base64.b64encode(bytes([11]) * 32).decode(),
                       psk=base64.b64encode(bytes([111]) * 32).decode(), password='reissued1')
            self.conn.execute('UPDATE devices SET wg_public_key = ?, wg_preshared_key = ?, ovpn_cert = ?, '
                              'ikev2_password = ? WHERE id = 1', (new['key'], new['psk'], self.issue(1), new['password']))
            self.conn.commit()
            self.apply_configs([new, devices[2], devices[3]])
        else:
            self.conn.execute('DELETE FROM devices WHERE id = 2')
            new = {'id': 2, 'key': base64.b64encode(bytes([42]) * 32).decode(),
                   'psk': base64.b64encode(bytes([142]) * 32).decode(), 'wg_ip': '10.66.66.43',
                   'user': 'other2', 'password': 'othersecret2', 'ike_ip': '10.99.1.43'}
            self.conn.execute('INSERT INTO devices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                              (2, 5002, 'private', new['key'], new['psk'], new['wg_ip'], self.issue(2), 'key',
                               new['user'], new['password'], new['ike_ip']))
            self.conn.commit()
            self.ccd(new)
            self.apply_configs([devices[1], new, devices[3]])
        if live_conf == 'no-wg':
            Path(self.WG_CONF).unlink()
        elif live_conf == 'no-ike':
            Path(self.IKE_CONF).unlink()
        else:
            with open(self.WG_CONF, 'ab') as f:
                f.write(b'# \xff\xfe\xfa\n')
        self.lose_live_db()
        result = self.restore()
        crl = self.crl_serials()
        self.assertIn(archived, crl, f'архивный сертификат устройства {n}, отозванный панелью, снова действует')
        self.assertIn(self.serial(n), crl, f'сертификат устройства {n}, выданный после архива, не отозван')
        wg = Path(self.WG_CONF).read_text()
        ike = Path(self.IKE_CONF).read_text()
        for d, who in ((old, 'архивный'), (new, 'выданный после архива')):
            self.assertNotIn(d['key'], wg, f'{live_conf}: {who} пир WireGuard устройства {n} вернулся')
            self.assertNotIn(f'"{d["password"]}"', ike, f'{live_conf}: {who} секрет IKEv2 устройства {n} вернулся')
        self.assertNotIn(f'eap-{n} {{', ike, f'{live_conf}: учётка IKEv2 устройства {n} вернулась')
        with sqlite3.connect(DB) as db:
            row = db.execute(f'SELECT wg_public_key, ovpn_cert, ikev2_password FROM devices WHERE id = {n}').fetchone()
        self.assertEqual(row, (None, None, None), f'{live_conf}: реквизиты устройства {n} не сняты')
        self.assertIn(f'устройство {n}: IKEv2, OpenVPN, WireGuard отозваны; ', result.stdout)
        label = {'reissued': 'перевыпущено после архива', 'deleted': 'удалено после архива'}
        if live_conf == 'no-ike':
            # Без учётки IKEv2 на узле перевыпуск от удаления не отличить: подпись нейтральная.
            self.assertNotRegex(result.stdout, rf'устройство {n}: [^\n]*(перевыпущено|удалено) после архива')
        else:
            self.assertIn(f'устройство {n}: IKEv2, OpenVPN, WireGuard отозваны; {label[how]}', result.stdout)
        other = 2 if n == 1 else 1
        self.assertEqual(self.access(devices[other]), self.FULL_ACCESS, f'устройство {other} из архива потеряло доступ')
        self.assertEqual(self.access(devices[3]), self.NO_ACCESS, 'устройство 3 выдано после архива — доступ остался')
        with sqlite3.connect(DB) as db:
            created = db.execute('INSERT INTO devices (created_at) VALUES (9999)').lastrowid
        self.assertGreater(created, 3, 'новое устройство получило уже выданный id')

    def test_issued_after_archive_reissued_live_db_lost_no_wg_config(self):
        self.issued_after_archive_taken('reissued', 'no-wg')

    def test_issued_after_archive_reissued_live_db_lost_no_ike_config(self):
        self.issued_after_archive_taken('reissued', 'no-ike')

    def test_issued_after_archive_deleted_id_reused_live_db_lost_no_wg_config(self):
        self.issued_after_archive_taken('deleted', 'no-wg')

    def test_issued_after_archive_deleted_id_reused_live_db_lost_no_ike_config(self):
        self.issued_after_archive_taken('deleted', 'no-ike')

    def test_issued_after_archive_reissued_live_db_lost_wg_config_not_utf8(self):
        self.issued_after_archive_taken('reissued', 'not-utf8')

    def issued_after_archive_wg_config_not_utf8(self, live_db):
        devices = self.issued_after_archive()
        with open(self.WG_CONF, 'ab') as f:
            f.write(b'# \xff\xfe\xfa\n')
        if live_db == 'lost':
            self.lose_live_db()
        self.restore()
        self.assert_only_archived_have_access(devices)

    def test_issued_after_archive_wg_config_not_utf8_live_db(self):
        self.issued_after_archive_wg_config_not_utf8('live')

    def test_issued_after_archive_wg_config_not_utf8_live_db_lost(self):
        self.issued_after_archive_wg_config_not_utf8('lost')

    def older_and_newer_archives(self):
        """Архив A (1, 2), выдано 3, архив B (1, 2, 3)."""
        devices = self.revocation_fixture()
        for n in (1, 2):
            self.ccd(devices[n])
        older = self.archive_name()
        self.add_device(devices[3])
        self.ccd(devices[3])
        self.apply_configs([devices[n] for n in (1, 2, 3)])
        return devices, older, self.archive_name()

    def test_issued_after_archive_older_newer_older(self):
        devices, older, newer = self.older_and_newer_archives()
        for step, archive, expected in (('A', older, self.NO_ACCESS), ('A-B', newer, self.FULL_ACCESS),
                                        ('A-B-A', older, self.NO_ACCESS), ('A-B-A-B', newer, self.FULL_ACCESS)):
            self.restore(archive)
            self.assertEqual(self.access(devices[3]), expected, f'{step}: устройство 3')
            for n in (1, 2):
                self.assertEqual(self.access(devices[n]), self.FULL_ACCESS, f'{step}: устройство {n}')
            crl = self.crl_serials()
            self.assertEqual(len(crl), len(set(crl)), f'{step}: дубли в CRL: {crl}')

    def test_issued_after_archive_without_ovpn_cert(self):
        devices = self.revocation_fixture()
        for n in (1, 2):
            self.ccd(devices[n])
        self.backup()
        d = devices[3]
        self.conn.execute('INSERT INTO devices VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)',
                          (3, 1003, 'private', d['key'], d['psk'], d['wg_ip'], d['user'], d['password'], d['ike_ip']))
        self.conn.commit()
        self.apply_configs([devices[n] for n in (1, 2, 3)])
        result = self.restore()
        self.assertNotIn(d['key'], Path('/etc/wireguard/wg0.conf').read_text(), 'пир устройства 3 остался')
        ike = Path('/etc/swanctl/conf.d/anysda-clients.conf').read_text()
        self.assertNotIn('eap-3 {', ike, 'учётка IKEv2 устройства 3 осталась')
        self.assertNotIn(f'"{d["password"]}"', ike, 'секрет IKEv2 устройства 3 остался')
        self.assertIn('устройство 3: IKEv2, WireGuard отозваны; выдано после архива', result.stdout)
        for n in (1, 2):
            self.assertEqual(self.access(devices[n]), self.FULL_ACCESS, f'устройство {n} из архива потеряло доступ')

    def test_issued_after_archive_released_then_deleted_stays_revoked(self):
        devices, older, newer = self.older_and_newer_archives()
        self.restore(older)
        self.restore(newer)
        # Панель удаляет устройство 3, как backend.py: отзыв без причины, строка и ccd уходят.
        self.revoke(3)
        self.conn.close()
        self.conn = sqlite3.connect(DB)
        self.conn.execute('DELETE FROM devices WHERE id = 3')
        self.conn.commit()
        Path('/etc/openvpn/server/ccd/dev_3').unlink()
        self.apply_configs([devices[1], devices[2]])
        self.restore(newer)
        # Строка устройства 3 в архиве B остаётся (без кредов), с ней и ccd: номер занят им же.
        access = self.access(devices[3])
        del access['ccd']
        self.assertEqual(access, dict.fromkeys(access, False), 'удалённое панелью устройство 3 вернуло доступ')
        for n in (1, 2):
            self.assertEqual(self.access(devices[n]), self.FULL_ACCESS, f'устройство {n} из архива потеряло доступ')

    def reissued_between_archives(self, new=()):
        """Архив A (1, 2); устройство 1 перевыпущено, как в панели, выданы new; архив B (1', 2, *new)."""
        devices = self.revocation_fixture()
        for n in (1, 2):
            self.ccd(devices[n])
        older = self.archive_name()
        archived = self.serial(1)
        self.revoke(1)
        devices[1] = dict(devices[1], key=base64.b64encode(bytes([11]) * 32).decode(),
                          psk=base64.b64encode(bytes([111]) * 32).decode(), password='reissued1')
        self.conn.execute('UPDATE devices SET wg_public_key = ?, wg_preshared_key = ?, ovpn_cert = ?, '
                          'ikev2_password = ? WHERE id = 1',
                          (devices[1]['key'], devices[1]['psk'], self.issue(1), devices[1]['password']))
        self.conn.commit()
        for n in new:
            self.add_device(devices[n])
            self.ccd(devices[n])
        self.apply_configs([devices[n] for n in (1, 2, *new)])
        return devices, archived, older, self.archive_name()

    def assert_reissued_restored(self, devices, result, step):
        """Перевыпущенное после архива устройство 1 остаётся в БД без доступа; отзыв не hold."""
        access = self.access(devices[1])
        del access['ccd']
        self.assertEqual(access, dict.fromkeys(access, False), f'{step}: у перевыпущенного устройства 1 доступ')
        index = {int(r[3], 16): r[2] for r in (line.split('\t') for line in
                 Path('/etc/openvpn/server/pki/index.txt').read_text().splitlines()) if len(r) == 6}
        self.assertNotIn('certificateHold', index[self.serial(1)],
                         f'{step}: сертификат устройства 1 на hold — следующий restore вернёт доступ')
        self.assertIn('устройство 1: IKEv2, OpenVPN, WireGuard отозваны; перевыпущено после архива', result.stdout)
        self.assertNotRegex(result.stdout, r'устройство 1: [^\n]*выдано после архива', step)
        self.assertEqual(self.access(devices[2]), self.FULL_ACCESS, f'{step}: устройство 2 из архива потеряло доступ')

    def issued_after_archive_reissued_between_archives(self, live_db='live'):
        devices, _archived, older, newer = self.reissued_between_archives()
        self.assert_reissued_restored(devices, self.restore(older), 'A')
        if live_db != 'live':
            self.conn = sqlite3.connect(DB)
            self.lose_live_db(corrupt=live_db == 'corrupt')
        self.assert_reissued_restored(devices, self.restore(newer), 'A-B')

    def test_issued_after_archive_reissued_between_archives_live_db(self):
        self.issued_after_archive_reissued_between_archives()

    def test_issued_after_archive_reissued_between_archives_live_db_lost(self):
        self.issued_after_archive_reissued_between_archives('lost')

    def issued_after_archive_reissued_then_deleted(self, live_db='live'):
        devices, _archived, older, newer = self.reissued_between_archives()
        self.restore(older)
        # Панель удаляет устройство 1: кредов после restore нет, отзывать нечего, строка и ccd уходят.
        self.conn = sqlite3.connect(DB)
        self.conn.execute('DELETE FROM devices WHERE id = 1')
        self.conn.commit()
        Path('/etc/openvpn/server/ccd/dev_1').unlink()
        self.apply_configs([devices[2]])
        if live_db != 'live':
            self.lose_live_db()
        result = self.restore(newer)
        # Строка устройства 1 в архиве B остаётся (без кредов), с ней и ccd: номер занят им же.
        access = self.access(devices[1])
        del access['ccd']
        self.assertEqual(access, dict.fromkeys(access, False), 'удалённое панелью устройство 1 вернуло доступ')
        self.assertRegex(result.stdout, r'устройство 1: IKEv2, OpenVPN, WireGuard отозваны; (удалено|отозвано) после архива')
        self.assertEqual(self.access(devices[2]), self.FULL_ACCESS, 'устройство 2 из архива потеряло доступ')

    def test_issued_after_archive_reissued_then_deleted_live_db(self):
        self.issued_after_archive_reissued_then_deleted()

    def test_issued_after_archive_reissued_then_deleted_live_db_lost(self):
        self.issued_after_archive_reissued_then_deleted('lost')

    def issued_after_archive_reissued_older_newer_older(self, live_db='live'):
        # Положительный контроль: выданное после A устройство 3 (в B есть) A-B-A-B возвращает доступ,
        # перевыпущенное 1 доступа не получает ни на одном шаге.
        devices, _archived, older, newer = self.reissued_between_archives(new=(3,))
        if live_db != 'live':
            self.lose_live_db()
        for step, archive, expected in (('A', older, self.NO_ACCESS), ('A-B', newer, self.FULL_ACCESS),
                                        ('A-B-A', older, self.NO_ACCESS), ('A-B-A-B', newer, self.FULL_ACCESS)):
            result = self.restore(archive)
            self.assertEqual(self.access(devices[3]), expected, f'{step}: устройство 3')
            self.assert_reissued_restored(devices, result, step)

    def test_issued_after_archive_reissued_older_newer_older_live_db(self):
        self.issued_after_archive_reissued_older_newer_older()

    def test_issued_after_archive_reissued_older_newer_older_live_db_lost(self):
        self.issued_after_archive_reissued_older_newer_older('lost')

    @staticmethod
    def delete_in_panel_ccd(n):
        """Удаление в панели, как backend.py: ccd остаётся, deny дописывает disable."""
        ccd = Path(f'/etc/openvpn/server/ccd/dev_{n}')
        write(str(ccd), f'ifconfig-push 10.67.67.{n + 1} 255.255.255.0\ndisable\n', 0o644)

    def issued_after_archive_deleted_in_panel(self, live_db):
        """Архив (1, 2), выдано 3; устройство 2 удалено в панели, номер никому не выдан; живой БД нет."""
        devices = self.issued_after_archive()
        archived = self.serial(2)
        self.revoke(2)
        self.conn.execute('DELETE FROM devices WHERE id = 2')
        self.conn.commit()
        self.delete_in_panel_ccd(2)
        self.apply_configs([devices[1], devices[3]])
        self.lose_live_db(corrupt=live_db == 'corrupt')
        result = self.restore()
        self.assertIn(archived, self.crl_serials(), 'сертификат удалённого устройства 2 снова действует')
        access = self.access(devices[2])
        del access['ccd']
        self.assertEqual(access, dict.fromkeys(access, False), 'удалённое панелью устройство 2 вернуло доступ')
        self.assertIn('устройство 2: IKEv2, OpenVPN, WireGuard отозваны; удалено после архива', result.stdout)
        self.assertNotRegex(result.stdout, r'устройство 2: [^\n]*перевыпущено после архива')
        self.assertEqual(self.access(devices[1]), self.FULL_ACCESS, 'устройство 1 из архива потеряло доступ')
        self.assertEqual(self.access(devices[3]), self.NO_ACCESS, 'устройство 3 выдано после архива — доступ остался')

    def test_issued_after_archive_deleted_in_panel_live_db_lost(self):
        self.issued_after_archive_deleted_in_panel('lost')

    def test_issued_after_archive_deleted_in_panel_live_db_corrupt(self):
        self.issued_after_archive_deleted_in_panel('corrupt')

    def issued_after_archive_reissued_between_archives_ccd(self, deleted):
        """Архив A (1, 2), устройство 1 перевыпущено, архив B (1', 2); restore A обнулил креды 1.
        deleted: панель удаляет 1 (ccd с disable); иначе 1 остаётся перевыпущенным. Живой БД нет."""
        devices, _archived, older, newer = self.reissued_between_archives()
        self.restore(older)
        self.conn = sqlite3.connect(DB)
        if deleted:
            self.conn.execute('DELETE FROM devices WHERE id = 1')
            self.conn.commit()
            self.delete_in_panel_ccd(1)
            self.apply_configs([devices[2]])
        self.lose_live_db()
        result = self.restore(newer)
        access = self.access(devices[1])
        del access['ccd']
        self.assertEqual(access, dict.fromkeys(access, False), 'устройство 1 получило доступ')
        if deleted:
            self.assertIn('устройство 1: IKEv2, OpenVPN, WireGuard отозваны; удалено после архива', result.stdout)
            self.assertNotRegex(result.stdout, r'устройство 1: [^\n]*перевыпущено после архива')
        else:
            # Положительный контроль: ccd перевыпущенного без disable — подпись прежняя.
            self.assertIn('устройство 1: IKEv2, OpenVPN, WireGuard отозваны; перевыпущено после архива', result.stdout)
        self.assertEqual(self.access(devices[2]), self.FULL_ACCESS, 'устройство 2 из архива потеряло доступ')

    def test_issued_after_archive_reissued_then_deleted_in_panel_live_db_lost(self):
        self.issued_after_archive_reissued_between_archives_ccd(deleted=True)

    def test_issued_after_archive_reissued_kept_ccd_live_db_lost(self):
        self.issued_after_archive_reissued_between_archives_ccd(deleted=False)

    def issued_after_archive_id_reused_older_newer_older(self, live_db):
        """Архив A (1, 2); устройство 2 удалено, номер 2 выдан другому (другой created_at), архив B (1, 2').
        Restore A снимает 2' обратимо (hold): A-B-A-B возвращает и снимает доступ, удалённое 2 не возвращается."""
        devices = self.revocation_fixture()
        for n in (1, 2):
            self.ccd(devices[n])
        older = self.archive_name()
        archived = self.serial(2)
        self.revoke(2)
        self.conn.execute('DELETE FROM devices WHERE id = 2')
        other = {'id': 2, 'key': base64.b64encode(bytes([42]) * 32).decode(),
                 'psk': base64.b64encode(bytes([142]) * 32).decode(), 'wg_ip': '10.66.66.43',
                 'user': 'other2', 'password': 'othersecret2', 'ike_ip': '10.99.1.43'}
        self.conn.execute('INSERT INTO devices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                          (2, 5002, 'private', other['key'], other['psk'], other['wg_ip'], self.issue(2), 'key',
                           other['user'], other['password'], other['ike_ip']))
        self.conn.commit()
        self.ccd(other)
        self.apply_configs([devices[1], other])
        newer = self.archive_name()
        if live_db != 'live':
            self.lose_live_db()
        for step, archive, expected in (('A', older, self.NO_ACCESS), ('A-B', newer, self.FULL_ACCESS),
                                        ('A-B-A', older, self.NO_ACCESS), ('A-B-A-B', newer, self.FULL_ACCESS)):
            result = self.restore(archive)
            access = self.access(other)
            if expected is self.NO_ACCESS:
                # ccd номера 2 — из архива A, его OpenVPN не пустит без сертификата.
                del access['ccd']
                self.assertEqual(access, dict.fromkeys(access, False), f'{step}: у устройства 2\' доступ')
            else:
                self.assertEqual(access, expected, f'{step}: устройство 2\' не вернуло доступ')
            self.assertIn(archived, self.crl_serials(), f'{step}: сертификат удалённого устройства 2 снова действует')
            self.assertNotIn(devices[2]['key'], Path(self.WG_CONF).read_text(),
                             f'{step}: пир WireGuard удалённого устройства 2 вернулся')
            self.assertNotIn(f'"{devices[2]["password"]}"', Path(self.IKE_CONF).read_text(),
                             f'{step}: секрет IKEv2 удалённого устройства 2 вернулся')
            self.assertEqual(self.access(devices[1]), self.FULL_ACCESS, f'{step}: устройство 1 потеряло доступ')
            if step == 'A':
                self.assertIn('устройство 2: IKEv2, OpenVPN, WireGuard отозваны; удалено после архива', result.stdout)
                self.assertNotRegex(result.stdout, r'устройство 2: [^\n]*перевыпущено после архива')

    def test_issued_after_archive_id_reused_older_newer_older_live_db(self):
        self.issued_after_archive_id_reused_older_newer_older('live')

    def test_issued_after_archive_id_reused_older_newer_older_live_db_lost(self):
        self.issued_after_archive_id_reused_older_newer_older('lost')

    def issued_after_archive_reissued_then_suspended(self, how, live_db='lost'):
        """Архив A (1, 2), выдано 3; устройство 1 перевыпущено (сертификат S', учётка IKEv2 та же), затем
        клиент заморожен или просрочен: учётки и пира на узле нет, в ccd disable. Архив B (1', 2, 3).
        Живой БД нет: restore A отзывает S' без причины, подпись не «удалено», A-B-A доступа не возвращает."""
        devices = self.revocation_fixture()
        for n in (1, 2):
            self.ccd(devices[n])
        older = self.archive_name()
        old = devices[1]
        archived = self.serial(1)
        self.add_device(devices[3])
        self.ccd(devices[3])
        self.revoke(1)
        devices[1] = dict(old, key=base64.b64encode(bytes([11]) * 32).decode(),
                          psk=base64.b64encode(bytes([111]) * 32).decode(), password='reissued1')
        self.conn.execute('UPDATE devices SET wg_public_key = ?, wg_preshared_key = ?, ovpn_cert = ?, '
                          'ikev2_password = ? WHERE id = 1',
                          (devices[1]['key'], devices[1]['psk'], self.issue(1), devices[1]['password']))
        self.conn.commit()
        reissued = self.serial(1)
        ccd = Path('/etc/openvpn/server/ccd/dev_1')
        if how == 'frozen':
            write(str(ccd), 'ifconfig-push 10.67.67.2 255.255.255.0\ndisable\n', 0o644)
        else:
            write(str(ccd), ccd.read_text() + 'disable\n', 0o644)
        self.apply_configs([devices[2], devices[3]])
        # Клиент устройства 1 заморожен или просрочен — для среза B он не обязан быть в конфигах.
        self.conn.execute('DROP TABLE clients')
        self.conn.execute('CREATE TABLE clients (id INTEGER PRIMARY KEY, frozen_manual INTEGER NOT NULL DEFAULT 0, '
                          'expires_at INTEGER)')
        self.conn.execute('INSERT INTO clients VALUES (1, 0, NULL)')
        self.conn.execute('INSERT INTO clients VALUES (2, ?, ?)',
                          (1, None) if how == 'frozen' else (0, int(time.time()) - 3600))
        self.conn.execute('ALTER TABLE devices ADD COLUMN client_id INTEGER')
        self.conn.execute('UPDATE devices SET client_id = CASE id WHEN 1 THEN 2 ELSE 1 END')
        self.conn.commit()
        newer = self.archive_name()
        self.lose_live_db(corrupt=live_db == 'corrupt')
        for step, archive in (('A', older), ('A-B', newer), ('A-B-A', older)):
            result = self.restore(archive)
            for d in (old, devices[1]):
                access = self.access(d)
                del access['ccd']
                self.assertEqual(access, dict.fromkeys(access, False), f'{how} {step}: у устройства 1 доступ')
            crl = self.crl_serials()
            self.assertIn(archived, crl, f'{how} {step}: сертификат архива A устройства 1 действует')
            self.assertIn(reissued, crl, f'{how} {step}: перевыпущенный сертификат S\' действует')
            index = {int(r[3], 16): r[2] for r in (line.split('\t') for line in
                     Path('/etc/openvpn/server/pki/index.txt').read_text().splitlines()) if len(r) == 6}
            self.assertNotIn('certificateHold', index[reissued],
                             f'{how} {step}: S\' на hold — следующий restore вернёт доступ')
            self.assertNotRegex(result.stdout, r'устройство 1: [^\n]*(удалено|выдано) после архива', f'{how} {step}')
            self.assertEqual(self.access(devices[2]), self.FULL_ACCESS, f'{how} {step}: устройство 2 потеряло доступ')
            if step == 'A':
                self.assertIn('устройство 1: IKEv2, OpenVPN, WireGuard отозваны; отозвано после архива', result.stdout)
                self.assertEqual(self.access(devices[3]), self.NO_ACCESS, f'{how} A: устройство 3 с доступом')

    def test_issued_after_archive_reissued_then_frozen_live_db_lost(self):
        self.issued_after_archive_reissued_then_suspended('frozen')

    def test_issued_after_archive_reissued_then_frozen_live_db_corrupt(self):
        self.issued_after_archive_reissued_then_suspended('frozen', 'corrupt')

    def test_issued_after_archive_reissued_then_expired_live_db_lost(self):
        self.issued_after_archive_reissued_then_suspended('expired')

    def test_persistent_mismatch_keeps_archive_with_warning(self):
        # Синхронизация при добавлении устройства упала: пира нет ни в одном срезе.
        key = base64.b64encode(bytes([7]) * 32).decode()
        psk = base64.b64encode(bytes([107]) * 32).decode()
        self.conn.execute('DROP TABLE clients')
        self.conn.execute('CREATE TABLE clients (id INTEGER PRIMARY KEY, frozen_manual INTEGER NOT NULL DEFAULT 0, '
                          'expires_at INTEGER)')
        self.conn.execute('CREATE TABLE devices (id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL, '
                          'deleting INTEGER NOT NULL DEFAULT 0, name TEXT NOT NULL, wg_public_key TEXT, '
                          'wg_preshared_key TEXT, wg_ip TEXT)')
        self.conn.execute('INSERT INTO clients VALUES (1, 0, NULL)')
        self.conn.execute("INSERT INTO devices VALUES (7, 1, 0, 'Телефон Ани', ?, ?, '10.66.66.8')", (key, psk))
        self.conn.commit()
        write('/etc/anysda/tgbot-secret.txt', 'test-tgbot-secret')
        self.addCleanup(Path('/etc/anysda/tgbot-secret.txt').unlink)
        write('/test-bin/curl', '#!/bin/sh\nprintf "%s\\n" "$@" >> /test-output/curl\n'
              'echo \'{"version":"test"}\'\n', 0o755)
        write('/test-bin/sleep', '#!/bin/sh\nexit 0\n', 0o755)
        result = self.script('backup')
        print('\n'.join(line for line in result.stdout.splitlines()
                        if 'срез 10' in line or 'устройство' in line or 'warning' in line or 'backup:' in line),
              flush=True)
        self.assertEqual(result.returncode, 4, result.stdout)
        archives = list(BACKUPS.glob('*.age'))
        self.assertEqual(len(archives), 1, result.stdout)
        decrypt(archives[0], '/test-output/backup.tar.gz')
        self.assertIn('устройство 7 «Телефон Ани»: нет пира WireGuard', result.stdout)
        self.assertRegex(result.stdout, r'(?m)^warning: +БД и конфиги VPN расходятся')
        events = Path('/test-output/curl').read_text()
        # Годный архив — предупреждение в ленте, а не «Бэкап не удался».
        self.assertIn('{"type":"backup_warning","detail":"БД и конфиги VPN расходятся (устройства: 7)'
                      ' — перевыпусти в панели"}', events)
        self.assertNotIn('backup_failed', events)
        for secret in (psk, 'test-tgbot-secret'):
            self.assertNotIn(secret, result.stdout + events)

    def test_intermediate_reissue_key_of_pending_client_is_not_a_stranger(self):
        # Перевыпуски без подтверждения: в retired остался только исходный ключ,
        # а чужая синхронизация уже записала промежуточный ключ на адрес устройства.
        def key(n):
            return base64.b64encode(bytes([n]) * 32).decode()
        psk = base64.b64encode(bytes([100]) * 32).decode()
        self.conn.execute('DROP TABLE clients')
        self.conn.execute('CREATE TABLE clients (id INTEGER PRIMARY KEY, frozen_manual INTEGER NOT NULL DEFAULT 0, '
                          'expires_at INTEGER)')
        self.conn.execute('CREATE TABLE devices (id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL, '
                          'deleting INTEGER NOT NULL DEFAULT 0, wg_public_key TEXT, wg_preshared_key TEXT, wg_ip TEXT)')
        self.conn.execute('CREATE TABLE access_operations (id TEXT PRIMARY KEY, client_id INTEGER NOT NULL, '
                          'status TEXT NOT NULL, payload TEXT NOT NULL)')
        self.conn.execute('INSERT INTO clients VALUES (1, 0, NULL)')
        self.conn.execute('INSERT INTO devices VALUES (1, 1, 0, ?, ?, ?)', (key(3), psk, '10.66.66.2'))
        self.conn.execute("INSERT INTO access_operations VALUES ('op', 1, 'pending', ?)",
                          (json.dumps({'retired': [{'id': 1, 'wgPublicKey': key(1), 'ovpnCert': None}]}),))
        self.conn.commit()

        def peers(*items):
            return f'[Interface]\nPrivateKey = {WG_KEYS["archived"]}\n' + ''.join(
                f'\n[Peer]\nPublicKey = {k}\nPresharedKey = {psk}\nAllowedIPs = {ip}/32, fd66:66::2/128\n'
                for k, ip in items)
        write('/etc/wireguard/wg0.conf', peers((key(1), '10.66.66.2'), (key(2), '10.66.66.2')))
        result = self.script('backup')
        print('\n'.join(line for line in result.stdout.splitlines() if 'WireGuard' in line or 'warning' in line),
              flush=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn('устройства нет в БД', result.stdout)

        # Положительный контроль: пир на чужом адресе по-прежнему ловится.
        write('/etc/wireguard/wg0.conf', peers((key(1), '10.66.66.2'), (key(9), '10.66.66.9')))
        result = self.script('backup')
        self.assertEqual(result.returncode, 4, result.stdout)
        self.assertIn(f'пир WireGuard {key(9)}: устройства нет в БД', result.stdout)

    def test_check_bundle_requires_unpacked_bundle(self):
        # Ручная сверка архива: путь мимо бандла — ошибка, а не «расхождений нет».
        bundle = self.backup()
        old = Path('/test-output/old') / bundle.name
        shutil.copytree(bundle, old)
        (old / 'db.sqlite').unlink()
        empty = Path('/test-output/empty')
        empty.mkdir()
        for args in ((), ('',), ('/test-output/nonexistent',), (str(empty),),
                     (str(bundle.parent),), (str(bundle / 'manifest.json'),)):
            with self.subTest(args=args):
                result = self.script('backup', '--check-bundle', *args)
                print(f'--check-bundle {args}: rc={result.returncode} {result.stdout.strip()}', flush=True)
                self.assertNotIn(result.returncode, (0, 3), result.stdout)
                self.assertIn('manifest.json', result.stdout)
        for path in (bundle, old):
            with self.subTest(path=path):
                result = self.script('backup', '--check-bundle', str(path))
                self.assertEqual((result.returncode, result.stdout), (0, ''))

    @staticmethod
    def digest(path):
        def file(p):
            return 'sha256:' + hashlib.sha256(Path(p).read_bytes()).hexdigest()
        if path.is_file():
            return file(path)
        h = hashlib.sha256()
        for root, dirs, files in os.walk(path):
            dirs.sort()
            for name in sorted(files):
                full = os.path.join(root, name)
                h.update(os.path.relpath(full, path).encode() + b'\0' + file(full).encode() + b'\n')
        return 'sha256:' + h.hexdigest()

if __name__ == '__main__':
    if os.environ.get('VPN2_BACKUP_TEST') != '1' or not Path('/.dockerenv').exists():
        sys.exit('Run only inside a disposable Docker container with VPN2_BACKUP_TEST=1')
    unittest.main(defaultTest=BackupRestore.__name__, verbosity=2)
