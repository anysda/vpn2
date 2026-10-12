"""Отзыв IKEv2 должен повторяться после временного отказа VICI."""
import hashlib
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from test_deploy import source


class Ikev2SyncTests(unittest.TestCase):
    def test_failed_revoke_is_retried(self):
        for failure in ('load', 'list', 'terminate'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                script = source('infra/scripts/27-ikev2.sh').split("<<'PYEOF'\n", 1)[1].split('\nPYEOF', 1)[0]
                sync = types.ModuleType('ikev2_sync')
                exec(compile(script, 'ikev2_sync', 'exec'), sync.__dict__)
                root = Path(directory)
                for name in ('CLIENTS_CONF', 'STATE_FILE', 'DIGEST_FILE', 'STATUS_FILE'):
                    setattr(sync, name, str(root / name))
                # Пароль изменён, прежняя SA всё ещё работает.
                Path(sync.CLIENTS_CONF).write_text('id = "phone"\nsecret = "new"\n')
                old = {'phone': hashlib.sha256(b'old').hexdigest()}
                sync.write_json(sync.STATE_FILE, old, 0o600)
                sync.write_json(sync.DIGEST_FILE, 'old-digest', 0o600)
                sa = {'conn': 'anysda-dev-1', 'uniqueid': '7', 'state': 'ESTABLISHED',
                      'username': 'phone', 'rx': 0, 'tx': 0}
                failed = {'load': '--load-all', 'terminate': '--terminate'}.get(failure)

                def command(*args):
                    return int(args[0] == failed)

                with patch.object(sync, 'swanctl', side_effect=command), \
                        patch.object(sync, 'live_sas', return_value=None if failure == 'list' else [sa]):
                    self.assertEqual(sync.main(), 1)
                self.assertEqual(sync.read_json(sync.STATE_FILE, {}), old)
                self.assertEqual(sync.read_json(sync.DIGEST_FILE, ''), 'old-digest')
                with patch.object(sync, 'swanctl', return_value=0) as calls, \
                        patch.object(sync, 'live_sas', return_value=[sa]):
                    self.assertEqual(sync.main(), 0)
                self.assertIn(('--terminate', '--ike-id', '7'), [c.args for c in calls.call_args_list])
                self.assertEqual(sync.read_json(sync.STATE_FILE, {}),
                                 {'phone': hashlib.sha256(b'new').hexdigest()})


if __name__ == '__main__':
    unittest.main()
