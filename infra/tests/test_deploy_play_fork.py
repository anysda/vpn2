"""Развилка стадии: плей ansible, если он есть, иначе прежний bash-скрипт."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
DEPLOY = (ROOT / 'infra/deploy.sh').read_text()


def function(name):
    body = DEPLOY.split(f'{name}() {{', 1)[1].split('\n}', 1)[0]
    return f'{name}() {{' + body + '\n}\n'


def fw_guard_stages():
    return next(line for line in DEPLOY.splitlines() if line.startswith('FW_GUARD_STAGES=')) + '\n'


def stage_args_check():
    """Проверка аргументов стадии из deploy.sh до открытия выпуска."""
    head = "case \"${1:-}\" in\n  ''|recover|backup|backup-list|restore|rollback) ;;"
    body = DEPLOY.split(head, 1)[1].split('\nesac', 1)[0]
    return head + body + '\nesac\n'


def bash(script, directory, env=None):
    return subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=directory,
                          text=True, capture_output=True, timeout=15,
                          env=dict(os.environ, **(env or {})))


STUBS = '''
DEPLOY_ROOT=$PWD
die() { echo "die: $*" >&2; exit 1; }
log() { :; }
ok() { :; }
warn() { :; }
load_env() { echo "load_env $1" >> calls; }
push() { echo "push $*" >> calls; }
ssh_exec() { echo "ssh_exec" >> calls; }
fw_guard_stage() { return 1; }
'''

# После библиотеки: колесо экзита и обвес плея без узла.
PLAY_STUBS = '''
: > wheel.whl
ansible_wheel_path() { echo "$PWD/wheel.whl"; }
load_env() { HOST_TAG=$1; }
push() { :; }
ssh_exec() { echo "ssh_exec $*" >> ssh; }
'''


class PlayForkTests(unittest.TestCase):
    def tree(self, directory, *plays):
        root = Path(directory)
        (root / 'scripts').mkdir()
        (root / 'scripts/42-x.sh').touch()
        (root / 'ansible/plays').mkdir(parents=True)
        for play in plays:
            (root / 'ansible/plays' / f'{play}.yml').write_text('- hosts: all\n')
        return root

    def test_stage_with_play_goes_to_run_play_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.tree(directory, '42-x')
            script = STUBS + function('run_stage_on_host') + '''
run_play() { echo "run_play $*" >> calls; }
run_stage_on_host 42-x ru
'''
            result = bash(script, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'calls').read_text().splitlines(), ['run_play 42-x ru'])

    def test_play_failure_is_stage_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            self.tree(directory, '42-x')
            script = STUBS + function('run_stage_on_host') + '''
run_play() { return 3; }
rc=0; run_stage_on_host 42-x ru || rc=$?
echo "rc=$rc"
'''
            result = bash(script, directory)
            self.assertEqual(result.stdout.strip(), 'rc=3', result.stderr)

    def test_stage_without_play_keeps_bash_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.tree(directory)
            script = STUBS + function('run_stage_on_host') + '''
run_play() { echo "run_play $*" >> calls; }
mkdir -p envs; touch envs/all.env envs/ru.env
run_stage_on_host 42-x ru
'''
            result = bash(script, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = (root / 'calls').read_text().splitlines()
            self.assertEqual(calls[0], 'load_env ru')
            self.assertNotIn('run_play', '\n'.join(calls))
            self.assertIn(f'push {root}/scripts/42-x.sh', calls)
            self.assertEqual(calls[-1], 'ssh_exec')

    def test_fw_guard_stage_play_runs_between_arm_and_disarm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.tree(directory, '00-bootstrap')
            venv = root / 'vpn2/venv'
            (venv / 'bin').mkdir(parents=True)
            (venv / '.vpn2-pins').write_text('x\n')
            playbook = venv / 'bin/ansible-playbook'
            playbook.write_text('#!/bin/sh\necho "play wheel=$VPN2_WHEEL_SHA256 dev=$VPN2_DEVELOPMENT" >> ssh\n')
            playbook.chmod(0o755)
            script = (STUBS + f'source {ROOT}/infra/lib/ansible-venv.sh\n' + PLAY_STUBS
                      + 'FW_GUARD_WINDOW=90\n'
                      + 'fw_guard_stage() { return 0; }\n'
                      + 'fw_guard_entry_baseline() { echo baseline >> ssh; }\n'
                      + 'fw_guard_beat() { sleep 30; }\n'
                      + 'fw_guard_disarm() { echo "disarm $1" >> ssh; }\n'
                      + 'run_play 00-bootstrap nl\n')
            result = bash(script, directory, {'VPN2_ROOT': str(root / 'vpn2')})
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = (root / 'ssh').read_text().splitlines()
            self.assertIn("apt_prepare; apt_auto_pause'", calls[1])
            empty = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
            self.assertEqual(calls[2:], [
                'ssh_exec bash --norc /tmp/anysda/fw-guard.sh arm 00-bootstrap 90',
                f'play wheel={empty} dev=false', 'disarm 00-bootstrap', 'ssh_exec rm -rf /tmp/anysda'])

    def test_run_play_command_line_prefix_and_code(self):
        for rc in (0, 2):
            with self.subTest(rc=rc), tempfile.TemporaryDirectory() as directory:
                root = self.tree(directory, '42-x')
                venv = root / 'vpn2/venv'
                (venv / 'bin').mkdir(parents=True)
                (venv / '.vpn2-pins').write_text('x\n')
                playbook = venv / 'bin/ansible-playbook'
                playbook.write_text('#!/bin/sh\necho "cfg=$ANSIBLE_CONFIG conf=$VPN2_CONFIG"\n'
                                    f'echo "args=$*"\nexit {rc}\n')
                playbook.chmod(0o755)
                script = (STUBS + fw_guard_stages()
                          + f'source {ROOT}/infra/lib/ansible-venv.sh\n' + PLAY_STUBS
                          + 'ansible_venv_start() { echo start >> calls; }\n'
                          + 'rc=0; run_play 42-x nl || rc=$?; echo "rc=$rc"\n')
                result = bash(script, directory, {'VPN2_ROOT': str(root / 'vpn2')})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), [
                    f'[nl]cfg={root}/ansible/ansible.cfg conf={root}/../config.yaml',
                    f'[nl]args={root}/ansible/plays/42-x.yml --limit nl --tags 42-x',
                    f'rc={rc}'])
                self.assertFalse((root / 'calls').exists(), 'venv готов - установка не нужна')

    def test_run_play_in_host_subshell_does_not_wait_parent_pid(self):
        # run_stage гонит хосты субшеллами: фон установки родителя wait там не ждёт.
        with tempfile.TemporaryDirectory() as directory:
            root = self.tree(directory, '42-x')
            playbook = root / 'vpn2/venv/bin/ansible-playbook'
            playbook.parent.mkdir(parents=True)
            playbook.write_text('#!/bin/sh\necho played\n')
            playbook.chmod(0o755)
            script = (STUBS + fw_guard_stages()
                      + f'source {ROOT}/infra/lib/ansible-venv.sh\n' + PLAY_STUBS
                      + 'ansible_wheel_build() { :; }\n'
                      + 'ansible_venv_install() { echo install >> calls; }\n'
                      + 'sleep 1 & _VENV_PID=$!\n'
                      + '( run_play 42-x nl ) & wait $!\n')
            result = bash(script, directory, {'VPN2_ROOT': str(root / 'vpn2')})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ['[nl]played'])
            self.assertEqual((root / 'calls').read_text().splitlines(), ['install'])
            self.assertEqual(list((root / 'vpn2').glob('ansible-venv.log*')), [])
            self.assertNotIn('пины те же', result.stdout + result.stderr)

    def test_run_play_in_host_subshell_failed_install_dies_with_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.tree(directory, '42-x')
            tmp = Path(directory) / 'tmp'
            tmp.mkdir()
            playbook = root / 'vpn2/venv/bin/ansible-playbook'
            playbook.parent.mkdir(parents=True)
            playbook.write_text('#!/bin/sh\necho played\n')
            playbook.chmod(0o755)
            script = (STUBS + fw_guard_stages()
                      + f'source {ROOT}/infra/lib/ansible-venv.sh\n' + PLAY_STUBS
                      + 'ansible_wheel_build() { :; }\n'
                      + 'ansible_venv_install() { echo pip-boom; return 1; }\n'
                      + 'rc=0; ( run_play 42-x nl ) || rc=$?; echo "rc=$rc"\n')
            result = bash(script, directory, {'VPN2_ROOT': str(root / 'vpn2'), 'TMPDIR': str(tmp)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ['rc=1'])
            self.assertIn('pip-boom', result.stderr)
            self.assertIn('die: ansible: venv не установился', result.stderr)
            self.assertEqual(list(tmp.iterdir()), [], 'временный файл не удалён')
            self.assertEqual(list((root / 'vpn2').glob('ansible-venv.log*')), [])

    def test_run_play_starts_venv_when_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.tree(directory, '42-x')
            script = (STUBS + fw_guard_stages()
                      + f'source {ROOT}/infra/lib/ansible-venv.sh\n' + PLAY_STUBS
                      + 'ansible_venv_start() { echo start >> calls; }\n'
                      + 'ansible_venv_wait() { echo wait >> calls; }\n'
                      + 'run_play 42-x nl || true\n')
            result = bash(script, directory, {'VPN2_ROOT': str(root / 'vpn2')})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'calls').read_text().splitlines(), ['start', 'wait'])

    def test_stage_args_accept_play_only_and_reject_neither(self):
        for stage, ok in (('42-x', True), ('43-y', True), ('44-z', False)):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'scripts').mkdir()
                (root / 'scripts/42-x.sh').touch()
                (root / 'ansible/plays').mkdir(parents=True)
                (root / 'ansible/plays/43-y.yml').touch()
                script = (STUBS + 'expand_hosts() { :; }\nset -- ' + stage + ' ru\n'
                          + stage_args_check() + 'echo passed\n')
                result = bash(script, directory)
                if ok:
                    self.assertEqual(result.stdout.strip(), 'passed', result.stderr)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('die: нет такой стадии: 44-z', result.stderr)

    def test_prereqs_start_venv_only_on_full_run(self):
        for args, started in (('', False), ('venv', True)):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as directory:
                script = (STUBS + function('install_prereqs')
                          + 'apt_have() { return 0; }\n'
                          + 'ansible_venv_start() { echo start >> calls; }\n'
                          + f'C_B= C_G= C_END=\ninstall_prereqs {args} >/dev/null\n'
                          + 'touch calls\n')
                result = bash(script, directory)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual('start' in (Path(directory) / 'calls').read_text(), started)


if __name__ == '__main__':
    unittest.main()
