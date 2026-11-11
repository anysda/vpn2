"""Роль zapret: плей привязан к стадии 19, шаги сами говорят об изменениях, шаблоны закрыты значениями."""
import unittest

import yaml

from play_stage import site_play

from zapret_support import ROLE, VALUES, render, stage, tasks


class ZapretRoleTests(unittest.TestCase):
    def test_play_runs_role_under_stage_tag(self):
        play = site_play('19-yt-zapret')
        self.assertEqual((play['hosts'], play['tags'], play['roles']), ('entry', ['19-yt-zapret'], ['zapret']))

    def test_route_selects_teardown_or_desync(self):
        main = yaml.safe_load((ROLE / 'tasks/main.yml').read_text())
        off = next(task for task in main if "'off'" in str(task.get('ansible.builtin.command', {}).get('argv')))
        self.assertEqual(off['when'], "zapret_route != 'zapret'")
        desync = next(task for task in main if 'ansible.builtin.import_tasks' in task)
        self.assertEqual((desync['ansible.builtin.import_tasks'], desync['when']),
                         ('desync.yml', "zapret_route == 'zapret'"))

    def test_steps_report_changes_in_order(self):
        steps = {}
        for task in tasks():
            argv = str(task.get('ansible.builtin.command', {}).get('argv'))
            if 'zapret_step' in argv:
                steps[argv.split("'")[1]] = task
        self.assertEqual(list(steps), ['off', 'source', 'facts', 'offload-keep', 'quic-off', 'restart', 'probe',
                                       'remeasure'])
        self.assertEqual(sorted(steps), sorted(stage.STEPS))
        for name, task in steps.items():
            # Повтор роли остаётся без изменений: шаг сам говорит, менял ли он хост.
            expected = False if name in ('facts', 'remeasure') else f"'changed=1' in {task['register']}.stdout"
            self.assertEqual(task['changed_when'], expected, name)

    def test_templates_use_only_step_facts(self):
        names = sorted(path.name.removesuffix('.j2') for path in (ROLE / 'templates').iterdir())
        self.assertEqual(names, ['anysda-yt-check', 'anysda-yt-nfqws-ready', 'anysda-yt-nfqws.service',
                                 'anysda-yt-offload.service', 'nftables-yt-zapret.conf'])
        for name in names:
            with self.subTest(template=name):
                render(name)
        unit = render('anysda-yt-nfqws.service')
        self.assertIn(f"ExecStart={VALUES['bin']} --qnum=200", unit)
        self.assertIn(VALUES['strategy'], unit)

    def test_window_rules_land_in_chain(self):
        cfg = stage.settings({'YT_ZAPRET_WSCALE': '9'})
        rules = stage.window_rules(cfg, 'eth0')
        table = render('nftables-yt-zapret.conf', window_rules=rules)
        self.assertIn(rules + '\n', table)
        self.assertIn('queue', table)


if __name__ == '__main__':
    unittest.main()
