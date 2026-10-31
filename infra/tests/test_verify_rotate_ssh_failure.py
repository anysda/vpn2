"""Сбой входа по ssh в проверке экзитов deploy - это сбой входа на узел, а не мёртвый туннель.

Настоящие verify_and_rotate_ports, _clash_delay, _probe_udp_one_port и ssh_exec
из ssh.sh; подставные только ssh (узлы entry и экзитов) и стадии. Узел по
FAKE_FAIL начинает отказывать во входе с N-го вызова операции и дальше
отказывает на всё: 255 с текстом OpenSSH, 5 как sshpass, 254 - shell узла не
дошёл до команды (вывод без метки).
"""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from test_deploy import ROOT

ENTRY_IP = '203.0.113.10'
EXIT_IPS = {'alpha': '198.51.100.11', 'beta': '198.51.100.12'}
OLD_EXCLUDED = '# прежний прогон\ngamma\n'
FAKE_SSH = r'''#!/usr/bin/env python3
import json, os, re, subprocess, sys
args = sys.argv[1:]
while args and args[0].startswith('-'):
    if args.pop(0) in ('-o', '-i', '-F', '-p', '-l'):
        args.pop(0)
hosts = dict(pair.split('=') for pair in os.environ['FAKE_HOSTS'].split())
node = os.environ['FAKE_NODE']
target = args.pop(0)
host = hosts[target.split('@')[-1]]
cmd = ' '.join(args)
if 'clash-secret.txt' in cmd: op = 'secret'
elif '/delay?' in cmd: op = 'delay'
elif 'nohup python3' in cmd: op = 'echo'
elif 'PING-' in cmd: op = 'probe'
elif 'ufw delete' in cmd: op = 'ufw'
elif cmd.rstrip().endswith('echo ok'): op = 'login'
else: op = 'other'
path = os.path.join(node, 'state.json')
state = json.load(open(path)) if os.path.exists(path) else {'count': {}, 'refused': {}}
key = f'{host}:{op}'
state['count'][key] = state['count'].get(key, 0) + 1
for rule in os.environ.get('FAKE_FAIL', '').split():
    rhost, rop, nth, kind = rule.split(':')
    if (rhost, rop, int(nth)) == (host, op, state['count'][key]):
        state['refused'][host] = kind
json.dump(state, open(path, 'w'))
with open(os.path.join(node, 'calls'), 'a') as calls:
    calls.write(f'ssh {host} {op}\n')
kind = state['refused'].get(host)
if kind == '255':
    sys.stderr.write(f'{target}: Permission denied (publickey,password).\n')
    sys.exit(255)
if kind == '5':
    sys.exit(5)
if kind == '254':
    print('bashrc: exec tmux: not a terminal')
    sys.exit(1)
out = '\nvpn2-ssh-output-begin\n'
code = 0
if op == 'secret':
    out += 'fixture-secret\n'
elif op == 'delay':
    tag = re.search(r'hy2-([a-z0-9-]+)-direct/delay', cmd).group(1)
    mode = dict(p.split(':') for p in os.environ.get('FAKE_TUNNEL', '').split()).get(tag, 'alive')
    if mode == 'rotate':
        mode = 'alive' if f'run_stage_on_host 10-foreign {tag}' in open(os.path.join(node, 'calls')).read() \
            else 'timeout'
    # Команда узла исполняется как есть; curl - подставная функция с ответом туннеля.
    curl = {'alive': "printf '%s' '{\"delay\":42}'", 'timeout': "printf '%s' '{\"message\":\"Timeout\"}'"}.get(
        mode, f'echo "curl: ({mode[4:]}) fixture" >&2; return {mode[4:]}')
    node_run = subprocess.run(['bash', '-c', f'curl() {{ {curl}; }}\n{cmd}'], stdout=subprocess.PIPE)
    sys.stdout.write(node_run.stdout.decode())
    sys.exit(node_run.returncode)
elif op == 'probe':
    ip, port = re.search(r'\(\"([^"]+)\", (\d+)\)', cmd).groups()
    out += f'PONG-PING-{port}' if hosts[ip] not in os.environ.get('FAKE_UDP_DEAD', '').split() \
        else 'TIMEOUT'
sys.stdout.write(out)
sys.exit(code)
'''
FAKE_SSHPASS = '#!/bin/bash\nshift\nexec "$@"\n'
RECORDER = '''import os, sys
with open(os.path.join(os.environ['FAKE_NODE'], 'calls'), 'a') as calls:
    calls.write(' '.join([os.path.basename(sys.argv[0])] + sys.argv[2:]) + '\\n')
'''

