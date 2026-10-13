"""Запускаем настоящий PANEL_CALL; живую панель предоставляет panel-e2e."""
import http.cookiejar
import json
import os
from pathlib import Path
import runpy
import shlex
import shutil
import subprocess
import tempfile
import unittest
import urllib.parse
import urllib.request

from test_deploy import ROOT


class PanelCallFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='speedtest-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'infra/lib').mkdir(parents=True)
        for name in ('config2env.py', 'hy2_bandwidth.py'):
            shutil.copy(ROOT / 'infra/lib' / name, self.root / 'infra/lib' / name)
        self.script = runpy.run_path(str(ROOT / 'infra/tools/speedtest.py'))['PANEL_CALL']
        for path, replacement in (
            ('/opt/anysda-vpn2', str(self.root)),
            ('/root/.speedtest-panel.jar', str(self.root / 'session.jar')),
            ('/etc/anysda/admin-password.txt', str(self.root / 'password')),
            ('/tmp/.speedtest-clients.json', str(self.root / 'clients.json')),
        ):
            self.script = self.script.replace(path, replacement)

    def configure(self, domain='', user='admin', password='test password'):
        quote = lambda value: "'" + value.replace("'", "''") + "'"
        (self.root / 'config.yaml').write_text(
            f'admin:\n  user: {quote(user)}\npanel:\n  domain: {quote(domain)}\n')
        (self.root / 'password').write_text(password)

    def call(self, prefix='', patches='1=fi'):
        return subprocess.run(['bash', '-c', prefix + self.script], text=True,
                              env={**os.environ, 'PATCHES': patches, 'TMPDIR': str(self.root)},
                              capture_output=True, timeout=30)


class PanelOriginTest(PanelCallFixture):
    def test_requests_follow_panel_origin_and_reuse_session(self):
        for domain, origin in (('', 'http://127.0.0.1:51821'),
                               ('Panel.Example.TEST.', 'https://panel.example.test')):
            with self.subTest(domain=domain):
                (self.root / 'session.jar').unlink(missing_ok=True)
                self.configure(domain, "test'admin")
                binary = self.root / 'bin'
                binary.mkdir(exist_ok=True)
                curl = binary / 'curl'
                curl.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
url = args[-1]
root = Path(os.environ['TMPDIR'])
with (root / 'calls').open('a') as log:
    log.write(json.dumps(args) + '\\n')
if '--data' in args:
    if 'Origin: ' + url.split('/api/')[0] not in args:
        sys.exit(22)
    if args[args.index('--data') + 1] == '@-':
        body = json.load(sys.stdin)
        if body['username'] != "test'admin":
            sys.exit(22)
        (root / 'session.jar').touch()
elif not (root / 'session.jar').exists():
    sys.exit(22)
print('[{"id":1,"name":"speed-test"}]')
''')
                curl.chmod(0o755)
                calls = self.root / 'calls'
                calls.unlink(missing_ok=True)
                for _ in range(2):
                    result = self.call('export PATH=' + shlex.quote(str(binary)) + ':"$PATH"\n')
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout)[0]['id'], 1)
                requests = [json.loads(line) for line in calls.read_text().splitlines()]
                self.assertEqual(sum('/sign-in/' in args[-1] for args in requests), 1)
                for args in requests:
                    self.assertTrue(args[-1].startswith(origin + '/api/'), args)
                    if '--data' in args:
                        self.assertIn('Origin: ' + origin, args)


@unittest.skipUnless(os.environ.get('E2E_BASE_URL'), 'живую панель поднимает panel-e2e')
class LivePanelTest(PanelCallFixture):
    def test_patch_changes_preferred_exit(self):
        base = os.environ['E2E_BASE_URL'].rstrip('/')
        password = os.environ['E2E_ADMIN_PASSWORD']
        cookies = http.cookiejar.MozillaCookieJar(str(self.root / 'session.jar'))
        api = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))

        def request(method, path, data=None):
            body = json.dumps(data).encode() if data is not None else None
            req = urllib.request.Request(base + path, data=body, method=method,
                                         headers={'Origin': base, 'Content-Type': 'application/json'})
            with api.open(req, timeout=20) as response:
                return json.load(response)

        request('POST', '/api/auth/sign-in/username', {'username': 'admin', 'password': password})
        cookies.save(ignore_discard=True)
        client = request('POST', '/api/clients', {'name': 'speedtest-origin-regression'})
        self.addCleanup(request, 'DELETE', f'/api/clients/{client["id"]}')
        self.configure(password=password)
        target = urllib.parse.urlsplit(base)
        connect = f'127.0.0.1:51821:{target.hostname}:{target.port or 80}'
        prefix = ('curl() { command curl --connect-to ' + shlex.quote(connect)
                  + ' --write-out "%{stderr}HTTP %{method} %{http_code}\\n" "$@"; }\n')
        result = self.call(prefix, f'{client["id"]}=fi')
        print(result.stderr, end='')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(request('GET', f'/api/clients/{client["id"]}')['preferredExit'], 'fi')
        self.assertIn('HTTP PATCH 200', result.stderr)
        # Повторить с нуля: логин из YAML-кавычек и Origin у POST входа.
        (self.root / 'session.jar').unlink()
        result = self.call(prefix, f'{client["id"]}=nl')
        print(result.stderr, end='')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(request('GET', f'/api/clients/{client["id"]}')['preferredExit'], 'nl')


if __name__ == '__main__':
    unittest.main()
