"""Run the real verify script with fake host commands; no root or services needed."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'infra/emergency'))
from vpn2_emergency import snapshot  # noqa: E402
MOCK = r'''#!/usr/bin/env python3
import datetime
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
args = ' '.join(sys.argv[1:])
fault = os.environ['FAULT']
if fault == 'bare' and name not in ('sysctl', 'ufw', 'ss', 'ip'):
    sys.exit(1)
if fault == name + '-error':
    sys.exit(1)
if fault == name + '-error2':
    sys.exit(2)
if name == 'systemctl':
    sys.exit(3 if fault == sys.argv[-1] else 0)
elif name == 'ssh':
    # Как настоящий вход: опции и хост отбрасываются, команда идёт в bash.
    rest = sys.argv[1:]
    while rest and rest[0].startswith('-'):
        rest = rest[2:] if rest[0] in ('-o', '-i', '-F', '-p', '-l') else rest[1:]
    os.execvp('bash', ['bash', '-c', ' '.join(rest[1:])])
elif name == 'sysctl':
    print(0 if fault in ('forwarding', 'yt-unknown-and-failed') else 1)
elif name == 'ufw':
    print('Status: inactive' if fault == 'firewall' else 'Status: active')
elif name == 'iptables':
    if fault == 'lost-ufw' and 'ufw-' in args:
        sys.exit(1)
    if fault == 'lost-tproxy' and 'TPROXY' in args:
        sys.exit(1)
    if fault == 'lost-mark-accept' and 'INPUT -m mark' in args:
        sys.exit(1)
elif name == 'curl':
    url = sys.argv[-1]
    if fault == url:
        sys.exit(22)
    if url.endswith('/targets'):
        counter = Path(os.environ['SCRAPE_COUNTER'])
        count = int(counter.read_text()) + 1 if counter.exists() else 1
        counter.write_text(str(count))
        if fault == 'bad-json':
            print('{')
        else:
            stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
            targets = [{'labels': {'host': host}, 'health': 'up', 'lastScrape': stamp}
                       for host in ('ru', 'exit')]
            if fault == 'empty-targets':
                targets = []
            elif fault == 'missing-target':
                targets.pop()
            elif fault == 'down-target':
                targets[1]['health'] = 'down'
            elif fault == 'warming-up' and count < 4:
                targets[1]['health'] = 'down'
                targets[1]['lastScrape'] = '2000-01-01T00:00:00Z'
            elif fault == 'stale-then-down':
                targets[1]['health'] = 'up' if count == 1 else 'down'
                if count == 1:
                    targets[1]['lastScrape'] = '2000-01-01T00:00:00Z'
            elif fault == 'stale-scrape':
                targets[1]['lastScrape'] = '2000-01-01T00:00:00Z'
            elif fault == 'future-scrape':
                targets[1]['lastScrape'] = '2999-01-01T00:00:00Z'
            elif fault == 'missing-scrape':
                del targets[1]['lastScrape']
            elif fault == 'invalid-scrape':
                targets[1]['lastScrape'] = 'unknown'
            elif fault == 'naive-scrape':
                targets[1]['lastScrape'] = stamp.split('+')[0]
            elif fault == 'empty-then-up' and count < 4:
                targets = []
            elif fault == 'missing-then-up' and count < 4:
                targets.pop()
            print(json.dumps({'status': 'success', 'data': {'activeTargets': targets}}))
elif name == 'ip':
    if fault == 'bare':
        sys.exit(0)
    if args == 'rule list' and fault != 'tproxy-rule':
        print('100: from all fwmark 0x42 lookup 101')
    elif args == 'route show table 101' and fault != 'tproxy-route':
        print('local default dev lo scope host')
    elif args == 'link show xfrm0':
        sys.exit(1 if fault == 'xfrm' else 0)
elif name == 'ss':
    if fault == 'bare':
        sys.exit(0)
    ports = ('7898', '500', '4500', '443', '8443', '9443')
    if fault == 'emergency-listening':
        ports += ('18443',)
    for port in ports:
        if fault != 'port-' + port and (':' + port in args or ':' not in args):
            print('UNCONN 0 0 127.0.0.1:' + port + ' 0.0.0.0:*')
elif name == 'swanctl' and args == '--stats':
    plugins = 'aes sha1 eap-mschapv2 vici'
    if fault == 'ikev2-plugin-missing':
        plugins = 'aes sha1 vici'
    elif fault == 'ikev2-plugin-substring':
        plugins = 'aes sha1 eap-mschapv2-extra vici'
    elif fault == 'ikev2-plugin-error':
        print('loaded plugins: ' + plugins)
        sys.exit(1)
    elif fault == 'ikev2-plugin-stderr':
        print('plugin eap-mschapv2 failed to load', file=sys.stderr)
        plugins = 'aes sha1 vici'
    print('loaded plugins: ' + plugins)
elif name == 'swanctl' and fault != 'ikev2-conn':
    print('anysda-ikev2: IKEv2')
if fault == name + '-partial':
    sys.exit(1)
'''

# Модуль трафика на фикстуре: печатает и дописывает строки, как настоящий.
TRAFFIC = r'''#!/usr/bin/env python3
import json
import os
import sys

fault = os.environ['FAULT']
report = sys.argv[sys.argv.index('--report') + 1]
if fault == 'traffic-crash':
    sys.exit(1)
rows = [('Работает', 'подключение', 'WireGuard', 'клиент подключён')]
if fault in ('traffic-failed', 'traffic-mixed'):
    rows.append(('Ошибка', 'маршрут: через экзит exit', 'WireGuard', 'нет эха экзита'))
if fault in ('traffic-unmeasured', 'traffic-mixed'):
    rows.append(('Не проверено', 'внешняя проба портов', 'IKEv2', 'нет внешней точки измерения'))
if fault == 'traffic-many':
    rows += [('Ошибка', 'маршрут: прямой', name, 'неверный выход') for name in ('WireGuard', 'OpenVPN', 'IKEv2')]
    rows.append(('Не проверено', 'внешняя проба портов', 'IKEv2', 'нет внешней точки измерения'))
with open(report, 'a') as out:
    for result, check, component, reason in rows:
        mark = {'Работает': '✓', 'Ошибка': '✗'}.get(result)
        print(f'[ru] {mark} {component}: {check} — {reason}' if mark else
              f'[ru] ? {component}: {check} — не проверено: {reason}')
        out.write(json.dumps({'check': check, 'component': component, 'result': result,
                              'reason': reason, 'time': '2026-01-01T00:00:00Z'}, ensure_ascii=False) + '\n')
sys.exit(1 if any(r[0] == 'Ошибка' for r in rows) else 2 if any(r[0] == 'Не проверено' for r in rows) else 0)
'''


class VerifyTest(unittest.TestCase):
    def test_loaded_eap_mschapv2_is_required(self):
        for fault in ('', 'ikev2-plugin-missing', 'ikev2-plugin-substring',
                      'ikev2-plugin-error', 'ikev2-plugin-stderr'):
            with self.subTest(fault=fault):
                result = self.run_verify(fault)
                print(f'{fault or "loaded"}: exit={result.returncode}')
                self.assertEqual(result.returncode, 1 if fault else 0, result.stdout)
                self.assertIn(('✗' if fault else '✓') +
                              ' ikev2: charon загрузил eap-mschapv2', result.stdout)

    def run_verify(self, fault='', role='ru', youtube='off', extra_env=None):
        with tempfile.TemporaryDirectory(prefix='verify-test-') as directory:
            root = Path(directory)
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            for name in ('systemctl', 'sysctl', 'ufw', 'curl', 'ip', 'ss', 'ssh', 'swanctl', 'iptables'):
                command = bin_dir / name
                command.write_text(MOCK)
                command.chmod(0o755)
            # File checks still run for real, against disposable fixture paths.
            networkd = root / 'networkd.conf'
            if fault != 'networkd':
                networkd.touch()
            status = root / 'status.json'
            if fault != 'missing-status':
                age = {'stale-status': 121, 'future-status': -60}.get(fault, 10)
                status.write_text('{' if fault == 'bad-status' else json.dumps({'updated': time.time() - age}))
            yt = root / 'yt-check'
            if fault != 'missing-yt':
                yt.write_text('#!/bin/sh\n' + ('echo "UNKNOWN: HTTP 403 не позволяет измерить обход."\n'
                              if fault == 'yt-unmeasured' else '')
                              + 'exit ' + ('2' if fault == 'yt-unmeasured' else
                                           '1' if fault == 'yt-failed' else '0') + '\n')
                yt.chmod(0o755)
            failover = root / 'yt-failover'
            # Настоящий --status на фикстуре selector/state: воспроизводит и
            # старый ложный успех, и новый код «нет измерений» без живого хоста.
            watch_ref = os.environ.get('VERIFY_REF')
            watch_source = subprocess.check_output(
                ['git', 'show', f'{watch_ref}:infra/lib/yt-failover.py'], cwd=ROOT, text=True,
            ) if watch_ref else (ROOT / 'infra/lib/yt-failover.py').read_text()
            watch_file = root / 'watch.py'
            watch_file.write_text(watch_source)
            state_file = root / 'probe.json'
            state_file.write_text(json.dumps({
                'good': 0 if fault in ('yt-no-sample', 'yt-unknown-and-failed') else 3, 'bad': 0,
                'last_probe': 'TCP video: HTTP 403' if fault == 'yt-no-sample' else 'TCP video: HTTP 206',
                'unmeasured': fault in ('yt-no-sample', 'yt-unknown-and-failed'), 'checked_at': time.time(),
            }))
            if fault == 'yt-missing-state':
                state_file.unlink()
            elif fault == 'yt-bad-sample':
                state = json.loads(state_file.read_text())
                state.update(good=0, bad=1, last_probe='очередь, nft-правила или TCP-путь обхода не работают')
                state_file.write_text(json.dumps(state))
            elif fault == 'yt-legacy-state':
                state = json.loads(state_file.read_text())
                del state['checked_at']
                state_file.write_text(json.dumps(state))
            # Аварийка из образа: версия и файлы, которые стадия 23 кладёт на узел.
            emergency = root / 'emergency'
            (emergency / 'vpn2_emergency/static').mkdir(parents=True)
            for name in ('agent.py', 'web.py', 'hy2.py', 'static/index.html', 'static/instructions.html'):
                if fault != 'emergency-' + name:
                    (emergency / 'vpn2_emergency' / name).touch()
            # Стадия 23 до образа оставила пустой штамп и не ставила release.json и Hysteria2.
            old_stage = fault == 'emergency-old-stage'
            if fault != 'emergency-release' and not old_stage:
                (emergency / 'release.json').write_text(json.dumps(
                    {'version': 'dev', 'revision': 'a' * 40, 'image': 'sha256:' + 'b' * 64}))
            stamp = root / 'stamp-23-emergency'
            if old_stage:
                stamp.touch()
            elif fault != 'emergency-not-yet':
                stamp.write_text('image sha256:' + 'b' * 64 + '\n')
            # Снимок клиентов на экзите - настоящим кодом аварийки.
            for name in ('__init__.py', 'snapshot.py', 'snapshot_sender.py'):
                (emergency / 'vpn2_emergency' / name).write_text(
                    (ROOT / 'infra/emergency/vpn2_emergency' / name).read_text())
            snapshots = root / 'emergency-state'
            snapshots.mkdir(mode=0o700)
            if fault not in ('emergency-snapshot-missing', 'emergency-before-23'):
                age = 300 if fault == 'emergency-snapshot-stale' else 10
                image = {'schema': 1, 'version': 1, 'installation_id': 'test',
                         'created_at': snapshot.iso_time(time.time() - age),
                         'admin_chat_id': None, 'bot_token': None,
                         'exits': [{'id': 'exit', 'name': 'exit', 'ip': '198.51.100.20', 'priority': 0}],
                         'clients': []}
                accepted = snapshot.accept(snapshots, image, None, 'test', time.time() - age)
                self.assertEqual(accepted['status'], 'accepted')
            # Штамп 10-foreign новее штампа 23: verify сразу после 10-foreign.
            foreign = root / 'stamp-10-foreign'
            foreign.touch()
            if fault != 'emergency-before-23':
                os.utime(foreign, (time.time() - 600,) * 2)
            setpriv = bin_dir / 'setpriv'
            setpriv.write_text('#!/bin/sh\nwhile [ "$1" != -- ]; do shift; done\nshift\nexec "$@"\n')
            setpriv.chmod(0o755)
            hy2_options = root / 'emergency-hy2.json'
            if fault != 'emergency-hy2-options' and not old_stage:
                hy2_options.write_text(json.dumps({'server_ip': '198.51.100.1', 'port': 18443}))
            config = root / 'router.json'
            config.write_text('{"outbounds": [{"tag": "youtube-auto"}]}')
            failover.write_text('#!/usr/bin/env python3\nimport runpy\nfrom pathlib import Path\n'
                                f'watch = runpy.run_path({str(watch_file)!r})\n'
                                'main = watch["main"]\n'
                                f'main.__globals__.update(CONFIG=Path({str(config)!r}), '
                                f'STATE=Path({str(state_file)!r}), api=lambda _: {{"now": "youtube-ru"}})\n'
                                + ('raise SystemExit(1)\n' if fault == 'yt-failover' else
                                   'raise SystemExit(main())\n'))
            failover.chmod(0o755)
            ref = os.environ.get('VERIFY_REF')
            source = subprocess.check_output(
                ['git', 'show', f'{ref}:infra/scripts/99-verify.sh'], cwd=ROOT, text=True,
            ) if ref else (ROOT / 'infra/scripts/99-verify.sh').read_text()
            source = source.replace('/etc/systemd/networkd.conf.d/60-anysda-foreign.conf', str(networkd))
            source = source.replace('/etc/anysda/ikev2-status.json', str(status))
            source = source.replace('/usr/local/bin/anysda-yt-check', str(yt))
            source = source.replace('/usr/local/bin/anysda-yt-failover', str(failover))
            source = source.replace('/tmp/yt-check.out', str(root / 'yt-check.out'))
            source = source.replace('/opt/vpn2-emergency', str(emergency))
            source = source.replace('/etc/vpn2/emergency-hy2.json', str(hy2_options))
            source = source.replace('/var/anysda/.stamps/23-emergency', str(stamp))
            source = source.replace('/var/anysda/.stamps/10-foreign', str(foreign))
            source = source.replace('/var/lib/vpn2-emergency', str(snapshots))
            script = root / 'verify.sh'
            script.write_text(source)
            traffic = root / 'traffic.py'
            traffic.write_text(TRAFFIC)
            report = root / 'report' / 'verify-report.json'
            env = {**os.environ, 'PATH': f'{bin_dir}:' + os.environ['PATH'],
                   'HOST_TAG': role, 'MGMT_IP': '198.51.100.254',
                   'EXIT_TAGS': '' if fault in ('bare', 'empty-exits') else 'exit',
                   'YT_ROUTE': youtube, 'FAULT': fault,
                   'SCRAPE_COUNTER': str(root / 'scrape-counter'),
                   'VERIFY_SCRAPE_TIMEOUT': '10' if fault in ('warming-up', 'empty-then-up', 'missing-then-up') else '2',
                   'VERIFY_SCRAPE_INTERVAL': '0.02',
                   'HY2_MGMT_PORT': '' if fault == 'missing-mgmt-port' else '9443',
                   'VERIFY_SNAPSHOT_WAIT': '0.2',
                   'VERIFY_TRAFFIC': str(traffic), 'VERIFY_REPORT': str(report), **(extra_env or {})}
            env.pop('BASH_ENV', None)
            result = subprocess.run(['bash', str(script)], env=env, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
            counter = root / 'scrape-counter'
            result.scrape_requests = int(counter.read_text()) if counter.exists() else 0
            result.report = json.loads(report.read_text()) if report.exists() else None
            return result

    def test_fresh_scrape_after_tunnel_restart_ten_times(self):
        false_failures = 0
        for attempt in range(10):
            result = self.run_verify('warming-up')
            print(f'10 -> 20 -> 99 simulation {attempt + 1}: exit={result.returncode}, '
                  f'scrapes={result.scrape_requests}')
            false_failures += result.returncode != 0
        print(f'false failures: {false_failures}/10')
        self.assertEqual(false_failures, 0)

    def test_discovery_settles(self):
        for fault in ('empty-then-up', 'missing-then-up'):
            with self.subTest(fault=fault):
                result = self.run_verify(fault)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertGreaterEqual(result.scrape_requests, 4)

    def test_stale_up_cannot_hide_down_target(self):
        result = self.run_verify('stale-then-down')
        print(result.stdout, end='')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn('verify done', result.stdout)
        self.assertGreater(result.scrape_requests, 1)

    def test_bare_host(self):
        result = self.run_verify('bare')
        print(result.stdout, end='')
        print(f'bare host verify exit={result.returncode}')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('sing-box', result.stdout)
        self.assertIn('tproxy', result.stdout)
        self.assertIn('wg-quick@wg0', result.stdout)
        self.assertNotIn('verify done', result.stdout)

    def test_healthy_roles_and_disabled_youtube(self):
        for role, youtube, fault in [('ru', 'off', 'missing-yt'), ('ru', 'zapret', ''), ('exit', 'off', '')]:
            with self.subTest(role=role, youtube=youtube):
                result = self.run_verify(fault, role, youtube)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn('verify done', result.stdout)

    def test_individual_entry_failures(self):
        faults = {
            'empty-exits': 'ssh экзитов',
            'lost-ufw': 'ufw', 'lost-tproxy': 'tproxy:', 'lost-mark-accept': 'tproxy:',
            'forwarding': 'ip_forward', 'firewall': 'ufw',
            'fail2ban': 'fail2ban', 'node_exporter': 'node_exporter', 'sing-box': 'sing-box',
            'wg-quick@wg0': 'wg-quick@wg0', 'openvpn-server@server': 'openvpn-server@server',
            'strongswan-starter': 'strongswan-starter', 'anysda-ikev2-routing': 'anysda-ikev2-routing',
            'anysda-tproxy-watchdog.timer': 'tproxy wd', 'anysda-ikev2-sync.timer': 'ikev2 sync',
            'tproxy-rule': 'tproxy:', 'tproxy-route': 'tproxy:', 'port-7898': 'tproxy udp',
            'networkd': 'networkd', 'ikev2-conn': 'ikev2:', 'swanctl-error': 'swanctl',
            'port-500': 'udp/500', 'port-4500': 'udp/4500', 'xfrm': 'xfrm0',
            'missing-status': 'ikev2 sync', 'bad-status': 'ikev2 sync',
            'stale-status': 'ikev2 sync', 'future-status': 'ikev2 sync',
            'empty-targets': 'mgmt scrape', 'missing-target': 'mgmt scrape',
            'down-target': 'mgmt scrape', 'bad-json': 'mgmt scrape',
            'stale-scrape': 'mgmt scrape', 'future-scrape': 'mgmt scrape',
            'missing-scrape': 'mgmt scrape', 'invalid-scrape': 'mgmt scrape',
            'naive-scrape': 'mgmt scrape',
            'http://127.0.0.1:8428/api/v1/targets': 'mgmt scrape',
            'http://127.0.0.1:80/': 'frontend', 'http://127.0.0.1:8428/-/healthy': 'vmsingle',
            'ssh-error': 'ssh exit', 'ss-error': 'tproxy udp', 'ip-error': 'tproxy:',
            'sysctl-error': 'ip_forward', 'ufw-error': 'ufw',
            'sysctl-partial': 'ip_forward', 'ufw-partial': 'ufw',
            'ss-partial': 'tproxy udp', 'ip-partial': 'tproxy:',
            'swanctl-partial': 'swanctl', 'curl-partial': 'mgmt scrape',
        }
        for fault, label in faults.items():
            with self.subTest(fault=fault):
                result = self.run_verify(fault)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertTrue(any('✗ ' in line and label in line for line in result.stdout.splitlines()), result.stdout)
                self.assertIn('verify FAILED:', result.stdout)
                self.assertNotIn('verify done', result.stdout)

    def test_enabled_youtube_failures(self):
        for fault, check in (('missing-yt', 'yt-zapret'), ('yt-failed', 'yt-zapret'),
                             ('yt-failover', 'yt-failover'), ('yt-bad-sample', 'yt-failover')):
            with self.subTest(fault=fault):
                result = self.run_verify(fault, youtube='zapret')
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(f'✗ {check}:', result.stdout)

    def test_unmeasured_checks_are_not_green_and_block_verify(self):
        for fault, label in (('yt-no-sample', 'yt-failover: selector'),
                             ('yt-unmeasured', 'yt-zapret: A/B-проверка')):
            with self.subTest(fault=fault):
                result = self.run_verify(fault, youtube='zapret')
                print('\n'.join(line for line in result.stdout.splitlines()
                                if label in line or 'good=' in line or 'verify ' in line))
                print(f'exit={result.returncode}')
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn('не проверено:', result.stdout)
                self.assertIn('HTTP 403', result.stdout)
                self.assertNotIn(f'✓ {label}', result.stdout)
                self.assertNotIn('verify done', result.stdout)

    def test_individual_exit_failures(self):
        for fault in ('port-443', 'port-8443', 'port-9443', 'missing-mgmt-port', 'sing-box', 'ss-error'):
            with self.subTest(fault=fault):
                result = self.run_verify(fault, role='exit')
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn('verify FAILED:', result.stdout)

    def test_emergency_image_version_and_standby(self):
        for role in ('ru', 'exit'):
            with self.subTest(role=role):
                result = self.run_verify(role=role)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn('аварийный образ: версия dev, ревизия aaaaaaaaaaaa', result.stdout)
                self.assertIn('✓ аварийка: образ установлен', result.stdout)
                self.assertIn('✓ аварийка: агент vpn2-emergency active', result.stdout)
        print('\n'.join(line for line in result.stdout.splitlines() if 'авари' in line))
        self.assertIn('✓ аварийка: Hysteria2 в ожидании', result.stdout)
        faults = {'emergency-release': 'аварийка: образ', 'emergency-static/instructions.html': 'аварийка: образ',
                  'emergency-agent.py': 'аварийка: образ', 'vpn2-emergency.service': 'аварийка: агент',
                  'emergency-listening': 'аварийка: Hysteria2', 'emergency-hy2-options': 'аварийка: Hysteria2'}
        for fault, label in faults.items():
            with self.subTest(fault=fault):
                result = self.run_verify(fault, role='exit')
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn('✗ ' + label, result.stdout)
        # First deploy verifies each exit right after 10-foreign, before stage 23.
        result = self.run_verify('emergency-not-yet', role='exit')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn('аварийка: образ', result.stdout)
        self.assertIn('аварийка: стадия 23 ещё не ставилась', result.stdout)

    def test_emergency_client_snapshot(self):
        # VPN2-514: entry отправляет снимок, экзит держит его свежим.
        result = self.run_verify(role='ru')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('✓ аварийка: отправка снимка клиентов enabled и active', result.stdout)
        result = self.run_verify(role='exit')
        print('\n'.join(line for line in result.stdout.splitlines() if 'снимок' in line))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('✓ аварийка: снимок клиентов от entry свежий', result.stdout)
        self.assertIn('предел 120 с', result.stdout)
        for fault, role in (('vpn2-emergency-snapshot.service', 'ru'), ('emergency-snapshot-missing', 'exit'),
                            ('emergency-snapshot-stale', 'exit')):
            with self.subTest(fault=fault):
                result = self.run_verify(fault, role=role)
                print('\n'.join(line for line in result.stdout.splitlines() if 'сним' in line))
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn('✗ аварийка: ' + ('отправка' if role == 'ru' else 'снимок'), result.stdout)
        # Verify сразу после 10-foreign: на entry ещё старая версия без отправки.
        result = self.run_verify('emergency-before-23', role='exit')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('снимок клиентов проверит итоговый verify', result.stdout)
        self.assertNotIn('аварийка: снимок клиентов от entry', result.stdout)

    def test_update_from_stage_23_without_image(self):
        # Update from a dev install whose stage 23 predates the image: the exit is
        # verified after 10-foreign, before the new stage 23 replaces the stamp.
        result = self.run_verify('emergency-old-stage', role='exit')
        print('\n'.join(line for line in result.stdout.splitlines() if 'авари' in line))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn('✗ аварийка', result.stdout)
        self.assertIn('✓ аварийка: агент vpn2-emergency active', result.stdout)
        self.assertIn('аварийка: стадия 23 стоит без образа', result.stdout)

    def test_only_youtube_can_report_unmeasured(self):
        for fault in ('systemctl-error2', 'ssh-error2', 'curl-error2'):
            with self.subTest(fault=fault):
                result = self.run_verify(fault)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn('✗ ', result.stdout)
                self.assertNotIn('не проверено: команда', result.stdout)

    def test_traffic_result_decides_exit_code(self):
        for fault, code, line in (('', 0, '✓ WireGuard: подключение'),
                                  ('traffic-unmeasured', 2, '? IKEv2: внешняя проба портов — не проверено'),
                                  ('traffic-failed', 1, '✗ WireGuard: маршрут: через экзит exit'),
                                  ('traffic-mixed', 1, '✗ WireGuard: маршрут: через экзит exit'),
                                  ('traffic-crash', 1, '✗ трафик: контрольный клиент')):
            with self.subTest(fault=fault):
                result = self.run_verify(fault)
                print(f'{fault or "healthy"}: exit={result.returncode}')
                self.assertEqual(result.returncode, code, result.stdout)
                self.assertIn(line, result.stdout)
                self.assertEqual(result.report['code'], code)

    def test_summary_counts_every_traffic_row(self):
        # Три «Ошибка» и одна «Не проверено» от модуля - столько же в итоге, не «1».
        result = self.run_verify('traffic-many')
        print('\n'.join(line for line in result.stdout.splitlines() if 'verify ' in line))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('verify FAILED: проверок не пройдено: 3; не проверено: 1', result.stdout)
        crashed = self.run_verify('traffic-crash')
        self.assertIn('verify FAILED: проверок не пройдено: 1; не проверено: 0', crashed.stdout)

    def test_insecure_exit_warning_in_summary_and_report(self):
        warning = 'exit: exits[].hy2_insecure: true — сертификат экзита не проверяется'
        for fault, code in (('', 0), ('traffic-unmeasured', 2), ('traffic-failed', 1)):
            with self.subTest(fault=fault):
                result = self.run_verify(fault, extra_env={'EXIT_HY2_INSECURE': '1'})
                self.assertEqual(result.returncode, code, result.stdout)
                self.assertIn('[ru] ⚠ ' + warning, result.stdout)
                self.assertEqual(len(result.report['warnings']), 1, result.report)
                self.assertTrue(result.report['warnings'][0].startswith(warning))
                self.assertEqual(result.report['code'], code)
        clean = self.run_verify('traffic-failed')
        self.assertEqual(clean.report['warnings'], [])
        self.assertNotIn('hy2_insecure', clean.stdout)

    def test_report_rows_for_ci(self):
        result = self.run_verify('traffic-mixed')
        rows = result.report['checks']
        self.assertEqual(result.report['host'], 'ru')
        for row in rows:
            self.assertEqual(set(row), {'check', 'component', 'result', 'reason', 'time'})
            self.assertIn(row['result'], ('Работает', 'Ошибка', 'Не проверено', 'Не требуется'))
        by_check = {(row['component'], row['check']): row for row in rows}
        self.assertEqual(by_check[('ru', 'sing-box active')]['result'], 'Работает')
        self.assertEqual(by_check[('tproxy', 'правило fwmark 0x42 → table 101')]['result'], 'Работает')
        self.assertEqual(by_check[('WireGuard', 'маршрут: через экзит exit')]['result'], 'Ошибка')
        self.assertEqual(by_check[('IKEv2', 'внешняя проба портов')]['result'], 'Не проверено')
        self.assertIn('отчёт: ', result.stdout)
        failed = self.run_verify('sing-box')
        row = next(r for r in failed.report['checks'] if r['check'] == 'sing-box active')
        self.assertEqual((row['result'], row['reason']), ('Ошибка', 'код 3'))
        self.assertEqual(failed.report['code'], 1)


if __name__ == '__main__':
    unittest.main()
