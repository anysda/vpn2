"""Вывод 99-verify в файл не теряет строк узлов.

99-verify идёт по узлам параллельно, вывод каждого узла копирует tee, и все
копии пишут в один stdout. Когда stdout - обычный файл (`deploy.sh > log`,
журнал CI), tee с splice берёт позицию файла без блокировки, и два узла пишут
по одному смещению: строка одного затирает строку другого. Настоящие run_stage
и run_stage_on_host с настоящим infra/lib/ssh.sh, ssh подменён.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_deploy import source
from test_stage_exit_login_hang import FAKE_SSH, FAKE_SSHPASS, STUBS

EXITS = tuple(f'x{i}' for i in range(8))
LINES = 300
STAGE = f'''#!/bin/bash
set -euo pipefail
source "$1"
for i in $(seq {LINES}); do echo "[$HOST_TAG] проверка $i"; done
echo "[$HOST_TAG] ✓ стадия $(basename "$0" .sh) пройдена"
'''


def deploy():
    with tempfile.TemporaryDirectory(prefix='stage-parallel-output-') as directory:
        top = Path(directory)
        root = top / 'infra'
        for sub in ('envs', 'scripts', 'lib', 'configs', 'bin', 'nodes', 'tmp'):
            (root / sub).mkdir(parents=True)
        (top / 'app/shared').mkdir(parents=True)
        (top / 'app/shared/vpn-pools.json').write_text('{}\n')
        exits = ' '.join(EXITS)
        (root / 'envs/all.env').write_text(f"EXIT_TAGS='{exits}'\nENTRY_HOST=127.0.0.1\n")
        (root / 'envs/exits.env').write_text(f"EXIT_TAGS='{exits}'\n")
        for tag in ('ru',) + EXITS:
            (root / f'envs/{tag}.env').write_text(
                f'HOST_TAG={tag}\nSSH_HOST={tag}.example\nSSH_USER=root\nSSH_PASS=fixture\n')
        (root / 'scripts/99-verify.sh').write_text(STAGE)
        (root / 'scripts/99-verify.sh').chmod(0o755)
        (root / 'lib/ssh.sh').write_text(source('infra/lib/ssh.sh'))
        (root / 'lib/anysda-svc.sh').write_text('apt_prepare() { :; }\n')
        (root / 'lib/ansible-venv.sh').write_text(source('infra/lib/ansible-venv.sh'))
        (root / 'lib/fw-guard.sh').write_text('exit 0\n')
        for path in STUBS:
            (root / path).touch()
        for name, text in (('ssh', FAKE_SSH), ('sshpass', FAKE_SSHPASS)):
            (root / 'bin' / name).write_text(text)
            (root / 'bin' / name).chmod(0o755)
        text = source('infra/deploy.sh')
        script = ('set -euo pipefail\nDEPLOY_ROOT=$PWD\nexport DEPLOY_ROOT\n'
                  'source "$DEPLOY_ROOT/lib/ssh.sh"\n'
                  + text[text.index('_STAGE_LOG='):text.index('# Без аргументов → полный pipeline.')]
                  + '\nC_R= C_G= C_Y= C_B= C_END=\nrun_stage 99-verify all\n')
        env = {k: v for k, v in os.environ.items()
               if k not in ('VPN2_RELEASE_MANIFEST', 'SKIPPED_EXIT_TAGS')}
        env.update({'PATH': f"{root / 'bin'}:{os.environ['PATH']}", 'TMPDIR': str(root / 'tmp'),
                    'SSH_PROBE_TIMEOUT': '60', 'FAKE_NODES': str(root / 'nodes'),
                    'FAKE_PIDS': str(root / 'pids'), 'FAKE_HANGS': str(root / 'hangs'),
                    'FAKE_SLEEP': '1'})
        log = root / 'out'
        with log.open('w') as handle:
            code = subprocess.run(['bash', '-c', script, 'deploy'], cwd=root, env=env,
                                  stdin=subprocess.DEVNULL, stdout=handle,
                                  stderr=subprocess.STDOUT, timeout=120).returncode
        return code, log.read_text()


class StageParallelOutputFileTests(unittest.TestCase):
    def test_every_node_line_reaches_shared_log_file(self):
        for attempt in range(3):
            code, out = deploy()
            self.assertEqual(code, 0, out[-3000:])
            lines = set(out.splitlines())
            lost = [f'[{tag}] проверка {i}' for tag in ('ru',) + EXITS
                    for i in range(1, LINES + 1) if f'[{tag}] проверка {i}' not in lines]
            self.assertEqual(lost[:5], [], f'прогон {attempt + 1}: потеряно {len(lost)} строк '
                             f'из {(len(EXITS) + 1) * LINES}')
            for tag in ('ru',) + EXITS:
                self.assertIn(f'[{tag}] ✓ стадия 99-verify пройдена', out)
                self.assertIn(f'[99-verify → {tag}] done', out)


if __name__ == '__main__':
    unittest.main()
