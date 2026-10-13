"""The local gate must isolate state and never hide a failed/missing shard."""

from contextlib import redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[2] / 'app/e2e/run-shards.py'
SPEC = importlib.util.spec_from_file_location('e2e_shards', SCRIPT)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class ShardTests(unittest.TestCase):
    def test_weighted_assignment_never_omits_new_or_renamed_specs(self):
        files = ['heavy.spec.ts', 'other.spec.ts', 'new.spec.ts', 'nested/new.spec.ts']
        groups = runner.assign_files(files, 2, {'heavy.spec.ts': 30, 'removed.spec.ts': 999})
        self.assertEqual(sorted(file for group in groups for file in group), sorted(files))
        self.assertEqual(groups[0], ['heavy.spec.ts'])
        self.assertEqual(groups, runner.assign_files(list(reversed(files)), 2, {'heavy.spec.ts': 30}))

    def run_gate(self, codes=(0, 0), missing=False, cancel=False, browser_port='52001', full=False):
        started = []
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory)
            (app / 'e2e').mkdir()
            (app / 'e2e/shard-weights.json').write_text('{}')

            def launch(args, **kwargs):
                env = kwargs['env']
                index = len(started)
                if not missing or index != 0:
                    Path(env['PLAYWRIGHT_BLOB_OUTPUT_FILE']).touch()
                process = Mock(pid=100 + index)
                process.wait.return_value = codes[index]
                process.poll.return_value = codes[index]
                if cancel and index == 0:
                    process.wait.side_effect = [KeyboardInterrupt(), 143]
                    process.poll.return_value = None
                started.append((args, env, process))
                return process

            env = {'E2E_SHARDS': '2', 'E2E_PORT': '52000', 'E2E_BROWSER_PORT': browser_port,
                   'E2E_CONTAINER': 'test-panel', 'DOCKER': 'sudo -n docker'}
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(runner, '__file__', str(app / 'e2e/run-shards.py')), \
                    patch.object(runner.sys, 'argv', [str(SCRIPT)] + ([] if full else ['e2e/auth.spec.ts'])), \
                    patch.object(runner.subprocess, 'check_output', return_value=
                                 '{"suites": [{"file": "auth.spec.ts"}, {"file": "nested/new.test.js"}]}'), \
                    patch.object(runner.signal, 'signal'), \
                    patch.object(runner.os, 'killpg') as kill, \
                    patch.object(runner.subprocess, 'Popen', side_effect=launch), \
                    patch.object(runner.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run, \
                    redirect_stdout(io.StringIO()):
                if cancel:
                    with self.assertRaises(KeyboardInterrupt):
                        runner.main()
                    kill.assert_called_once_with(100, runner.signal.SIGTERM)
                    result = 143
                else:
                    result = runner.main()
            calls = [call.args[0] for call in run.call_args_list]
            return result, started, calls

    def test_full_run_uses_every_file_reported_by_playwright(self):
        code, started, _calls = self.run_gate(full=True)
        self.assertEqual(code, 0)
        arguments = [arg for args, _, _ in started for arg in args]
        self.assertNotIn('--shard', arguments)
        self.assertEqual(arguments.count(r'e2e/auth\.spec\.ts$'), 1)
        self.assertEqual(arguments.count(r'e2e/nested/new\.test\.js$'), 1)

    def test_shards_have_distinct_panels_ports_and_reports(self):
        code, started, calls = self.run_gate()
        self.assertEqual(code, 0)
        for key in ('E2E_CONTAINER', 'E2E_PORT', 'E2E_BROWSER_PORT',
                    'E2E_REPORT_DIR', 'PLAYWRIGHT_BLOB_OUTPUT_FILE'):
            self.assertEqual(len({env[key] for _, env, _ in started}), 2, key)
        self.assertEqual([env['E2E_SHARD'] for _, env, _ in started], ['1/2', '2/2'])
        for args, _env, process in started:
            self.assertIn('e2e/auth.spec.ts', args)
            process.wait.assert_called_once()
        self.assertEqual(sum('merge-reports' in args for args in calls), 1)
        self.assertEqual(sum('rm' in args for args in calls), 4)

    def test_failure_is_reported_after_waiting_for_every_shard(self):
        code, started, calls = self.run_gate(codes=(1, 0))
        self.assertEqual(code, 1)
        for _, _, process in started:
            process.wait.assert_called_once()
        self.assertTrue(any('merge-reports' in args for args in calls))

    def test_missing_report_cannot_pass(self):
        self.assertEqual(self.run_gate(missing=True)[0], 1)

    def test_cancellation_stops_children_and_removes_containers(self):
        code, _started, calls = self.run_gate(cancel=True)
        self.assertEqual(code, 143)
        self.assertEqual(sum('rm' in args for args in calls), 4)

    def test_overlapping_ports_fail_before_start(self):
        with self.assertRaisesRegex(SystemExit, 'distinct valid ports'):
            self.run_gate(browser_port='52002')


if __name__ == '__main__':
    unittest.main()
