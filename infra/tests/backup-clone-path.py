#!/usr/bin/env python3
"""Клон не в /opt: restore, backup и speedtest берут исходники из клона деплоя.

VPN2_BACKUP_TEST=1 python3 infra/tests/backup-clone-path.py /path/to/repository
Только в одноразовом контейнере, как backup-restore.py (его фикстура общая).
Клон отмечает настоящий run_stage_on_host 26-backup ru из deploy.sh: ssh_exec и
push выполняются здесь же, как деплой с самой entry, и стадия ставит restore и
backup в /usr/local/bin, откуда путь клона не виден.
"""

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import unittest


_spec = importlib.util.spec_from_file_location(
    'backup_restore', Path(__file__).with_name('backup-restore.py'))
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)  # забирает путь репозитория из argv
REPO = fixture.REPO
OPT = Path('/opt/anysda-vpn2')
CLONE = Path('/root/vpn2')
STAGES = ('22-adguard', '30-frontend', '35-telegram', '99-verify')
OUT = Path('/test-output')


def deploy_functions():
    """Все функции deploy.sh без его верхнего уровня (тот сразу запускает деплой)."""
    text = (REPO / 'infra/deploy.sh').read_text()
    return '\n'.join(re.findall(r'^[A-Za-z_][\w-]*\(\) \{\n.*?^\}$', text, re.M | re.S)) + '\n'


def make_source(root):
    """Исходники как у клона: настоящие infra и миграции, стадии restore - заглушки."""
    root.mkdir(parents=True)
    shutil.copytree(REPO / 'infra', root / 'infra',
                    ignore=shutil.ignore_patterns('secrets', 'envs', '__pycache__'))
    shutil.copytree(REPO / 'app/shared', root / 'app/shared')
    shutil.copytree(REPO / 'app/server/database/migrations', root / 'app/server/database/migrations')
    for stage in STAGES:
        fixture.write(root / 'infra/scripts' / (stage + '.sh'), '''#!/bin/sh
printf '%s|%s|%s\\n' "$0" "$(cat /tmp/anysda/anysda-config.yaml.tpl)" "$(grep '^SOURCE=' "$1")" >> /test-output/stages
''', 0o755)
    fixture.write(root / 'infra/configs/anysda-config.yaml.tpl', f'template from {root}')
    fixture.write(root / 'infra/envs/all.env', f'SOURCE={root}\n')
    fixture.write(root / 'infra/envs/ru.env', '''HOST_TAG=ru
BACKUP_ENABLED=true
BACKUP_BACKEND=local
BACKUP_LOCAL_DIR=/var/backups/anysda-vpn2
BACKUP_RETENTION=2
BACKUP_SCHEDULE=off
BACKUP_PASSPHRASE=test-backup-passphrase
''')
    host = root.name + '.example.test'
    fixture.write(root / 'config.yaml', f"admin:\n  user: 'clone-admin'\npanel:\n  domain: '{host}'\n")
    git = ('git', '-C', str(root), '-c', 'user.name=test', '-c', 'user.email=test@example.test')
    for args in (('init', '-q'), ('add', '-A'), ('commit', '-qm', 'source')):
        result = fixture.run(*git, *args)
        assert result.returncode == 0, result.stdout
    return fixture.run(*git, 'rev-parse', '--short', 'HEAD').stdout.strip()


