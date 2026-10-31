"""ssh_probe и ssh_fresh: код 124/137 с узла - отказ узла, а не срабатывание предела.

ssh отдаёт код команды на узле. Мгновенный exit 124 или exit 137 в .bashrc
root (до метки _ssh_begin) - тот же exit в .bashrc, что exit 1: «shell узла
не дошёл до команды», $_SSH_NO_MARK. Тот же код из команды (после метки) -
код команды, как exit 1 оттуда. «Нет ответа за N с» - только если предел
SSH_PROBE_TIMEOUT действительно вышел. Для exit_ssh в 99-verify это уже так
(test_verify_exit_ssh_remote_code). Подставной ssh - sshd с login shell:
сначала .bashrc узла (режим из FAKE_BASHRC), затем команда через bash.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

from test_deploy import ROOT

FAKE_SSH = r'''#!/bin/bash
while (($#)); do
  case $1 in -[oiFpl]) shift 2 ;; -*) shift ;; *) shift; break ;; esac
done
cmd="$*"
echo $$ >> "$FAKE_NODE/calls"
echo $$ >> "$FAKE_NODE/pids"
noise() { printf 'Welcome to node\nmotd line\n'; printf 'bashrc warning\n' >&2; }
quiet() { "$@" </dev/null >/dev/null 2>&1 & echo $! >> "$FAKE_NODE/pids"; }
case ${FAKE_BASHRC:-} in
  before*) noise; exit "${FAKE_BASHRC#before}" ;;
  after*)  bash -c "$cmd"; exit "${FAKE_BASHRC#after}" ;;
  slow)    noise; sleep "$FAKE_SLEEP" ;;
  # Висит дольше предела и TERM не слушает; дети тоже: один в группе
  # заглушки, другой отвязался в свою сессию (setsid).
  hang)    trap '' TERM
           noise
           quiet bash -c "trap '' TERM; exec sleep $FAKE_SLEEP"
           quiet setsid bash -c "trap '' TERM; exec sleep $FAKE_SLEEP"
           quiet sleep "$FAKE_SLEEP"
           wait ;;
esac
bash -c "$cmd"
'''
FAKE_SSHPASS = '#!/bin/bash\nshift 2\nexec ssh "$@"\n'
NO_MARK = r'не дош[её]л|не выполн'
LIMIT_REASON = r'(?i)нет ответа за|предел|тайм-?аут|timeout|не ответил|завис'
LIMIT = 3


def timeout_binaries():
    """{'GNU': путь, 'uutils': путь} - те реализации timeout, что есть на машине."""
    found = {}
    candidates = [shutil.which('gnutimeout'), shutil.which('timeout'), '/usr/lib/cargo/bin/coreutils/timeout',
                  shutil.which('uu-timeout'), '/usr/lib/cargo/bin/timeout']
    for path in candidates:
        if not path or not os.access(path, os.X_OK):
            continue
        version = subprocess.run([path, '--version'], text=True, capture_output=True).stdout
        kind = 'uutils' if 'uutils' in version else 'GNU' if 'GNU' in version else None
        if kind:
            found.setdefault(kind, path)
    return found


class Node:
    """Подставной узел: ssh и sshpass в PATH, при желании свой timeout первым."""

    def __init__(self, bashrc, sleep=0, timeout=None):
        self.directory = tempfile.TemporaryDirectory(prefix='ssh-limit-')
        self.root = Path(self.directory.name)
        for sub in ('bin', 'node', 'first'):
            (self.root / sub).mkdir()
        for name, text in (('ssh', FAKE_SSH), ('sshpass', FAKE_SSHPASS)):
            (self.root / 'bin' / name).write_text(text)
            (self.root / 'bin' / name).chmod(0o755)
        if timeout:
            (self.root / 'first' / 'timeout').symlink_to(timeout)
        self.env = {k: v for k, v in os.environ.items() if k != 'SSH_PROBE_TIMEOUT'}
        self.env.update({'PATH': f'{self.root}/first:{self.root}/bin:{os.environ["PATH"]}',
                         'LC_ALL': 'C.UTF-8', 'FAKE_NODE': str(self.root / 'node'),
                         'FAKE_BASHRC': bashrc, 'FAKE_SLEEP': str(sleep)})

    def run(self, body, limit=None):
        """(код, stdout, stderr, секунды) сценария bash с lib/ssh.sh; limit=None - переменной нет."""
        env = dict(self.env)
        if limit is not None:
            env['SSH_PROBE_TIMEOUT'] = limit
        script = (f'DEPLOY_ROOT={ROOT}/infra\nsource {ROOT}/infra/lib/ssh.sh\n'
                  'SSH_USER=root SSH_HOST=node.example SSH_PASS=fixture\n' + body)
        started = time.monotonic()
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=self.root, env=env,
                                text=True, capture_output=True, timeout=60)
        return result.returncode, result.stdout, result.stderr, time.monotonic() - started

    def probe(self, limit=str(LIMIT)):
        return self.run('ssh_probe\n', limit)

    def fresh(self, limit=str(LIMIT)):
        return self.run('ssh_fresh echo ok\n', limit)

    def calls(self):
        path = self.root / 'node' / 'calls'
        return len(path.read_text().splitlines()) if path.exists() else 0

    def pids(self):
        path = self.root / 'node' / 'pids'
        return [int(p) for p in path.read_text().split()] if path.exists() else []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        for pid in alive(self.pids()):
            os.kill(pid, 9)
        self.directory.cleanup()


def alive(pids):
    live = []
    for pid in pids:
        try:
            state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            continue
        if state != 'Z':
            live.append(pid)
    return live


class SshLimitRemoteCodeTests(unittest.TestCase):
    def outcome(self, call, bashrc):
        with Node(bashrc) as node:
            code, out, err, elapsed = call(node)
            print(f'{call.__name__} {bashrc}: rc={code} {elapsed:.1f}s out={out.strip()!r}')
            self.assertEqual(node.calls(), 1, f'{bashrc}: ssh вызван не один раз')
        return code, out, err, elapsed

    def assert_like(self, call, bashrc, reference):
        code, out, err, elapsed = self.outcome(call, bashrc)
        ref_code, ref_out, _, _ = self.outcome(call, reference)
        self.assertNotRegex(out + err, LIMIT_REASON,
                            f'{call.__name__}: мгновенный код {bashrc} с узла назван пределом времени')
        self.assertLess(elapsed, LIMIT - 1, f'{call.__name__} ждал предел при мгновенном ответе узла')
        self.assertEqual(code, ref_code, f'{call.__name__} {bashrc}: код не как у {reference}: {out + err}')
        return out, err, ref_out

    def test_1_bashrc_exit_124_137_is_bashrc_exit(self):
        for rc in (124, 137):
            for call in (Node.probe, Node.fresh):
                with self.subTest(call=call.__name__, rc=rc):
                    out, err, ref_out = self.assert_like(call, f'before{rc}', 'before1')
                    self.assertRegex(out + err, NO_MARK, 'причина не про .bashrc')
                    self.assertEqual(out, ref_out)

    def test_2_command_exit_124_137_is_remote_code(self):
        for rc in (124, 137):
            with self.subTest(call='ssh_probe', rc=rc):
                out, _, _ = self.assert_like(Node.probe, f'after{rc}', 'after1')
                self.assertRegex(out, rf'(?<!\d){rc}(?!\d)', 'ssh_probe не назвал код узла')
                self.assertNotRegex(out, NO_MARK)
            with self.subTest(call='ssh_fresh', rc=rc):
                code, out, err, elapsed = self.outcome(Node.fresh, f'after{rc}')
                self.assertEqual((code, out), (rc, 'ok\n'), err)
                self.assertNotRegex(err, LIMIT_REASON + '|' + NO_MARK)
                self.assertLess(elapsed, LIMIT - 1)

    def test_3_real_hang_is_still_the_limit(self):
        with Node('slow', sleep=LIMIT + 3) as node:
            code, out, _, elapsed = node.probe()
            self.assertEqual(code, 1, out)
            self.assertRegex(out, rf'нет ответа за {LIMIT} с', out)
            self.assertLess(elapsed, LIMIT + 2.5)
        with Node('slow', sleep=LIMIT + 3) as node:
            code, _, err, _ = node.fresh()
            self.assertIn(code, (124, 137), err)
            self.assertNotRegex(err, NO_MARK)


if __name__ == '__main__':
    unittest.main()
