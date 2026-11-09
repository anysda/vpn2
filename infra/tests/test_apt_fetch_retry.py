import hashlib
import http.server
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from test_deploy import source

TOOLS = ('apt-get', 'dpkg-deb', 'gpg')
# Proxy's «Remote or cache error» hits every file of the suite, the Release fallback too
SUITE = ('InRelease', 'Release', 'Release.gpg')


def build_repo(root, home):
    """Signed one-package repository, the shape apt sees from an Ubuntu mirror."""
    package = root / 'build'
    (package / 'DEBIAN').mkdir(parents=True)
    (package / 'DEBIAN' / 'control').write_text(
        'Package: zzfake\nVersion: 1\nArchitecture: all\nMaintainer: t <t@t>\nDescription: t\n')
    pool = root / 'pool'
    pool.mkdir()
    deb = pool / 'zzfake_1_all.deb'
    subprocess.run(['dpkg-deb', '--root-owner-group', '-b', str(package), str(deb)],
                   check=True, capture_output=True)
    data = deb.read_bytes()
    index = root / 'dists' / 'x' / 'main' / 'binary-amd64'
    index.mkdir(parents=True)
    packages = (f'Package: zzfake\nVersion: 1\nArchitecture: all\nFilename: pool/{deb.name}\n'
                f'Size: {len(data)}\nSHA256: {hashlib.sha256(data).hexdigest()}\nDescription: t\n\n').encode()
    (index / 'Packages').write_bytes(packages)
    release = root / 'dists' / 'x' / 'Release'
    release.write_text('Suite: x\nCodename: x\nArchitectures: amd64\nComponents: main\n'
                       'Date: Mon, 09 Nov 2026 00:00:00 UTC\nSHA256:\n'
                       f' {hashlib.sha256(packages).hexdigest()} {len(packages)} main/binary-amd64/Packages\n')
    gpg = ['gpg', '--homedir', str(home), '--batch', '--quiet', '--pinentry-mode', 'loopback', '--passphrase', '']
    subprocess.run(gpg + ['--quick-gen-key', 'mirror <m@m>', 'ed25519', 'sign', 'never'],
                   check=True, capture_output=True)
    subprocess.run(gpg + ['--yes', '--clearsign', '-o', str(release.with_name('InRelease')), str(release)],
                   check=True, capture_output=True)
    subprocess.run(gpg + ['--yes', '--detach-sign', '-o', str(release.with_name('Release.gpg')), str(release)],
                   check=True, capture_output=True)
    key = root.parent / 'mirror.gpg'
    key.write_bytes(subprocess.run(gpg + ['--export'], check=True, capture_output=True).stdout)
    return key


class Mirror(http.server.ThreadingHTTPServer):
    """Mirror behind the proxy: a path in `failing` answers 500 that many times, then serves the file."""

    def __init__(self, root):
        self.root = root
        self.failing = {}
        self.hits = {}
        self.lock = threading.Lock()
        super().__init__(('127.0.0.1', 0), MirrorHandler)


class MirrorHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(args[2].root), **kwargs)

    def do_GET(self):
        name = self.path.rsplit('/', 1)[-1]
        with self.server.lock:
            self.server.hits[name] = self.server.hits.get(name, 0) + 1
            left = self.server.failing.get(name, 0)
            if left:
                self.server.failing[name] = left - 1
        if left:
            self.send_response(500, 'Remote or cache error')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        super().do_GET()

    def log_message(self, *_args):  # noqa - called by BaseHTTPRequestHandler
        pass


