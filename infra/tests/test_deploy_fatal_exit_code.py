"""FATAL deploy после обрыва ssh на entry кончается кодом 1 и всеми строками итога.

Полный deploy держит замок на entry открытым ssh (coproc в release_start), и
ловушка EXIT пишет в него строку отпуска. Фильтр на entry сбрасывает и это
соединение: замок гибнет раньше, чем deploy дойдёт до die. Настоящие
release_start, release_unlock, verify_and_rotate_ports и ssh_exec; подставной
только ssh. Узел с FAKE_RESET сбрасывает вход на N-м вызове операции и дальше
отказывает на всё, вместе с открытым соединением замка.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_deploy import ROOT

ENTRY_IP = '203.0.113.10'
EXIT_IP = '198.51.100.11'
FAKE_SSH = r'''#!/usr/bin/env python3
import os, signal, sys, time
args = sys.argv[1:]
while args and args[0].startswith('-'):
    if args.pop(0) in ('-o', '-i', '-F', '-p', '-l'):
        args.pop(0)
target = args.pop(0)
cmd = ' '.join(args)
node = os.environ['FAKE_NODE']
if 'flock' in cmd: op = 'lock'
elif 'clash-secret.txt' in cmd: op = 'secret'
elif '/delay?' in cmd: op = 'delay'
else: op = 'other'
# Вызовы считаются после release_start: замок взят, выпуск открыт.
n = 0
if os.path.exists(os.path.join(node, 'coproc.pid')):
    count = os.path.join(node, f'count-{op}')
    n = int(open(count).read()) + 1 if os.path.exists(count) else 1
    open(count, 'w').write(str(n))
refused = os.path.join(node, 'refused')
if os.environ.get('FAKE_RESET') == f'{op}:{n}' and not os.path.exists(refused):
    open(refused, 'w').close()
    # Сброс по порту 22 рвёт и живое соединение замка: оно и его ssh_exec
    # на entry гибнут до того, как deploy узнает об отказе.
    if os.environ.get('FAKE_RESET_LOCK') == '1':
        os.killpg(int(open(os.path.join(node, 'lock.pgid')).read()), signal.SIGKILL)
        coproc = open(os.path.join(node, 'coproc.pid')).read().strip()
        for _ in range(400):
            try:
                if open(f'/proc/{coproc}/stat').read().split(') ')[1][0] == 'Z':
                    break
            except FileNotFoundError:
                break
            time.sleep(0.05)
if os.path.exists(refused):
    sys.stderr.write('kex_exchange_identification: read: Connection reset by peer\n')
    sys.exit(255)
if op == 'lock':
    os.setpgrp()
    open(os.path.join(node, 'lock.pgid'), 'w').write(str(os.getpgrp()))
    cmd = cmd.replace('/var/lib/vpn2-deploy', os.path.join(node, 'lock'))
    os.execvp('bash', ['bash', '-c', cmd])
sys.stdout.write('\nvpn2-ssh-output-begin\n')
if op == 'secret':
    sys.stdout.write('fixture-secret\n')
elif op == 'delay':
    sys.stdout.write('{"delay":42}')
'''
FAKE_SSHPASS = '#!/bin/bash\nshift\nexec "$@"\n'
JOURNAL = 'deploy interrupted: operation journal retained'


class Deploy:
    """Каталог deploy с entry и одним экзитом alpha."""

    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix='deploy-fatal-exit-')
        self.root = Path(self.directory.name) / 'infra'
        self.fake = Path(self.directory.name) / 'node'
        for path in ('envs', 'lib', 'state', 'tmp'):
            (self.root / path).mkdir(parents=True)
        for path in ('bin', 'lock'):
            (self.fake / path).mkdir(parents=True)
        (self.root / 'envs/all.env').write_text('ENTRY_HOST=entry.example\n')
        (self.root / 'envs/exits.env').write_text("EXIT_TAGS='alpha'\n")
        for tag, ip in (('ru', ENTRY_IP), ('alpha', EXIT_IP)):
            (self.root / f'envs/{tag}.env').write_text(
                f'HOST_TAG={tag}\nSSH_HOST={ip}\nSSH_USER=root\nSSH_PASS=fixture\n')
        for name in ('ssh.sh', 'anysda-svc.sh', 'deploy-release.sh', 'release.py', 'deploy_state.py',
                     'panel-transaction.sh'):
            (self.root / 'lib' / name).write_text((ROOT / 'infra/lib' / name).read_text())
        (self.root / '.known_hosts').touch()
        for name, text in (('ssh', FAKE_SSH), ('sshpass', FAKE_SSHPASS)):
            (self.fake / 'bin' / name).write_text(text)
            (self.fake / 'bin' / name).chmod(0o755)

    def run(self, body, reset='', reset_lock=True):
        deploy = (ROOT / 'infra/deploy.sh').read_text()
        deploy = deploy[deploy.index('_STAGE_LOG='):deploy.index('# Без аргументов → полный pipeline.')]
        script = '''set -euo pipefail
DEPLOY_ROOT=$PWD
export DEPLOY_ROOT
source "$DEPLOY_ROOT/lib/ssh.sh"
''' + deploy + r'''
C_R= C_G= C_Y= C_B= C_END=
install_prereqs() { :; }
sleep() { :; }
source "$DEPLOY_ROOT/lib/deploy-release.sh"
VPN2_DEVELOPMENT=true
release_start
echo "$VPN2_LOCK_PID" > "$FAKE_NODE/coproc.pid"
''' + body
        env = {**os.environ, 'PATH': f'{self.fake}/bin:{os.environ["PATH"]}',
               'FAKE_NODE': str(self.fake), 'FAKE_RESET': reset,
               'FAKE_RESET_LOCK': '1' if reset_lock else '0',
               'TMPDIR': str(self.root / 'tmp'), 'SSH_PROBE_TIMEOUT': '5'}
        env.pop('BASH_ENV', None)
        return subprocess.run(['bash', '-c', script], cwd=self.root, env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.directory.cleanup()


class DeployFatalExitCodeTests(unittest.TestCase):
    def show(self, title, result):
        print(f'\n--- {title} rc={result.returncode} ---\n{result.stdout}', end='')

    def assert_fatal_summary(self, result, words):
        out = result.stdout
        self.assertEqual(result.returncode, 1, f'код выхода deploy после FATAL:\n{out}')
        lines = out.splitlines()
        fatal = [i for i, line in enumerate(lines) if 'FATAL' in line]
        self.assertTrue(fatal, f'нет строки FATAL:\n{out}')
        for word in words:
            self.assertIn(word, lines[fatal[-1]], out)
        # Строки итога после FATAL дошли до оператора.
        self.assertTrue(any(JOURNAL in line for line in lines[fatal[-1] + 1:]),
                        f'после FATAL нет строки итога {JOURNAL!r}:\n{out}')
        self.assertNotIn('verify-done', out)

    def test_entry_reset_during_verify_exits_1_with_summary(self):
        """Карточка: entry сбрасывает ssh во время «verify: проверяю tunnels»."""
        for op in ('secret:1', 'delay:1'):
            with self.subTest(reset=op), Deploy() as deploy:
                result = deploy.run('verify_and_rotate_ports\necho verify-done\n', reset=op)
                self.show(f'verify reset {op}', result)
                self.assert_fatal_summary(result, ('verify_and_rotate_ports', 'entry', ENTRY_IP))

    def test_any_fatal_after_lost_lock_exits_1_with_summary(self):
        """Общий путь: любой die после гибели соединения замка, не только verify."""
        with Deploy() as deploy:
            result = deploy.run('''load_env ru
ssh_exec true || true
die "стадия упала"
''', reset='other:1')
            self.show('die после потери замка', result)
            self.assert_fatal_summary(result, ('стадия упала',))

    def test_fatal_with_live_lock_still_exits_1(self):
        """Прежнее поведение: замок жив, entry отказывает только новым входам."""
        with Deploy() as deploy:
            result = deploy.run('verify_and_rotate_ports\necho verify-done\n', reset='delay:1',
                                reset_lock=False)
            self.show('verify, замок жив', result)
            self.assert_fatal_summary(result, ('verify_and_rotate_ports', 'entry'))


if __name__ == '__main__':
    unittest.main()
