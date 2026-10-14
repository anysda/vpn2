"""Отказ во входе не подставляет entry под fail2ban экзита.

fail2ban экзита (00-bootstrap) считает строки `Failed password`: одна попытка
sshpass с неверным паролем - одна строка. Деплой не должен набирать maxretry
за два запуска подряд, а адреса entry стоят в ignoreip.
"""
from pathlib import Path
import re
import tempfile
import unittest

from test_deploy import bash, source
from test_ssh_refusal import PREFLIGHT, WRONG_PASSWORD, run

KEY_ONLY = 'root@203.0.113.7: Permission denied (publickey).\n'
RECORD_SLEEP = 'sleep() { echo "pause $1" >&2; }\n'


def maxretry():
    return int(re.search(r'^maxretry = (\d+)$', source('infra/scripts/00-bootstrap.sh'), re.M)[1])


def fail2ban_block():
    text = source('infra/scripts/00-bootstrap.sh')
    start = text.index('echo "[$HOST_TAG] [5/6] fail2ban"')
    end = text.index('\n', text.index('svc_restart_if_changed fail2ban', start))
    end = text.index('\n', text.index('/etc/fail2ban/jail.d/anysda.local /usr/sbin/nft', end))
    return text[start:end + 1]


def jail(entry_host, ssh_client, resolved=''):
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, 'jail.d').mkdir()
        script = f'''
HOST_TAG=de ENTRY_HOST={entry_host!r} SSH_CLIENT={ssh_client!r}
getent() {{ [[ $1 == ahosts ]] && printf '%b\\n' {resolved!r} | sed '/^$/d;s/$/ STREAM x/'; }}
systemctl() {{ :; }}
svc_restart_if_changed() {{ :; }}
''' + fail2ban_block().replace('/etc/fail2ban/', directory + '/')
        result = bash(script, directory)
        path = Path(directory, 'jail.d/anysda.local')
        return result, path.read_text() if path.exists() else ''


def ignoreip(text):
    found = re.search(r'^ignoreip = (.*)$', text, re.M)
    return found[1].split() if found else []


class SshAuthAttemptsTests(unittest.TestCase):
    def test_preflight_wrong_password_two_runs_stay_below_fail2ban_maxretry(self):
        result, calls, _ = run(WRONG_PASSWORD, 5, RECORD_SLEEP + PREFLIGHT)
        out = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        # Человек видит причину с первой попытки.
        first = next(line for line in out.splitlines() if 'попытка 1/' in line)
        self.assertIn('нода отказала во входе', first)
        self.assertLess(2 * calls, maxretry(), out)

    def test_preflight_waits_for_late_password_once(self):
        # После переустановки ОС пароль выставляется не сразу: один повтор с паузой.
        result, calls, _ = run(WRONG_PASSWORD, 5, RECORD_SLEEP + PREFLIGHT)
        out = result.stdout + result.stderr
        self.assertEqual(calls, 2, out)
        pauses = [int(n) for n in re.findall(r'^pause (\d+)$', out, re.M)]
        self.assertEqual(len(pauses), 1, out)
        self.assertGreaterEqual(pauses[0], 20, out)
        self.assertIn('пароль мог ещё не выставиться', out)

    def test_push_stops_on_wrong_password(self):
        result, calls, _ = run(WRONG_PASSWORD, 5, 'push err')
        self.assertEqual(result.returncode, 5)
        self.assertEqual(calls, 1, result.stderr)
        self.assertIn('нода отказала во входе', result.stderr)
        self.assertNotIn('повтор 1/4', result.stderr)

    def test_stage_ssh_stops_when_key_refused(self):
        for name in ('ssh_exec true', 'push err'):
            with self.subTest(name):
                result, calls, _ = run(KEY_ONLY, 255, name)
                self.assertEqual(result.returncode, 255, result.stderr)
                self.assertIn('нода отказала во входе', result.stderr)
                self.assertNotIn('повтор 1/4', result.stderr)
                self.assertLessEqual(calls, 2, result.stderr)

    def test_exit_fail2ban_ignores_entry_addresses(self):
        result, text = jail('203.0.113.5', '198.51.100.9 50122 22')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('maxretry = 5', text)
        addrs = ignoreip(text)
        for addr in ('127.0.0.1/8', '::1', '203.0.113.5', '198.51.100.9'):
            self.assertIn(addr, addrs, text)

    def test_exit_fail2ban_resolves_entry_name_to_every_address(self):
        result, text = jail('entry.example.net', '2001:db8::5 50122 22',
                            '203.0.113.5\n2001:db8::7\n203.0.113.5')
        self.assertEqual(result.returncode, 0, result.stderr)
        addrs = ignoreip(text)
        for addr in ('203.0.113.5', '2001:db8::7', '2001:db8::5'):
            self.assertIn(addr, addrs, text)
        self.assertEqual(len(addrs), len(set(addrs)), text)
        self.assertNotIn('entry.example.net', addrs)

    def test_exit_fail2ban_skips_garbage(self):
        result, text = jail('entry.example.net', '127.0.0.1 50122 22', 'not-an-ip;rm')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(ignoreip(text), ['127.0.0.1/8', '::1'])


if __name__ == '__main__':
    unittest.main()
