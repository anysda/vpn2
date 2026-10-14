"""sshd config checks survive a missing privilege separation directory."""
from pathlib import Path
import re
import tempfile
import unittest

from test_deploy import ROOT, bash, source


# sshd from OpenSSH: without /run/sshd both -t and -T exit 255.
FAKE = r'''
HOST_TAG=de
install() {
  [[ "$*" == '-d -m 0755 /run/sshd' ]] || return 1
  touch privsep
}
sshd() {
  if [[ ! -e privsep ]]; then
    echo 'Missing privilege separation directory: /run/sshd' >&2
    return 255
  fi
  [[ $1 == -T ]] && echo 'persourcepenalties authfail:5s'
  return 0
}
write_sshd_auth() { echo "auth $1" >> calls; }
systemctl() { echo "systemctl $*" >> calls; }
'''


class SshdPrivsepTests(unittest.TestCase):
    def test_fresh_openssh_upgrade_without_run_sshd(self):
        text = source('infra/scripts/00-bootstrap.sh')
        start = text.index('\n', text.index("KbdInteractiveAuthentication no/' \"$SSHD\""))
        end = text.index('systemctl reload ssh\n', start) + len('systemctl reload ssh\n')
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'sshd_config.d').mkdir()
            block = text[start:end].replace('/etc/ssh/', directory + '/')
            result = bash(FAKE + block, directory)
            calls = (Path(directory) / 'calls').read_text() if (Path(directory) / 'calls').exists() else ''
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn('откатываюсь', result.stdout)
            self.assertEqual(calls, 'systemctl reload ssh\n')
            # sshd -T sees the directory too, so OpenSSH 9.8+ penalties are disabled.
            dropin = Path(directory, 'sshd_config.d/99-anysda-no-penalties.conf')
            self.assertEqual(dropin.read_text(), 'PerSourcePenalties no\n')

    def test_every_sshd_check_in_infra_creates_run_sshd_first(self):
        check = re.compile(r'\bsshd\s+-[tT]\b')
        for path in sorted((ROOT / 'infra').rglob('*.sh')):
            lines = path.read_text().splitlines()
            for number, line in enumerate(lines):
                if not check.search(line) or line.lstrip().startswith('#'):
                    continue
                with self.subTest(path=str(path.relative_to(ROOT)), line=number + 1):
                    self.assertTrue(any('install -d -m 0755 /run/sshd' in previous
                                        for previous in lines[:number]),
                                    'sshd config check without install -d /run/sshd before it')


if __name__ == '__main__':
    unittest.main()
