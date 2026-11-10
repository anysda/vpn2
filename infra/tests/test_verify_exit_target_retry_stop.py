"""Повтор цели эха на новом порту не оставляет на экзите цель старого порта.

Подставной ssh в PATH ведёт себя как настоящий sshd без pty: цель
verify-target.py идёт отдельным процессом в своей сессии, и kill ssh-клиента
её не гасит. Остановка (pkill по имени цели) гасит цели своего экзита и
пишется в журнал остановок.

Попытка 1: на us порт прогона занят, de поднялся. Попытка 2 на новом порту:
us поднялся, de отказал во входе. После stop() ни одной живой цели, цель de
старого порта не слушает, каждый экзит получил ровно одну остановку.
"""
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / 'infra/vpn2/src/vpn2/verify/traffic.py'


def load():
    spec = importlib.util.spec_from_file_location('verify_traffic', MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vt = load()
WAN = '127.0.0.1'
ADDRESS = {'us': '127.0.0.3', 'de': '127.0.0.4'}
EXITS = list(ADDRESS)
FAKE_SSH = r'''#!/usr/bin/env python3
import os, re, signal, subprocess, sys, time
args = sys.argv[1:]
while args and args[0].startswith('-'):
    if args.pop(0) in ('-o', '-i', '-F', '-p', '-l'):
        args.pop(0)
tag, command = args[0], ' '.join(args[1:])
node = os.environ['FAKE_NODE']
if 'pkill' in command:
    with open(os.path.join(node, 'stops'), 'a') as stops:
        stops.write(tag + '\n')
    path = os.path.join(node, 'pids')
    rows = [line.split() for line in open(path).read().splitlines()] if os.path.exists(path) else []
    for row_tag, pid, _port in rows:
        if row_tag == tag:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
    sys.exit(0)
port = re.search(r'--port (\d+)', command).group(1)
lifetime = re.search(r'--lifetime (\d+)', command).group(1)
tries = os.path.join(node, 'tries-' + tag)
with open(tries, 'a') as f:
    f.write(port + '\n')
if tag == 'de' and len(open(tries).read().split()) > 1:
    sys.stderr.write('Permission denied (publickey).\n')
    sys.exit(255)
script = os.path.join(node, f'target-{tag}-{port}.py')
with open(script, 'w') as f:
    f.write(sys.stdin.read())
# Как sshd без pty: цель в своей сессии, смерть клиента ssh её не трогает.
target = subprocess.Popen([sys.executable, '-I', script, '--listen', {'us': '127.0.0.3', 'de': '127.0.0.4'}[tag],
                           '--port', port, '--lifetime', lifetime],
                          stdin=subprocess.DEVNULL, start_new_session=True)
with open(os.path.join(node, 'pids'), 'a') as pids:
    pids.write(f'{tag} {target.pid} {port}\n')
sys.exit(target.wait())
'''
# Ведущий поток выходит один (SYS_exit), второй поток держит слушающий сокет процесса.
LEADER_EXITS = r'''
import ctypes, os, socket, sys, threading, time
sock = socket.socket()
sock.bind((sys.argv[1], 0))
sock.listen()
threading.Thread(target=time.sleep, args=(60,)).start()
print(sock.getsockname()[1], flush=True)
ctypes.CDLL(None).syscall({'x86_64': 60, 'aarch64': 93}[os.uname().machine], 0)
'''


def alive(pid):
    # Ведущий поток в Z, пока другие потоки ещё выходят: дескрипторы и порт процесса открыты.
    try:
        tasks = list(Path(f'/proc/{pid}/task').iterdir())
    except OSError:
        return False
    for task in tasks:
        try:
            state = (task / 'stat').read_text().rsplit(')', 1)[1].split()[0]
        except OSError:
            continue
        if state != 'Z':
            return True
    return False


def listening(address, port):
    try:
        socket.create_connection((address, port), timeout=1).close()
    except OSError:
        return False
    return True


class Router:
    outbounds = {}

    @staticmethod
    def exit_ip(tag):
        return '192.0.2.' + str(EXITS.index(tag) + 1)


class ExitTargetRetryStopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='verify-exit-retry-stop-'))
        self.addCleanup(self.reap)
        (self.tmp / 'bin').mkdir()
        (self.tmp / 'bin/ssh').write_text(FAKE_SSH)
        (self.tmp / 'bin/ssh').chmod(0o755)
        self.targets = None
        real_run = vt.run

        def run(*args, **kwargs):
            # Наружу идёт только ssh к экзитам; маршруты и iptables entry не нужны.
            return real_run(*args, **kwargs) if args[0] == 'ssh' else None

        saved = {name: getattr(vt, name) for name in ('run', 'wan_device')}
        saved_timeout = vars(vt.Targets).get('READY_TIMEOUT')
        vt.run, vt.wan_device = run, lambda: None
        vt.Targets.READY_TIMEOUT = 5
        saved_path = os.environ['PATH']
        os.environ['PATH'] = f'{self.tmp}/bin:{saved_path}'
        os.environ['FAKE_NODE'] = str(self.tmp)

        def restore():
            for name, value in saved.items():
                setattr(vt, name, value)
            vt.Targets.READY_TIMEOUT = saved_timeout
            os.environ['PATH'] = saved_path
            os.environ.pop('FAKE_NODE', None)

        self.addCleanup(restore)

    def occupy(self, address, port):
        sock = socket.socket()
        self.addCleanup(sock.close)
        sock.bind((address, port))
        sock.listen()

    def pids(self):
        path = self.tmp / 'pids'
        rows = [line.split() for line in path.read_text().splitlines()] if path.exists() else []
        return [(tag, int(pid), int(port)) for tag, pid, port in rows]

    def stops(self):
        path = self.tmp / 'stops'
        return sorted(path.read_text().split()) if path.exists() else []

    def reap(self):
        targets = self.targets
        for process in [targets.local, targets.echo, *targets.remote.values()] if targets else []:
            if process and process.poll() is None:
                process.kill()
                process.wait()
        for _tag, pid, _port in self.pids():
            if alive(pid):
                os.kill(pid, 9)

    def test_stop_reaches_exit_started_in_earlier_attempt(self):
        self.targets = targets = vt.Targets(WAN, EXITS, Router())
        first = targets.port
        self.occupy(ADDRESS['us'], first)
        targets.start(900)
        second = targets.port
        self.assertNotEqual(first, second)
        self.assertEqual(sorted(targets.remote), ['us'], targets.failed)
        self.assertIn('Permission denied', targets.failed.get('de', ''), targets.failed)
        started = {(tag, port) for tag, _pid, port in self.pids()}
        self.assertEqual(started, {('us', first), ('de', first), ('us', second)}, self.pids())
        self.assertTrue(listening(ADDRESS['de'], first), 'цель de первой попытки не поднялась')

        targets.stop()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(alive(pid) for _tag, pid, _port in self.pids()):
            time.sleep(0.1)
        self.assertEqual([(tag, port) for tag, pid, port in self.pids() if alive(pid)], [],
                         'цель на экзите пережила остановку')
        self.assertFalse(listening(ADDRESS['de'], first), f'de слушает старый порт {first}')
        self.assertEqual(self.stops(), sorted(EXITS), 'одна остановка на каждый экзит, где была цель')

    @unittest.skipUnless(os.uname().machine in ('x86_64', 'aarch64'), 'номер SYS_exit')
    def test_leader_zombie_with_live_thread_is_alive(self):
        # Цель многопоточная: при SIGTERM ведущий поток бывает в Z, пока поток таймера ещё держит порт.
        process = subprocess.Popen([sys.executable, '-I', '-c', LEADER_EXITS, ADDRESS['de']],
                                   stdout=subprocess.PIPE, text=True)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        port = int(process.stdout.readline())
        process.stdout.close()
        stat = Path(f'/proc/{process.pid}/stat')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and stat.read_text().rsplit(')', 1)[1].split()[0] != 'Z':
            time.sleep(0.05)
        self.assertEqual(stat.read_text().rsplit(')', 1)[1].split()[0], 'Z', 'ведущий поток не вышел')
        self.assertTrue(listening(ADDRESS['de'], port), 'порт закрылся вместе с ведущим потоком')
        self.assertTrue(alive(process.pid), 'процесс с живым потоком держит порт, а alive() считает его мёртвым')


if __name__ == '__main__':
    unittest.main()