class ClonePath(unittest.TestCase):
    def setUp(self):
        for path in ('/etc/anysda', OPT, CLONE, '/tmp/anysda', '/usr/local/lib/anysda'):
            shutil.rmtree(path, ignore_errors=True)
        for name in ('backup', 'restore', 'backup-list'):
            Path(f'/usr/local/bin/anysda-{name}.sh').unlink(missing_ok=True)
        Path('/root/.speedtest-panel.jar').unlink(missing_ok=True)
        Path('/etc/systemd/system').mkdir(parents=True, exist_ok=True)
        fixture.BackupRestore.setUp(self)
        # Фикстура кладёт исходники в /opt; каждый случай ставит их сам.
        shutil.rmtree(OPT)

    def deploy(self):
        """Деплой из клона на этой же машине: стадия 26-backup через deploy.sh."""
        script = deploy_functions() + f'''
DEPLOY_ROOT={CLONE}/infra
load_env() {{ :; }}
log() {{ echo "$*"; }}
ok() {{ echo "$*"; }}
warn() {{ echo "$*" >&2; }}
die() {{ echo "$*" >&2; exit 1; }}
fw_guard_stage() {{ return 1; }}
ssh_exec() {{ bash --norc -c "$1"; }}
push() {{
  local target=/tmp/anysda/${{2:-$(basename "$1")}}
  mkdir -p /tmp/anysda && rm -rf "$target" && cp -a "$1" "$target"
}}
run_stage_on_host 26-backup ru
'''
        result = fixture.run('bash', '-euo', 'pipefail', '-c', script, cwd=CLONE, env=self.env)
        self.assertEqual(result.returncode, 0, result.stdout)

    def legacy_install(self):
        """Старая установка: скрипты стоят, отметки клона нет."""
        for name in ('backup', 'restore', 'backup-list'):
            target = Path(f'/usr/local/bin/anysda-{name}.sh')
            shutil.copy(OPT / f'infra/lib/anysda-{name}.sh', target)
            target.chmod(0o755)
        Path('/usr/local/lib/anysda').mkdir(parents=True)
        shutil.copy(OPT / 'infra/lib/anysda-svc.sh', '/usr/local/lib/anysda/anysda-svc.sh')
        shutil.copy(OPT / 'app/shared/vpn-pools.json', '/usr/local/lib/anysda/vpn-pools.json')

    def manifest(self):
        result = fixture.run('/usr/local/bin/anysda-backup.sh', env=self.env)
        self.assertEqual(result.returncode, 0, result.stdout)
        archive = max(fixture.BACKUPS.glob('*.age'), key=lambda p: p.stat().st_mtime)
        target = OUT / ('manifest-' + archive.stem)
        fixture.decrypt(archive, str(target) + '.tar.gz')
        with tarfile.open(str(target) + '.tar.gz') as tar:
            tar.extractall(target, filter='data')
        return json.loads(next(target.glob('anysda-vpn2-*/manifest.json')).read_text())

    def assert_restore_uses(self, root):
        self.manifest()
        stages = OUT / 'stages'
        stages.unlink(missing_ok=True)
        result = fixture.run('/usr/local/bin/anysda-restore.sh', 'latest', '--force', env=self.env)
        self.assertEqual(result.returncode, 0, result.stdout[-1500:])
        self.assertIn('anysda-restore: SUCCESS', result.stdout)
        self.assertEqual(stages.read_text().splitlines(),
                         [f'{root}/infra/scripts/{stage}.sh|template from {root}|SOURCE={root}'
                          for stage in STAGES])

    def assert_speedtest_uses(self, root):
        text = (REPO / 'infra/tools/speedtest.py').read_text()
        panel_call = text.split("PANEL_CALL = r'''", 1)[1].split("'''", 1)[0]
        calls = OUT / 'curl-calls'
        calls.unlink(missing_ok=True)
        fixture.write(OUT / 'speedtest-bin/curl', f'''#!/bin/sh
echo "$*" >> {calls}
echo '[{{"id":1,"name":"speed-test"}}]'
''', 0o755)
        result = subprocess.run(['bash', '-c', panel_call], text=True, capture_output=True,
                                env=dict(self.env, PATCHES='', PATH=f"{OUT}/speedtest-bin:{self.env['PATH']}"),
                                timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
        self.assertEqual(json.loads(result.stdout)[0]['id'], 1)
        self.assertIn(f'https://{root.name}.example.test/api/clients', calls.read_text())

    # 1. restore: исходники только в клоне, деплой шёл из него.
    def test_restore_takes_migrations_template_envs_stages_verify_from_clone(self):
        make_source(CLONE)
        self.deploy()
        self.assertFalse(OPT.exists())
        self.assert_restore_uses(CLONE)

    # 2. speedtest: infra/lib и config.yaml - из клона.
    def test_speedtest_panel_call_reads_clone(self):
        make_source(CLONE)
        self.deploy()
        self.assert_speedtest_uses(CLONE)

    # 3. backup: в манифесте коммит клона.
    def test_backup_manifest_has_clone_commit(self):
        commit = make_source(CLONE)
        self.deploy()
        self.assertEqual(self.manifest()['git_commit'], commit)

    # 4. Старая установка без отметки: всё из /opt, как раньше.
    def test_legacy_install_without_mark_uses_opt(self):
        commit = make_source(OPT)
        self.legacy_install()
        self.assertEqual(self.manifest()['git_commit'], commit)
        self.assert_restore_uses(OPT)
        self.assert_speedtest_uses(OPT)


if __name__ == '__main__':
    if os.environ.get('VPN2_BACKUP_TEST') != '1' or not Path('/.dockerenv').exists():
        sys.exit('Run only inside a disposable Docker container with VPN2_BACKUP_TEST=1')
    unittest.main(defaultTest=ClonePath.__name__, verbosity=2)