LOGIN_WORDS = ('вход', 'login', 'auth')
REASONS = {'255': ('permission denied', 'отказ', '255'),
           '5': ('парол', 'отказ', 'sshpass', 'код 5'),
           '254': ('shell', 'не дошёл', '254')}


class Node:
    """Каталог deploy с entry и экзитами alpha, beta; прогоны на одном каталоге - повтор."""

    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix='verify-rotate-ssh-')
        self.root = Path(self.directory.name) / 'infra'
        self.fake = Path(self.directory.name) / 'node'
        for path in ('envs', 'lib', 'state', 'tmp'):
            (self.root / path).mkdir(parents=True)
        (self.fake / 'bin').mkdir(parents=True)
        (self.root / 'envs/all.env').write_text('ENTRY_HOST=entry.example\n')
        (self.root / 'envs/exits.env').write_text("EXIT_TAGS='alpha beta'\n")
        for tag, ip in (('ru', ENTRY_IP), *EXIT_IPS.items()):
            (self.root / f'envs/{tag}.env').write_text(
                f'HOST_TAG={tag}\nSSH_HOST={ip}\nSSH_USER=root\nSSH_PASS=fixture\n')
        for name in ('ssh.sh', 'anysda-svc.sh'):
            (self.root / 'lib' / name).write_text((ROOT / 'infra/lib' / name).read_text())
        for name in ('config-set-port.py', 'config2env.py'):
            (self.root / 'lib' / name).write_text(RECORDER)
        (self.root / 'state/excluded-exits.txt').write_text(OLD_EXCLUDED)
        (self.root / '.known_hosts').touch()
        for name, text in (('ssh', FAKE_SSH), ('sshpass', FAKE_SSHPASS)):
            (self.fake / 'bin' / name).write_text(text)
            (self.fake / 'bin' / name).chmod(0o755)

    def run(self, fail='', tunnel='', udp_dead=''):
        for name in ('state.json', 'calls'):
            (self.fake / name).unlink(missing_ok=True)
        deploy = (ROOT / 'infra/deploy.sh').read_text()
        deploy = deploy[deploy.index('_STAGE_LOG='):deploy.index('# Без аргументов → полный pipeline.')]
        script = '''set -euo pipefail
DEPLOY_ROOT=$PWD
export DEPLOY_ROOT
source "$DEPLOY_ROOT/lib/ssh.sh"
''' + deploy + r'''
C_R= C_G= C_Y= C_B= C_END=
date() { echo 100; }
sleep() { :; }
run_stage_on_host() { echo "run_stage_on_host $*" >> "$FAKE_NODE/calls"; }
prep_ru_router() { echo prep_ru_router >> "$FAKE_NODE/calls"; }
verify_and_rotate_ports
echo verify-done
'''
        hosts = ' '.join(f'{ip}={tag}' for tag, ip in (('ru', ENTRY_IP), *EXIT_IPS.items()))
        env = {**os.environ, 'PATH': f'{self.fake}/bin:{os.environ["PATH"]}',
               'FAKE_NODE': str(self.fake), 'FAKE_HOSTS': hosts, 'FAKE_FAIL': fail,
               'FAKE_TUNNEL': tunnel, 'FAKE_UDP_DEAD': udp_dead, 'TMPDIR': str(self.root / 'tmp'),
               'SSH_PROBE_TIMEOUT': '5'}
        env.pop('BASH_ENV', None)
        result = subprocess.run(['bash', '-c', script], cwd=self.root, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        calls = self.fake / 'calls'
        result.calls = calls.read_text().splitlines() if calls.exists() else []
        excluded = self.root / 'state/excluded-exits.txt'
        result.excluded = excluded.read_text() if excluded.exists() else None
        return result

    def tree(self):
        return {str(p.relative_to(self.root)): p.read_bytes() if p.is_file() else None
                for p in sorted(self.root.rglob('*'))}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.directory.cleanup()


def stages(result):
    return [call for call in result.calls
            if call.startswith(('config-set-port.py', 'config2env.py', 'run_stage_on_host', 'prep_ru_router'))]


def fatal(output):
    lines = [line for line in output.splitlines() if 'FATAL' in line]
    return lines[-1].lower() if lines else ''


class VerifyRotateSshFailureTests(unittest.TestCase):
    def show(self, title, result):
        print(f'\n--- {title} rc={result.returncode} ---\n{result.stdout}', end='')

    def assert_entry_login_failure(self, result, kind):
        out = result.stdout
        self.assertNotEqual(result.returncode, 0, out)
        self.assertNotIn('verify-done', out)
        self.assertNotIn('все экзиты недостижимы', out)
        self.assertNotIn('МЁРТВ', out)
        # Прежний список исключённых экзитов не стёрт и не переписан.
        self.assertEqual(result.excluded, OLD_EXCLUDED, out)
        last = fatal(out)
        self.assertTrue(last, f'deploy не остановился через die:\n{out}')
        self.assertTrue('entry' in last or ENTRY_IP in last, f'итог не называет entry: {last!r}\n{out}')
        self.assertTrue(any(word in last for word in LOGIN_WORDS), f'итог не про вход: {last!r}\n{out}')
        self.assertTrue(any(word in last for word in REASONS[kind]), f'итог без причины: {last!r}\n{out}')

    def test_entry_refuses_on_secret_and_curl(self):
        """Край 1: entry не пускает на чтении секрета и на curl к clash API."""
        for op in ('secret', 'delay'):
            for kind in REASONS:
                with self.subTest(op=op, kind=kind), Node() as node:
                    result = node.run(fail=f'ru:{op}:1:{kind}')
                    self.show(f'entry {op} {kind}', result)
                    self.assert_entry_login_failure(result, kind)
                    self.assertEqual(stages(result), [], result.stdout)

    def test_entry_refuses_on_recheck_after_rotation(self):
        """Край 1: entry не пускает на повторной проверке после перекатки порта."""
        for kind in REASONS:
            with self.subTest(kind=kind), Node() as node:
                # Первый curl - туннель alpha мёртв, второй (после перекатки) - отказ входа.
                result = node.run(fail=f'ru:delay:2:{kind}', tunnel='alpha:timeout')
                self.show(f'entry recheck {kind}', result)
                self.assert_entry_login_failure(result, kind)
                # Одна перекатка до отказа, ни следующего кандидата, ни отката порта.
                ports = [call for call in result.calls if call.startswith('config-set-port.py')]
                self.assertEqual(ports, ['config-set-port.py alpha 4443'], result.stdout)
                self.assertEqual(sum('10-foreign' in call for call in result.calls), 1, result.stdout)

    def test_exit_refuses_on_echo_start(self):
        """Край 2: экзит не пускает при запуске эха - не мёртв, сбой входа с тегом, beta как обычно."""
        for kind in REASONS:
            with self.subTest(kind=kind), Node() as node:
                result = node.run(fail=f'alpha:echo:1:{kind}', tunnel='alpha:timeout beta:rotate')
                self.show(f'exit echo {kind}', result)
                out = result.stdout
                self.assertNotIn('МЁРТВ', out)
                self.assertNotIn('все экзиты недостижимы', out)
                self.assertFalse(result.excluded and re.search(r'^alpha$', result.excluded, re.M), out)
                lines = [line.lower() for line in out.splitlines() if 'alpha' in line]
                self.assertTrue([line for line in lines if any(w in line for w in LOGIN_WORDS)
                                 and any(w in line for w in REASONS[kind])],
                                f'нет строки о сбое входа на экзит alpha:\n{out}')
                self.assertFalse([call for call in result.calls
                                  if call.startswith('config-set-port.py') and ' alpha ' in call], out)
                # Экзит beta проверен как обычно: перекатка на 4443 и туннель ответил.
                self.assertIn('config-set-port.py beta 4443', result.calls, out)
                self.assertIn('run_stage_on_host 10-foreign beta', result.calls, out)
                self.assertIn('RTT=42ms ✓', out)

    def test_entry_refuses_on_udp_probe(self):
        """Край 2: entry не пускает при отправке UDP-пробы - как край 1."""
        for kind in REASONS:
            with self.subTest(kind=kind), Node() as node:
                # Первый curl на entry проходит (туннель alpha мёртв), отказ - на пробе.
                result = node.run(fail=f'ru:probe:1:{kind}', tunnel='alpha:timeout')
                self.show(f'entry probe {kind}', result)
                self.assert_entry_login_failure(result, kind)
                self.assertEqual(stages(result), [], result.stdout)

    def test_dead_tunnel_with_working_ssh_unchanged(self):
        """Край 3: туннель мёртв при рабочем ssh - прежние «×», пробник, перекатка, исключение."""
        with Node() as node:
            result = node.run(tunnel='alpha:rotate')
            self.show('rotation', result)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn('hy2-alpha-direct port=443   ×', result.stdout)
            self.assertIn('UDP проходит на :33445', result.stdout)
            self.assertIn('config-set-port.py alpha 4443', result.calls)
            self.assertIn('verify-done', result.stdout)
            # Все живы - прежний список исключённых снят.
            self.assertIsNone(result.excluded, result.stdout)
        for mode in ('timeout', 'curl7', 'curl28'):
            with self.subTest(mode=mode), Node() as node:
                result = node.run(tunnel=f'alpha:{mode}', udp_dead='alpha')
                self.show(f'dead alpha {mode}', result)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn('hy2-alpha-direct port=443   ×', result.stdout)
                self.assertIn('все 5 портов фильтруются → exit МЁРТВ', result.stdout)
                self.assertEqual(result.excluded.splitlines()[-1], 'alpha', result.stdout)
                self.assertIn('run_stage_on_host 20-ru-router ru', result.calls)
                self.assertIn('verify-done', result.stdout)

    def test_all_exits_really_dead_still_dies(self):
        """Край 3: все экзиты по-настоящему мертвы - прежний die."""
        with Node() as node:
            result = node.run(tunnel='alpha:timeout beta:curl28', udp_dead='alpha beta')
            self.show('all dead', result)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn('все экзиты недостижимы по udp с ru направления', fatal(result.stdout))
            self.assertEqual(result.excluded.splitlines()[-2:], ['alpha', 'beta'])

    def test_repeat_with_login_failure(self):
        """Край 5: второй прогон с тем же сбоем входа ведёт себя так же, побочных файлов нет."""
        for fail, tunnel in (('ru:secret:1:255', ''), ('alpha:echo:1:5', 'alpha:timeout')):
            with self.subTest(fail=fail), Node() as node:
                before = node.tree()
                first = node.run(fail=fail, tunnel=tunnel)
                after_first = node.tree()
                second = node.run(fail=fail, tunnel=tunnel)
                self.show(f'repeat {fail}', second)
                self.assertEqual(second.stdout, first.stdout)
                self.assertEqual(second.returncode, first.returncode)
                self.assertEqual(second.calls, first.calls)
                self.assertEqual(node.tree(), after_first)
                self.assertEqual(set(after_first) - set(before), set(), 'прогон оставил новые файлы')
                if fail.startswith('ru:'):
                    self.assertEqual(after_first, before, 'сбой входа на entry изменил файлы')
                    self.assert_entry_login_failure(second, '255')
                else:
                    self.assertNotIn('МЁРТВ', second.stdout)


if __name__ == '__main__':
    unittest.main()
