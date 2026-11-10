"""Роль router: файлы роли равны эталону слепка entry, плей привязан к стадии 20."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import unittest

import yaml

ROOT = Path(os.environ.get('VPN2_AUDIT_ROOT', Path(__file__).resolve().parents[2]))
ROLE = ROOT / 'infra/ansible/roles/router'
SNAPSHOTS = ROOT / 'infra/tests/fixtures/snapshot'
INSTALLED = {
    'sing-box.service': ('/etc/systemd/system/sing-box.service', '0644'),
    'anysda-apply-routes.service': ('/etc/systemd/system/anysda-apply-routes.service', '0644'),
    'anysda-apply-routes.path': ('/etc/systemd/system/anysda-apply-routes.path', '0644'),
    '99-anysda-vpn.conf': ('/etc/sysctl.d/99-anysda-vpn.conf', '0644'),
}


class RouterRoleTests(unittest.TestCase):
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
                if isinstance(name, str) and name in INSTALLED:
                    dest = copy['dest'] + name if copy['dest'].endswith('/') else copy['dest']
                    copied[name] = (dest, copy['mode'])
        self.assertEqual(copied, INSTALLED)

    def test_play_runs_role_on_entry_under_stage_tag(self):
        play, = yaml.safe_load((ROOT / 'infra/ansible/plays/20-ru-router.yml').read_text())
        self.assertEqual(play['hosts'], 'entry')
        self.assertEqual(play['tags'], ['20-ru-router'])
        self.assertEqual(play['roles'], ['router'])
        self.assertFalse((ROOT / 'infra/scripts/20-ru-router.sh').exists())


    def test_binary_comes_from_shared_installer_with_package_python(self):
        tasks = yaml.safe_load((ROLE / 'tasks/main.yml').read_text())
        task, = [t for t in tasks if 'install-sing-box.sh' in str(t.get('ansible.builtin.command'))]
        defaults = yaml.safe_load((ROLE / 'defaults/main.yml').read_text())
        self.assertEqual(task['environment']['PATH'], '{{ router_path }}')
        self.assertTrue(defaults['router_path'].startswith('/opt/vpn2/venv/bin:'))
        self.assertIn('VPN2_RELEASE_MANIFEST', task['environment'])

    def test_steps_report_change_by_package_output(self):
        tasks = yaml.safe_load((ROLE / 'tasks/main.yml').read_text())
        steps = [t for t in tasks if 'router_step' in str((t.get('ansible.builtin.command') or {}).get('argv'))]
        self.assertGreaterEqual(len(steps), 6)
        for task in steps:
            with self.subTest(task=task['name']):
                self.assertIn('changed_when', task)


if __name__ == '__main__':
    unittest.main()
