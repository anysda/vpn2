"""Счётчик попыток pre-flight не обещает попыток, которых не будет.

Отказ во входе пробуется дважды, сменившийся host-key - один раз, сетевой
сбой - до PREFLIGHT_ATTEMPTS. Строка «попытка N/M» должна называть тот
предел, которым цикл на самом деле закончится.
"""
from pathlib import Path
import re
import tempfile
import unittest

from test_deploy import ROOT, bash
from test_ssh_refusal import CHANGED_KEY, PREFLIGHT, REFUSED, WRONG_PASSWORD

KEY_ONLY = 'root@203.0.113.7: Permission denied (publickey).\n'


def run(*cases):
    """Подставной sshpass: вызов i отвечает cases[i] (последний повторяется)."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'bin').mkdir()
        for i, (err, rc) in enumerate(cases):
            (root / f'err{i}').write_text(err)
            (root / f'rc{i}').write_text(str(rc))
        fake = root / 'bin/sshpass'
        fake.write_text(f'''#!/bin/sh
echo call >> "$CALLS"
n=$(($(wc -l < "$CALLS") - 1)); [ "$n" -lt {len(cases)} ] || n={len(cases) - 1}
cat "$CASES/err$n" >&2
exit "$(cat "$CASES/rc$n")"
''')
        fake.chmod(0o755)
        script = f'''
export PATH="$PWD/bin:$PATH" CALLS="$PWD/calls" CASES="$PWD"
DEPLOY_ROOT=$PWD
source {ROOT}/infra/lib/ssh.sh
sleep() {{ :; }}
SSH_USER=root SSH_HOST=203.0.113.7 SSH_PASS=secret
{PREFLIGHT}
'''
        result = bash(script, directory)
        out = result.stdout + result.stderr
        calls = (root / 'calls').read_text().count('call') if (root / 'calls').exists() else 0
        return out, calls, [(int(a), int(b)) for a, b in re.findall(r'попытка (\d+)/(\d+)', out)]


class PreflightAttemptsTests(unittest.TestCase):
    def assertHonest(self, out, calls, counters):
        self.assertEqual(len(counters), calls, out)
        self.assertEqual([n for n, _ in counters], list(range(1, calls + 1)), out)
        # Последняя строка называет ровно ту попытку, на которой цикл встал.
        self.assertEqual(counters[-1][0], counters[-1][1], out)
        for n, limit in counters:
            self.assertLessEqual(n, limit, out)

    def test_auth_refusal_counts_to_two(self):
        for err, rc in ((WRONG_PASSWORD, 5), (KEY_ONLY, 255)):
            with self.subTest(err):
                out, calls, counters = run((err, rc))
                self.assertEqual(calls, 2, out)
                self.assertEqual(counters, [(1, 2), (2, 2)], out)
                self.assertNotIn('/5', out)

    def test_changed_host_key_promises_no_retry(self):
        out, calls, counters = run((CHANGED_KEY, 255))
        self.assertEqual(calls, 1, out)
        self.assertEqual(counters, [(1, 1)], out)

    def test_network_failure_keeps_five(self):
        out, calls, counters = run((REFUSED, 255))
        self.assertEqual(calls, 5, out)
        self.assertEqual(counters, [(n, 5) for n in range(1, 6)], out)

    def test_network_then_auth_refusal_ends_where_it_says(self):
        out, calls, counters = run((REFUSED, 255), (WRONG_PASSWORD, 5))
        self.assertEqual(calls, 3, out)
        self.assertEqual(counters, [(1, 5), (2, 3), (3, 3)], out)
        self.assertHonest(out, calls, counters)

    def test_auth_then_network_then_auth(self):
        out, calls, counters = run((WRONG_PASSWORD, 5), (REFUSED, 255), (WRONG_PASSWORD, 5))
        self.assertEqual(calls, 3, out)
        self.assertHonest(out, calls, counters)

    def test_network_then_changed_host_key(self):
        out, calls, counters = run((REFUSED, 255), (CHANGED_KEY, 255))
        self.assertEqual(calls, 2, out)
        self.assertHonest(out, calls, counters)


if __name__ == '__main__':
    unittest.main()
