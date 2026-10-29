"""Локальная цель эха, не поднявшаяся, - отказ подготовки с причиной до проб, а не ложный провал проб."""
import importlib.util
import io
from pathlib import Path
import socket
import tempfile
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


class Stub:
    def __init__(self, *_args, **_kwargs):
        self.outbounds = {vt.DIRECT: {'bind_interface': 'lo'}}

    def up(self):
        pass

    def cleanup(self):
        pass

    @staticmethod
    def exit_ip(_tag):
        return None


class LocalTargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='verify-local-target-'))
        self.calls = []
        self.targets = []
        self.port = None
        test = self

        class Targets(vt.Targets):
            def __init__(self, *args):
                super().__init__(*args)
                if test.port:
                    self.port = test.port
                test.targets.append(self)

        def protocol(_verifier, protocol):
            self.calls.append(('проба', protocol.name))

        stubs = {
            'detect_wan': lambda: WAN, 'ECHO_IP': ECHO, 'WORK': self.tmp / 'work', 'Router': Stub,
            'Namespace': Stub, 'WireGuard': type('WireGuard', (Stub,), {'name': 'WireGuard'}),
            'OpenVPN': type('OpenVPN', (Stub,), {'name': 'OpenVPN'}),
            'IKEv2': type('IKEv2', (Stub,), {'name': 'IKEv2'}), 'Targets': Targets,
            'cleanup': lambda *_: self.calls.append(('уборка',)), 'leftovers': lambda: [],
            'manual_markers': lambda *_, **__: {}, 'INTERNET': (), 'probe_dns': lambda *_: None,
            'wg_apply_check': lambda _report: None,
            'run': lambda *args, **_: self.calls.append(('run',) + args),
        }
        saved = {name: getattr(vt, name) for name in stubs}
        saved_protocol, saved_timeout = vt.Verifier.protocol, vars(vt.Targets).get('READY_TIMEOUT')
        for name, value in stubs.items():
            setattr(vt, name, value)
        vt.Verifier.protocol = protocol

        def restore():
            for name, value in saved.items():
                setattr(vt, name, value)
            vt.Verifier.protocol = saved_protocol
            if saved_timeout is not None:
                vt.Targets.READY_TIMEOUT = saved_timeout
            elif 'READY_TIMEOUT' in vars(vt.Targets):
                del vt.Targets.READY_TIMEOUT
            for targets in self.targets:
                for process in (targets.local, targets.echo):
                    if process and process.poll() is None:
                        process.kill()
                        process.wait()

        self.addCleanup(restore)

    def occupy(self, address):
        sock = socket.socket()
        sock.bind((address, 0))
        sock.listen()
        self.addCleanup(sock.close)
        self.port = sock.getsockname()[1]

    def verify(self):
        report = vt.Report('ru')
        with redirect_stdout(io.StringIO()) as out:
            vt.verify(report, [], '')
        print(out.getvalue().strip())
        return report, out.getvalue()

    def assert_refused(self, report, out, *needles):
        rows = [row for row in report.rows if row['check'] == 'подготовка']
        self.assertEqual(len(rows), 1, out)
        self.assertEqual(rows[0]['result'], 'Ошибка', out)
        self.assertIn('локальная цель эха', rows[0]['reason'])
        self.assertIn('не поднялась', rows[0]['reason'])
        for needle in needles:
            self.assertIn(needle, rows[0]['reason'])
        self.assertNotIn(('проба', 'WireGuard'), self.calls, 'пробы после отказа цели')
        # Уборка - тем же путём, что при прочих сбоях: до и после попытки.
        self.assertEqual(self.calls.count(('уборка',)), 2, self.calls)
        self.assertEqual(report.rows[-1]['check'], 'восстановление', out)
        self.assertEqual(report.code(), 1, out)

    def test_wan_target_port_busy(self):
        self.occupy(WAN)
        report, out = self.verify()
        self.assert_refused(report, out, f'{WAN}:{self.port}', 'код 1', 'Address already in use')
        # Адрес эха не ставился: отказ до него.
        self.assertFalse([call for call in self.calls if call[:2] == ('run', 'iptables')], self.calls)

    def test_echo_target_port_busy(self):
        self.occupy(ECHO)
        report, out = self.verify()
        self.assert_refused(report, out, f'{ECHO}:{self.port}', 'код 1', 'Address already in use')
        # Цель на WAN, поднятая до отказа, остановлена.
        local = self.targets[0].local
        self.assertIsNotNone(local)
        self.assertIsNotNone(local.poll(), 'цель на WAN брошена живой')

    def test_silent_target_is_killed(self):
        pid_file = self.tmp / 'pid'
        silent = self.tmp / 'silent.py'
        silent.write_text(f'import os, time\nopen({str(pid_file)!r}, "w").write(str(os.getpid()))\n'
                          'time.sleep(120)\n')
        saved = vt.TARGET
        vt.TARGET = silent
        self.addCleanup(setattr, vt, 'TARGET', saved)
        vt.Targets.READY_TIMEOUT = 1
        report, out = self.verify()
        self.assert_refused(report, out, 'не ответила за 1 с')
        pid = int(pid_file.read_text())
        # Убита и дождана: ни живого процесса, ни зомби.
        self.assertFalse(Path(f'/proc/{pid}').exists(), f'цель {pid} брошена')

    def test_free_port_target_starts(self):
        report, out = self.verify()
        self.assertFalse([row for row in report.rows if row['check'] == 'подготовка'], out)
        self.assertEqual([call for call in self.calls if call[0] == 'проба'],
                         [('проба', 'WireGuard'), ('проба', 'OpenVPN'), ('проба', 'IKEv2')])
        targets = self.targets[0]
        self.assertEqual(targets.local.args[targets.local.args.index('--listen') + 1], WAN)
        self.assertEqual(targets.echo.args[targets.echo.args.index('--listen') + 1], ECHO)
        self.assertIsNotNone(targets.local.poll())
        self.assertEqual(report.code(), 0, out)


if __name__ == '__main__':
    unittest.main()
