"""Счётчик попыток pre-flight не обещает попыток, которых не будет.

Отказ во входе пробуется дважды, сменившийся host-key - один раз, сетевой
сбой - до PREFLIGHT_ATTEMPTS, пока укладывается в PREFLIGHT_BUDGET. Строка
«попытка N/M» должна называть тот предел, которым цикл на самом деле закончится.
"""
from pathlib import Path
import re
import tempfile
import unittest

from test_deploy import ROOT, bash
from test_ssh_refusal import CHANGED_KEY, PREFLIGHT, REFUSED, WRONG_PASSWORD

KEY_ONLY = 'root@203.0.113.7: Permission denied (publickey).\n'


HANG = None


def run(*cases, env=''):
    """Подставной sshpass: вызов i отвечает cases[i] (последний повторяется).

    Код HANG - нода висит: sshpass молчит дольше SSH_PROBE_TIMEOUT.
    """
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'bin').mkdir()
        for i, (err, rc) in enumerate(cases):
            (root / f'err{i}').write_text(err)
            (root / f'rc{i}').write_text('hang' if rc is HANG else str(rc))
        fake = root / 'bin/sshpass'
        fake.write_text(f'''#!/bin/sh
echo call >> "$CALLS"
n=$(($(wc -l < "$CALLS") - 1)); [ "$n" -lt {len(cases)} ] || n={len(cases) - 1}
cat "$CASES/err$n" >&2
rc=$(cat "$CASES/rc$n")
[ "$rc" != hang ] || {{ sleep 30; exit 255; }}
exit "$rc"
''')
        fake.chmod(0o755)
        script = f'''
export PATH="$PWD/bin:$PATH" CALLS="$PWD/calls" CASES="$PWD"
DEPLOY_ROOT=$PWD
source {ROOT}/infra/lib/ssh.sh
# Паузы не ждём, но часы цикла они двигают, как настоящие.
sleep() {{ SECONDS=$((SECONDS + $1)); }}
{env}
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

    def test_hanging_node_counts_to_what_fits_in_budget(self):
        # Каждая попытка съедает весь SSH_PROBE_TIMEOUT: в бюджет 10 с
        # влезают две, третьей не будет - строка не должна её обещать.
        out, calls, counters = run(('', HANG),
                                   env='SSH_PROBE_TIMEOUT=2 PREFLIGHT_BUDGET=10')
        self.assertEqual(calls, 2, out)
        self.assertEqual(counters, [(1, 2), (2, 2)], out)
        self.assertNotIn('/5', out)
        self.assertIn('время pre-flight на ноду вышло (10 с)', out)

    def test_fast_refusal_is_not_told_about_budget(self):
        out, _, _ = run((REFUSED, 255))
        self.assertNotIn('время pre-flight', out)


if __name__ == '__main__':
    unittest.main()
