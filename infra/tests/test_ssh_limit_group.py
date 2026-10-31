"""После предела SSH_PROBE_TIMEOUT не остаётся ни ssh, ни sshpass, ни их детей.

Подставной ssh висит дольше предела, TERM не слушает и порождает детей с
тем же TERM: один в своей группе, другой отвязался в свою сессию (setsid).
После возврата ssh_probe, ssh_fresh (infra/lib/ssh.sh) и exit_ssh
(99-verify) живых процессов заглушки быть не должно - и с GNU timeout, и с
uutils timeout (coreutils по умолчанию на Ubuntu 26.04: там `timeout -k`
добивает только прямого ребёнка). Нужная реализация timeout стоит первой в PATH.
"""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import test_verify
from test_ssh_limit_remote_code import FAKE_SSH, Node, alive, timeout_binaries
from test_verify_exit_ssh_bashrc import LABEL, MOCK

LIMIT = 2
HANG = 30
# Предел и запас на добивание: вызов не должен ждать, пока дети доспят.
BOUND = LIMIT + 7


class SshLimitGroupTests(unittest.TestCase):
    def assert_nothing_left(self, what, pids, elapsed):
        self.assertGreaterEqual(len(pids), 4, f'{what}: заглушка ssh не записала pid свой и детей')
        self.assertLess(elapsed, BOUND, f'{what}: {elapsed:.1f} с при пределе {LIMIT} с')
        deadline = time.monotonic() + 2
        while alive(pids) and time.monotonic() < deadline:
            time.sleep(0.1)
        live = alive(pids)
        names = [Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0', b' ').decode().strip()
                 for pid in live]
        self.assertEqual(live, [], f'{what}: после предела живы процессы заглушки: {names}')

    def implementations(self):
        found = timeout_binaries()
        for kind in ('GNU', 'uutils'):
            with self.subTest(timeout=kind):
                if kind not in found:
                    self.skipTest(f'{kind} timeout нет на машине')
                yield kind, found[kind]

    def test_1_ssh_probe_and_ssh_fresh_leave_no_processes(self):
        for kind, path in self.implementations():
            for call in (Node.probe, Node.fresh):
                with self.subTest(timeout=kind, call=call.__name__), Node('hang', HANG, path) as node:
                    code, out, err, elapsed = call(node, str(LIMIT))
                    print(f'{kind} {call.__name__}: rc={code} {elapsed:.1f}s {out.strip()!r}')
                    self.assertNotEqual(code, 0, err)
                    if call is Node.probe:
                        self.assertRegex(out, rf'нет ответа за {LIMIT} с')
                    self.assert_nothing_left(f'{kind} {call.__name__}', node.pids(), elapsed)

    def verify(self, path, directory, bashrc, sleep=0):
        root = Path(directory)
        (root / 'node').mkdir(exist_ok=True)
        (root / 'first').mkdir(exist_ok=True)
        if not (root / 'first' / 'timeout').exists():
            (root / 'first' / 'timeout').symlink_to(path)
        fake = root / 'ssh'
        fake.write_text(FAKE_SSH)
        fake.chmod(0o755)
        env = {'FAKE_SSH': str(fake), 'FAKE_BASHRC': bashrc, 'FAKE_SLEEP': str(sleep),
               'FAKE_NODE': str(root / 'node'), 'SSH_PROBE_TIMEOUT': str(LIMIT)}
        started = time.monotonic()
        with mock.patch.object(test_verify, 'MOCK', MOCK), \
             mock.patch.dict(os.environ, {'PATH': f'{root}/first:{os.environ["PATH"]}'}):
            result = test_verify.VerifyTest('run_verify').run_verify(extra_env=env)
        elapsed = time.monotonic() - started
        rows = [r for r in (result.report or {}).get('checks', []) if r['check'] == LABEL]
        self.assertEqual(len(rows), 1, f'нет строки отчёта «{LABEL}»:\n{result.stdout}')
        return rows[0], elapsed

    def test_2_exit_ssh_leaves_no_processes(self):
        for kind, path in self.implementations():
            with self.subTest(timeout=kind), tempfile.TemporaryDirectory(prefix='ssh-limit-verify-') as directory:
                pids = Path(directory) / 'node' / 'pids'
                try:
                    _, baseline = self.verify(path, directory, '')
                    pids.unlink(missing_ok=True)
                    row, elapsed = self.verify(path, directory, 'hang', HANG)
                    print(f'{kind} exit_ssh: {elapsed:.1f}s {row["result"]}: {row["reason"]}')
                    self.assertEqual(row['result'], 'Ошибка')
                    self.assertRegex(row['reason'], rf'предел {LIMIT} с')
                    self.assert_nothing_left(f'{kind} exit_ssh', [int(p) for p in pids.read_text().split()],
                                             elapsed - baseline)
                finally:
                    if pids.exists():
                        for pid in alive(int(p) for p in pids.read_text().split()):
                            os.kill(pid, 9)


if __name__ == '__main__':
    unittest.main()
