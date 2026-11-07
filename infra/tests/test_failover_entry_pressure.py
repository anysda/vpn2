"""VPN2-656: сторож путает замерший sing-box с мёртвыми экзитами.

Когда сам entry задыхается (память или CPU), sing-box не отвечает clash-api,
все delay-пробы истекают, и сторож через RESTART_AFTER_CHECKS проверок шлёт
«Экзиты: Все каналы мертвы». Экзиты при этом живы. В момент решения
о перезапуске сторож смотрит PSI entry (PSI_DIR, по умолчанию /proc/pressure):
memory some avg10 >= PRESSURE_MEM_AVG10 (10) или cpu some avg10 >=
PRESSURE_CPU_AVG10 (60) - уведомление «entry перегружен» с цифрами PSI и топом
процессов по RSS (PROC_DIR, по умолчанию /proc), тема не «Экзиты». Порог
и предел частоты перезапуска прежние.

Сторож гоняется настоящим main(): подменены clash-api (_req), часы, sleep,
перезапуск (_restart_sing_box) и бот (_bot_event).
"""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_failover_restart import Clash, Stop


ROOT = Path(os.environ.get('VPN2_AUDIT_ROOT', Path(__file__).resolve().parents[2]))
HOG = 'memhog-656'   # имя процесса-пожирателя в подменном /proc


class Clock:
    """Одни часы на monotonic и time; проверка = sleep(INTERVAL) в main()."""

    def __init__(self, interval, mono=1000.0, wall=1_800_000_000.0):
        self.interval = interval
        self.mono, self.shift = mono, wall - mono
        self.checks = 0
        self.limit = 0

    def monotonic(self):
        return self.mono

    def time(self):
        return self.mono + self.shift

    def sleep(self, seconds):
        self.mono += seconds
        if seconds != self.interval:
            return   # до-проверка внутри тика
        self.checks += 1
        if self.checks >= self.limit:
            raise Stop


def psi_line(avg10):
    return f'some avg10={avg10:.2f} avg60={avg10:.2f} avg300={avg10:.2f} total=123456789\n'


def make_psi(root, mem=None, cpu=None):
    """Каталог PSI как /proc/pressure; None - каталога нет вовсе."""
    psi = Path(root) / 'pressure'
    if mem is None and cpu is None:
        return psi
    psi.mkdir()
    full = 'full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n'
    (psi / 'memory').write_text(psi_line(mem) + full)
    (psi / 'cpu').write_text(psi_line(cpu) + full)
    (psi / 'io').write_text(psi_line(0.0) + full)
    return psi


def make_proc(root):
    """/proc с тремя процессами: пожиратель памяти, sing-box и мелочь."""
    proc = Path(root) / 'proc'
    for pid, name, rss_kb in ((4242, HOG, 1_843_200), (777, 'sing-box', 512_000),
                              (12, 'sshd', 6_000)):
        d = proc / str(pid)
        d.mkdir(parents=True)
        (d / 'comm').write_text(name + '\n')
        (d / 'cmdline').write_bytes(name.encode() + b'\0')
        (d / 'status').write_text(f'Name:\t{name}\nState:\tS (sleeping)\nPid:\t{pid}\n'
                                  f'VmRSS:\t{rss_kb:>8} kB\n')
        (d / 'statm').write_text(f'{rss_kb // 2} {rss_kb // 4} 0 0 0 0 0\n')
    (proc / 'meminfo').write_text('MemTotal:        2000000 kB\nMemAvailable:      40000 kB\n')
    return proc


class Run:
    """Прогон main() на checks проверок при всех истёкших пробах."""

    def __init__(self, tmp, mem=None, cpu=None, restart_err=''):
        state = Path(tmp) / 'state'
        state.mkdir()
        self.env = {'CLASH_SECRET': 'test',
                    'LANE_STATE_FILE': str(state / 'lanes.json'),
                    'DISABLED_EXITS_FILE': str(state / 'exits-disabled.json'),
                    'PSI_DIR': str(make_psi(tmp, mem, cpu)),
                    'PROC_DIR': str(make_proc(tmp))}
        spec = importlib.util.spec_from_file_location('watchdog', ROOT / 'infra/lib/failover-watchdog.py')
        self.w = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, self.env, clear=True):
            spec.loader.exec_module(self.w)
        self.clash = Clash(['aa'])   # alive пуст: все пробы истекают
        self.clock = Clock(self.w.INTERVAL)
        self.restart_err = restart_err
        self.restart_check = []
        self.events = []
        self.out = io.StringIO()

    def restart(self, *_args, **_kwargs):
        self.restart_check.append(self.clock.checks + 1)
        return self.restart_err

    def event(self, *args, **kwargs):
        subject = kwargs.get('subject', args[0] if args else '')
        detail = kwargs.get('detail', args[1] if len(args) > 1 else '')
        self.events.append((subject, detail))

    def go(self, checks):
        self.clock.limit = self.clock.checks + checks
        w = self.w
        with patch.dict(os.environ, self.env, clear=True), \
                patch.object(w, '_req', self.clash.req), \
                patch.object(w.time, 'monotonic', self.clock.monotonic), \
                patch.object(w.time, 'time', self.clock.time), \
                patch.object(w.time, 'sleep', self.clock.sleep), \
                patch.object(w, '_restart_sing_box', side_effect=self.restart, create=True), \
                patch.object(w, '_bot_event', side_effect=self.event, create=True), \
                contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(self.out):
            try:
                w.main()
            except Stop:
                pass
        return self

    def tail(self):
        return f'\nсобытия: {self.events}\nжурнал:\n' + self.out.getvalue()[-800:]


