"""Шаг «config» роли adguard (vpn2.adguard.stage, vpn2.adguard.dns) на каталогах во временной папке.

Пароль bcrypt и длинный UTF-8, вход UI на loopback, настройки DNS из панели при повторном деплое,
политика DNS установленного хаба, имена из config.yaml как данные, белый список пулов.
"""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import yaml

from envs import node_envs, sourced
from vpn2 import containers, resources
from vpn2.adguard import dns, stage
from vpn2.containers import compose

ROOT = Path(__file__).resolve().parents[3]
UPSTREAMS = ["[/ru/рф/xn--p1ai/su/]77.88.8.8", "https://1.1.1.1/dns-query", "https://dns.google/dns-query"]
VALUES = ['operator"name', "operator'name", r'operator\name', 'operator$HOME$(touch INJECTED)', 'operator\nname\n']


class Hub:
    """Каталоги /opt/AdGuardHome и /etc/anysda во временной папке."""

    def __init__(self, directory, port=3001, user='anysda'):
        self.root = Path(directory)
        self.agh, self.plain = self.root / 'agh', self.root / 'plain'
        self.etc, self.work = self.root / 'etc-anysda', self.root / 'work'
        for path in (self.agh, self.plain, self.etc, self.work):
            path.mkdir(parents=True)
        self.config = self.agh / 'AdGuardHome.yaml'
        self.env = {'AGH_PORT': str(port), 'MGMT_IP': '192.0.2.53'}
        self.user = user
        self.changed = False

    def deploy(self):
        """Сборка и публикация обоих конфигов; возвращает stderr шага."""
        log = io.StringIO()
        with contextlib.redirect_stderr(log), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(stage, 'password_hash', lambda *_: 'hash'), \
                mock.patch.object(resources, 'hardware', lambda: (8000, 8192)), \
                mock.patch.object(stage.svc, 'active', lambda _unit: False), \
                mock.patch.object(stage.svc, 'forget', lambda _unit: None):
            staged = stage.build_configs(self.env, self.user, 'pw', self.work, agh_dir=self.agh, plain_dir=self.plain,
                                         etc=self.etc, pools=ROOT / 'app/shared/vpn-pools.json')
            published = [stage.publish(unit, path, directory / 'AdGuardHome.yaml')
                         for unit, path, directory in zip(stage.UNITS, staged, (self.agh, self.plain))]
        self.changed = published[0]
        return log.getvalue()

    def dns(self):
        return yaml.safe_load(self.config.read_text())['dns']

    def edit(self, **values):
        """Как AdGuard после POST /control/dns_config из панели."""
        config = yaml.safe_load(self.config.read_text())
        config['dns'].update(values)
        self.config.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))


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
        for name, settings, expected in (
                ('defaults of old stage are migrated', old, DEFAULTS),
                # До 12-10 стадия не задавала оптимистичный кэш: false на таком
                # хабе неотличим от умолчания AdGuard и включается, как раньше.
                ('admin choice is kept', {**old, **PANEL}, {**PANEL, 'cache_optimistic': True})):
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                hub = Hub(directory)
                hub.config.write_text(yaml.safe_dump(
                    {'users': [{'name': 'anysda', 'password': 'hash'}], 'dns': settings},
                    allow_unicode=True))
                hub.deploy()
                self.assertEqual(managed(hub.dns()), expected)
                self.assertEqual(hub.dns()['fallback_dns'], [])
                state = hub.etc / 'agh-dns.json'
                self.assertEqual(state.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(state.read_text()), DEFAULTS)


class AdguardPasswordTests(unittest.TestCase):
    def test_stage_and_panel_use_same_complete_password(self):
        for password in ('short-ASCII', 'a' * 72, 'a' * 73, 'a' * 256, 'a' * 300,
                         'я' * 36, 'я' * 37, '🔑' * 300, 'a' * 299 + 'b'):
            with self.subTest(length=len(password.encode())), tempfile.TemporaryDirectory() as directory:
                work = Path(directory)
                (work / 'anysda').mkdir()
                with mock.patch.object(stage, 'ETC', str(work / 'anysda')), mock.patch.object(stage, 'UNITS', {}), \
                        mock.patch.object(stage, 'build_configs', lambda *_a, **_k: ()):
                    stage.configure({'ADMIN_PASSWORD': password, 'AGH_PORT': '3000'})
                backend = (work / 'anysda/adguard-password.txt').read_text()
                digest = stage.password_hash(None, 'anysda', backend)
                panel = stage.agh_password(password)
                self.assertEqual(stage.password_hash({'users': [{'name': 'anysda', 'password': digest}]}, 'anysda', panel), digest)
                expected = password.encode()
                if len(expected) > 72:
                    expected = hashlib.sha256(b'vpn2-adguard-v1\0' + expected).hexdigest().encode()
                self.assertEqual(backend.encode(), expected)
                self.assertEqual(panel.encode(), expected)
                self.assertEqual((work / 'anysda/admin-password.txt').read_text(), password)
                saved = work / 'anysda/adguard-password.txt'
                self.assertEqual(saved.read_bytes(), expected)
                self.assertEqual(saved.stat().st_mode & 0o777, 0o600)


