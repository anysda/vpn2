from pathlib import Path
import tempfile
import unittest

from test_deploy import bash, source


class RestoreSsoTests(unittest.TestCase):
    def test_explicit_database_restore_retires_newer_control_versions(self):
        script = source('infra/lib/anysda-restore.sh')
        block = 'if [[ -f "$BUNDLE_DIR/db.sqlite" ]]; then' + script.split(
            'if [[ -f "$BUNDLE_DIR/db.sqlite" ]]; then', 1)[1].split(
            'if [[ -d "$BUNDLE_DIR/etc-anysda" ]]; then', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('bundle', 'data', 'control', 'work'):
                (root / name).mkdir()
            (root / 'bundle/db.sqlite').write_text('restored database')
            (root / 'control/wg.json').write_text('{"version":999}')
            body = block.replace('/var/lib/anysda-vpn2', str(root / 'data')).replace(
                '/var/lib/vpn2-control', str(root / 'control')).replace('-o vpn2-control -g vpn2-control ', '')
            result = bash('BUNDLE_DIR="$PWD/bundle"; WORK="$PWD/work"\n'
                          'id() { return 0; }; chown() { :; }; systemctl() { echo "$*"; }\n' + body, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / 'control/wg.json').exists(), 'restored versions must not be rejected forever')
            self.assertEqual((root / 'work/control-before-restore/wg.json').read_text(), '{"version":999}')
            self.assertIn('stop vpn2-control.service vpn2-control.socket', result.stdout)

    def test_backup_materializes_app_runtime_without_following_arbitrary_links(self):
        script = source('infra/lib/anysda-backup.sh')
        block = 'if [[ -d /etc/anysda ]]; then' + script.split(
            'if [[ -d /etc/anysda ]]; then', 1)[1].split('# 3. WireGuard:', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            live, runtime, bundle = (root / name for name in ('live', 'runtime', 'bundle'))
            for path in (live, runtime, bundle):
                path.mkdir()
            for name in ('sso-runtime.json', 'telegram-runtime.json'):
                (runtime / name).write_text('preserved runtime')
                (live / name).symlink_to(runtime / name)
            result = bash('BUNDLE_DIR="$PWD/bundle"\n' + block.replace(
                '/etc/anysda', str(live)).replace('/var/lib/anysda-vpn2/runtime', str(runtime)), directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in ('sso-runtime.json', 'telegram-runtime.json'):
                archived = bundle / 'etc-anysda' / name
                self.assertFalse(archived.is_symlink(), 'archive must survive removal of the old host')
                self.assertEqual(archived.read_text(), 'preserved runtime')
            (runtime / 'sso-runtime.json').unlink()
            (runtime / 'sso-runtime.json').symlink_to('/etc/passwd')
            result = bash('BUNDLE_DIR="$PWD/bundle2"; mkdir "$BUNDLE_DIR"\n' + block.replace(
                '/etc/anysda', str(live)).replace('/var/lib/anysda-vpn2/runtime', str(runtime)), directory)
            self.assertNotEqual(result.returncode, 0, 'app must not trick backup into exporting root files')

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
