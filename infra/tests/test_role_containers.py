"""Роль containers: плеи стадий 25, 30 и 35 зовут шаг пакета и отдают ему всё его окружение."""
import os
from pathlib import Path
import ast
import unittest

import jinja2
import yaml

from play_stage import site_play

ROOT = Path(os.environ.get('VPN2_AUDIT_ROOT', Path(__file__).resolve().parents[2]))
ROLE = ROOT / 'infra/ansible/roles/containers'
PLAYS = {'25-monitoring': 'monitoring', '30-frontend': 'panel', '35-telegram': 'telegram'}
class ContainersRoleTests(unittest.TestCase):
    def setUp(self):
        self.defaults = yaml.safe_load((ROLE / 'defaults/main.yml').read_text())
        self.tasks = yaml.safe_load((ROLE / 'tasks/main.yml').read_text())

    def test_plays_run_role_under_stage_tag(self):
        for stage, service in PLAYS.items():
            with self.subTest(stage=stage):
                play = site_play(stage)
                self.assertEqual((play['hosts'], play['tags']), ('entry', [stage]))
                self.assertEqual(play['roles'], [{'role': 'containers', 'containers_service': service}])
        self.assertEqual(sorted(self.defaults['containers_tools']), sorted(PLAYS.values()))

    def test_last_task_runs_package_step_and_reports_change(self):
        step = self.tasks[-1]
        self.assertEqual(step['ansible.builtin.command']['argv'],
                         ['{{ containers_vpn2 }}', 'containers', '{{ containers_service }}'])
        self.assertEqual(step['environment'], '{{ containers_env }}')
        self.assertEqual(step['changed_when'], f"'changed=1' in {step['register']}.stdout")
        # Шаг без отметки changed=1 не должен считаться изменением ни в одном задании роли.
        tasks = self.tasks + yaml.safe_load((ROLE / 'tasks/panel.yml').read_text())
        for task in tasks:
            if 'ansible.builtin.command' in task:
                with self.subTest(task=task['name']):
                    self.assertEqual(task['changed_when'], f"'changed=1' in {task['register']}.stdout")

    def test_step_gets_every_node_variable_but_the_entry_login(self):
        filters = {'dict2items': lambda data: [{'key': key, 'value': value} for key, value in data.items()],
                   'items2dict': lambda items: {item['key']: item['value'] for item in items},
                   'combine': lambda data, other: {**data, **other}, 'from_yaml': yaml.safe_load}
        environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
        environment.filters.update(filters)
        node = {'ENTRY_HOST': '192.0.2.1', 'SSH_PASS': 'secret', 'TLS_ACME_URL': 'https://acme.test/dir'}
        rendered = environment.from_string(self.defaults['containers_env']).render(
            vpn2_env=node, containers_infra='infra',
            lookup=lambda _kind, path: (ROOT / path).read_text())
        env = ast.literal_eval(rendered)
        self.assertEqual(env.pop('VM_VERSION'), yaml.safe_load((ROOT / 'infra/versions.yml').read_text())['victoriametrics'])
        self.assertEqual(env, {'ENTRY_HOST': '192.0.2.1', 'TLS_ACME_URL': 'https://acme.test/dir'})

    def test_tools_exist_in_tree(self):
        for service, tools in self.defaults['containers_tools'].items():
            for tool in tools:
                with self.subTest(service=service, tool=tool['src']):
                    self.assertTrue((ROOT / 'infra' / tool['src']).is_file())
        for name in ('vpn2-tls.service', 'vpn2-tls.timer'):
            self.assertTrue((ROLE / 'files' / name).is_file())


if __name__ == '__main__':
    unittest.main()
