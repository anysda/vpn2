"""Роль failover: файлы роли равны эталону слепка entry, плей привязан к стадии 21."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import unittest

import yaml

ROOT = Path(os.environ.get('VPN2_AUDIT_ROOT', Path(__file__).resolve().parents[2]))
ROLE = ROOT / 'infra/ansible/roles/failover'
SNAPSHOTS = ROOT / 'infra/tests/fixtures/snapshot'
INSTALLED = {
    'anysda-failover-watchdog.service': ('/etc/systemd/system/anysda-failover-watchdog.service', '0644'),
    'anysda-tproxy-watchdog.service': ('/etc/systemd/system/anysda-tproxy-watchdog.service', '0644'),
    'anysda-tproxy-watchdog.timer': ('/etc/systemd/system/anysda-tproxy-watchdog.timer', '0644'),
    'anysda-tproxy-watchdog.sh': ('/usr/local/sbin/anysda-tproxy-watchdog.sh', '0755'),
}


class FailoverRoleTests(unittest.TestCase):
    def test_role_files_match_entry_snapshot(self):
        self.assertEqual(sorted(path.name for path in (ROLE / 'files').iterdir()), sorted(INSTALLED))
        references = sorted(SNAPSHOTS.glob('ubuntu-*-entry.json.gz'))
        self.assertTrue(references)
        for reference in references:
            files = json.loads(gzip.decompress(reference.read_bytes()))['files']
            for name, (target, mode) in INSTALLED.items():
                with self.subTest(reference=reference.name, file=name):
                    digest = hashlib.sha256((ROLE / 'files' / name).read_bytes()).hexdigest()
                    self.assertEqual(files[target]['sha256'], digest)
                    self.assertEqual(files[target]['mode'], mode)

    def test_tasks_install_files_with_snapshot_modes(self):
        tasks = yaml.safe_load((ROLE / 'tasks/main.yml').read_text())
        copied = {}
        for task in tasks:
            copy = task.get('ansible.builtin.copy') or {}
            for name in task.get('loop') or [copy.get('src')]:
                if name in INSTALLED:
                    dest = copy['dest'] + name if copy['dest'].endswith('/') else copy['dest']
                    copied[name] = (dest, copy['mode'])
        self.assertEqual(copied, INSTALLED)

    def test_play_runs_role_on_entry_under_stage_tag(self):
        play, = yaml.safe_load((ROOT / 'infra/ansible/plays/21-failover-watchdog.yml').read_text())
        self.assertEqual(play['hosts'], 'entry')
        self.assertEqual(play['tags'], ['21-failover-watchdog'])
        self.assertEqual(play['roles'], ['failover'])
        self.assertFalse((ROOT / 'infra/scripts/21-failover-watchdog.sh').exists())


if __name__ == '__main__':
    unittest.main()
