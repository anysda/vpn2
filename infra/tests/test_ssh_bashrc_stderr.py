"""ssh_exec и push не пропускают к вызывающему stderr .bashrc узла.

Метка _SSH_MARK отрезает то, что .bashrc печатает в stdout до команды, но
stderr ssh шёл к вызывающему целиком: `echo NOISE >&2` в .bashrc root экзита
давал строки шума в выводе deploy. Ошибки самой команды после метки и
сообщения самого ssh о соединении при этом должны оставаться видны.

«Узел» - настоящий /usr/sbin/sshd без root на свободном порту 127.0.0.1 с
ключом хоста, authorized_keys и конфигом во временном каталоге: у настоящего
sshd stdout и stderr идут разными каналами, и их порядок не тот, что у
подставного ssh. .bashrc лежит в своём HOME, который задаёт SetEnv HOME=;
bash, запущенный sshd, читает его тем же путём, что root на экзите.
Настоящие ~/.bashrc и ~/.ssh не трогаются.

sshpass подменён обёрткой над настоящим ssh с этим конфигом: пароля нет,
вход по ключу. Обёртка переносит /tmp/anysda узла во временный каталог и
ставит свои опции первыми (у ssh побеждает первое значение): без
мультиплексора, одна попытка соединения, порт и ключ из FAKE_PORT и
FAKE_KEY - так получаются отказ в соединении и отказ во входе. У sshd
выключен PerSourcePenalties: иначе после отказов во входе он рвёт следующие
соединения с 127.0.0.1 ещё до обмена ключами.
"""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

from test_deploy import ROOT