@unittest.skipUnless(all(shutil.which(tool) for tool in TOOLS), 'нужны apt-get, dpkg-deb и gpg')
class TestAptFetchRetry(unittest.TestCase):
    """apt-get ходит в свой HTTP-сервер: разовый 500 зеркала - повтор, стойкий - понятный отказ."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        home = base / 'gnupg'
        home.mkdir(mode=0o700)
        repo = base / 'repo'
        repo.mkdir()
        key = build_repo(repo, home)
        cls.mirror = Mirror(repo)
        threading.Thread(target=cls.mirror.serve_forever, daemon=True).start()
        cls.source_line = f'deb [signed-by={key}] http://127.0.0.1:{cls.mirror.server_port} x main\n'

    @classmethod
    def tearDownClass(cls):
        cls.mirror.shutdown()
        cls.mirror.server_close()
        cls.tmp.cleanup()

    def run_apt(self, failing, primed=False):
        """00-bootstrap's pair: apt_get update, then apt_get install (download only, no root).
        primed: lists of an earlier update are there, apt only warns and keeps them."""
        with self.mirror.lock:
            self.mirror.failing = {}
        work = Path(tempfile.mkdtemp(dir=self.tmp.name))
        for sub in ('etc/apt.conf.d', 'etc/preferences.d', 'etc/sources.list.d',
                    'lists/partial', 'cache/archives/partial'):
            (work / sub).mkdir(parents=True)
        (work / 'etc' / 'sources.list').write_text(self.source_line)
        (work / 'status').touch()
        (work / 'apt.conf').write_text(
            f'Dir::Etc "{work}/etc";\nDir::State::Lists "{work}/lists";\n'
            f'Dir::Cache "{work}/cache";\nDir::State::status "{work}/status";\n'
            'Debug::NoLocking "true";\nAPT::Get::List-Cleanup "false";\n')
        env = dict(os.environ, APT_CONFIG=str(work / 'apt.conf'), WORK=str(work), LC_ALL='C.UTF-8')
        if primed:
            subprocess.run(['apt-get', '-qq', 'update'], env=env, check=True, capture_output=True, timeout=30)
        with self.mirror.lock:
            self.mirror.failing = dict(failing)
            self.mirror.hits = {}
        script = source('infra/lib/anysda-svc.sh') + '''
apt_wait_idle() { :; }
sleep() { echo "pause $1" >> "$WORK/pauses"; }
apt_get update -qq
apt_get install -y -qq --download-only zzfake
'''
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=work, env=env,
                                text=True, capture_output=True, timeout=60)
        pauses = (work / 'pauses').read_text().split('\n') if (work / 'pauses').exists() else []
        return result, list(work.glob('cache/archives/zzfake_*.deb')), dict(self.mirror.hits), pauses

    def test_one_off_500_on_inrelease_is_retried(self):
        result, debs, hits, pauses = self.run_apt(dict.fromkeys(SUITE, 1))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(debs), 1, result.stdout + result.stderr)
        self.assertEqual(hits['InRelease'], 2)
        self.assertEqual(len([p for p in pauses if p]), 1)

    def test_one_off_500_over_old_lists_is_retried(self):
        # The stand's case: apt only warns «Failed to fetch», keeps old lists and exits 0
        result, debs, hits, _ = self.run_apt(dict.fromkeys(SUITE, 1), primed=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(hits['InRelease'], 2, result.stdout + result.stderr)
        self.assertNotIn('Failed to fetch', result.stdout + result.stderr)

    def test_one_off_500_on_deb_is_retried(self):
        result, debs, hits, _ = self.run_apt({'zzfake_1_all.deb': 1})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(debs), 1, result.stdout + result.stderr)
        self.assertEqual(hits['zzfake_1_all.deb'], 2)

    def test_mirror_down_fails_the_step_with_a_plain_reason(self):
        result, debs, hits, _ = self.run_apt(dict.fromkeys(SUITE + ('zzfake_1_all.deb',), 1000))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(debs, [])
        # Bounded: a few attempts, not a loop until the step's timeout
        self.assertLessEqual(hits['InRelease'], 3)
        self.assertIn('зеркало apt не отдало', result.stdout + result.stderr)
        self.assertIn('/dists/x/InRelease', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
