"""Переход IP→домен выполняется тем же обработчиком, что и ротация Caddy."""
from pathlib import Path
import tempfile
import unittest

from test_deploy import bash, source, function


class Ikev2DomainTests(unittest.TestCase):
    def test_pending_domain_installs_watcher_and_retry_timer(self):
        stage = source('infra/scripts/27-ikev2.sh')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for child in ('etc/anysda', 'etc/systemd/system', 'var/anysda/.stamps', 'pki'):
                (root / child).mkdir(parents=True)
            (root / 'pki/ca.crt').write_text('existing CA')
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
IKEV2_SERVER_HOST=192.0.2.1
PANEL_DOMAIN=vpn.example.test
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
            self.assertIn('нужно скачать заново', result.stdout)

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
