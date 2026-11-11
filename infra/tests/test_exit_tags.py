import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from containers_support import compose
from router_support import stage
from test_deploy import ROOT, bash, function


class ExitTagTests(unittest.TestCase):
    def test_hyphenated_tag_through_deploy_consumers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config.yaml').write_text(
                'entry:\n  host: 192.0.2.1\n  password: test-password\n'
                'exits:\n  - tag: us-2\n    host: 192.0.2.2\n'
                '    password: test-password\n    hy2_direct_port: 1443\n'
                '  - tag: us2\n    host: 192.0.2.3\n    password: test-password\n')
            result = subprocess.run([sys.executable, str(ROOT / 'infra/lib/config2env.py'),
                                     directory], text=True, capture_output=True,
                                    env={**os.environ, 'VPN2_STATE_DIR': f'{directory}/state'})
            self.assertEqual(result.returncode, 0, result.stderr)
            (root / 'certs').mkdir()
            (root / 'certs/us-2.pem').write_text('fixture-cert')
            (root / 'certs/us2.pem').write_text('fixture-cert')
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
set -a
source "$DEPLOY_ROOT/envs/all.env"
source "$DEPLOY_ROOT/envs/ru.env"
set +a
env -0 > stage.env
'''
            script += '_current_port us-2 > port.txt\n'
            result = bash(script, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            environ = dict(item.split('=', 1) for item in (root / 'stage.env').read_text().split('\0') if item)
            foreign = stage.parse_secrets((root / 'infra/secrets/foreign-secrets.env').read_text())
            env, _ = stage.exit_environment(environ, foreign, root / 'certs', root / 'pinned', 'lo')
            env['RU_CLASH_SECRET'] = 'test-secret'
            cfg = json.loads(stage.render_config(env))
            tags = environ['EXIT_TAGS'].split()
            (root / 'ssh.conf').write_text(stage.ssh_config_text(tags, environ, root / 'known_hosts')[0])
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
            self.assertEqual(compose.node_hosts(environ), 'ru=192.0.2.1,us-2=192.0.2.2,us2=192.0.2.3')
            scrape, mgmt = compose.scrape_config(environ)[0], compose.mgmt_ips(environ)
            for tag in ('us-2', 'us2'):
                self.assertTrue((root / f'infra/envs/{tag}.env').is_file())
                mon = next(i for i in cfg['inbounds'] if i['tag'] == f'mon-{tag}')
                port = mon['listen_port']
                self.assertIn(f'node-{tag}', scrape)
                self.assertIn(f'socks5://127.0.0.1:{port}', scrape)
                self.assertIn(f'Host {tag}\n', (root / 'ssh.conf').read_text())
                self.assertIn(f'127.0.0.1:{port} %h %p', (root / 'ssh.conf').read_text())
                self.assertIn(f'{tag}:', mgmt)


if __name__ == '__main__':
    unittest.main()
