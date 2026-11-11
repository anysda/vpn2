"""Роль adguard: файлы и юниты равны эталону слепка entry, плей привязан к стадии 22."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

from play_stage import site_play

from adguard_support import render_unit, stage

ROOT = Path(os.environ.get('VPN2_AUDIT_ROOT', Path(__file__).resolve().parents[2]))
ROLE = ROOT / 'infra/ansible/roles/adguard'
SNAPSHOTS = ROOT / 'infra/tests/fixtures/snapshot'
FILES = {
    'anysda-doh-routing': ('/usr/local/sbin/anysda-doh-routing', '0755'),
    'anysda-doh-routing.service': ('/etc/systemd/system/anysda-doh-routing.service', '0644'),
}
UNITS = ('adguardhome', 'adguardhome-unfiltered')


def snapshots():
    found = sorted(SNAPSHOTS.glob('ubuntu-*-entry.json.gz'))
    assert found
    return [(path.name, json.loads(gzip.decompress(path.read_bytes()))['files']) for path in found]


class AdguardRoleTests(unittest.TestCase):
    def test_role_files_match_entry_snapshot(self):
        self.assertEqual(sorted(path.name for path in (ROLE / 'files').iterdir()), sorted(FILES))
        for reference, files in snapshots():
            for name, (target, mode) in FILES.items():
                with self.subTest(reference=reference, file=name):
                    digest = hashlib.sha256((ROLE / 'files' / name).read_bytes()).hexdigest()
                    self.assertEqual(files[target]['sha256'], digest)
                    self.assertEqual(files[target]['mode'], mode)

    def test_units_render_to_snapshot_on_stand_machine(self):
        # Эталон снят на машине стенда с 4 ГБ: лимиты памяти там сняты.
        profile = stage.limits({'VPN2_ENTRY_RESOURCES': str(ROOT / 'infra/ansible/roles/resources/files/entry_resources.py')},
                               '--ram-mib', '4096', '--free-mib', '8192')
        for reference, files in snapshots():
            for unit in UNITS:
                with self.subTest(reference=reference, unit=unit):
                    text = render_unit(unit, profile)
                    target = files[f'/etc/systemd/system/{unit}.service']
                    self.assertEqual(target['sha256'], hashlib.sha256(text.encode()).hexdigest())
                    self.assertEqual(target['mode'], '0644')

    def test_tasks_install_with_snapshot_modes_and_report_changes(self):
        tasks = yaml.safe_load((ROLE / 'tasks/main.yml').read_text())
        copied = {task['ansible.builtin.copy'].get('src'): task['ansible.builtin.copy']
                  for task in tasks if 'ansible.builtin.copy' in task}
        for name, (target, mode) in FILES.items():
            self.assertEqual((copied[name]['dest'], copied[name]['mode']), (target, mode))
        steps = {}
        for task in tasks:
            argv = str(task.get('ansible.builtin.command', {}).get('argv'))
            if 'adguard_step' in argv:
                steps[argv.split("'")[1]] = task
        self.assertEqual(list(steps), ['binary', 'config', 'limits', 'restart'])
        for name, task in steps.items():
            # Шаг сам говорит, менял ли он хост: повтор роли остаётся без изменений.
            expected = False if name == 'limits' else f"'changed=1' in {task['register']}.stdout"
            self.assertEqual(task['changed_when'], expected)

    def test_play_runs_role_under_stage_tag(self):
        play = site_play('22-adguard')
        self.assertEqual((play['hosts'], play['tags'], play['roles']), ('entry', ['22-adguard'], ['adguard']))

    def test_restore_wrapper_runs_config_and_restart(self):
        # Восстановление зовёт infra/scripts/22-adguard.sh с env-файлом: без установки, только конфиги.
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / 'vpn2').write_text('#!/bin/sh\necho "$* port=$AGH_PORT pass=${ADMIN_PASSWORD-unset} '
                                       'res=$VPN2_ENTRY_RESOURCES pools=$VPN2_POOLS" >> "$CALLS"\n')
            (work / 'vpn2').chmod(0o755)
            wrapper = ROOT / 'infra/scripts/22-adguard.sh'
            self.assertTrue(os.access(wrapper, os.X_OK))
            for tag, calls in (('ru', 2), ('se', 0)):
                (work / 'calls').write_text('')
                (work / 'stage.env').write_text(f'HOST_TAG={tag}\nAGH_PORT=3000\nunset ADMIN_PASSWORD\n')
                result = subprocess.run(
                    ['bash', str(wrapper), str(work / 'stage.env')], capture_output=True, text=True,
                    env=dict(os.environ, VPN2_BIN=str(work / 'vpn2'), CALLS=str(work / 'calls'), ADMIN_PASSWORD='x'))
                self.assertEqual(result.returncode, 0, result.stderr)
                lines = (work / 'calls').read_text().splitlines()
                self.assertEqual(len(lines), calls)
                if calls:
                    self.assertEqual([line.split()[:2] for line in lines],
                                     [['adguard', 'config'], ['adguard', 'restart']])
                    self.assertIn('port=3000 pass=unset', lines[0])
                    self.assertIn(f'res={ROOT}/infra/ansible/roles/resources/files/entry_resources.py pools={ROOT}/app/shared/vpn-pools.json',
                                  lines[0])


if __name__ == '__main__':
    unittest.main()
