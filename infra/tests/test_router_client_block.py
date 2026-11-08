"""Клиент VPN не достаёт link-local, частные сети и службы самого entry."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from router_support import ROOT, SB, apply, prepare
import test_router_telegram
from test_router_telegram import ENV, GENERATOR

# Адреса entry, которые vpn2-entry-addrs положил в набор entry-addrs.
ENTRY = ['203.0.113.10', '2001:db8::10', '172.17.0.1']


def generate(mode):
    return json.loads(subprocess.check_output(
        [sys.executable, GENERATOR],
        env={**ENV, 'YT_ROUTE': mode, 'YT_QUIC': 'block'}, text=True))


def route(cfg, **changes):
    return test_router_telegram.route(cfg, entry_addrs=ENTRY, **changes)


class ClientBlockTests(unittest.TestCase):
    def test_metadata_link_local_and_entry_services_are_rejected(self):
        for mode in ('off', 'direct', 'zapret'):
            cfg = generate(mode)
            for ip, port in (('169.254.169.254', 80), ('169.254.0.1', 443), ('fe80::1', 80),
                             ('172.17.0.1', 80), ('100.100.100.200', 80), ('10.1.2.3', 443),
                             ('203.0.113.10', 8080), ('203.0.113.10', 9090),
                             ('2001:db8::10', 3000)):
                for network in ('tcp', 'udp'):
                    for extra in ({}, {'geoip': ['ru']}, {'domain': 'metadata.example.ru'}):
                        with self.subTest(mode=mode, ip=ip, port=port, network=network, **extra):
                            self.assertEqual(route(cfg, ip=ip, port=port, network=network,
                                                   **extra), 'reject')
            # Публичные порты entry (SSH, панель) клиенту доступны как снаружи.
            for port in (22, 80, 443):
                self.assertNotEqual(route(cfg, ip='203.0.113.10', port=port), 'reject')
            self.assertEqual(route(cfg, ip='203.0.113.10', port=443, network='udp'), 'reject')

    def test_entry_service_traffic_is_not_broken(self):
        cfg = generate('zapret')
        self.assertEqual(route(cfg, ip='203.0.113.1'), 'lane-07')
        self.assertEqual(route(cfg, ip='203.0.113.1', geoip=['ru']), 'direct-ru')
        self.assertEqual(route(cfg, inbound='mon-aa', ip='127.0.0.1'), 'hy2-aa-mgmt')
        self.assertEqual(route(cfg, inbound='tg-proxy'), 'tg-best')
        dns = route(cfg, ip='10.99.0.1', port=53, network='udp', protocol='dns')
        self.assertEqual(dns, 'dns-unfiltered')

    def test_block_is_ahead_of_every_exit_choice(self):
        cfg = generate('zapret')
        rules = cfg['route']['rules']
        block = next(i for i, rule in enumerate(rules)
                     if rule.get('action') == 'reject' and '169.254.0.0/16' in rule.get('ip_cidr', []))
        self.assertTrue(all('outbound' not in rule or 'inbound' in rule for rule in rules[:block]),
                        'клиентский выход выбирается раньше блокировки')
        # Ручные маршруты панели встают после «головы»: inbound- и reject-правил.
        self.assertTrue(all('inbound' in rule or rule.get('action') in ('reject', 'sniff')
                            for rule in rules[:block + 1]))

    @unittest.skipUnless(SB, 'set SING_BOX_BIN to sing-box 1.12.25')
    def test_manual_default_route_stays_below_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            script = prepare(directory)
            (directory / 'manual.json').write_text(json.dumps([
                {'type': 'ip_cidr', 'value': '0.0.0.0/0', 'outbound': 'hy2-aa-direct'}]))
            result = apply(script)
            self.assertEqual(result.returncode, 0, result.stderr)
            rules = json.loads((directory / 'config.json').read_text())['route']['rules']
            manual = next(i for i, rule in enumerate(rules)
                          if any(tag.startswith('manual-') for tag in rule.get('rule_set', [])))
            for net in ('169.254.0.0/16', 'fe80::/10'):
                block = next(i for i, rule in enumerate(rules)
                             if rule.get('action') == 'reject' and net in rule.get('ip_cidr', []))
                self.assertLess(block, manual, net)
            entry = next(i for i, rule in enumerate(rules)
                         if rule.get('action') == 'reject' and rule.get('type') == 'logical'
                         and 'entry-addrs' in rule['rules'][0].get('rule_set', []))
            self.assertLess(entry, manual)

    @unittest.skipUnless(SB and os.environ.get('RUN_NETNS_TESTS') == '1',
                         'set SING_BOX_BIN and RUN_NETNS_TESTS=1 with sudo/iproute2')
    def test_address_added_after_deploy_is_closed_after_restart(self):
        result = subprocess.run([
            'sudo', '-n', 'unshare', '--net', 'env', f'SING_BOX_BIN={SB}',
            f'INFRA_TEST_ROOT={ROOT}', sys.executable,
            str(Path(__file__).with_name('probe_entry_addrs.py')),
        ], text=True, capture_output=True, timeout=90)
        print(result.stdout.strip())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('новый адрес, перезапуск sing-box: 198.51.100.8 служба :8080 закрыта',
                      result.stdout)
        self.assertIn('новый адрес, перезапуск sing-box: 2001:db8::8 служба :8080 закрыта',
                      result.stdout)


class EntryAddrsScriptTests(unittest.TestCase):
    """vpn2-entry-addrs стадии 20 (ExecStartPre sing-box) без root: ip подменён."""

    def run_script(self, directory, ip_json, ip_rc=0):
        source = (ROOT / 'infra/scripts/20-ru-router.sh').read_text()
        body = source.split("cat > /usr/local/sbin/vpn2-entry-addrs << 'ADDRS'\n", 1)[1]
        body = body.split('\nADDRS\n', 1)[0].replace('/etc/sing-box/', f'{directory}/')
        script = directory / 'vpn2-entry-addrs'
        script.write_text(body + '\n')
        bindir = directory / 'bin'
        bindir.mkdir(exist_ok=True)
        (bindir / 'ip').write_text(f"#!/bin/sh\ncat <<'J'\n{json.dumps(ip_json)}\nJ\nexit {ip_rc}\n")
        (bindir / 'ip').chmod(0o755)
        return subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                              env={**os.environ, 'PATH': f'{bindir}:{os.environ["PATH"]}'})

    def test_writes_current_addresses_and_keeps_last_set_on_failure(self):
        links = [
            {'ifname': 'eth0', 'addr_info': [
                {'family': 'inet', 'local': '203.0.113.10', 'prefixlen': 24, 'scope': 'global'},
                {'family': 'inet6', 'local': '2001:db8::10', 'prefixlen': 64, 'scope': 'global'}]},
            {'ifname': 'eth1', 'addr_info': [
                {'family': 'inet', 'local': '198.51.100.8', 'prefixlen': 32, 'scope': 'global'},
                {'family': 'inet', 'local': '203.0.113.10', 'prefixlen': 24, 'scope': 'global'}]},
            {'ifname': 'docker0'},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            result = self.run_script(directory, links)
            self.assertEqual(result.returncode, 0, result.stderr)
            print(result.stdout.strip())
            path = directory / 'entry-addrs.json'
            expected = {'version': 2, 'rules': [{'ip_cidr': [
                '198.51.100.8/32', '203.0.113.10/32', '2001:db8::10/128']}]}
            self.assertEqual(json.loads(path.read_text()), expected)
            if SB:
                compiled = subprocess.run([SB, 'rule-set', 'compile', '-o', os.devnull, str(path)],
                                          capture_output=True, text=True)
                self.assertEqual(compiled.returncode, 0, compiled.stderr)
            mtime = path.stat().st_mtime_ns
            # Те же адреса - файл не переписывается (живой sing-box его не перечитывает).
            self.assertEqual(self.run_script(directory, links).returncode, 0)
            self.assertEqual(path.stat().st_mtime_ns, mtime)
            # ip не ответил - остаётся набор прошлого старта.
            failed = self.run_script(directory, [], ip_rc=1)
            self.assertEqual(failed.returncode, 1)
            self.assertIn('набор прошлого старта', failed.stderr)
            self.assertEqual(json.loads(path.read_text()), expected)

    def test_missing_set_is_created_empty_so_sing_box_still_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            failed = self.run_script(directory, [], ip_rc=1)
            self.assertEqual(failed.returncode, 1)
            self.assertEqual(json.loads((directory / 'entry-addrs.json').read_text()),
                             {'version': 2, 'rules': []})
            empty = self.run_script(directory, [{'ifname': 'lo', 'addr_info': []}])
            self.assertEqual(empty.returncode, 0, empty.stderr)
            self.assertIn('закрыты службы на -', empty.stdout)


if __name__ == '__main__':
    unittest.main()
