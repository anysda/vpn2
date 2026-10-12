from pathlib import Path
import tempfile
import unittest

from test_deploy import bash, source


class RestoreSsoTests(unittest.TestCase):
    def test_runtime_matches_archive_without_removing_other_files(self):
        script = source('infra/lib/anysda-restore.sh')
        block = script.split('if [[ -d "$BUNDLE_DIR/etc-anysda" ]]; then', 1)[1]
        block = 'if [[ -d "$BUNDLE_DIR/etc-anysda" ]]; then' + block.split(
            'if [[ -f "$BUNDLE_DIR/etc-wireguard/wg0.conf" ]]; then', 1)[0]
        for archived_runtime in (None, '{"passwordLogin":true}'):
            with self.subTest(archived_runtime=archived_runtime), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                live = root / 'live'
                live.mkdir()
                bundle = root / 'bundle/etc-anysda'
                bundle.mkdir(parents=True)
                runtime = live / 'sso-runtime.json'
                runtime.write_text('{"passwordLogin":false}')
                (live / 'backup.env').write_text('preserve backup settings')
                (live / 'unrelated').write_text('preserve unrelated data')
                if archived_runtime is not None:
                    (bundle / runtime.name).write_text(archived_runtime)
                result = bash('BUNDLE_DIR="$PWD/bundle"\n' + block.replace('/etc/anysda', str(live)), directory)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((live / 'backup.env').read_text(), 'preserve backup settings')
                self.assertEqual((live / 'unrelated').read_text(), 'preserve unrelated data')
                if archived_runtime is None:
                    self.assertFalse(runtime.exists(), 'restore kept stale dev SSO settings')
                else:
                    self.assertEqual(runtime.read_text(), archived_runtime)

    def test_partial_backup_without_etc_anysda_preserves_runtime(self):
        script = source('infra/lib/anysda-restore.sh')
        block = script.split('if [[ -d "$BUNDLE_DIR/etc-anysda" ]]; then', 1)[1]
        block = 'if [[ -d "$BUNDLE_DIR/etc-anysda" ]]; then' + block.split(
            'if [[ -f "$BUNDLE_DIR/etc-wireguard/wg0.conf" ]]; then', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / 'sso-runtime.json'
            runtime.write_text('unchanged')
            result = bash('BUNDLE_DIR="$PWD/missing"\n' + block.replace('/etc/anysda', directory), directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(runtime.read_text(), 'unchanged')
