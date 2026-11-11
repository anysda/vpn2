"""Роль containers: плеи стадий 25, 30 и 35 зовут шаг пакета и отдают ему всё его окружение."""
import os
from pathlib import Path
import re
import unittest

import yaml

from play_stage import site_play

ROOT = Path(os.environ.get('VPN2_AUDIT_ROOT', Path(__file__).resolve().parents[2]))
ROLE = ROOT / 'infra/ansible/roles/containers'
PACKAGE = ROOT / 'infra/vpn2/src/vpn2/containers'
PLAYS = {'25-monitoring': 'monitoring', '30-frontend': 'panel', '35-telegram': 'telegram'}
# Пути-подмены для тестов пакета: плей их не задаёт, шаг берёт умолчания хоста.
OVERRIDES = {'VPN2_ENTRY_RESOURCES', 'VPN2_PANEL_CONFIG_TPL', 'VPN2_DEPLOY_STATE'}
# Массивы из ответа расчёта ресурсов, а не переменные окружения.
RESOURCE_ARRAYS = {'ENTRY_DOCKER_ARGS', 'ENTRY_RUNTIME_ARGS'}
READ = re.compile(r'''(?:\bget\(|environ\[)\s*(?:f?["'])([A-Z][A-Z0-9_]*)["']''')
LOOP = re.compile(r'for name in \(([^)]*)\)')


def package_names():
    names = set()
    for path in sorted(PACKAGE.glob('*.py')):
        text = path.read_text()
        names.update(READ.findall(text))
        for group in LOOP.findall(text):
            names.update(re.findall(r"'([A-Z][A-Z0-9_]*)'", group))
    return names


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

    def test_step_gets_every_variable_the_package_reads(self):
        names = package_names()
        self.assertGreater(len(names), 30)
        prefixes = {name for name in names if name.endswith('_')}
        self.assertEqual(prefixes - {'TELEGRAM_'}, {'MGMT_IP_', 'DOMAIN_'})
        self.assertEqual(sorted(prefix + '_' for prefix in self.defaults['containers_env_exit_prefix']),
                         ['DOMAIN_', 'MGMT_IP_'])
        passed = set(self.defaults['containers_env_common'])
        self.assertIn("combine({'VM_VERSION':", self.defaults['containers_env'])
        missing = {name for name in names - prefixes - OVERRIDES - RESOURCE_ARRAYS - {'VM_VERSION'} if not name.startswith('NUXT_')} - passed
        self.assertEqual(missing, set())
        # Отрицательный контроль: без имени в списке проверка называет именно его.
        self.assertIn('TLS_ACME_URL', names - (passed - {'TLS_ACME_URL'}))
        self.assertEqual(len(passed), len(self.defaults['containers_env_common']))

    def test_tools_exist_in_tree(self):
        for service, tools in self.defaults['containers_tools'].items():
            for tool in tools:
                with self.subTest(service=service, tool=tool['src']):
                    self.assertTrue((ROOT / 'infra' / tool['src']).is_file())
        for name in ('vpn2-tls.service', 'vpn2-tls.timer'):
            self.assertTrue((ROLE / 'files' / name).is_file())


if __name__ == '__main__':
    unittest.main()