class AdguardWebBindTests(unittest.TestCase):
    def test_old_public_bind_moves_to_loopback_once(self):
        for old in ('0.0.0.0:3001', '[::]:3001', '192.0.2.7:3001', None):
            with self.subTest(old=old), tempfile.TemporaryDirectory() as directory:
                hub = Hub(directory, port=3101, user='admin')
                config = {'dns': {'aaaa_disabled': True}, 'users': [{'name': 'admin', 'password': 'hash'}]}
                if old:
                    config['http'] = {'address': old, 'session_ttl': '720h'}
                hub.config.write_text(yaml.safe_dump(config))
                hub.deploy()
                # Опубликованный конфиг сменился: отпечаток службы сброшен, она перезапустится.
                self.assertTrue(hub.changed)
                after = yaml.safe_load(hub.config.read_text())
                self.assertEqual(after['http']['address'], '127.0.0.1:3101')
                if old:
                    self.assertEqual(after['http']['session_ttl'], '720h')
                self.assertEqual(after['users'], [{'name': 'admin', 'password': 'hash'}])
                text = hub.config.read_text()
                hub.deploy()
                self.assertFalse(hub.changed)
                self.assertEqual(hub.config.read_text(), text)


class AdguardPatchTests(unittest.TestCase):
    def test_installed_config_and_idempotence(self):
        before = yaml.safe_load(dns.fresh_text("test-user", "test-hash", "3000", "192.0.2.1", "true", "1000"))
        # Состояние установленного хаба, включая пользовательские настройки.
        before["dns"].update(upstream_dns=["77.88.8.8", *UPSTREAMS[1:],
                                           "[/youtube.com/googlevideo.com/]77.88.8.8"],
                              fallback_dns=["77.88.8.8"], cache_optimistic=False,
                              upstream_dns_file="/etc/custom-upstreams.txt", use_http3_upstreams=True)
        before["user_rules"] = ["||ads.example.test^"]
        before["clients"] = {"persistent": [{"name": "custom", "ids": ["192.0.2.10"]}]}
        with tempfile.TemporaryDirectory() as work:
            hub = Hub(work, user="test-user")
            hub.env.update(WG_SERVER_IP="192.0.2.1", OVPN_SUBNET="198.51.100.0", IKEV2_SUBNET="203.0.113.0/24")
            hub.config.write_text(yaml.safe_dump(before, allow_unicode=True, sort_keys=False))
            hub.deploy()
            self.assertTrue(hub.changed)
            after = yaml.safe_load(hub.config.read_text())
            self.assertEqual(after["dns"]["upstream_dns"], UPSTREAMS)
            self.assertEqual(after["dns"]["fallback_dns"], [])
            self.assertEqual(after["dns"]["upstream_dns_file"], "")
            self.assertTrue(after["dns"]["cache_optimistic"])
            self.assertFalse(after["dns"]["use_http3_upstreams"])
            self.assertEqual(after["users"], [{"name": "test-user", "password": "hash"}])
            for key in ("filters", "user_rules", "clients"):
                self.assertEqual(after[key], before[key])
            first = hashlib.sha256(hub.config.read_bytes()).hexdigest()
            hub.deploy()
            self.assertFalse(hub.changed)
            self.assertEqual(first, hashlib.sha256(hub.config.read_bytes()).hexdigest())

    def test_unfiltered_instance_has_same_policy_without_lists(self):
        filtered = {"users": [{"name": "test", "password": "hash"}],
                    "dns": {"upstream_dns": UPSTREAMS, "cache_optimistic": True},
                    "filters": [{"enabled": True}], "user_rules": ["||example.test^"]}
        plain = dns.plain_config(filtered, {}, "3000")
        self.assertEqual(plain["dns"]["upstream_dns"], UPSTREAMS)
        self.assertEqual(plain["dns"]["bind_hosts"], ["127.0.0.2"])
        self.assertFalse(plain["filtering"]["protection_enabled"])
        self.assertFalse(plain["filtering"]["filtering_enabled"])
        self.assertEqual(plain["filters"], [])
        self.assertEqual(plain["user_rules"], [])
        enriched = copy.deepcopy(plain)
        enriched["dns"]["unrelated_default"] = True
        enriched["schema_version"] = 30
        self.assertEqual(dns.plain_config(filtered, enriched, "3000"), enriched)


