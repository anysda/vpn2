import json
from pathlib import Path
import tempfile
import unittest

from router_support import SB, apply, prepare, role_tasks, snapshot


@unittest.skipUnless(SB, 'set SING_BOX_BIN to sing-box 1.12.25')
class ManualRoutesTests(unittest.TestCase):
    def test_broken_input_keeps_last_routes(self):
        for filename, broken in [('manual.json', '{'), ('prefs.json', '{'),
                                 ('manual.json', '{}'), ('prefs.json', '[]')]:
            with self.subTest(filename=filename, broken=broken), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                script = prepare(directory)
                (directory / 'manual.json').write_text(json.dumps([
                    {'type': 'domain', 'value': 'example.com', 'outbound': 'hy2-aa-direct'}]))
                (directory / 'prefs.json').write_text('{"aa": ["192.0.2.2"]}')
                result = apply(script)
                self.assertEqual(result.returncode, 0, result.stderr)
                before = snapshot(directory)
                (directory / filename).write_text(broken)
                result = apply(script)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(snapshot(directory), before)

    def test_empty_lists_are_valid_and_reapply_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            script = prepare(directory)
            result = apply(script)
            self.assertEqual(result.returncode, 0, result.stderr)
            before = snapshot(directory)
            result = apply(script)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(snapshot(directory), before)

    def test_stage_does_not_fall_back_to_base_on_apply_failure(self):
        # Сбой apply-routes останавливает роль: базу поверх действующего конфига никто не кладёт.
        tasks = role_tasks()
        task, = [t for t in tasks if 'apply-routes' in str(t.get('ansible.builtin.command'))]
        self.assertFalse({'ignore_errors', 'failed_when', 'rescue'} & set(task))
        self.assertFalse([t for t in tasks if 'config.json' in str(t.get('ansible.builtin.copy'))])


if __name__ == '__main__':
    unittest.main()
