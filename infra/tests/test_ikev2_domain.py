"""Переход IP→домен выполняется тем же обработчиком, что и ротация Caddy."""
from pathlib import Path
import tempfile
import unittest

from test_deploy import ROOT, bash, source, function


class Ikev2DomainTests(unittest.TestCase):
    def test_pending_domain_installs_watcher_and_retry_timer(self):
        stage = source('infra/scripts/27-ikev2.sh')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for child in ('etc/anysda', 'etc/systemd/system', 'var/anysda/.stamps', 'pki'):
                (root / child).mkdir(parents=True)
            (root / 'pki/ca.crt').write_text('existing CA')
            (root / 'etc/anysda/ikev2-profile-notice').write_text('old notice')
            # Исполняем финал стадии: именно там должны быть установлены
            # watchers после загрузки charon, независимо от начального режима.
            tail = stage[stage.index('touch /var/anysda/.stamps/27-ikev2'):]
            marker = '# Маркеры публикуем после успешной загрузки конфигурации charon.'
            if marker in stage:
                tail = stage[stage.index(marker):]
            tail = tail.replace('/etc/', directory + '/etc/').replace('/var/', directory + '/var/')
            result = bash('''
HOST_TAG=ru
IKEV2_MODE=self-signed
IKEV2_SERVER_HOST=vpn.example.test
PANEL_DOMAIN=vpn.example.test
LE_DOMAIN=vpn.example.test
LE_CERT=/caddy/not-yet-created.crt
LE_KEY=/caddy/not-yet-created.key
PKI=$PWD/pki
systemctl() { echo "$*" >> systemctl.calls; }
''' + tail, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            timer = root / 'etc/systemd/system/anysda-ikev2-cert-sync.timer'
            self.assertTrue(timer.exists(), 'pending self-signed has no certificate retry timer')
            self.assertIn('OnUnitActiveSec=30s', timer.read_text())
            service = (timer.parent / 'anysda-ikev2-cert-sync.service').read_text()
            self.assertIn('"vpn.example.test"', service)
            # Профили по IP продолжают работать: старое предупреждение снимается.
            self.assertFalse((root / 'etc/anysda/ikev2-profile-notice').exists())
            self.assertEqual((root / 'etc/anysda/ikev2-domain').read_text(), 'vpn.example.test')

    def test_appearance_rotation_and_reload_failure(self):
        stage = source('infra/scripts/27-ikev2.sh')
        helper = stage.split("<<'LESYNC'\n", 1)[1].split('\nLESYNC', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for child in ('etc/swanctl/x509', 'etc/swanctl/x509ca', 'etc/swanctl/private',
                          'etc/swanctl/conf.d', 'etc/anysda', 'run'):
                (root / child).mkdir(parents=True)
            helper = helper.replace('/etc/', directory + '/etc/').replace('/run/', directory + '/run/')
            (root / 'helper.sh').write_text(helper)
            conf = root / 'etc/swanctl/conf.d/anysda.conf'
            conf.write_text('connections {\n  anysda-ikev2 {\n    local-server {\n'
                            '      id    = 192.0.2.1\n    }\n  }\n}\n')
            mode = root / 'etc/anysda/ikev2-mode'
            host = root / 'etc/anysda/ikev2-server-host'
            mode.write_text('self-signed')
            host.write_text('192.0.2.1')
            domain = root / 'etc/anysda/ikev2-domain'
            domain.write_text('vpn.example.test')
            fixture = bash('''
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
  -keyout key.pem -out leaf.pem -days 1 -subj /CN=vpn.example.test \
  -addext subjectAltName=DNS:vpn.example.test
''', directory)
            self.assertEqual(fixture.returncode, 0, fixture.stderr)
            prelude = '''
swanctl() {
  echo "$*" >> calls
  if [[ $1 == --load-conns && -f fail ]]; then return 1; fi
}
export -f swanctl
'''
            call = 'bash helper.sh leaf.pem key.pem vpn.example.test'
            (root / 'fail').touch()
            failed = bash(prelude + call, directory)
            self.assertNotEqual(failed.returncode, 0, 'reload failure must be retried')
            self.assertEqual(mode.read_text(), 'self-signed')
            (root / 'fail').unlink()
            applied = bash(prelude + call, directory)
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertEqual(mode.read_text(), 'letsencrypt')
            self.assertEqual(host.read_text(), 'vpn.example.test')
            self.assertIn('id    = vpn.example.test', conf.read_text())
            before = (root / 'calls').read_text()
            repeat = bash(prelude + call, directory)
            self.assertEqual(repeat.returncode, 0, repeat.stderr)
            self.assertEqual((root / 'calls').read_text(), before, 'unchanged cert must not reload')
            (root / 'key.pem').write_text('incomplete key')
            invalid = bash(prelude + call, directory)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertEqual((root / 'calls').read_text(), before)
            self.assertEqual(mode.read_text(), 'letsencrypt', 'never downgrade while Caddy renews')
            domain.write_text('')
            cancelled = bash(prelude + call, directory)
            self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
            self.assertEqual((root / 'calls').read_text(), before, 'stale watcher must not restore removed domain')

    def test_server_address_follows_client_configs_and_keeps_ip_profiles(self):
        stage = source('infra/scripts/27-ikev2.sh')
        start = stage.index('# Адрес сервера в профилях тот же')
        head = stage[start:stage.index('# ── 1. apt strongswan')]
        cases = (
            # (WG_PUBLIC_HOST, прошлые маркеры mode/server-host, есть LE-cert) →
            # (режим, адрес в профилях, id сервера, домен под LE)
            (None, None, False, ('self-signed', '192.0.2.1', '192.0.2.1', '')),
            # env без WG_PUBLIC_HOST: адресом всё равно служит домен панели.
            ('', ('letsencrypt', 'vpn.example.test'), True,
             ('letsencrypt', 'vpn.example.test', 'vpn.example.test', 'vpn.example.test')),
            ('vpn.example.test', None, False,
             ('self-signed', 'vpn.example.test', '%any', 'vpn.example.test')),
            ('vpn.example.test', None, True,
             ('letsencrypt', 'vpn.example.test', 'vpn.example.test', 'vpn.example.test')),
            # Домен добавлен к серверу, раздававшему профили по IP: свой CA остаётся.
            ('vpn.example.test', ('self-signed', '192.0.2.1'), True,
             ('self-signed', 'vpn.example.test', '%any', '')),
            ('vpn.example.test', ('letsencrypt', 'vpn.example.test'), True,
             ('letsencrypt', 'vpn.example.test', 'vpn.example.test', 'vpn.example.test')),
            # Явный public_host важнее домена панели.
            ('edge.example.net', None, True, ('self-signed', 'edge.example.net', '%any', '')),
        )
        for public, previous, issued, expected in cases:
            with self.subTest(public=public, previous=previous, issued=issued), \
                    tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'etc/anysda').mkdir(parents=True)
                if previous:
                    (root / 'etc/anysda/ikev2-mode').write_text(previous[0])
                    (root / 'etc/anysda/ikev2-server-host').write_text(previous[1])
                caddy = root / 'caddy/vpn.example.test'
                caddy.mkdir(parents=True)
                if issued:
                    for suffix in ('crt', 'key'):
                        (caddy / f'vpn.example.test.{suffix}').write_text('pem')
                result = bash(f'''
HOST_TAG=ru
ENTRY_HOST=192.0.2.1
PANEL_DOMAIN={'' if public is None else 'vpn.example.test'}
WG_PUBLIC_HOST={public or ''}
CADDY_CERT_DIR=$PWD/caddy
''' + head.replace('/etc/', directory + '/etc/') + '''
printf '%s|%s|%s|%s' "$IKEV2_MODE" "$IKEV2_SERVER_HOST" "$IKEV2_LOCAL_ID" "$LE_DOMAIN" > result
''', directory)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(tuple((root / 'result').read_text().split('|')), expected)
                # Решение остаться на своём CA не отменяется следующим деплоем.
                self.assertEqual((root / 'etc/anysda/ikev2-own-ca-kept').exists(),
                                 previous == ('self-signed', '192.0.2.1'))

    def test_own_certificate_names_domain_and_entry_ip_with_same_ca(self):
        stage = source('infra/scripts/27-ikev2.sh')
        block = '  ikev2_pki_build() {' + stage.split('  ikev2_pki_build() {', 1)[1].split(
            '  pki_transaction', 1)[0]
        lib = ROOT / 'infra/lib/anysda-pki.sh'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'pki').mkdir()
            run = f'source {lib}\nHOST_TAG=ru\nENTRY_HOST=192.0.2.1\nPKI=$PWD/pki\n' + block
            before = bash(run + 'ikev2_pki_build "$PKI"\n', directory)
            self.assertEqual(before.returncode, 0, before.stderr)
            ca = (root / 'pki/ca.crt').read_bytes()
            old = (root / 'pki/server.crt').read_bytes()
            after = bash(run.replace('ENTRY_HOST=192.0.2.1\n', 'ENTRY_HOST=192.0.2.1\n'
                                     'IKEV2_PUBLIC_HOST=vpn.example.test\n')
                         + 'ikev2_pki_build "$PKI"\n', directory)
            self.assertEqual(after.returncode, 0, after.stderr)
            self.assertIn('SAN не содержит vpn.example.test', after.stdout)
            self.assertEqual((root / 'pki/ca.crt').read_bytes(), ca, 'clients trust the same CA')
            self.assertNotEqual((root / 'pki/server.crt').read_bytes(), old)
            text = bash('openssl x509 -in pki/server.crt -noout -text', directory).stdout
            self.assertIn('Subject: CN=vpn.example.test', text)
            self.assertIn('DNS:vpn.example.test, IP Address:192.0.2.1', text)
            for name in ('vpn.example.test', '192.0.2.1'):
                flag = '-verify_ip' if name[0].isdigit() else '-verify_hostname'
                check = bash(f'openssl verify -CAfile pki/ca.crt -purpose sslserver {flag} {name} '
                             'pki/server.crt', directory)
                self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
            again = bash(run.replace('ENTRY_HOST=192.0.2.1\n', 'ENTRY_HOST=192.0.2.1\n'
                                     'IKEV2_PUBLIC_HOST=vpn.example.test\n')
                         + 'ikev2_pki_build "$PKI"\n', directory)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertNotIn('перевыпуск', again.stdout, 'certificate with both names is kept')

    def test_summary_reads_actual_mode(self):
        deploy = source('infra/deploy.sh')
        self.assertTrue('print_ikev2_notice()' in deploy, 'deploy never reports IKEv2 mode')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'ikev2-mode').write_text('self-signed')
            (root / 'ikev2-server-host').write_text('192.0.2.1')
            (root / 'ikev2-domain').write_text('vpn.example.test')
            body = function('print_ikev2_notice').replace('/etc/anysda/', directory + '/')
            result = bash('load_env() { :; }; ssh_exec() { bash -c "$1"; }; warn() { echo "$*"; };\n'
                          + body + '\nprint_ikev2_notice\n', directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('mode=self-signed server-host=192.0.2.1', result.stdout)
            self.assertIn('ожидается сертификат Caddy', result.stdout)


if __name__ == '__main__':
    unittest.main()
