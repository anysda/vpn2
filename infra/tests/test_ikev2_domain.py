"""Переход IP→домен выполняется тем же обработчиком, что и ротация Caddy."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_deploy import ROOT, bash, source, function
import vpn_roles
from vpn_roles import package

LE_SYNC = ROOT / 'infra/ansible/roles/ikev2/files/anysda-ikev2-le-sync.sh'


class Ikev2DomainTests(unittest.TestCase):
    def test_pending_domain_installs_watcher_and_retry_timer(self):
        ikev2 = package('ikev2')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'etc').mkdir()
            (root / 'etc/ikev2-profile-notice').write_text('old notice')
            calls = []

            def run(argv, **_kwargs):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0)
            changed = ikev2.server(
                '192.0.2.1', 'vpn.example.test', 'vpn.example.test', '10.68.68.0/24', '10.99.0.1',
                lambda _text: None, lambda: False, etc=root / 'etc', swanctl=root / 'swanctl',
                target=root / 'pki', caddy=root / 'caddy', lock_path=root / 'lock', run=run)
            self.assertTrue(changed)
            self.assertEqual(calls, [['swanctl', what] for what in
                                     ('--load-creds', '--load-pools', '--load-conns')])
            # Профили по IP продолжают работать: старое предупреждение снимается.
            self.assertFalse((root / 'etc/ikev2-profile-notice').exists())
            self.assertEqual((root / 'etc/ikev2-domain').read_text(), 'vpn.example.test')
            self.assertEqual((root / 'etc/ikev2-mode').read_text(), 'self-signed')
            self.assertIn('id    = %any', (root / 'swanctl/conf.d/anysda.conf').read_text())
            again = ikev2.server(
                '192.0.2.1', 'vpn.example.test', 'vpn.example.test', '10.68.68.0/24', '10.99.0.1',
                lambda _text: None, lambda: False, etc=root / 'etc', swanctl=root / 'swanctl',
                target=root / 'pki', caddy=root / 'caddy', lock_path=root / 'lock', run=run)
            self.assertFalse(again, 'повтор без изменений считается изменением')
        # Ожидающий домен: роль ставит наблюдатель и таймер повтора по отметке ikev2-domain.
        role = ROOT / 'infra/ansible/roles/ikev2'
        tasks = vpn_roles.role_tasks('ikev2')
        self.assertIn('ikev2_le_domain | length > 0', tasks)
        for unit in ('anysda-ikev2-cert-sync.path', 'anysda-ikev2-cert-sync.timer'):
            self.assertIn(unit, tasks.split('ikev2_le_domain | length > 0', 1)[1])
        self.assertIn('OnUnitActiveSec=30s', (role / 'files/anysda-ikev2-cert-sync.timer').read_text())
        self.assertIn('"{{ ikev2_le_domain }}"',
                      (role / 'templates/anysda-ikev2-cert-sync.service.j2').read_text())
        self.assertIn("ikev2_domain.content", vpn_roles.role_defaults('ikev2'))

    def test_appearance_rotation_and_reload_failure(self):
        helper = LE_SYNC.read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for child in ('etc/swanctl/x509', 'etc/swanctl/x509ca', 'etc/swanctl/private',
                          'etc/swanctl/conf.d', 'etc/anysda', 'run'):
                (root / child).mkdir(parents=True)
            helper = helper.replace('/etc/', directory + '/etc/').replace('/run/', directory + '/run/')
            (root / 'helper.sh').write_text(helper)
            conf = root / 'etc/swanctl/conf.d/anysda.conf'
            # Раскладку на обе цепочки деплой написал заранее: скрипт меняет только пару.
            pending = ('connections {\n  anysda-ikev2 {\n    local-server {\n'
                       '      certs = anysda-server.crt, anysda-own.crt\n      id    = %any\n    }\n  }\n}\n')
            conf.write_text(pending)
            (root / 'etc/swanctl/x509ca/anysda-le-chain-9.crt').write_text('previous chain')
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
            self.assertEqual(conf.read_text(), pending, 'conn со своим CA для прежних профилей не трогается')
            self.assertEqual((root / 'etc/swanctl/x509/anysda-server.crt').read_text().strip(),
                             (root / 'leaf.pem').read_text().strip())
            self.assertEqual(list((root / 'etc/swanctl/x509ca').iterdir()), [], 'цепочка прежнего сертификата')
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
        ikev2 = package('ikev2')
        domain = 'vpn.example.test'
        cases = (
            # (адрес профилей, прошлые отметки mode/server-host, есть LE-cert) →
            # (режим, адрес в профилях, id сервера, домен под LE, обе цепочки)
            ('192.0.2.1', '', None, False, ('self-signed', '192.0.2.1', '192.0.2.1', '', False)),
            (domain, domain, ('letsencrypt', domain), True, ('letsencrypt', domain, domain, domain, False)),
            (domain, domain, None, False, ('self-signed', domain, '%any', domain, True)),
            (domain, domain, None, True, ('letsencrypt', domain, domain, domain, False)),
            # Домен добавлен к серверу, раздававшему профили по IP: Let's Encrypt для новых
            # клиентов, свой CA рядом для прежних. Так же после ожидания сертификата.
            (domain, domain, ('self-signed', '192.0.2.1'), True, ('letsencrypt', domain, '%any', domain, True)),
            (domain, domain, ('self-signed', domain), True, ('letsencrypt', domain, '%any', domain, True)),
            (domain, domain, ('self-signed', '192.0.2.1'), False, ('self-signed', domain, '%any', domain, True)),
            # Явный public_host важнее домена панели.
            ('edge.example.net', domain, None, True, ('self-signed', 'edge.example.net', '%any', '', False)),
        )
        for public, panel, previous, issued, expected in cases:
            with self.subTest(public=public, previous=previous, issued=issued), \
                    tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'etc').mkdir()
                if previous:
                    (root / 'etc/ikev2-mode').write_text(previous[0])
                    (root / 'etc/ikev2-server-host').write_text(previous[1])
                if previous and previous[0] == 'self-signed':
                    (root / 'swanctl/x509ca').mkdir(parents=True)
                    (root / 'swanctl/x509ca/anysda-ca.crt').write_text('own')
                    # Отметка прежних версий, оставлявшая такой сервер на своём CA.
                    (root / 'etc/ikev2-own-ca-kept').touch()
                caddy = root / 'caddy' / domain
                caddy.mkdir(parents=True)
                if issued:
                    for suffix in ('crt', 'key'):
                        (caddy / f'{domain}.{suffix}').write_text('pem')
                chosen = ikev2.plan('192.0.2.1', public, panel, etc=root / 'etc',
                                    swanctl=root / 'swanctl', caddy=root / 'caddy')
                self.assertEqual((chosen.mode, chosen.server_host, chosen.local_id, chosen.le_domain,
                                  chosen.both), expected)
        # Адрес профилей: WG_PUBLIC_HOST, без него домен панели, без него адрес входа.
        defaults = vpn_roles.role_defaults('ikev2')
        line = defaults.split('ikev2_public_host:', 1)[1].split('ikev2_subnet:', 1)[0]
        self.assertLess(line.index('WG_PUBLIC_HOST'), line.index('ikev2_panel_domain'))
        self.assertLess(line.index('ikev2_panel_domain'), line.index('ikev2_entry_host'))

    def test_restored_letsencrypt_pair_is_kept_until_caddy_issues(self):
        ikev2 = package('ikev2')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for child in ('etc', 'swanctl/x509', 'swanctl/private'):
                (root / child).mkdir(parents=True)
            (root / 'etc/ikev2-mode').write_text('letsencrypt')
            (root / 'etc/ikev2-server-host').write_text('vpn.example.test')
            (root / 'swanctl/x509/anysda-server.crt').write_text('pem')
            (root / 'swanctl/private/anysda-server.key').write_text('pem')
            chosen = ikev2.plan('192.0.2.1', 'vpn.example.test', 'vpn.example.test', etc=root / 'etc',
                                swanctl=root / 'swanctl', caddy=root / 'caddy')
            self.assertEqual(chosen.mode, 'letsencrypt')

    def test_own_certificate_names_domain_and_entry_ip_with_same_ca(self):
        pki = package('pki')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'pki').mkdir()
            said = []
            pki.build_ikev2(root / 'pki', '192.0.2.1', '192.0.2.1', say=said.append)
            ca = (root / 'pki/ca.crt').read_bytes()
            old = (root / 'pki/server.crt').read_bytes()
            pki.build_ikev2(root / 'pki', 'vpn.example.test', '192.0.2.1', say=said.append)
            self.assertIn('SAN не содержит vpn.example.test, перевыпуск server cert', said)
            self.assertEqual((root / 'pki/ca.crt').read_bytes(), ca, 'clients trust the same CA')
            self.assertNotEqual((root / 'pki/server.crt').read_bytes(), old)
            text = bash('openssl x509 -in pki/server.crt -noout -text', directory).stdout
            subject = bash('openssl x509 -in pki/server.crt -noout -subject -nameopt RFC2253',
                           directory).stdout
            self.assertEqual(subject.strip(), 'subject=CN=vpn.example.test')
            self.assertIn('DNS:vpn.example.test, IP Address:192.0.2.1', text)
            for name in ('vpn.example.test', '192.0.2.1'):
                flag = '-verify_ip' if name[0].isdigit() else '-verify_hostname'
                check = bash(f'openssl verify -CAfile pki/ca.crt -purpose sslserver {flag} {name} '
                             'pki/server.crt', directory)
                self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
            kept = (root / 'pki/server.crt').read_bytes()
            del said[:]
            pki.build_ikev2(root / 'pki', 'vpn.example.test', '192.0.2.1', say=said.append)
            self.assertEqual(said, [], 'certificate with both names is kept')
            self.assertEqual((root / 'pki/server.crt').read_bytes(), kept)
            self.assertEqual(sorted(path.name for path in (root / 'pki').iterdir()),
                             ['ca.crt', 'ca.key', 'server.crt', 'server.key'])

    def test_summary_reads_actual_mode(self):
        # Итог деплоя (print_summary из infra/lib/deploy-report.sh) называет режим IKEv2.
        self.assertRegex(source('infra/deploy.sh'), r'(?m)^\s+print_summary$')
        self.assertIn('\n  print_ikev2_notice\n', function('print_summary'), 'deploy never reports IKEv2 mode')
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
