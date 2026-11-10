"""Повторный деплой без изменений не трогает работающую панель."""
from pathlib import Path
import tempfile
import unittest

from test_deploy import ROOT, bash, source

SETUP = '''
HOST_TAG=ru ENTRY_HOST=192.0.2.1 EXIT_TAGS='' MGMT_IP=192.0.2.1 AGH_PORT=3000
ADMIN_USER=admin ADMIN_PASS=test-only-password SESSION_SECRET=test-only-session
NODE_HOSTS=ru=192.0.2.1 PANEL_IMAGE=test-only-image
PANEL_UID=10001 PANEL_GID=10001 CONTROL_GID=10003
SSO_ENABLED=false SSO_REDIRECT_URL=''
ENTRY_DOCKER_ARGS=()
agh_password() { echo test-only-hash; }
panel_replace() { echo replace >> calls; }
panel_db_owner() { echo owner >> calls; }
curl() { [[ ! -e panel-down ]]; }
docker() {
  case "$1" in
    image) cat image-id ;;
    inspect)
      if [[ $* == *vpn2.spec* ]]; then
        [[ -e label ]] || return 1
        echo "$(cat label) true"
      else
        echo NUXT_NODE_HOSTS=ru=198.51.100.1
      fi ;;
    rm) echo rm >> calls ;;
    run) echo run >> calls; printf '%s\\n' "$@" > run.args ;;
  esac
}
'''


def decision():
    stage = source('infra/scripts/30-frontend.sh')
    return 'panel_container() {' + stage.split('panel_container() {', 1)[1].split('\n  panel_container\nfi\n', 1)[0] \
        + '\n  panel_container\nfi\n'


class PanelKeepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'anysda-config.yaml').write_text('admin: first\n')
        (self.root / 'image-id').write_text('sha256:first\n')
        self.script = SETUP + decision().replace('/etc/anysda', str(self.root))

    def tearDown(self):
        self.tmp.cleanup()

    def deploy(self):
        """Один проход решения; метку запущенного контейнера запоминает как docker."""
        calls = self.root / 'calls'
        calls.unlink(missing_ok=True)
        result = bash(self.script, self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        done = calls.read_text().split() if calls.exists() else []
        if 'run' in done:
            args = (self.root / 'run.args').read_text().splitlines()
            label = next(arg for arg in args if arg.startswith('vpn2.spec='))
            self.assertRegex(label, r'^vpn2\.spec=[0-9a-f]{64}$')
            (self.root / 'label').write_text(label.split('=', 1)[1])
        return done, result.stdout

    def test_first_deploy_starts_panel_and_repeat_keeps_it(self):
        self.assertEqual(self.deploy()[0], ['replace', 'owner', 'rm', 'run'])
        args = (self.root / 'run.args').read_text().splitlines()
        self.assertIn('NUXT_PREVIOUS_NODE_HOSTS=ru=198.51.100.1', args)
        done, out = self.deploy()
        self.assertEqual(done, [])
        self.assertIn('панель не изменилась', out)

    def test_changed_inputs_replace_panel(self):
        changes = {
            'config': lambda: (self.root / 'anysda-config.yaml').write_text('admin: second\n'),
            'image': lambda: (self.root / 'image-id').write_text('sha256:second\n'),
            'argument': lambda: setattr(self, 'script', self.script.replace('ENTRY_HOST=192.0.2.1', 'ENTRY_HOST=192.0.2.9')),
        }
        self.deploy()
        for name, change in changes.items():
            with self.subTest(change=name):
                before = (self.root / 'label').read_text()
                change()
                self.assertEqual(self.deploy()[0], ['replace', 'owner', 'rm', 'run'])
                self.assertNotEqual((self.root / 'label').read_text(), before)
                self.assertEqual(self.deploy()[0], [])

    def test_stopped_or_silent_panel_is_replaced(self):
        self.deploy()
        (self.root / 'panel-down').touch()
        self.assertEqual(self.deploy()[0], ['replace', 'owner', 'rm', 'run'])
        (self.root / 'panel-down').unlink()
        (self.root / 'label').unlink()
        self.assertEqual(self.deploy()[0], ['replace', 'owner', 'rm', 'run'])


class PanelTransactionTests(unittest.TestCase):
    def run_steps(self, steps):
        library = f"source {ROOT / 'infra/lib/panel-transaction.sh'}"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'caddy.conf').write_text('before\n')
            script = library + f'''
panel_state={root}/state
PANEL_IMAGE=new
python3() {{ echo "python3 $*" >> {root}/calls; }}
docker() {{ echo "docker $*" >> {root}/calls; [[ $1 != inspect ]]; }}
panel_health() {{ return 0; }}
''' + steps
            result = bash(script, root)
            calls = (root / 'calls').read_text() if (root / 'calls').exists() else ''
            state = sorted(str(path.relative_to(root / 'state')) for path in (root / 'state').rglob('*'))
            return result, calls, state, (root / 'caddy.conf').read_text()

    def test_kept_panel_leaves_no_backup_stop_or_file_copies(self):
        result, calls, state, _ = self.run_steps(
            'panel_begin\npanel_guard_files "$PWD/caddy.conf"\necho after > caddy.conf\npanel_finish\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(' backup', calls)
        self.assertNotIn('docker', calls)
        self.assertEqual(state, ['transaction.lock'])

    def test_failure_before_replace_restores_files_and_keeps_panel(self):
        result, calls, state, caddy = self.run_steps(
            'panel_begin\npanel_guard_files "$PWD/caddy.conf"\necho after > caddy.conf\nfalse\n')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(caddy, 'before\n')
        self.assertNotIn('docker', calls)
        self.assertNotIn('no previous release', result.stderr)
        self.assertEqual(state, ['transaction.lock'])

    def test_replace_keeps_file_copies_for_rollback(self):
        result, calls, state, _ = self.run_steps(
            'panel_begin\npanel_guard_files "$PWD/caddy.conf"\necho after > caddy.conf\npanel_replace\npanel_finish\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(' backup', calls)
        self.assertTrue(any(name.startswith('files/') and name.endswith('caddy.conf') for name in state), state)
        self.assertFalse(any(name.startswith('files.pending') for name in state), state)


if __name__ == '__main__':
    unittest.main()
