"""Неверный SSH_PROBE_TIMEOUT отклоняется до открытия выпуска.

Возврат со стенда: `SSH_PROBE_TIMEOUT=0 ./deploy.sh` печатал FATAL про
SSH_PROBE_TIMEOUT уже после копий БД и записи выпуска в applying, и следующий
`./deploy.sh` упирался в «unfinished deploy». Тест гоняет настоящий
infra/deploy.sh из копии дерева. Подставной ssh выполняет на «ноде» во
временном каталоге только замок, проверку журнала, push и заглушку
deploy_state.py; остальные команды узла не выполняются.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from test_deploy import ROOT

FAKE_SSH = r'''#!/bin/bash
while (($#)); do
  case $1 in -[oiFpl]) shift 2 ;; -*) shift ;; *) shift; break ;; esac
done
begin=$1; shift
cmd="$*"
case $cmd in
  *'exec flock'*|*'unfinished deploy'*|'python3 /tmp/anysda/deploy_state.py '*|*"cat > '/tmp/anysda/"*|*'tar -xmf'*)
    cmd=${cmd//python3 \/tmp\/anysda\/deploy_state.py/python3 $FAKE_NODE/deploy_state.py}
    cmd=${cmd//\/var\/lib\/vpn2-deploy/$FAKE_NODE/lock}
    cmd=${cmd//\/tmp\/anysda/$FAKE_NODE/anysda}
    exec bash -c "$begin $cmd" ;;
esac
printf '%s\n' "$cmd" >> "$FAKE_NODE/skipped.log"
exec bash -c "$begin"
'''
FAKE_SSHPASS = '#!/bin/bash\nshift 2\nexec ssh "$@"\n'

# deploy_state.py узла: копия БД на backup и фаза выпуска в operation.json.
FAKE_STATE = r'''import json, os, shutil, sys
from pathlib import Path
node = Path(os.environ['FAKE_NODE'])
with (node / 'state.log').open('a') as log:
    log.write(' '.join(sys.argv[1:]) + '\n')
if sys.argv[1] == 'backup':
    copies = node / 'backups'
    copies.mkdir(exist_ok=True)
    copy = copies / f'db-{len(list(copies.iterdir()))}.sqlite'
    shutil.copyfile(node / 'data' / 'db.sqlite', copy)
    print(f'backup: {copy} verified')
elif sys.argv[1] == 'phase':
    (node / 'lock' / 'operation.json').write_text(json.dumps({'phase': sys.argv[3]}))
'''

NOOP_STAGE = '90-probe-noop'
BAD_VALUES = ('0', '1.5', 'abc')


class Tree:
    """Копия infra с envs entry и одного экзита, «нода» в том же каталоге."""

    def __init__(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        infra = self.root / 'infra'
        shutil.copytree(ROOT / 'infra', infra,
                        ignore=shutil.ignore_patterns('tests', 'envs', 'secrets', 'state'))
        (self.root / 'app/shared').mkdir(parents=True)
        shutil.copyfile(ROOT / 'app/shared/vpn-pools.json', self.root / 'app/shared/vpn-pools.json')
        (infra / 'scripts' / f'{NOOP_STAGE}.sh').write_text('true\n')
        envs = infra / 'envs'
        envs.mkdir()
        (envs / 'all.env').write_text('')
        (envs / 'exits.env').write_text('EXIT_TAGS=se\n')
        for host in ('ru', 'se'):
            (envs / f'{host}.env').write_text(f'SSH_USER=root\nSSH_HOST={host}.example\nSSH_PASS=fixture\n')
        self.node = self.root / 'node'
        for sub in ('lock', 'anysda', 'data'):
            (self.node / sub).mkdir(parents=True)
        (self.node / 'data' / 'db.sqlite').write_text('db')
        (self.node / 'deploy_state.py').write_text(FAKE_STATE)
        tools = self.root / 'bin'
        tools.mkdir()
        fakes = {'ssh': FAKE_SSH, 'sshpass': FAKE_SSHPASS}
        for name in ('scp', 'envsubst', 'curl', 'rsync', 'openssl'):
            if shutil.which(name) is None:
                fakes[name] = '#!/bin/sh\nexit 0\n'
        for name, text in fakes.items():
            (tools / name).write_text(text)
            (tools / name).chmod(0o755)
        home = self.root / 'home'
        home.mkdir()
        self.env = {key: value for key, value in os.environ.items() if key != 'SSH_PROBE_TIMEOUT'}
        self.env.update(PATH=f'{tools}:{os.environ["PATH"]}', HOME=str(home),
                        FAKE_NODE=str(self.node), VPN2_DEVELOPMENT='true')

    def deploy(self, timeout, *args):
        env = dict(self.env)
        if timeout is not None:
            env['SSH_PROBE_TIMEOUT'] = timeout
        return subprocess.run(['bash', str(self.root / 'infra/deploy.sh'), *args],
                              cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                              text=True, capture_output=True, timeout=60)

    def journal(self):
        return self.node / 'lock' / 'operation.json'

    def copies(self):
        folder = self.node / 'backups'
        return sorted(p.name for p in folder.iterdir()) if folder.exists() else []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.directory.cleanup()


class ProbeTimeoutBeforeReleaseTests(unittest.TestCase):
    def assert_rejected_before_release(self, tree, result, value):
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        fatal = [line for line in result.stderr.splitlines() if 'FATAL' in line]
        self.assertEqual(len(fatal), 1, output)
        self.assertIn(f"SSH_PROBE_TIMEOUT='{value}'", fatal[0], output)
        self.assertFalse(tree.journal().exists(),
                         f'выпуск открыт: {tree.journal().read_text() if tree.journal().exists() else ""}\n{output}')
        self.assertEqual(tree.copies(), [], output)
        self.assertNotIn('verified', output)
        self.assertNotIn('operation journal retained', output)

    def test_bad_value_rejected_before_release_and_next_deploy_runs(self):
        for args in ((), (NOOP_STAGE, 'ru')):
            for value in BAD_VALUES:
                with self.subTest(args=args, value=value), Tree() as tree:
                    self.assert_rejected_before_release(tree, tree.deploy(value, *args), value)
                    # Следующий запуск с верным значением идёт без recover.
                    fixed = tree.deploy('5', NOOP_STAGE, 'ru')
                    output = fixed.stdout + fixed.stderr
                    self.assertNotIn('unfinished deploy', output)
                    self.assertEqual(fixed.returncode, 0, output)
                    # Копия БД на каждой ноде: entry и экзит.
                    self.assertEqual(len(tree.copies()), 2, output)

    def test_full_deploy_after_bad_value_passes_release_check(self):
        with Tree() as tree:
            self.assert_rejected_before_release(tree, tree.deploy('0'), '0')
            fixed = tree.deploy(None)
            output = fixed.stdout + fixed.stderr
            self.assertNotIn('unfinished deploy', output)
            self.assertIn('phase --phase applying', (tree.node / 'state.log').read_text(), output)


if __name__ == '__main__':
    unittest.main()
