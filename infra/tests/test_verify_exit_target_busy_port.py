"""Порт цели эха, занятый на экзите, не валит проверку: цель поднимается, порт один на все узлы.

Подставной ssh в PATH запускает настоящий verify-target.py со stdin на адресе
экзита (127.0.0.3 и 127.0.0.4) и с портом из команды. Порт, выбранный прогоном,
на экзите us заранее занят слушающим сокетом теста - как слушатель или
исходящее соединение на настоящем экзите. Пробы подключаются к цели по порту
прогона.
"""
import importlib.util
import io
import os
from pathlib import Path
import socket
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
ADDRESS = {'us': '127.0.0.3', 'de': '127.0.0.4'}
EXITS = list(ADDRESS)
FAKE_SSH = r'''#!/usr/bin/env python3
import os, re, sys
args = sys.argv[1:]
while args and args[0].startswith('-'):
    if args.pop(0) in ('-o', '-i', '-F', '-p', '-l'):
        args.pop(0)
tag, command = args[0], ' '.join(args[1:])
port = re.search(r'--port (\d+)', command).group(1)
lifetime = re.search(r'--lifetime (\d+)', command).group(1)
with open(os.path.join(os.environ['FAKE_NODE'], 'pids'), 'a') as pids:
    pids.write(f'{tag} {os.getpid()} {port}\n')
address = {'us': '127.0.0.3', 'de': '127.0.0.4'}[tag]
os.execvp(sys.executable, [sys.executable, '-I', '-', '--listen', address, '--port', port,
                           '--lifetime', lifetime])
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


class ExitTargetBusyPortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='verify-exit-busy-port-'))
        self.addCleanup(self.reap)
        (self.tmp / 'bin').mkdir()
        (self.tmp / 'bin/ssh').write_text(FAKE_SSH)
        (self.tmp / 'bin/ssh').chmod(0o755)
        self.targets = []
        self.busy = []
        self.probed = []
        test = self

        class Targets(vt.Targets):
            def __init__(self, *args):
                super().__init__(*args)
                test.targets.append(self)
                test.occupy(ADDRESS['us'], self.port)

        def probe(verifier, _protocol, _kind, args, _dest=None, **_kwargs):
            # Проба доходит до цели экзита по порту прогона, иначе - нет соединения.
            tag = next((t for t in EXITS if verifier.markers.get(t) in args), None)
            if tag is None:
                return None, []
            self.probed.append((tag, args[1]))
            try:
                socket.create_connection((ADDRESS[tag], args[1]), timeout=2).close()
            except OSError:
                return None, []
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
            'run': lambda *_args, **_: None,
        }
        methods = {'probe': probe, 'direct_target': lambda _self: False,
                   'direct_missing': lambda _self: 'подменено', 'egress_check': lambda *_: None,
                   'internet_check': lambda *_: None, 'protocol': vt.Verifier.paths}
        saved = {name: getattr(vt, name) for name in stubs}
        saved_methods = {name: getattr(vt.Verifier, name) for name in methods}
        saved_timeout = vars(vt.Targets).get('READY_TIMEOUT')
        for name, value in stubs.items():
            setattr(vt, name, value)
        for name, value in methods.items():
            setattr(vt.Verifier, name, value)
        vt.Targets.READY_TIMEOUT = 5
        saved_path = os.environ['PATH']
        os.environ['PATH'] = f'{self.tmp}/bin:{saved_path}'
        os.environ['FAKE_NODE'] = str(self.tmp)

        def restore():
            for name, value in saved.items():
                setattr(vt, name, value)
            for name, value in saved_methods.items():
                setattr(vt.Verifier, name, value)
            vt.Targets.READY_TIMEOUT = saved_timeout
            os.environ['PATH'] = saved_path
            os.environ.pop('FAKE_NODE', None)

        self.addCleanup(restore)

    def occupy(self, address, port):
        sock = socket.socket()
        self.addCleanup(sock.close)
        try:
            sock.bind((address, port))
            sock.listen()
        except OSError:
            pass  # порт уже кем-то занят - тем лучше для теста
        self.busy.append((address, port))

    def pids(self):
        path = self.tmp / 'pids'
        rows = [line.split() for line in path.read_text().splitlines()] if path.exists() else []
        return [(tag, int(pid), int(port)) for tag, pid, port in rows]

    def reap(self):
        for targets in self.targets:
            for process in [targets.local, targets.echo, *targets.remote.values()]:
                if process and process.poll() is None:
                    process.kill()
                    process.wait()
        for _tag, pid, _port in self.pids():
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

    def test_busy_port_on_exit_does_not_fail_targets(self):
        report, out = self.verify()
        self.assertEqual(len(self.busy), 1)
        busy_port = self.busy[0][1]
        self.assertNotIn('не поднялась', out)
        self.assertFalse([row for row in report.rows if row['check'] == 'подготовка'], out)
        for tag in EXITS:
            for name in ('WireGuard', 'OpenVPN', 'IKEv2'):
                with self.subTest(tag=tag, protocol=name):
                    rows = [row for row in report.rows
                            if row['check'] == f'маршрут: через экзит {tag}' and row['component'] == name]
                    self.assertEqual([row['result'] for row in rows], [vt.STATUSES[vt.OK]], out)
        # Цель одна на все узлы прогона: entry, адрес эха, оба экзита и пробы - на одном порту.
        targets = self.targets[-1]
        port = targets.port
        for process in (targets.local, targets.echo):
            self.assertEqual(process.args[process.args.index('--port') + 1], str(port))
        last = {tag: started for tag, _pid, started in self.pids()}
        self.assertEqual(sorted(targets.remote), sorted(EXITS), out)
        self.assertEqual(last, {tag: port for tag in EXITS}, self.pids())
        self.assertEqual({p for _tag, p in self.probed}, {port}, self.probed)
        self.assertNotEqual(port, busy_port, 'цель на занятом порту')


if __name__ == '__main__':
    unittest.main()
