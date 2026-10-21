"""./deploy.sh without flags: the verified release comes from the published manifest of
the checked-out commit, without gh or a GitHub account."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'infra/lib'))
import release  # noqa: E402

HEAD = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                      capture_output=True, text=True, check=True).stdout.strip()
REPOSITORY = 'anysda/vpn2/panel'


def contract(commit=HEAD, stages=None):
    return {'format': 1, 'version': '0.0.0-test', 'commit': commit, 'rollback': 'forward-only',
            'images': {}, 'binaries': {},
            'stages': release.source_files(ROOT) if stages is None else stages}


class Registry:
    """OCI distribution subset that GHCR serves to anonymous clients."""

    def __init__(self, blob=None, layer_digest=None, auth=True):
        self.objects = {}
        self.requests = []
        if blob is not None:
            digest = layer_digest or 'sha256:' + hashlib.sha256(blob).hexdigest()
            self.objects[f'/v2/{REPOSITORY}/blobs/{digest}'] = blob
            self.objects[f'/v2/{REPOSITORY}/manifests/release-{HEAD}'] = json.dumps({
                'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                'artifactType': 'application/vnd.anysda.vpn2.release.v1+json',
                'config': {'mediaType': 'application/vnd.oci.empty.v1+json', 'size': 2,
                           'digest': 'sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a'},
                'layers': [{'mediaType': release.MANIFEST_MEDIA_TYPE, 'digest': digest, 'size': len(blob)}],
            }).encode()
        registry = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                registry.requests.append(self.path)
                if self.path.startswith('/token?'):
                    return self.reply(200, b'{"token": "anonymous"}')
                if auth and self.headers.get('Authorization') != 'Bearer anonymous':
                    self.send_response(401)
                    self.send_header('WWW-Authenticate', f'Bearer realm="{registry.url}/token",'
                                     f'service="test",scope="repository:{REPOSITORY}:pull"')
                    self.end_headers()
                    return None
                body = registry.objects.get(self.path)
                return self.reply(200, body) if body is not None else self.reply(404, b'{}')

            def reply(self, code, body):
                self.send_response(code)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):  # noqa - called by BaseHTTPRequestHandler
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.source = f'{self.url}/{REPOSITORY}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def run_python(*args, source):
    return subprocess.run([sys.executable, str(ROOT / 'infra/lib/release.py'), *args],
                          env=dict(os.environ, VPN2_RELEASE_SOURCE=source),
                          capture_output=True, text=True, timeout=60)


class ReleaseQuickstartTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        # release_start calls `python3`: the interpreter under test is used.
        (self.work / 'bin').mkdir()
        (self.work / 'bin/python3').symlink_to(sys.executable)

    def release_start(self, source):
        script = f'''
source {shlex.quote(str(ROOT / 'infra/lib/deploy-release.sh'))}
install_prereqs() {{ echo UNEXPECTED_REMOTE_CHANGE; exit 99; }}
release_start
'''
        env = dict(os.environ, DEPLOY_ROOT=str(ROOT / 'infra'), VPN2_DEVELOPMENT='false',
                   VPN2_RELEASE_SOURCE=source, PATH=f'{self.work / "bin"}:{os.environ["PATH"]}')
        env.pop('VPN2_RELEASE_MANIFEST', None)
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('UNEXPECTED_REMOTE_CHANGE', result.stdout)
        self.assertNotIn('Traceback', result.stderr)
        self.assertNotIn('manifest path required', result.stderr)
        print(result.stderr.strip())
        return result.stderr

    def test_unpublished_commit_explains_what_to_do_before_remote_changes(self):
        registry = Registry()
        self.addCleanup(registry.close)
        stderr = self.release_start(registry.source)
        self.assertIn(f'манифест commit {HEAD[:12]} не опубликован', stderr)
        self.assertIn('./deploy.sh --development', stderr)
        self.assertNotIn('gh ', stderr)

    def test_unreachable_publication_explains_network_and_offline_path(self):
        stderr = self.release_start('http://127.0.0.1:9/' + REPOSITORY)
        self.assertIn(f'не удалось скачать манифест commit {HEAD[:12]}', stderr)
        self.assertIn('без сети: ./deploy.sh --manifest <файл>', stderr)

    def test_published_manifest_is_fetched_anonymously_and_verified(self):
        registry = Registry(json.dumps(contract()).encode())
        self.addCleanup(registry.close)
        output = self.work / 'state/release-manifest.json'
        result = run_python('--manifest', str(output), 'fetch', str(ROOT), source=registry.source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('/token?', ''.join(registry.requests))  # GHCR-style anonymous token
        check = run_python('--manifest', str(output), 'check', str(ROOT), source=registry.source)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn(f'манифест commit {HEAD} (версия 0.0.0-test) проверен', check.stdout)

    def test_cached_manifest_of_same_commit_survives_network_loss(self):
        output = self.work / 'release-manifest.json'
        output.write_text(json.dumps(contract()))
        result = run_python('--manifest', str(output), 'fetch', str(ROOT), source='http://127.0.0.1:9/x')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('скачанный раньше', result.stderr)
        output.write_text(json.dumps(contract(commit='0' * 40)))
        result = run_python('--manifest', str(output), 'fetch', str(ROOT), source='http://127.0.0.1:9/x')
        self.assertEqual(result.returncode, 1)

    def test_tampered_publication_is_rejected(self):
        cases = (
            (Registry(json.dumps(contract()).encode(), layer_digest='sha256:' + 'a' * 64), 'повреждён'),
            (Registry(json.dumps(contract(commit='1' * 40)).encode()), 'манифест другого commit'),
        )
        for registry, message in cases:
            self.addCleanup(registry.close)
            output = self.work / 'rejected.json'
            result = run_python('--manifest', str(output), 'fetch', str(ROOT), source=registry.source)
            self.assertEqual(result.returncode, 1)
            self.assertIn(message, result.stderr)
            self.assertFalse(output.exists())

    def test_changed_stage_names_the_file(self):
        stages = release.source_files(ROOT)
        stages['infra/scripts/30-frontend.sh'] = '0' * 64
        output = self.work / 'release-manifest.json'
        output.write_text(json.dumps(contract(stages=stages)))
        result = run_python('--manifest', str(output), 'check', str(ROOT), source='')
        self.assertEqual(result.returncode, 1)
        self.assertIn('infra/scripts/30-frontend.sh', result.stderr)
        self.assertIn('git stash', result.stderr)

    def test_image_pinned_by_previous_install_is_ignored_with_warning(self):
        config = (ROOT / 'config.example.yaml').read_text()
        config += '\npanel:\n  image: ghcr.io/anysda/vpn2/panel:1.0.0\ntgbot:\n  image: ghcr.io/anysda/vpn2/tgbot:1.0.0\n'
        (self.work / 'config.yaml').write_text(config)
        for development, expected in (('false', None), ('true', "PANEL_IMAGE='ghcr.io/anysda/vpn2/panel:1.0.0'")):
            result = subprocess.run([sys.executable, str(ROOT / 'infra/lib/config2env.py'), str(self.work)],
                                    env=dict(os.environ, VPN2_DEVELOPMENT=development),
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            env = (self.work / 'infra/envs/ru.env').read_text()
            if expected is None:
                self.assertNotRegex(env, re.compile('^(PANEL|TGBOT)_IMAGE=', re.M))
                self.assertIn('panel.image: ghcr.io/anysda/vpn2/panel:1.0.0 не используется', result.stdout)
                self.assertIn('tgbot.image', result.stdout)
            else:
                self.assertIn(expected, env)
                self.assertNotIn('не используется', result.stdout)

    def test_readme_install_needs_no_manual_release_steps(self):
        readme = (ROOT / 'README.md').read_text()
        install = readme.split('# 🚀 Quickstart', 1)[1].split('\n## ', 2)
        install = install[0] + install[1]  # Quickstart and «Настройка»
        for forbidden in ('gh ', 'gh auth', 'gh run', '--manifest', 'release-manifest'):
            self.assertNotIn(forbidden, install)
        for step in ('ssh-copy-id', 'git clone', '\n./setup.sh\n', '\n./deploy.sh\n'):
            self.assertIn(step, install)
        self.assertLess(install.index('git clone'), install.index('\n./setup.sh\n'))
        self.assertLess(install.index('\n./setup.sh\n'), install.index('\n./deploy.sh\n'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
