"""sshd config checks survive a missing privilege separation directory."""
from pathlib import Path
import re
import tempfile
import unittest

from test_deploy import ROOT, bash, source


# sshd from OpenSSH: without /run/sshd both -t and -T exit 255.
# systemctl: the ssh.service state lives in the file `service`.
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
systemctl() {
  local state
  state=$(cat service)
  [[ $2 == ssh || $2 == ssh.service ]] || { echo "unexpected unit $2" >&2; return 1; }
  case "$1:$state" in
    reload:active|reload-or-restart:active|try-reload-or-restart:active|try-restart:active)
      echo 'sshd reads config' >> calls ;;
    reload:inactive)
      echo 'ssh.service is not active, cannot reload.' >&2; return 1 ;;
    reload-or-restart:inactive|restart:*|start:inactive)
      echo active > service; echo 'sshd reads config' >> calls ;;
    try-reload-or-restart:inactive|try-restart:inactive) ;;
    *) echo "unexpected systemctl $*" >&2; return 1 ;;
  esac
}
'''

APPLY = re.compile(r'^systemctl [\w-]+ ssh\n', re.M)


def sshd_block():
    text = source('infra/scripts/00-bootstrap.sh')
    start = text.index('\n', text.index("KbdInteractiveAuthentication no/' \"$SSHD\""))
    return text[start:APPLY.search(text, start).end()]


def run_block(directory, service):
    Path(directory, 'sshd_config.d').mkdir()
    Path(directory, 'service').write_text(service + '\n')
    block = sshd_block().replace('/etc/ssh/', directory + '/')
    result = bash(FAKE + block, directory)
    calls = Path(directory, 'calls')
    return result, calls.read_text() if calls.exists() else ''


class SshdPrivsepTests(unittest.TestCase):
    def test_fresh_openssh_upgrade_without_run_sshd(self):
        with tempfile.TemporaryDirectory() as directory:
            result, calls = run_block(directory, 'active')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn('откатываюсь', result.stdout)
            self.assertEqual(calls, 'sshd reads config\n')
            # sshd -T sees the directory too, so OpenSSH 9.8+ penalties are disabled.
            dropin = Path(directory, 'sshd_config.d/99-anysda-no-penalties.conf')
            self.assertEqual(dropin.read_text(), 'PerSourcePenalties no\n')

    def test_ssh_service_stopped_by_openssh_upgrade(self):
        # Ubuntu 26.04: apt stops ssh.service, ssh.socket stays. A host without
        # the socket must not be left without sshd either, so the unit is started.
        with tempfile.TemporaryDirectory() as directory:
            result, calls = run_block(directory, 'inactive')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(calls, 'sshd reads config\n')
            self.assertEqual(Path(directory, 'service').read_text(), 'active\n')

    def test_no_plain_systemctl_reload_in_deploy_scripts(self):
        # apt stops units on upgrade; `systemctl reload` on a stopped unit fails.
        plain = re.compile(r'\bsystemctl\s+(?:-\S+\s+)*reload\s')
        paths = sorted((ROOT / 'infra').rglob('*.sh')) + [ROOT / 'deploy.sh', ROOT / 'setup.sh']
        for path in paths:
            if 'tests' in path.relative_to(ROOT).parts:
                continue
            for number, line in enumerate(path.read_text().splitlines()):
                if line.lstrip().startswith('#') or not plain.search(line):
                    continue
                with self.subTest(path=str(path.relative_to(ROOT)), line=number + 1):
                    self.fail('systemctl reload fails on a stopped unit: ' + line.strip())

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