SSHD = '/usr/sbin/sshd'
NOISE = 'NOISE-bashrc'
BASHRC = {
    'noise': f'echo {NOISE}-out\necho {NOISE}-err >&2\necho {NOISE}-err2 >&2\n',
    'clean': '',
    'exit': f'echo Welcome\necho {NOISE}-why >&2\nexit 0\n',
}
REASON = r'не дош[её]л до команды'
FAKE_SSHPASS = r'''#!/bin/bash
shift 2
args=()
for a in "$@"; do args+=("${a//\/tmp\/anysda/$FAKE_NODE/anysda}"); done
exec ssh -F "$FAKE_ROOT/ssh_config" -o ControlMaster=no -o ControlPath=none \
  -o ConnectionAttempts=1 -p "${FAKE_PORT:-$FAKE_SSHD_PORT}" \
  -i "$FAKE_ROOT/${FAKE_KEY:-client}" "${args[@]}"
'''


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class SshBashrcStderrTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (os.access(SSHD, os.X_OK) and shutil.which('ssh') and shutil.which('ssh-keygen')):
            raise AssertionError(f'нужны {SSHD}, ssh и ssh-keygen (openssh-server и openssh-client)')
        cls.tmp = tempfile.TemporaryDirectory(prefix='ssh-bashrc-stderr-')
        root = cls.root = Path(cls.tmp.name)
        cls.home = root / 'home'
        cls.home.mkdir()
        for name in ('bin', 'node', 'deploy'):
            (root / name).mkdir()
        for key in ('hostkey', 'otherhost', 'client', 'stranger'):
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(root / key)], check=True)
        (root / 'authorized_keys').write_text((root / 'client.pub').read_text())
        cls.port = free_port()
        config = (f'Port {cls.port}\nListenAddress 127.0.0.1\nHostKey {root}/hostkey\n'
                  f'PidFile {root}/sshd.pid\nAuthorizedKeysFile {root}/authorized_keys\n'
                  'StrictModes no\nUsePAM no\nPasswordAuthentication no\n'
                  'KbdInteractiveAuthentication no\nPermitUserRC no\nPermitUserEnvironment no\n'
                  'MaxStartups 100\n'
                  f'SetEnv HOME={cls.home}\n')
        # PerSourcePenalties есть с OpenSSH 9.8; более старый sshd на ней выходит, штрафов у него нет
        (root / 'sshd_config').write_text(config + 'PerSourcePenalties no\n')
        if subprocess.run([SSHD, '-t', '-f', str(root / 'sshd_config')], capture_output=True).returncode:
            (root / 'sshd_config').write_text(config)
        (root / 'ssh_config').write_text(
            'Host *\n  HostName 127.0.0.1\n  IdentitiesOnly yes\n  IdentityAgent none\n'
            '  GlobalKnownHostsFile /dev/null\n')
        sshpass = root / 'bin' / 'sshpass'
        sshpass.write_text(FAKE_SSHPASS)
        sshpass.chmod(0o755)
        cls.log = open(root / 'sshd.log', 'w')
        cls.sshd = subprocess.Popen([SSHD, '-D', '-e', '-f', str(root / 'sshd_config')],
                                    stdin=subprocess.DEVNULL, stdout=cls.log, stderr=cls.log)
        deadline = time.monotonic() + 10
        while True:
            try:
                socket.create_connection(('127.0.0.1', cls.port), timeout=1).close()
                break
            except OSError:
                if cls.sshd.poll() is not None or time.monotonic() > deadline:
                    log = (root / 'sshd.log').read_text()
                    cls.tearDownClass()
                    raise AssertionError('sshd не поднялся:\n' + log)
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.sshd.terminate()
        try:
            cls.sshd.wait(5)
        except subprocess.TimeoutExpired:
            cls.sshd.kill()
            cls.sshd.wait()
        cls.log.close()
        cls.tmp.cleanup()

    def run_ssh(self, body, bashrc='noise', tag='us', known_hosts='', **env):
        (self.home / '.bashrc').write_text(BASHRC[bashrc])
        (self.root / 'deploy' / '.known_hosts').write_text(known_hosts)
        env = {**os.environ, 'PATH': f'{self.root}/bin:{os.environ["PATH"]}',
               'FAKE_ROOT': str(self.root), 'FAKE_NODE': str(self.root / 'node'),
               'FAKE_SSHD_PORT': str(self.port), 'HOST_TAG': tag, **env}
        script = f'''DEPLOY_ROOT={self.root}/deploy
source {ROOT}/infra/lib/ssh.sh
sleep() {{ :; }}
SSH_USER=$(id -un) SSH_HOST=node.example SSH_PASS=fixture SSH_PROBE_TIMEOUT=10
''' + body
        return subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=self.root,
                              env=env, text=True, capture_output=True, timeout=60,
                              stdin=subprocess.DEVNULL)

    def assert_no_noise(self, result):
        self.assertNotIn(NOISE, result.stdout, 'stdout .bashrc узла дошёл до вызывающего')
        self.assertNotIn(NOISE, result.stderr, 'stderr .bashrc узла дошёл до вызывающего')

    def test_exec_and_push_hide_bashrc_stderr(self):
        src = self.root / 'payload'
        src.write_text('data\n')
        written = self.root / 'node' / 'anysda' / 'payload'
        for tag in ('us', 'ru'):
            with self.subTest(tag=tag):
                written.unlink(missing_ok=True)
                result = self.run_ssh(f'''out=$(ssh_exec 'echo hi')
[[ $out == hi ]] || {{ echo "bad-exec $out" >&2; exit 1; }}
push {src}
echo all-clean
''', tag=tag)
                self.assert_no_noise(result)
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (0, 'all-clean\n', ''))
                self.assertEqual(written.read_text(), 'data\n')

    def test_command_stderr_and_code_after_marker_stay(self):
        for bashrc in ('noise', 'clean'):
            for tag in ('us', 'ru'):
                with self.subTest(bashrc=bashrc, tag=tag):
                    result = self.run_ssh('''rc=0; ssh_exec 'echo out; echo err >&2; exit 3' || rc=$?
echo "rc=$rc"
''', bashrc=bashrc, tag=tag)
                    self.assert_no_noise(result)
                    self.assertEqual((result.returncode, result.stdout, result.stderr),
                                     (0, 'out\nrc=3\n', 'err\n'),
                                     'stderr или код упавшей команды узла потерян')

    def test_without_marker_behaviour_is_unchanged(self):
        result = self.run_ssh('''rc=0; ssh_exec 'echo hi' || rc=$?
echo "rc=$rc"
''', bashrc='exit')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'Welcome\nrc=254\n')
        self.assertRegex(result.stderr, REASON)
        self.assertIn(f'{NOISE}-why', result.stderr, 'без метки вывод .bashrc - единственная подсказка оператору')
        self.assertNotIn('повтор', result.stderr)

    def test_ssh_connection_errors_stay_visible(self):
        stranger = (self.root / 'otherhost.pub').read_text().split()
        changed = f'[127.0.0.1]:{self.port} {stranger[0]} {stranger[1]}\n'
        cases = (
            ('refused', {'FAKE_PORT': str(free_port())}, '', 'Connection refused', 'net', 'повтор 4/4'),
            ('denied', {'FAKE_KEY': 'stranger'}, '', 'Permission denied', 'auth', 'отказала во входе'),
            ('hostkey', {}, changed, 'REMOTE HOST IDENTIFICATION HAS CHANGED', 'hostkey', 'сменился host-key'),
        )
        for name, env, known, text, kind, warning in cases:
            for helper in ('ssh_exec', 'push'):
                with self.subTest(case=name, helper=helper):
                    arg = "'echo hi'" if helper == 'ssh_exec' else __file__
                    result = self.run_ssh(f'''rc=0; err=$({helper} {arg} 2>&1 >/dev/null) || rc=$?
printf '%s\\n' "$err" >&2
echo "rc=$rc kind=$(ssh_fail_kind "$rc" "$err")"
''', known_hosts=known, **env)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, f'rc=255 kind={kind}\n', result.stderr)
                    self.assertIn(text, result.stderr, 'сообщение самого ssh о соединении пропало')
                    self.assertIn(warning, result.stderr)


if __name__ == '__main__':
    unittest.main()
