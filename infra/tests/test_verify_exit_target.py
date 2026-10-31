"""Цель эха на экзите, не поднявшаяся, - строка с тегом, кодом и причиной, а не «нет эха (ssh)».

Подставной ssh в PATH: экзит up поднимает цель («ready» и ждёт), denied
отказывает во входе (255), crash - цель вышла кодом 1, silent молчит без
«ready». Настоящие Targets и Verifier.paths, пробы в netns подменены.
"""
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import time
import unittest
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / 'infra/lib/verify-traffic.py'


def load():
    spec = importlib.util.spec_from_file_location('verify_traffic', MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vt = load()
WAN, ECHO = '127.0.0.1', '127.0.0.2'
EXITS = ['up', 'denied', 'crash', 'silent']
STDERR = {'denied': 'root@denied: Permission denied (publickey,password).',
          'crash': 'OSError: [Errno 98] Address already in use',
          'silent': "Warning: Permanently added 'silent' (ED25519) to the list of known hosts."}
FAKE_SSH = r'''#!/usr/bin/env python3
import os, sys, time
args = sys.argv[1:]
while args and args[0].startswith('-'):
    if args.pop(0) in ('-o', '-i', '-F', '-p', '-l'):
        args.pop(0)
tag = args[0]
node = os.environ['FAKE_NODE']
with open(os.path.join(node, 'pids'), 'a') as pids:
    pids.write(f'{tag} {os.getpid()}\n')
sys.stdin.read()
if tag == 'denied':
    sys.stderr.write('root@denied: Permission denied (publickey,password).\n')
    sys.exit(255)
if tag == 'crash':
    sys.stderr.write('Traceback (most recent call last):\n  File "<stdin>", line 9\n'
                     'OSError: [Errno 98] Address already in use\n')
    sys.exit(1)
if tag == 'silent':
    sys.stderr.write("Warning: Permanently added 'silent' (ED25519) to the list of known hosts.\n")
    sys.stderr.flush()
    time.sleep(300)
    sys.exit(0)
sys.stdout.write('\nready\n')
sys.stdout.flush()
time.sleep(300)
'''


class Stub:
    def __init__(self, *_args, **_kwargs):
        self.outbounds = {vt.DIRECT: {'bind_interface': 'lo'}}

    def up(self):
        pass

    def cleanup(self):
        pass

    def stop(self):
        pass

    @staticmethod
    def health(_port):
        return True

    @staticmethod
    def exit_ip(tag):
        return '192.0.2.' + str(EXITS.index(tag) + 1)


class ExitTargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='verify-exit-target-'))
        self.addCleanup(self.reap)
        (self.tmp / 'bin').mkdir()
        (self.tmp / 'bin/ssh').write_text(FAKE_SSH)
        (self.tmp / 'bin/ssh').chmod(0o755)
        (self.tmp / 'scratch').mkdir()
        self.calls = []
        self.targets = []
        test = self

        class Targets(vt.Targets):
            def __init__(self, *args):
                super().__init__(*args)
                test.targets.append(self)

        def protocol(verifier, protocol):
            # Настоящий разбор маршрутов через экзиты - один раз; пробы в netns подменены.
            if protocol.name == 'WireGuard':
                vt.Verifier.paths(verifier, protocol)

        def probe(verifier, _protocol, kind, args, _dest=None, **_kwargs):
            self.calls.append(('проба', kind, args[0]))
            tag = next((t for t in EXITS if verifier.markers.get(t) in args), None)
            chain = f'hy2-{tag}-direct'
            return {'local': True}, [{'chains': [chain], 'rule': f'manual-pin-{chain}', 'payload': ''}]

        stubs = {
            'detect_wan': lambda: WAN, 'ECHO_IP': ECHO, 'WORK': self.tmp / 'work', 'Router': Stub,
            'Namespace': Stub, 'Relay': Stub, 'WireGuard': type('WireGuard', (Stub,), {'name': 'WireGuard'}),
            'OpenVPN': type('OpenVPN', (Stub,), {'name': 'OpenVPN'}),
            'IKEv2': type('IKEv2', (Stub,), {'name': 'IKEv2'}), 'Targets': Targets,
            'cleanup': lambda *_: None, 'leftovers': lambda: [],
            'manual_markers': lambda exits, **__: {t: f'm-{t}' for t in exits}, 'INTERNET': (),
            'probe_dns': lambda *_: None, 'wg_apply_check': lambda _report: None,
            'run': lambda *args, **_: self.calls.append(('run',) + args),
        }
        methods = {'protocol': protocol, 'probe': probe, 'direct_target': lambda _self: False,
                   'direct_missing': lambda _self: 'подменено', 'egress_check': lambda *_: None,
                   'internet_check': lambda *_: None}
        saved = {name: getattr(vt, name) for name in stubs}
        saved_methods = {name: getattr(vt.Verifier, name) for name in methods}
        saved_timeout = vars(vt.Targets).get('READY_TIMEOUT')
        for name, value in stubs.items():
            setattr(vt, name, value)
        for name, value in methods.items():
            setattr(vt.Verifier, name, value)
        vt.Targets.READY_TIMEOUT = 2
        saved_path, saved_tempdir = os.environ['PATH'], tempfile.tempdir
        os.environ['PATH'] = f'{self.tmp}/bin:{saved_path}'
        os.environ['FAKE_NODE'] = str(self.tmp)
        tempfile.tempdir = str(self.tmp / 'scratch')

        def restore():
            for name, value in saved.items():
                setattr(vt, name, value)
            for name, value in saved_methods.items():
                setattr(vt.Verifier, name, value)
            vt.Targets.READY_TIMEOUT = saved_timeout
            os.environ['PATH'] = saved_path
            os.environ.pop('FAKE_NODE', None)
            tempfile.tempdir = saved_tempdir

        self.addCleanup(restore)

    def pids(self):
        path = self.tmp / 'pids'
        rows = [line.split() for line in path.read_text().splitlines()] if path.exists() else []
        return [(tag, int(pid)) for tag, pid in rows]

    def reap(self):
        for targets in self.targets:
            for process in [targets.local, targets.echo, *targets.remote.values()]:
                if process and process.poll() is None:
                    process.kill()
                    process.wait()
        for _tag, pid in self.pids():
            if Path(f'/proc/{pid}').exists():
                os.kill(pid, 9)

    def verify(self):
        report = vt.Report('ru')
        start = time.monotonic()
        with redirect_stdout(io.StringIO()) as out:
            vt.verify(report, list(EXITS), '')
        print(out.getvalue().strip())
        report.elapsed = time.monotonic() - start
        return report, out.getvalue()

    def assert_failed_targets(self, out):
        lines = out.splitlines()
        for tag, reason in (('denied', 'код 255'), ('crash', 'код 1'), ('silent', 'не ответила за 2 с')):
            with self.subTest(tag=tag):
                found = [line for line in lines if tag in line and reason in line and STDERR[tag] in line]
                self.assertTrue(found, f'нет строки с тегом {tag}, «{reason}» и stderr:\n{out}')

    def test_failed_exit_targets_named_with_code_and_stderr(self):
        _report, out = self.verify()
        self.assert_failed_targets(out)
        # Поднявшийся экзит проверен как обычно.
        self.assertIn('[ru] ✓ WireGuard: маршрут: через экзит up — эхо экзита ответило с самого экзита '
                      'через hy2-up-direct', out)
        # Процессы ssh неподнявшихся целей не живы и дожданы: ни процесса, ни зомби.
        for tag, pid in self.pids():
            if tag != 'up':
                self.assertFalse(Path(f'/proc/{pid}').exists(), f'ssh цели {tag} ({pid}) брошен')
        # stop() гасит только поднявшиеся цели.
        stops = [call[-2] for call in self.calls
                 if call[:3] == ('run', 'ssh', '-n')]
        self.assertEqual(stops, ['up'], self.calls)

    def test_repeat_behaves_the_same_without_leftovers(self):
        first = self.verify()[1]
        second = self.verify()[1]
        self.assert_failed_targets(second)

        def exit_rows(out):
            return [line.split(' — ', 1) for line in out.splitlines()
                    if any(f'экзит {tag}' in line for tag in EXITS)]
        self.assertEqual(exit_rows(second), exit_rows(first))
        self.assertEqual(list((self.tmp / 'scratch').iterdir()), [], 'побочные временные файлы')
        for tag, pid in self.pids():
            if tag != 'up':
                self.assertFalse(Path(f'/proc/{pid}').exists(), f'ssh цели {tag} ({pid}) брошен')


if __name__ == '__main__':
    unittest.main()
