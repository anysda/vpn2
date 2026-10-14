"""Отказ ssh: pre-flight и ssh_exec называют настоящую причину.

Подставной sshpass отдаёт stderr и код настоящего OpenSSH для каждого случая.
"""
from pathlib import Path
import re
import tempfile
import unittest

from test_deploy import ROOT, bash, function, source

CHANGED_KEY = '''@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @
@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
IT IS POSSIBLE THAT SOMEONE IS DOING SOMETHING NASTY!
Offending ED25519 key in /root/.known_hosts:1
Host key for 203.0.113.7 has changed and you have requested strict checking.
Host key verification failed.
'''
WRONG_PASSWORD = 'Permission denied, please try again.\n'
REFUSED = 'ssh: connect to host 203.0.113.7 port 22: Connection refused\n'


def run(case_err, case_rc, body):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'bin').mkdir()
        fake = root / 'bin/sshpass'
        fake.write_text('#!/bin/sh\necho call >> "$CALLS"\nprintf %s "$FAKE_ERR" >&2\nexit "$FAKE_RC"\n')
        fake.chmod(0o755)
        (root / 'err').write_text(case_err)
        script = f'''
export PATH="$PWD/bin:$PATH" CALLS="$PWD/calls" FAKE_RC={case_rc}
FAKE_ERR=$(cat err); export FAKE_ERR
DEPLOY_ROOT=$PWD
source {ROOT}/infra/lib/ssh.sh
sleep() {{ :; }}
SSH_USER=root SSH_HOST=203.0.113.7 SSH_PASS=secret
{body}
'''
        result = bash(script, directory)
        calls = (root / 'calls').read_text().count('call') if (root / 'calls').exists() else 0
        return result, calls, str(root / '.known_hosts')


PREFLIGHT = (''.join(function(name) for name in
                     re.findall(r'^(_?preflight\w*)\(\) \{', source('infra/deploy.sh'), re.M)) + '''
expand_hosts() { echo at; }
load_env() { :; }
_tcp22_open() { return 0; }
preflight_ssh
''')


class SshRefusalTests(unittest.TestCase):
    def test_changed_host_key_names_key_and_gives_removal_command(self):
        result, calls, known = run(CHANGED_KEY, 255, PREFLIGHT)
        out = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('host-key', out)
        self.assertIn(f'ssh-keygen -R 203.0.113.7 -f {known}', out)
        self.assertNotIn('PasswordAuthentication', out)
        self.assertNotIn('firewall', out)
        # Повтор смену ключа не лечит.
        self.assertEqual(calls, 1, out)

    def test_wrong_password_points_at_credentials_not_host_key(self):
        result, _, _ = run(WRONG_PASSWORD, 5, PREFLIGHT)
        out = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('пароль', out)
        self.assertIn('PasswordAuthentication', out)
        self.assertNotIn('ssh-keygen', out)
        self.assertNotIn('firewall', out)

    def test_unreachable_node_points_at_network(self):
        result, _, _ = run(REFUSED, 255, PREFLIGHT)
        out = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Connection refused', out)
        self.assertIn('firewall', out)
        self.assertNotIn('ssh-keygen', out)
        self.assertNotIn('PasswordAuthentication', out)

    def test_stage_ssh_stops_on_changed_host_key(self):
        for name in ('ssh_exec true', 'push err'):
            with self.subTest(name):
                result, calls, known = run(CHANGED_KEY, 255, name)
                self.assertEqual(result.returncode, 255, result.stderr)
                self.assertIn(f'ssh-keygen -R 203.0.113.7 -f {known}', result.stderr)
                self.assertNotIn('повтор 1/4', result.stderr)
                self.assertLessEqual(calls, 2, result.stderr)

    def test_stage_ssh_still_retries_network_flap(self):
        result, calls, _ = run(REFUSED, 255, 'ssh_exec true')
        self.assertEqual(result.returncode, 255)
        self.assertIn('повтор 1/4', result.stderr)
        self.assertNotIn('ssh-keygen', result.stderr)
        self.assertGreaterEqual(calls, 5)


if __name__ == '__main__':
    unittest.main()
