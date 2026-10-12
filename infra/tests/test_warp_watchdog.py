"""Exercise the exit watchdog with fake WARP, clock and systemctl; no network."""
import contextlib
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse


ROOT = Path(os.environ.get('VPN2_AUDIT_ROOT', Path(__file__).resolve().parents[2]))


class TestWarpWatchdog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('warp_watchdog', ROOT / 'infra/lib/warp-watchdog.py')
        cls.wd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.wd)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.state = self.directory / 'state.json'
        self.config = {
            'outbounds': [{'type': 'wireguard', 'tag': 'warp', 'server': 'engage.cloudflareclient.com'}],
            'experimental': {'clash_api': {'external_controller': '127.0.0.1:9090', 'secret': 'test-only'}},
        }

    def ticks(self, statuses, clock):
        with patch.object(self.wd, 'probe', side_effect=statuses), \
                patch.object(self.wd.time, 'time', side_effect=clock), \
                patch.object(self.wd.subprocess, 'run') as systemctl, \
                contextlib.redirect_stdout(io.StringIO()):
            for _ in statuses:
                self.wd.tick(self.config, self.state)
        return systemctl

    def test_stall_one_restart_then_recovery(self):
        systemctl = self.ticks([False] * 7 + [True] * 20, range(0, 810, 30))
        systemctl.assert_called_once_with(['systemctl', 'restart', 'sing-box'], check=True, timeout=20)
        self.assertNotIn('bad_since', json.loads(self.state.read_text()))

    def test_live_no_restarts(self):
        systemctl = self.ticks([True] * 240, range(0, 7200, 30))
        systemctl.assert_not_called()

    def test_long_outage_backoff_and_rolling_hour_cap(self):
        timestamps = []

        def restart(*args, **kwargs):
            self.assertEqual(args[0], ['systemctl', 'restart', 'sing-box'])
            self.assertEqual(kwargs['timeout'], 20)
            # Must be committed before attempting systemctl, even if it fails.
            timestamps.append(json.loads(self.state.read_text())['restarts'][-1])

        with patch.object(self.wd, 'probe', return_value=False), \
                patch.object(self.wd.subprocess, 'run', side_effect=restart), \
                contextlib.redirect_stdout(io.StringIO()):
            for now in range(0, 14400, 30):
                with patch.object(self.wd.time, 'time', return_value=now):
                    self.wd.tick(self.config, self.state)
        self.assertEqual(timestamps[:4], [180, 480, 1080, 3780])
        self.assertGreater(len(timestamps), 4)  # Recovery attempts must resume.
        for now in timestamps:
            self.assertLessEqual(sum(now - 3600 < stamp <= now for stamp in timestamps), 3)
        self.assertTrue(all(b - a >= 1800 for a, b in zip(timestamps[3:], timestamps[4:])))

    def test_short_recovery_does_not_clear_budget(self):
        self.ticks([False, False, True, False, False], [0, 180, 190, 200, 480])
        state = json.loads(self.state.read_text())
        self.assertEqual(state['restarts'], [180, 480])
        self.assertEqual(state['next_restart'], 1080)

    def test_failed_systemctl_is_still_budgeted(self):
        for failure in (subprocess.CalledProcessError(1, 'systemctl'), subprocess.TimeoutExpired('systemctl', 20)):
            self.state.write_text('{"bad_since": 0}')
            with self.subTest(failure=failure), patch.object(self.wd, 'probe', return_value=False), \
                    patch.object(self.wd.time, 'time', return_value=180), \
                    patch.object(self.wd.subprocess, 'run', side_effect=failure) as systemctl, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.wd.tick(self.config, self.state)
                self.wd.tick(self.config, self.state)
                self.assertEqual(systemctl.call_count, 1)
        self.assertEqual(json.loads(self.state.read_text())['restarts'], [180])

    def test_failed_state_save_never_restarts(self):
        self.state.write_text('{"bad_since": 0}')
        with patch.object(self.wd, 'probe', return_value=False), \
                patch.object(self.wd.time, 'time', return_value=180), \
                patch.object(self.wd, 'save_state', side_effect=OSError('disk full')), \
                patch.object(self.wd.subprocess, 'run') as systemctl, self.assertRaises(OSError):
            self.wd.tick(self.config, self.state)
        systemctl.assert_not_called()

    def test_stable_recovery_resets_backoff_but_keeps_hourly_history(self):
        self.ticks([False, False, True, True, False, False], [0, 180, 210, 510, 540, 720])
        state = json.loads(self.state.read_text())
        self.assertEqual(state['restarts'], [180, 720])
        self.assertEqual(state['next_restart'], 1020)

    def test_backward_clock_does_not_replenish_budget(self):
        self.ticks([False] * 6, [0, 180, 480, 1080, 900, 800])
        state = json.loads(self.state.read_text())
        self.assertEqual(state['restarts'], [180, 480, 1080])
        self.assertEqual(state['last_seen'], 1080)

    def test_corrupt_state_fails_closed(self):
        self.state.write_text('{broken')
        with patch.object(self.wd.subprocess, 'run') as systemctl, self.assertRaises(ValueError):
            self.wd.tick(self.config, self.state)
        systemctl.assert_not_called()

    def test_lock_excludes_timer_during_bootstrap(self):
        with self.state.with_suffix('.lock').open('a') as lock, \
                patch.object(self.wd, 'tick') as tick, contextlib.redirect_stdout(io.StringIO()):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.wd.run(self.config, self.state)
        tick.assert_not_called()

    def test_bootstrap_waits_recovers_after_one_restart(self):
        with patch.object(self.wd, 'probe', side_effect=[False] * 7 + [True]), \
                patch.object(self.wd.time, 'time', side_effect=range(0, 240, 30)), \
                patch.object(self.wd.time, 'sleep'), \
                patch.object(self.wd.subprocess, 'run') as systemctl, \
                patch.object(self.wd, 'log'):
            self.wd.run(self.config, self.state, bootstrap=True)
        self.assertEqual(systemctl.call_count, 1)

    def test_bootstrap_stops_after_two_restarts_with_warning(self):
        with patch.object(self.wd, 'probe', return_value=False), \
                patch.object(self.wd.time, 'time', side_effect=range(0, 600, 30)), \
                patch.object(self.wd.time, 'sleep'), \
                patch.object(self.wd.subprocess, 'run') as systemctl, \
                patch.object(self.wd, 'log') as log:
            self.wd.run(self.config, self.state, bootstrap=True)
        self.assertEqual(systemctl.call_count, 2)
        self.assertIn('WARNING WARP still unavailable', log.call_args.args[0])

    def test_bootstrap_respects_existing_hourly_budget_and_deadline(self):
        self.state.write_text(json.dumps({'restarts': [180, 480, 1080], 'bad_since': 1080}))
        with patch.object(self.wd, 'probe', return_value=False), \
                patch.object(self.wd.time, 'time', return_value=1400), \
                patch.object(self.wd.time, 'monotonic', side_effect=[0, 0, 540]), \
                patch.object(self.wd.time, 'sleep'), \
                patch.object(self.wd.subprocess, 'run') as systemctl, \
                patch.object(self.wd, 'log') as log:
            self.wd.run(self.config, self.state, bootstrap=True)
        systemctl.assert_not_called()
        self.assertIn('WARNING WARP still unavailable', log.call_args.args[0])

    def test_probe_specific_outbound_and_second_destination(self):
        with patch.object(self.wd.urllib.request, 'build_opener') as build:
            opener = build.return_value
            opener.open.side_effect = [urllib.error.URLError('timeout'),
                                      contextlib.closing(io.StringIO('{"delay": 42}'))]
            self.assertTrue(self.wd.probe(self.config))
            self.assertEqual(opener.open.call_count, 2)
            for call, target in zip(opener.open.call_args_list, self.wd.PROBE_URLS):
                request = call.args[0]
                url = urllib.parse.urlsplit(request.full_url)
                self.assertEqual(url.netloc, '127.0.0.1:9090')
                self.assertEqual(url.path, '/proxies/warp/delay')
                self.assertEqual(urllib.parse.parse_qs(url.query)['url'], [target])
                self.assertEqual(request.get_header('Authorization'), 'Bearer test-only')
                self.assertEqual(call.kwargs['timeout'], 7)

    def test_probe_rejects_invalid_responses(self):
        for body in ('{}', '{"delay": 0}', '{"delay": true}', 'null', '[]', 'bad json'):
            with self.subTest(body=body), patch.object(self.wd.urllib.request, 'build_opener') as build:
                build.return_value.open.side_effect = [contextlib.closing(io.StringIO(body)) for _ in range(2)]
                self.assertFalse(self.wd.probe(self.config))

    def test_disabled_and_placeholder(self):
        self.assertTrue(self.wd.enabled(self.config))
        for outbounds in ([], [{'type': 'direct', 'tag': 'warp'}],
                          [{'type': 'wireguard', 'tag': 'warp', 'server': '127.0.0.1'}],
                          [{'type': 'wireguard', 'tag': 'warp', 'server': '::1'}]):
            self.assertFalse(self.wd.enabled({'outbounds': outbounds}))

    def test_cli_disabled_does_not_create_state(self):
        config = self.directory / 'config.json'
        config.write_text('{"outbounds": []}')
        result = subprocess.run(['python3', str(ROOT / 'infra/lib/warp-watchdog.py'),
                                 '--config', str(config), '--state', str(self.state), '--bootstrap'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.state.exists())

    def test_cli_bootstrap_corrupt_state_warns_without_failing(self):
        config = self.directory / 'config.json'
        config.write_text(json.dumps(self.config))
        self.state.write_text('{broken')
        result = subprocess.run(['python3', str(ROOT / 'infra/lib/warp-watchdog.py'),
                                 '--config', str(config), '--state', str(self.state), '--bootstrap'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('WARNING WARP watchdog failed closed', result.stdout)
        self.assertEqual(self.state.read_text(), '{broken')


class TestWarpInstall(unittest.TestCase):
    def test_stage_install_repeat_and_remove(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        source = (ROOT / 'infra/scripts/10-foreign.sh').read_text()
        # On the old stage this is an empty block, so the assertion below
        # demonstrates that no watchdog was installed (independent of imports).
        start = source.index('EOF\n', source.index('cat > /etc/systemd/system/sing-box.service')) + 4
        block = source[start:source.index('systemctl daemon-reload')]
        paths = ('/etc/systemd/system', '/usr/local/bin', '/tmp/anysda')
        for path in paths:
            (directory / path.lstrip('/')).mkdir(parents=True)
            block = block.replace(path, str(directory / path.lstrip('/')))
        fixture = directory / 'tmp/anysda/warp-watchdog.py'
        # The real enabled/config detection is covered above. This executable
        # fixture tests the stage's systemctl/file lifecycle without root.
        fixture.write_text('#!/bin/sh\nexit "${PROBE_DISABLED:-0}"\n')
        calls = directory / 'calls'
        script = '\n'.join(['set -euo pipefail',
                            f'systemctl() {{ echo "$*" >> {shlex.quote(str(calls))}; }}',
                            block])
        service = directory / 'etc/systemd/system/anysda-warp-watchdog.service'
        timer = service.with_suffix('.timer')
        for _ in range(2):
            subprocess.run(['bash', '-c', script], check=True, capture_output=True)
            self.assertTrue(service.exists(), '10-foreign must install WARP recovery service')
            self.assertIn('TimeoutStartSec=45', service.read_text())
            self.assertIn('OnUnitInactiveSec=30', timer.read_text())
        for _ in range(2):
            subprocess.run(['bash', '-c', 'export PROBE_DISABLED=1\n' + script],
                           check=True, capture_output=True)
            self.assertFalse(service.exists())
            self.assertFalse(timer.exists())
            self.assertFalse((directory / 'usr/local/bin/anysda-warp-watchdog').exists())
        self.assertEqual(calls.read_text().count('disable --now anysda-warp-watchdog.timer'), 2)


if __name__ == '__main__':
    unittest.main()
