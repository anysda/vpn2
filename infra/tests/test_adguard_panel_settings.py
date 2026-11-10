"""Повторный деплой не затирает настройки DNS, заданные в панели.

Гоняется шаг «config» роли adguard: первая установка, правка тех полей, что
панель меняет через API AdGuard, и повторный прогон.
"""

import json
import tempfile
import unittest

import yaml

from adguard_support import Hub

DEFAULTS = {
    'upstream_dns': ['[/ru/рф/xn--p1ai/su/]77.88.8.8',
                     'https://1.1.1.1/dns-query', 'https://dns.google/dns-query'],
    'bootstrap_dns': ['8.8.8.8', '8.8.4.4'],
    'upstream_mode': 'parallel',
    'cache_optimistic': True,
    'ratelimit': 0,
    'aaaa_disabled': True,
}
# То, что админ выставляет во вкладке DNS → «Настройки DNS».
PANEL = {
    'upstream_dns': ['https://9.9.9.9/dns-query', '[/lan/]192.0.2.1'],
    'bootstrap_dns': ['1.0.0.1'],
    # «Балансировка» из панели: AdGuard хранит её пустой строкой.
    'upstream_mode': '',
    'cache_optimistic': False,
    'ratelimit': 30,
    'aaaa_disabled': False,
}


def managed(dns):
    return {key: dns.get(key) for key in DEFAULTS}


class AdguardPanelSettingsTests(unittest.TestCase):
    def test_redeploy_keeps_every_setting_changed_in_panel(self):
        with tempfile.TemporaryDirectory() as directory:
            hub = Hub(directory)
            hub.deploy()
            self.assertEqual(managed(hub.dns()), DEFAULTS)
            hub.edit(**PANEL)
            log = hub.deploy()
            self.assertEqual(managed(hub.dns()), PANEL)
            self.assertIn('настройки из панели оставлены', log)
            text = hub.config.read_text()
            hub.deploy()
            self.assertEqual(hub.config.read_text(), text)

    def test_each_field_is_kept_alone_and_the_rest_follow_deploy(self):
        for key, value in PANEL.items():
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                hub = Hub(directory)
                hub.deploy()
                hub.edit(**{key: value})
                hub.deploy()
                self.assertEqual(managed(hub.dns()), {**DEFAULTS, key: value})

    def test_deploy_still_updates_values_it_wrote_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            hub = Hub(directory)
            hub.deploy()
            # Прежний выпуск записал другое значение и запомнила его.
            state = hub.etc / 'agh-dns.json'
            if state.exists():
                saved = json.loads(state.read_text())
                saved.update(ratelimit=20, upstream_mode='fastest_addr')
                state.write_text(json.dumps(saved))
            hub.edit(ratelimit=20, upstream_mode='fastest_addr')
            hub.deploy()
            self.assertEqual(managed(hub.dns()), DEFAULTS)

    def test_hub_installed_before_state_file(self):
        old = {'upstream_dns': ['77.88.8.8', 'https://1.1.1.1/dns-query',
                                'https://dns.google/dns-query',
                                '[/youtube.com/googlevideo.com/]77.88.8.8'],
               'bootstrap_dns': ['77.88.8.8', '8.8.8.8'], 'fallback_dns': ['77.88.8.8'],
               'upstream_mode': 'parallel', 'cache_optimistic': False,
               'ratelimit': 20, 'aaaa_disabled': True}
        for name, dns, expected in (
                ('defaults of old stage are migrated', old, DEFAULTS),
                # До 12-10 стадия не задавала оптимистичный кэш: false на таком
                # хабе неотличим от умолчания AdGuard и включается, как раньше.
                ('admin choice is kept', {**old, **PANEL}, {**PANEL, 'cache_optimistic': True})):
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                hub = Hub(directory)
                hub.config.write_text(yaml.safe_dump(
                    {'users': [{'name': 'anysda', 'password': 'hash'}], 'dns': dns},
                    allow_unicode=True))
                hub.deploy()
                self.assertEqual(managed(hub.dns()), expected)
                self.assertEqual(hub.dns()['fallback_dns'], [])
                state = hub.etc / 'agh-dns.json'
                self.assertEqual(state.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(state.read_text()), DEFAULTS)


if __name__ == '__main__':
    unittest.main()
