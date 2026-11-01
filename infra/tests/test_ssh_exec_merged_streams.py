"""ssh_exec, чьи stdout и stderr идут в один файл, отдаёт все байты обоих.

deploy пишет вывод стадий в журнал так: `run_stage_on_host ... >"$log" 2>&1`
(перекатка порта экзита и пересборка entry без исключённых экзитов), а внутри
стадия - ssh_exec. stderr ssh пишет в журнал сам, stdout уходит через
_ssh_output, и тот же журнал получает его вторым писателем. Две тысячи строк
в каждый поток должны дойти все, в том числе через `2>&1 | tee`, как у
стадий по узлам; код команды узла сохраняется.

«Узел» - настоящий /usr/sbin/sshd без root на свободном порту 127.0.0.1 с
ключом хоста, authorized_keys и конфигом во временном каталоге: у настоящего
sshd stdout и stderr идут разными каналами. sshpass подменён обёрткой над
настоящим ssh с этим конфигом: пароля нет, вход по ключу, без мультиплексора.
У sshd выключен PerSourcePenalties, если он его знает. Настоящие ~/.bashrc и
~/.ssh не трогаются: HOME узла задаёт SetEnv HOME=.
"""
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

from test_deploy import ROOT

SSHD = '/usr/sbin/sshd'
LINES = 2000
LOOP = f'for i in $(seq {LINES}); do echo o$i; echo e$i >&2; done; exit 7'
FAKE_SSHPASS = r'''#!/bin/bash
shift 2
exec ssh -F "$FAKE_ROOT/ssh_config" -o ControlMaster=no -o ControlPath=none \
  -o ConnectionAttempts=1 -p "$FAKE_SSHD_PORT" -i "$FAKE_ROOT/client" "$@"
'''
WAYS = {
    'файл, как deploy.sh: >"$log" 2>&1': f"rc=0; ssh_exec '{LOOP}' >\"$log\" 2>&1 || rc=$?",
    'канал, как deploy.sh: 2>&1 | tee': f"set +o pipefail; ssh_exec '{LOOP}' 2>&1 | tee \"$log\" >/dev/null; "
                                        'rc=${PIPESTATUS[0]}',
}


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class SshExecMergedStreamsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (os.access(SSHD, os.X_OK) and shutil.which('ssh') and shutil.which('ssh-keygen')):
            raise AssertionError(f'нужны {SSHD}, ssh и ssh-keygen (openssh-server и openssh-client)')
        cls.tmp = tempfile.TemporaryDirectory(prefix='ssh-exec-merged-')
        root = cls.root = Path(cls.tmp.name)
        for name in ('home', 'bin', 'deploy'):
            (root / name).mkdir()
        (root / 'home' / '.bashrc').write_text('')
        for key in ('hostkey', 'client'):
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(root / key)], check=True)
        (root / 'authorized_keys').write_text((root / 'client.pub').read_text())
        cls.port = free_port()
        config = (f'Port {cls.port}\nListenAddress 127.0.0.1\nHostKey {root}/hostkey\n'
                  f'PidFile {root}/sshd.pid\nAuthorizedKeysFile {root}/authorized_keys\n'
                  'StrictModes no\nUsePAM no\nPasswordAuthentication no\n'
                  'KbdInteractiveAuthentication no\nPermitUserRC no\nPermitUserEnvironment no\n'
                  f'SetEnv HOME={root}/home\n')
        # PerSourcePenalties есть с OpenSSH 9.8; более старый sshd на ней выходит
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

    def run_merged(self, way, tag):
        log = self.root / 'stage.log'
        log.unlink(missing_ok=True)
        (self.root / 'deploy' / '.known_hosts').write_text('')
        script = f'''DEPLOY_ROOT={self.root}/deploy
source {ROOT}/infra/lib/ssh.sh
SSH_USER=$(id -un) SSH_HOST=node.example SSH_PASS=fixture SSH_PROBE_TIMEOUT=10
log={log}
{WAYS[way]}
echo "rc=$rc"
'''
        env = {**os.environ, 'PATH': f'{self.root}/bin:{os.environ["PATH"]}',
               'FAKE_ROOT': str(self.root), 'FAKE_SSHD_PORT': str(self.port), 'HOST_TAG': tag}
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=self.root, env=env,
                                text=True, capture_output=True, timeout=120, stdin=subprocess.DEVNULL)
        return result, log.read_text() if log.exists() else ''

    def test_merged_output_keeps_every_line_and_code(self):
        want = sum(len(f'o{i}\ne{i}\n') for i in range(1, LINES + 1))
        for way in WAYS:
            for tag in ('us', 'ru'):
                for attempt in range(3):
                    with self.subTest(way=way, tag=tag, attempt=attempt):
                        result, text = self.run_merged(way, tag)
                        self.assertEqual((result.returncode, result.stdout), (0, 'rc=7\n'),
                                         result.stderr + (self.root / 'sshd.log').read_text()[-2000:])
                        lines = text.splitlines()
                        got = set(lines)
                        missing = [f'{s}{i}' for s in 'oe' for i in range(1, LINES + 1) if f'{s}{i}' not in got]
                        self.assertEqual(
                            (len(text.encode()), missing[:5], len(missing)), (want, [], 0),
                            f'журнал стадии потерял строки: o {sum(m[0] == "o" for m in missing)}, '
                            f'e {sum(m[0] == "e" for m in missing)}, обрывки '
                            f'{[s for s in lines if not re.fullmatch(r"[oe][0-9]+", s)][:5]}')


if __name__ == '__main__':
    unittest.main()