class AdguardLiteralTests(unittest.TestCase):
    def test_fresh_credentials(self):
        for value in [*VALUES, 'true', 'name: value', '*alias']:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as work:
                hub = Hub(work, user=value)
                hub.deploy()
                cfg = yaml.safe_load(hub.config.read_text())
                self.assertEqual(cfg['users'], [{'name': value, 'password': 'hash'}])
                self.assertEqual(hub.config.stat().st_mode & 0o777, 0o600)

    def test_sync_credentials(self):
        for value in VALUES:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as work:
                # Имя проходит весь путь: config.yaml, inventory.py --env, env узла, конфиг AdGuard.
                config = ('entry:\n  host: entry.example.test\n  password: fake\n'
                          'exits:\n  - tag: test\n    host: exit.example.test\n'
                          '    password: fake\nadmin:\n  user: ' + json.dumps(value) + '\n')
                prepared, texts = node_envs(work, config)
                self.assertTrue(texts, prepared.stderr)
                user = sourced(work, texts['ru'], 'printf %s "$ADMIN_USER"')
                self.assertEqual(user.stdout, value, user.stderr)
                hub = Hub(Path(work, 'hub'), user=user.stdout)
                hub.config.write_text(yaml.safe_dump({'users': [], 'custom': 'keep'}))
                hub.deploy()
                cfg = yaml.safe_load(hub.config.read_text())
                self.assertEqual(cfg['users'], [{'name': value, 'password': 'hash'}])
                self.assertEqual(cfg['custom'], 'keep')
                self.assertTrue(hub.changed)
                before = hub.config.read_bytes()
                hub.deploy()
                self.assertFalse(hub.changed)
                self.assertEqual(before, hub.config.read_bytes())
                self.assertFalse((Path(work) / 'INJECTED').exists())


class FrontendLiteralTests(unittest.TestCase):
    def test_management_pairs(self):
        for value in [*VALUES, '*', 'two  words']:
            with self.subTest(value=value):
                environ = {'EXIT_TAGS': 'test tail', 'MGMT_IP_RU': '192.0.2.1', 'MGMT_IP_TEST': value,
                           'MGMT_IP_TAIL': '192.0.2.3'}
                self.assertEqual(compose.mgmt_ips(environ), f'ru:192.0.2.1,test:{value},tail:192.0.2.3')


class TelegramLiteralTests(unittest.TestCase):
    def test_runtime_json(self):
        for value in VALUES:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as work:
                path = Path(work) / 'telegram-runtime.json'
                with mock.patch.object(containers, 'ETC', work):
                    containers.sync_runtime({'TELEGRAM_BOT_TOKEN': value, 'TELEGRAM_CHAT_ID': value,
                                             'TELEGRAM_ADMIN_USERNAME': value}, [])
                self.assertEqual(json.loads(path.read_text()),
                                 dict(bot_token=value, chat_id=value, admin_username=value))
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertFalse((Path(work) / 'INJECTED').exists())


class PoolWhitelistTests(unittest.TestCase):
    def test_ratelimit_whitelist_covers_every_pool_address(self):
        with tempfile.TemporaryDirectory() as directory:
            hub = Hub(directory)
            hub.config.write_text('users: []\ndns:\n  aaaa_disabled: true\n  ratelimit_whitelist: [203.0.113.1]\n')
            hub.deploy()
            hub.deploy()
            addresses = hub.dns()['ratelimit_whitelist']
            self.assertEqual(len(addresses), 24 * 254 + 1)
            self.assertEqual(len(set(addresses)), len(addresses))
            self.assertIn('203.0.113.1', addresses)
