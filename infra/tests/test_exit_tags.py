import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from test_deploy import ROOT, bash, function, source


class ExitTagTests(unittest.TestCase):
    def test_previous_panel_hosts_survive_failed_upgrade_retry(self):
        stage = source('infra/scripts/30-frontend.sh')
        capture = 'PREVIOUS_NODE_HOSTS=' + stage.split('PREVIOUS_NODE_HOSTS=', 1)[1].split('\ndocker rm', 1)[0]
        for previous in ('', 'ru=192.0.2.1,se=192.0.2.2'):
            with self.subTest(previous=previous):
                env = 'NUXT_NODE_HOSTS=ru=192.0.2.1,se1=192.0.2.2\n'
                env += f'NUXT_PREVIOUS_NODE_HOSTS={previous}\nUNRELATED_SECRET=not-for-output\n'
                result = bash('docker() { printf "%s" ' + shlex.quote(env) + '; }\n'
                              + capture + '\nprintf "%s" "$PREVIOUS_NODE_HOSTS"', ROOT)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, previous or 'ru=192.0.2.1,se1=192.0.2.2')
        result = bash('docker() { return 1; }\n' + capture + '\nprintf "%s" "$PREVIOUS_NODE_HOSTS"', ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')

    def test_hyphenated_tag_through_deploy_consumers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config.yaml').write_text(
                'entry:\n  host: 192.0.2.1\n  password: test-password\n'
                'exits:\n  - tag: us-2\n    host: 192.0.2.2\n'
                '    password: test-password\n    hy2_direct_port: 1443\n'
                '  - tag: us2\n    host: 192.0.2.3\n    password: test-password\n')
            result = subprocess.run([sys.executable, str(ROOT / 'infra/lib/config2env.py'),
                                     directory], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            stage = source('infra/scripts/20-ru-router.sh')
            secrets = stage.split('while IFS= read -r _line', 1)[1].split(
                '# ----------------------------------------------------------------------------', 1)[0]
            exports = stage.split('export RU_CLASH_SECRET EXIT_TAGS', 1)[1].split(
                '# TLS pinning', 1)[0]
            pins = stage.split('mkdir -p /etc/sing-box/exit-certs', 1)[1].split(
                '# Write the BASE config', 1)[0]
            pins = ('mkdir -p /etc/sing-box/exit-certs' + pins).replace(
                '/etc/sing-box/exit-certs', str(root / 'pinned')).replace(
                '/tmp/anysda/exit-certs', str(root / 'certs'))
            (root / 'certs').mkdir()
            (root / 'certs/us-2.pem').write_text('fixture-cert')
            monitoring = source('infra/scripts/25-monitoring.sh').split(
                '\n{\n', 1)[1].split('\n} > /etc/anysda/vmsingle-scrape.yml', 1)[0]
            ssh = stage.split('  echo "# anysda-vpn — генерирует', 1)[1].split(
                '\n} > "$SSH_MGMT_CONF"', 1)[0].split('\n', 1)[1]
            panel = source('infra/scripts/30-frontend.sh')
            hosts = 'NODE_HOSTS=' + panel.split('NODE_HOSTS=', 1)[1].split('\ndocker rm', 1)[0]
            mgmt = panel.split('  -e NUXT_MGMT_IPS=', 1)[1].split(' \\\n', 1)[0]
            script = function('prep_ru_router') + function('_current_port') + '''
DEPLOY_ROOT=$PWD/infra
source "$DEPLOY_ROOT/envs/all.env"
source "$DEPLOY_ROOT/envs/ru.env"
log() { :; }; ok() { :; }; warn() { :; }
die() { echo "$*" >&2; exit 1; }
exit_tags() { echo "$EXIT_TAGS"; }
load_env() { :; }
ssh_exec() {
  case $* in
    *'echo "DIRECT='*)
      for key in DIRECT WARP OBFS MGMT CLASH; do
        printf '%s=ABCDEFGHIJKLMNOP\n' "$key"
      done;;
    *) return 1;;
  esac
}
prep_ru_router
FS=$DEPLOY_ROOT/secrets/foreign-secrets.env
RU_CLASH_SECRET=test-secret
SSH_MGMT_KNOWN=$PWD/known_hosts
export WG_OUT_IFACE=lo
''' + 'while IFS= read -r _line' + secrets + 'export RU_CLASH_SECRET EXIT_TAGS' + exports + pins
            script += '\npython3 ' + shlex.quote(str(ROOT / 'infra/lib/gen-router-config.py')) + ' > router.json\n'
            script += '_current_port us-2 > port.txt\n'
            script += '{\n' + monitoring + '\n} > scrape.yml\n'
            script += '{\n' + ssh + '\n} > ssh.conf\n'
            script += hosts + '\nprintf "%s\\n" "$NODE_HOSTS" > hosts.txt\n'
            script += 'printf "%s\\n" ' + mgmt + ' > mgmt.txt\n'
            result = bash(script, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            cfg = json.loads((root / 'router.json').read_text())
            direct = next(o for o in cfg['outbounds'] if o['tag'] == 'hy2-us-2-direct')
            if direct.get('detour'):
                direct = next(o for o in cfg['outbounds'] if o['tag'] == direct['detour'])
            self.assertEqual((direct['server'], direct['server_port']), ('192.0.2.2', 1443))
            self.assertEqual(direct['password'], 'ABCDEFGHIJKLMNOP')
            self.assertEqual(direct['tls']['certificate_path'], str(root / 'pinned/us-2.pem'))
            other = next(o for o in cfg['outbounds'] if o['tag'] == 'hy2-us2-direct')
            if other.get('detour'):
                other = next(o for o in cfg['outbounds'] if o['tag'] == other['detour'])
            self.assertEqual((other['server'], other['server_port']), ('192.0.2.3', 443))
            self.assertEqual((root / 'port.txt').read_text(), '1443\n')
            self.assertEqual((root / 'hosts.txt').read_text(),
                             'ru=192.0.2.1,us-2=192.0.2.2,us2=192.0.2.3\n')
            for tag in ('us-2', 'us2'):
                self.assertTrue((root / f'infra/envs/{tag}.env').is_file())
                mon = next(i for i in cfg['inbounds'] if i['tag'] == f'mon-{tag}')
                port = mon['listen_port']
                self.assertIn(f'node-{tag}', (root / 'scrape.yml').read_text())
                self.assertIn(f'socks5://127.0.0.1:{port}', (root / 'scrape.yml').read_text())
                self.assertIn(f'Host {tag}\n', (root / 'ssh.conf').read_text())
                self.assertIn(f'127.0.0.1:{port} %h %p', (root / 'ssh.conf').read_text())
                self.assertIn(f'{tag}:', (root / 'mgmt.txt').read_text())


if __name__ == '__main__':
    unittest.main()
