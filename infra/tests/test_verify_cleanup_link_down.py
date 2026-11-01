"""Очистка 99-verify гасит vp2verify до удаления и даёт networkd-dispatcher его прочитать.

Удаление поднятой ссылки ядро сопровождает переходом в off. На off у Ubuntu
есть хук (chrony-onoffline в off.d), и networkd-dispatcher зовёт
`networkctl status vp2verify`, когда ссылки уже нет: в journalctl -p err
строка `Interface "vp2verify" not found.` на каждый прогон. Unmanaged=yes
не спасает: неуправляемые ссылки networkd тоже ведёт по состоянию.

Очистка сначала опускает vp2verify, пока ссылка жива, и ждёт; удаление
опущенной ссылки в off уже не переходит.
"""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / 'infra/lib/verify-traffic.py'


def load():
    spec = importlib.util.spec_from_file_location('verify_traffic_cleanup', MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vt = load()


class CleanupLinkDownTests(unittest.TestCase):
    def cleanup_calls(self):
        calls = []

        def run(*args, **_):
            calls.append(('run',) + args)
            return subprocess.CompletedProcess(args, 0, '', '')

        def sleep(seconds):
            calls.append(('sleep', seconds))

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with mock.patch.multiple(vt, run=run, clean_iptables=lambda: None,
                                     manual_markers=mock.Mock(), echo_addresses=lambda: [],
                                     CCD=tmp / 'ccd', SWAN_FILE=tmp / 'swan.conf', WORK=tmp / 'work',
                                     MANUAL_DIR=tmp / 'manual'), \
                    mock.patch.object(vt.time, 'sleep', sleep):
                vt.cleanup()
        return calls

    def test_host_link_down_and_wait_before_removal(self):
        calls = self.cleanup_calls()
        removals = [i for i, call in enumerate(calls)
                    if call[:4] == ('run', 'ip', 'netns', 'del') or call == ('run', 'ip', 'link', 'del', vt.HOST_IF)]
        self.assertTrue(removals, calls)
        first = min(removals)
        downs = [i for i, call in enumerate(calls[:first])
                 if call == ('run', 'ip', 'link', 'set', vt.HOST_IF, 'down')
                 or call == ('run', 'ip', 'link', 'set', 'dev', vt.HOST_IF, 'down')]
        self.assertTrue(downs, f'{vt.HOST_IF} не опущен до удаления: {calls[:first + 1]}')
        waited = sum(call[1] for call in calls[downs[0]:first] if call[0] == 'sleep')
        self.assertGreaterEqual(waited, 0.5, f'нет паузы между down и удалением: {calls[downs[0]:first + 1]}')

    def test_removal_still_happens(self):
        calls = self.cleanup_calls()
        self.assertIn(('run', 'ip', 'netns', 'del', vt.NS), calls)
        self.assertIn(('run', 'ip', 'link', 'del', vt.HOST_IF), calls)


if __name__ == '__main__':
    unittest.main()