class TestFailoverEntryPressure(unittest.TestCase):

    def one_event(self, run):
        self.assertEqual(run.restart_check, [3],
                         'при мёртвых пробах 3 проверки подряд ждём один перезапуск '
                         f'на 3-й проверке, было {run.restart_check}' + run.tail())
        self.assertEqual(len(run.events), 1, 'событие в бот о перезапуске - ровно одно' + run.tail())
        return run.events[0]

    def assert_entry_overloaded(self, run, value):
        subject, detail = self.one_event(run)
        self.assertIn('entry перегружен', detail,
                      'entry под давлением, а уведомление не про entry' + run.tail())
        self.assertNotIn('Все каналы мертвы', detail,
                         'entry под давлением, а уведомление винит экзиты' + run.tail())
        self.assertNotEqual(subject, 'Экзиты', 'тема уведомления про entry - не «Экзиты»' + run.tail())
        self.assertIn(value, detail, f'в уведомлении нет цифры PSI {value}' + run.tail())
        self.assertIn(HOG, detail, 'в уведомлении нет топа процессов по RSS' + run.tail())
        return detail

    # 1
    def test_memory_pressure_notifies_entry_overloaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Run(tmp, mem=35.0, cpu=1.0).go(3)
            self.assert_entry_overloaded(run, '35')

    # 2
    def test_cpu_pressure_notifies_entry_overloaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Run(tmp, mem=0.5, cpu=80.0).go(3)
            detail = self.assert_entry_overloaded(run, '80')
            self.assertIn('CPU', detail, 'причина перегруза - CPU, в уведомлении её нет' + run.tail())

    # 3
    def test_normal_pressure_keeps_exits_notification(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Run(tmp, mem=0.5, cpu=0.5).go(3)
            subject, detail = self.one_event(run)
            self.assertEqual(subject, 'Экзиты', run.tail())
            self.assertEqual(detail, 'Все каналы мертвы 3 проверки подряд - перезапустил sing-box',
                             run.tail())

    # 4
    def test_no_psi_dir_keeps_exits_notification(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Run(tmp).go(3)   # каталога PSI нет: старое ядро, контейнер
            self.assertNotIn('Traceback', run.out.getvalue(), run.tail())
            subject, detail = self.one_event(run)
            self.assertEqual(subject, 'Экзиты', run.tail())
            self.assertEqual(detail, 'Все каналы мертвы 3 проверки подряд - перезапустил sing-box',
                             run.tail())

    # 5
    def test_restart_failure_under_pressure_reports_both(self):
        err = 'Job for sing-box.service failed because a timeout was exceeded.'
        with tempfile.TemporaryDirectory() as tmp:
            run = Run(tmp, mem=35.0, cpu=1.0, restart_err=err).go(3)
            detail = self.assert_entry_overloaded(run, '35')
            self.assertIn(err, detail, 'перезапуск не удался, а ошибки в уведомлении нет' + run.tail())

    # 6
    def test_pressure_does_not_change_restart_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Run(tmp, mem=35.0, cpu=80.0).go(2)
            self.assertEqual(run.restart_check, [], 'при перегрузе перезапуск раньше 3 проверок'
                             + run.tail())
            self.assertEqual(run.events, [], run.tail())
        with tempfile.TemporaryDirectory() as tmp:
            run = Run(tmp, mem=35.0, cpu=80.0)
            run.go(3 + int(590 / run.w.INTERVAL))
            self.assertEqual(run.restart_check, [3], 'при перегрузе повтор перезапуска раньше 600 с'
                             + run.tail())


if __name__ == '__main__':
    unittest.main()
