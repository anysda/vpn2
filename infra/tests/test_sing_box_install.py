"""Offline regression of the real install sections of both deployment stages.

VPN2_TEST_ROOT can point to a git archive of the baseline; no host paths are used.
Only the fixture copy of the installer gets fixture digests, never production code.
"""
import hashlib
import os
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
STAGES = ('10-foreign', '20-ru-router')
VERSION = '1.12.25'


class SingBoxInstallTests(unittest.TestCase):
    def exercise(self, stage, scenario, arch='amd64'):
        with tempfile.TemporaryDirectory(prefix='sing-box-test.') as directory:
            work = Path(directory)
            tools = work / 'tools'
            tools.mkdir()
            scratch = work / 'scratch'
            scratch.mkdir()
            dest = work / 'sing-box'
            executed = work / 'executed'
            downloads = work / 'downloads'
            unpacked = work / 'unpacked'
            continued = work / 'continued'

            def script(path, body):
                path.write_text('#!/bin/bash\nset -eu\n' + body)
                path.chmod(0o755)

            old_version = VERSION if scenario == 'already' else '1.12.24'
            if scenario == 'near_version':
                old_version = VERSION + '0'
            script(dest, f'echo "sing-box version {old_version}"\n')
            old = dest.read_bytes()
            before = dest.stat()
            if scenario == 'fresh':
                dest.unlink()

            archive = work / 'release.tar.gz'

            def release(version):
                payload = work / 'payload'
                script(payload, f'echo ran >> {shlex.quote(str(executed))}\n'
                       f'echo "sing-box version {version}"\n')
                with tarfile.open(archive, 'w:gz') as tar:
                    tar.add(payload, arcname=f'sing-box-{VERSION}-linux-{arch}/sing-box')

            release('0.0.0' if scenario == 'wrong_version' else VERSION)
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            if scenario == 'tampered':
                release('9.9.9')
            elif scenario == 'checksum':
                digest = '0' * 64
            elif scenario == 'broken_tar':
                archive.write_bytes(b'not a tar archive')
                digest = hashlib.sha256(archive.read_bytes()).hexdigest()

            helper = ROOT / 'infra/lib/install-sing-box.sh'
            local_helper = work / 'install-sing-box.sh'
            if helper.exists():
                source = re.sub(r"(SB_SHA256_(?:AMD64|ARM64)=')[0-9a-f]{64}",
                                lambda match: match[1] + digest, helper.read_text())
                local_helper.write_text(source.replace('/usr/local/bin/sing-box', str(dest)))

            # These exact stage sections include the post-install version execution.
            source = (ROOT / f'infra/scripts/{stage}.sh').read_text()
            section = re.search(r'echo "\[\$HOST_TAG\] \[\d/\d\] sing-box"\n(.*?)\n# -+',
                                source, re.S)[0].rsplit('\n# -', 1)[0]
            # Isolate the legacy tar -C /tmp and fixed extraction directory as well.
            section = section.replace('/tmp', str(scratch)) if not helper.exists() else section
            section = section.replace('/usr/local/bin/sing-box', str(dest))
            section = section.replace('/tmp/anysda/install-sing-box.sh', str(local_helper))
            stage_file = work / 'stage.sh'
            script(stage_file, 'set -o pipefail\nHOST_TAG=test\n' + section
                   + f'\ntouch {shlex.quote(str(continued))}\n')
            script(tools / 'uname', f'echo {"aarch64" if arch == "arm64" else "x86_64"}\n')
            script(tools / 'curl', '''
printf '%s\\n' "$*" >> "$DOWNLOADS"
out=''
fail=0
while (($#)); do
  case "$1" in
    -o) out=$2; shift ;;
    -fsSL|--fail) fail=1 ;;
  esac
  shift
done
if [[ "$SCENARIO" == signal ]]; then kill -TERM "$PPID"; exit 143; fi
if [[ "$SCENARIO" == network ]]; then exit 7; fi
if [[ "$SCENARIO" == http404 && "$fail" == 1 ]]; then
  echo 'curl: (22) The requested URL returned error: 404' >&2
  exit 22
fi
if [[ -n "$out" ]]; then cp "$ARCHIVE" "$out"; else cat "$ARCHIVE"; fi
''')
            script(tools / 'tar', 'touch "$UNPACKED"\nexec /usr/bin/tar "$@"\n')
            if scenario in ('install_error', 'mv_error'):
                command = 'install' if scenario == 'install_error' else 'mv'
                body = 'printf partial > "${@: -1}"\n' if command == 'install' else ''
                script(tools / command, body + 'exit 28\n')

            env = dict(os.environ, PATH=f'{tools}:' + os.environ['PATH'],
                       TMPDIR=str(scratch), DOWNLOADS=str(downloads),
                       ARCHIVE=str(archive), SCENARIO=scenario, UNPACKED=str(unpacked))

            def run():
                return subprocess.run(['bash', str(stage_file)], env=env,
                                      capture_output=True, text=True, timeout=15)

            result = run()
            success = scenario in ('good', 'fresh', 'already', 'near_version')
            if success:
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(continued.exists())
                if scenario == 'already':
                    self.assertFalse(downloads.exists())
                    self.assertEqual(dest.stat().st_ino, before.st_ino)
                    self.assertEqual(dest.stat().st_mtime_ns, before.st_mtime_ns)
                else:
                    self.assertEqual(dest.stat().st_mode & 0o777, 0o755)
                    self.assertTrue(downloads.exists(), 'required install was skipped')
                    self.assertEqual(dest.read_bytes(), (work / 'payload').read_bytes())
                    self.assertIn(f'linux-{arch}.tar.gz', downloads.read_text())
                    calls = downloads.read_text()
                    installed = dest.stat()
                    repeat = run()
                    self.assertEqual(repeat.returncode, 0, repeat.stderr)
                    self.assertEqual(downloads.read_text(), calls, 'repeat downloaded')
                    self.assertEqual(dest.stat().st_ino, installed.st_ino)
                    self.assertEqual(dest.stat().st_mtime_ns, installed.st_mtime_ns)
            else:
                self.assertNotEqual(result.returncode, 0, 'stage accepted an unsafe/failed install')
                self.assertFalse(continued.exists(), 'stage continued after failure')
                self.assertEqual(dest.read_bytes(), old, 'previous binary overwritten')
                self.assertEqual(dest.stat().st_ino, before.st_ino)
                self.assertEqual(dest.stat().st_mtime_ns, before.st_mtime_ns)
                if scenario != 'wrong_version' and scenario != 'mv_error':
                    self.assertFalse(executed.exists(), 'downloaded binary executed')
                if scenario in ('tampered', 'checksum', 'http404', 'network', 'signal'):
                    self.assertFalse(unpacked.exists(), 'unverified archive unpacked')
            self.assertEqual(list(scratch.iterdir()), [], 'temporary directory leaked')
            self.assertEqual(list(work.glob('.sing-box.*')), [], 'candidate leaked')

    def test_bad_downloads_stop_each_stage(self):
        for stage in STAGES:
            for scenario in ('tampered', 'checksum', 'http404', 'network'):
                with self.subTest(stage=stage, scenario=scenario):
                    self.exercise(stage, scenario)

    def test_install_and_repeat_both_architectures(self):
        for stage in STAGES:
            for arch in ('amd64', 'arm64'):
                with self.subTest(stage=stage, arch=arch):
                    self.exercise(stage, 'good', arch)

    def test_fresh_install(self):
        for stage in STAGES:
            with self.subTest(stage=stage):
                self.exercise(stage, 'fresh')

    def test_existing_exact_version_is_untouched(self):
        for stage in STAGES:
            with self.subTest(stage=stage):
                self.exercise(stage, 'already')

    def test_version_match_is_exact(self):
        for stage in STAGES:
            with self.subTest(stage=stage):
                self.exercise(stage, 'near_version')

    def test_failures_preserve_binary_and_cleanup(self):
        for stage in STAGES:
            for scenario in ('broken_tar', 'install_error', 'mv_error', 'wrong_version', 'signal'):
                with self.subTest(stage=stage, scenario=scenario):
                    self.exercise(stage, scenario)


if __name__ == '__main__':
    unittest.main()
