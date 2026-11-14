"""Стадия verify (`vpn2.verify.stage`) на подставных командах хоста: без root и служб.

Подставной ssh (FAKE_SSH) - sshd с login shell: сначала .bashrc экзита в режиме FAKE_BASHRC, потом команда.
"""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import pytest

from vpn2.emergency import snapshot
from vpn2.verify import stage as verify_stage

ROOT = Path(__file__).resolve().parents[3]
# Предел на весь сценарий - защита от зависания, а не мерило скорости.
SCENARIO_HANG = 120
MOCK = r'''#!/usr/bin/env python3
import datetime
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
args = ' '.join(sys.argv[1:])
if name == 'ssh' and os.environ.get('FAKE_SSH'):
    os.execv(os.environ['FAKE_SSH'], [os.environ['FAKE_SSH']] + sys.argv[1:])
slow = os.environ.get('SLOW_ONCE')
if slow and not Path(slow).exists():
    import time
    Path(slow).touch()
    time.sleep(22)
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


FAKE_SSH = r'''#!/bin/bash
while (($#)); do
  case $1 in -[oiFpl]) shift 2 ;; -*) shift ;; *) shift; break ;; esac
done
cmd="$*"
echo $$ >> "${FAKE_PIDS:-/dev/null}"
noise() { printf 'Welcome to exit node\nmotd line\n'; printf 'bashrc warning\n' >&2; }
hang() { sleep "$FAKE_SLEEP" & echo $! >> "${FAKE_PIDS:-/dev/null}"; wait $!; }
case ${FAKE_BASHRC:-} in
  noise) noise ;;
  exit0) exit 0 ;;
  exit1) noise; exit 1 ;;
  conn) echo 'ssh: connect to host exit port 22: Connection refused' >&2; exit 255 ;;
  hang-before) noise; hang ;;
  hang-after) noise; bash -c "$cmd"; hang; exit 0 ;;
  late) printf 'Welcome to exit node\n'; bash -c "$cmd"; rc=$?; printf 'bashrc warning\n' >&2; exit $rc ;;
  erronly) bash -c "$cmd"; rc=$?; printf 'bash: foo: command not found\n' >&2; exit $rc ;;
  nomark) printf 'bashrc warning\n' >&2; exit 0 ;;
  code-before) noise; sleep "${FAKE_SLEEP:-0}"; exit "$FAKE_RC" ;;
  code-after) bash -c "$cmd"; sleep "${FAKE_SLEEP:-0}"; exit "$FAKE_RC" ;;
esac
bash -c "$cmd"
'''


def run_verify(fault='', role='ru', youtube='off', extra_env=None):
    """Стадия verify на временном дереве: вывод, код, число опросов метрик и отчёт."""
    with tempfile.TemporaryDirectory(prefix='verify-test-') as directory:
        root = Path(directory)
        bin_dir = root / 'bin'
        bin_dir.mkdir()
        for name in ('systemctl', 'sysctl', 'ufw', 'curl', 'ip', 'ss', 'ssh', 'swanctl', 'iptables'):
            (bin_dir / name).write_text(MOCK)
            (bin_dir / name).chmod(0o755)
        (root / 'fake-ssh').write_text(FAKE_SSH)
        (root / 'fake-ssh').chmod(0o755)
        # Файлы проверяются по-настоящему, на одноразовых путях.
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
                          + 'exit ' + ('2' if fault == 'yt-unmeasured' else '1' if fault == 'yt-failed' else '0') + '\n')
            yt.chmod(0o755)
        state_file = root / 'probe.json'
        unmeasured = fault in ('yt-no-sample', 'yt-unknown-and-failed')
        state = {'good': 0 if unmeasured else 3, 'bad': 0, 'unmeasured': unmeasured, 'checked_at': time.time(),
                 'last_probe': 'TCP video: HTTP 403' if fault == 'yt-no-sample' else 'TCP video: HTTP 206'}
        if fault == 'yt-bad-sample':
            state.update(good=0, bad=1, last_probe='очередь, nft-правила или TCP-путь обхода не работают')
        elif fault == 'yt-legacy-state':
            del state['checked_at']
        if fault != 'yt-missing-state':
            state_file.write_text(json.dumps(state))
        # Аварийка: статика страницы из образа, агент - пакет vpn2 из venv хоста.
        emergency = root / 'emergency'
        (emergency / 'static').mkdir(parents=True)
        for name in ('static/index.html', 'static/instructions.html'):
            if fault != 'emergency-' + name:
                (emergency / name).touch()
        # Прежние раскладки держали копию пакета рядом: выпуск - vpn2_emergency, dev - vpn2.
        layouts = {'emergency-release-layout': 'vpn2_emergency', 'emergency-dev-layout': 'vpn2/emergency',
                   'emergency-release-layout-after-23': 'vpn2_emergency'}
        if fault in layouts:
            shutil.rmtree(emergency / 'static')
            (emergency / layouts[fault] / 'static').mkdir(parents=True)
        venv = root / 'venv'
        (venv / 'bin').mkdir(parents=True)
        (venv / 'bin/python').write_text(f'#!/bin/sh\nPYTHONPATH={ROOT / "infra/vpn2/src"} exec {sys.executable} "$@"\n')
        (venv / 'bin/python').chmod(0o755)
        if fault != 'emergency-venv':
            (venv / 'bin/vpn2').write_text('#!/bin/sh\n')
            (venv / 'bin/vpn2').chmod(0o755)
        # Стадия 23 до образа оставила пустой штамп и не ставила release.json и Hysteria2.
        old_stage = fault == 'emergency-old-stage'
        if fault != 'emergency-release' and not old_stage:
            (emergency / 'release.json').write_text(json.dumps(
                {'version': 'dev', 'revision': 'a' * 40, 'image': 'sha256:' + 'b' * 64}))
        (root / 'stamps').mkdir()
        stamp = root / 'stamps/23-emergency'
        if old_stage:
            stamp.touch()
        elif fault != 'emergency-not-yet':
            stamp.write_text('image sha256:' + 'b' * 64 + '\n')
        snapshots = root / 'emergency-state'
        snapshots.mkdir(mode=0o700)
        if fault not in ('emergency-snapshot-missing', 'emergency-before-23'):
            age = 300 if fault == 'emergency-snapshot-stale' else 10
            image = {'schema': 1, 'version': 1, 'installation_id': 'test',
                     'created_at': snapshot.iso_time(time.time() - age), 'admin_chat_id': None, 'bot_token': None,
                     'exits': [{'id': 'exit', 'name': 'exit', 'ip': '198.51.100.20', 'priority': 0}], 'clients': []}
            (snapshots / snapshot.NAME).write_bytes(snapshot.encode(image))
            os.utime(snapshots / snapshot.NAME, (time.time() - age,) * 2)
        # Штамп 10-foreign новее штампа 23: verify сразу после 10-foreign.
        foreign = root / 'stamps/10-foreign'
        foreign.touch()
        if fault not in ('emergency-before-23', 'emergency-release-layout', 'emergency-dev-layout'):
            os.utime(foreign, (time.time() - 600,) * 2)
        (bin_dir / 'setpriv').write_text('#!/bin/sh\nwhile [ "$1" != -- ]; do shift; done\nshift\nexec "$@"\n')
        (bin_dir / 'setpriv').chmod(0o755)
        hy2_options = root / 'emergency-hy2.json'
        if fault != 'emergency-hy2-options' and not old_stage:
            hy2_options.write_text(json.dumps({'server_ip': '198.51.100.1', 'port': 18443}))
        config = root / 'router.json'
        config.write_text('{"outbounds": [{"tag": "youtube-auto"}]}')
        paths = {'NETWORKD_DROPIN': networkd, 'IKEV2_STATUS': status, 'EMERGENCY_ROOT': emergency, 'VENV': venv,
                 'EMERGENCY_HY2': hy2_options, 'STAMPS': stamp.parent, 'EMERGENCY_STATE': snapshots}
        script = root / 'verify.py'
        script.write_text(
            'import os\nimport sys\nfrom pathlib import Path\n'
            f'sys.path.insert(0, {str(ROOT / "infra/vpn2/src")!r})\n'
            'from vpn2.verify import stage\n'
            + ''.join(f'stage.{name} = Path({str(path)!r})\n' for name, path in paths.items())
            + f'stage.YT_CHECK = {str(yt)!r}\n'
            # Сторож YouTube: его состояние и clash API, отказ API - сбой контроллера.
            + f'stage.yt.CONFIG, stage.yt.STATE = Path({str(config)!r}), Path({str(state_file)!r})\n'
            + ('def api(*_):\n    raise OSError("clash api")\n' if fault == 'yt-failover'
               else 'def api(*_):\n    return {"now": "youtube-ru"}\n')
            + 'stage.yt.api = api\n'
            # Селекторы транспорта sing-box: hy2 на всех экзитах.
            + 'stage.exit_transport = lambda _tag: (0, "")\n'
            'raise SystemExit(stage.run(dict(os.environ)))\n')
        (root / 'traffic.py').write_text(TRAFFIC)
        report = root / 'report' / 'verify-report.json'
        env = {**os.environ, 'PATH': f'{bin_dir}:' + os.environ['PATH'],
               'HOST_TAG': role, 'MGMT_IP': '198.51.100.254',
               'EXIT_TAGS': '' if fault in ('bare', 'empty-exits') else 'exit',
               'YT_ROUTE': youtube, 'FAULT': fault, 'SCRAPE_COUNTER': str(root / 'scrape-counter'),
               'VERIFY_SCRAPE_TIMEOUT': '10' if fault in ('warming-up', 'empty-then-up', 'missing-then-up') else '2',
               'VERIFY_SCRAPE_INTERVAL': '0.02', 'HY2_MGMT_PORT': '' if fault == 'missing-mgmt-port' else '9443',
               'VERIFY_SNAPSHOT_WAIT': '0.2', 'VERIFY_TRAFFIC': str(root / 'traffic.py'), 'VERIFY_REPORT': str(report),
               **(extra_env or {})}
        if env.get('FAKE_BASHRC') is not None:
            env['FAKE_SSH'] = str(root / 'fake-ssh')
        env.pop('BASH_ENV', None)
        result = subprocess.run([sys.executable, '-B', str(script)], env=env, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=SCENARIO_HANG)
        counter = root / 'scrape-counter'
        result.scrape_requests = int(counter.read_text()) if counter.exists() else 0
        result.report = json.loads(report.read_text()) if report.exists() else None
        return result


def failed(result, label):
    return any('✗ ' in line and label in line for line in result.stdout.splitlines())


# Каждый сбой входа - отдельный прогон: verify падает, называет свою проверку и не пишет «verify done».
ENTRY_FAULTS = {
    'empty-exits': 'ssh экзитов', 'lost-ufw': 'ufw', 'lost-tproxy': 'tproxy:', 'lost-mark-accept': 'tproxy:',
    'forwarding': 'ip_forward', 'firewall': 'ufw', 'fail2ban': 'fail2ban', 'node_exporter': 'node_exporter',
    'sing-box': 'sing-box', 'wg-quick@wg0': 'wg-quick@wg0', 'openvpn-server@server': 'openvpn-server@server',
    'strongswan-starter': 'strongswan-starter', 'anysda-ikev2-routing': 'anysda-ikev2-routing',
    'anysda-tproxy-watchdog.timer': 'tproxy wd', 'anysda-ikev2-sync.timer': 'ikev2 sync',
    'tproxy-rule': 'tproxy:', 'tproxy-route': 'tproxy:', 'port-7898': 'tproxy udp', 'networkd': 'networkd',
    'ikev2-conn': 'ikev2:', 'swanctl-error': 'swanctl', 'port-500': 'udp/500', 'port-4500': 'udp/4500',
    'xfrm': 'xfrm0', 'missing-status': 'ikev2 sync', 'bad-status': 'ikev2 sync',
    'stale-status': 'ikev2 sync', 'future-status': 'ikev2 sync',
    **{name: 'mgmt scrape' for name in ('empty-targets', 'missing-target', 'down-target', 'bad-json',
                                         'stale-scrape', 'future-scrape', 'missing-scrape', 'invalid-scrape',
                                         'naive-scrape', 'http://127.0.0.1:8428/api/v1/targets',
                                         'curl-partial')},
    'http://127.0.0.1:80/': 'frontend', 'http://127.0.0.1:8428/-/healthy': 'vmsingle',
    'ssh-error': 'ssh exit', 'ss-error': 'tproxy udp', 'ip-error': 'tproxy:', 'sysctl-error': 'ip_forward',
    'ufw-error': 'ufw', 'sysctl-partial': 'ip_forward', 'ufw-partial': 'ufw', 'ss-partial': 'tproxy udp',
    'ip-partial': 'tproxy:', 'swanctl-partial': 'swanctl',
}


@pytest.mark.parametrize('fault, label', ENTRY_FAULTS.items())
def test_individual_entry_failure(fault, label):
    result = run_verify(fault)
    assert result.returncode != 0, result.stdout
    assert failed(result, label), result.stdout
    assert 'verify FAILED:' in result.stdout and 'verify done' not in result.stdout


class VerifyTests(unittest.TestCase):
    def test_loaded_eap_mschapv2_is_required(self):
        for fault in ('', 'ikev2-plugin-missing', 'ikev2-plugin-substring', 'ikev2-plugin-error', 'ikev2-plugin-stderr'):
            with self.subTest(fault=fault):
                result = run_verify(fault)
                self.assertEqual(result.returncode, 1 if fault else 0, result.stdout)
                self.assertIn(('✗' if fault else '✓') + ' ikev2: charon загрузил eap-mschapv2', result.stdout)

    def test_fresh_scrape_after_tunnel_restart_ten_times(self):
        self.assertEqual([run_verify('warming-up').returncode for _ in range(10)], [0] * 10)

    def test_discovery_settles_and_stale_up_cannot_hide_down_target(self):
        for fault in ('empty-then-up', 'missing-then-up'):
            with self.subTest(fault=fault):
                result = run_verify(fault)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertGreaterEqual(result.scrape_requests, 4)
        result = run_verify('stale-then-down')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn('verify done', result.stdout)
        self.assertGreater(result.scrape_requests, 1)

    def test_bare_host(self):
        result = run_verify('bare')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        for name in ('sing-box', 'tproxy', 'wg-quick@wg0'):
            self.assertIn(name, result.stdout)
        self.assertNotIn('verify done', result.stdout)

    def test_healthy_roles_and_disabled_youtube(self):
        for role, youtube, fault in [('ru', 'off', 'missing-yt'), ('ru', 'zapret', ''), ('exit', 'off', '')]:
            with self.subTest(role=role, youtube=youtube):
                result = run_verify(fault, role, youtube)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn('verify done', result.stdout)

    def test_enabled_youtube_failures(self):
        for fault, check in (('missing-yt', 'yt-zapret'), ('yt-failed', 'yt-zapret'),
                             ('yt-failover', 'yt-failover'), ('yt-bad-sample', 'yt-failover')):
            with self.subTest(fault=fault):
                result = run_verify(fault, youtube='zapret')
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(f'✗ {check}:', result.stdout)

    def test_unmeasured_checks_are_not_green_and_block_verify(self):
        for fault, label in (('yt-no-sample', 'yt-failover: selector'), ('yt-unmeasured', 'yt-zapret: A/B-проверка')):
            with self.subTest(fault=fault):
                result = run_verify(fault, youtube='zapret')
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn('не проверено:', result.stdout)
                self.assertIn('HTTP 403', result.stdout)
                self.assertNotIn(f'✓ {label}', result.stdout)
                self.assertNotIn('verify done', result.stdout)

    def test_only_youtube_can_report_unmeasured(self):
        for fault in ('systemctl-error2', 'ssh-error2', 'curl-error2'):
            with self.subTest(fault=fault):
                result = run_verify(fault)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn('✗ ', result.stdout)
                self.assertNotIn('не проверено: команда', result.stdout)

    def test_individual_exit_failures(self):
        for fault in ('port-443', 'port-8443', 'port-9443', 'missing-mgmt-port', 'sing-box', 'ss-error'):
            with self.subTest(fault=fault):
                result = run_verify(fault, role='exit')
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn('verify FAILED:', result.stdout)

    def test_emergency_image_version_and_standby(self):
        for role in ('ru', 'exit'):
            with self.subTest(role=role):
                result = run_verify(role=role)
                self.assertEqual(result.returncode, 0, result.stdout)
                for line in ('аварийный образ: версия dev, ревизия aaaaaaaaaaaa', '✓ аварийка: образ установлен',
                             '✓ аварийка: агент vpn2-emergency active'):
                    self.assertIn(line, result.stdout)
        self.assertIn('✓ аварийка: Hysteria2 в ожидании', result.stdout)
        faults = {'emergency-release': 'аварийка: образ', 'emergency-static/instructions.html': 'аварийка: образ',
                  'emergency-venv': 'аварийка: образ', 'vpn2-emergency.service': 'аварийка: агент',
                  'emergency-listening': 'аварийка: Hysteria2', 'emergency-hy2-options': 'аварийка: Hysteria2'}
        for fault, label in faults.items():
            with self.subTest(fault=fault):
                result = run_verify(fault, role='exit')
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn('✗ ' + label, result.stdout)
        # Первый деплой проверяет каждый экзит сразу после 10-foreign, до стадии 23.
        result = run_verify('emergency-not-yet', role='exit')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn('аварийка: образ', result.stdout)
        self.assertIn('аварийка: стадия 23 ещё не ставилась', result.stdout)

    def test_emergency_client_snapshot(self):
        # VPN2-514: entry отправляет снимок, экзит держит его свежим.
        self.assertIn('✓ аварийка: отправка снимка клиентов enabled и active', run_verify(role='ru').stdout)
        result = run_verify(role='exit')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('✓ аварийка: снимок клиентов от entry свежий', result.stdout)
        self.assertIn('предел 120 с', result.stdout)
        for fault, role in (('vpn2-emergency-snapshot.service', 'ru'), ('emergency-snapshot-missing', 'exit'),
                            ('emergency-snapshot-stale', 'exit')):
            with self.subTest(fault=fault):
                result = run_verify(fault, role=role)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn('✗ аварийка: ' + ('отправка' if role == 'ru' else 'снимок'), result.stdout)
        # Verify сразу после 10-foreign: на entry ещё старая версия без отправки.
        result = run_verify('emergency-before-23', role='exit')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('снимок клиентов проверит итоговый verify', result.stdout)
        self.assertNotIn('аварийка: снимок клиентов от entry', result.stdout)

    def test_update_from_stage_23_without_image_or_with_package_copy(self):
        result = run_verify('emergency-old-stage', role='exit')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn('✗ аварийка', result.stdout)
        self.assertIn('✓ аварийка: агент vpn2-emergency active', result.stdout)
        self.assertIn('аварийка: стадия 23 стоит без образа', result.stdout)
        # Обновление с выпуска или прежнего dev: копию пакета рядом заменит стадия 23 этого deploy.
        for fault in ('emergency-release-layout', 'emergency-dev-layout'):
            with self.subTest(fault=fault):
                result = run_verify(fault, role='exit')
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn('✓ аварийка: образ установлен', result.stdout)
                self.assertIn('прежняя раскладка до стадии 23', result.stdout)
        # После стадии 23 копия пакета - ошибка: стадия не заменила файлы.
        result = run_verify('emergency-release-layout-after-23', role='exit')
        self.assertEqual(result.returncode, 1, result.stdout)
        for line in ('✗ аварийка: образ установлен', 'нет static/index.html', 'осталась копия пакета'):
            self.assertIn(line, result.stdout)

    def test_traffic_result_decides_exit_code(self):
        for fault, code, line in (('', 0, '✓ WireGuard: подключение'),
                                  ('traffic-unmeasured', 2, '? IKEv2: внешняя проба портов — не проверено'),
                                  ('traffic-failed', 1, '✗ WireGuard: маршрут: через экзит exit'),
                                  ('traffic-mixed', 1, '✗ WireGuard: маршрут: через экзит exit'),
                                  ('traffic-crash', 1, '✗ трафик: контрольный клиент')):
            with self.subTest(fault=fault):
                result = run_verify(fault)
                self.assertEqual((result.returncode, result.report['code']), (code, code), result.stdout)
                self.assertIn(line, result.stdout)

    def test_summary_counts_every_traffic_row(self):
        result = run_verify('traffic-many')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('verify FAILED: проверок не пройдено: 3; не проверено: 1', result.stdout)
        self.assertIn('verify FAILED: проверок не пройдено: 1; не проверено: 0', run_verify('traffic-crash').stdout)

    def test_insecure_exit_warning_in_summary_and_report(self):
        warning = 'exit: exits[].hy2_insecure: true — сертификат экзита не проверяется'
        for fault, code in (('', 0), ('traffic-unmeasured', 2), ('traffic-failed', 1)):
            with self.subTest(fault=fault):
                result = run_verify(fault, extra_env={'EXIT_HY2_INSECURE': '1'})
                self.assertEqual((result.returncode, result.report['code']), (code, code), result.stdout)
                self.assertIn('[ru] ⚠ ' + warning, result.stdout)
                self.assertEqual(len(result.report['warnings']), 1, result.report)
                self.assertTrue(result.report['warnings'][0].startswith(warning))
        clean = run_verify('traffic-failed')
        self.assertEqual(clean.report['warnings'], [])
        self.assertNotIn('hy2_insecure', clean.stdout)

    def test_report_rows_for_ci(self):
        result = run_verify('traffic-mixed')
        self.assertEqual(result.report['host'], 'ru')
        for row in result.report['checks']:
            self.assertEqual(set(row), {'check', 'component', 'result', 'reason', 'time'})
            self.assertIn(row['result'], ('Работает', 'Ошибка', 'Не проверено', 'Не требуется'))
        by_check = {(row['component'], row['check']): row['result'] for row in result.report['checks']}
        self.assertEqual(by_check[('ru', 'sing-box active')], 'Работает')
        self.assertEqual(by_check[('tproxy', 'правило fwmark 0x42 → table 101')], 'Работает')
        self.assertEqual(by_check[('WireGuard', 'маршрут: через экзит exit')], 'Ошибка')
        self.assertEqual(by_check[('IKEv2', 'внешняя проба портов')], 'Не проверено')
        self.assertIn('отчёт: ', result.stdout)
        failed_run = run_verify('sing-box')
        row = next(r for r in failed_run.report['checks'] if r['check'] == 'sing-box active')
        self.assertEqual((row['result'], row['reason'], failed_run.report['code']), ('Ошибка', 'код 3', 1))

    def test_slow_healthy_scenario_is_not_a_hang(self):
        # Медленный раннер: один вызов заглушки спит 22 с, исправный сценарий всё равно зелёный.
        with tempfile.TemporaryDirectory() as directory:
            once = os.path.join(directory, 'slept')
            result = run_verify(extra_env={'SLOW_ONCE': once})
            self.assertTrue(os.path.exists(once), 'заглушка не спала: сценарий не медленный')
        self.assertEqual(result.returncode, 0, result.stdout)


LABEL = 'ssh exit через hy2-mgmt'
LIMIT = 3
NOISE = ('Welcome to exit node', 'motd line', 'bashrc warning', 'command not found')
NO_MARK = r'не дош[её]л|не выполн'
BASHRC = r'\.bashrc|' + NO_MARK
LIMIT_REASON = r'(?i)предел|тайм-?аут|timeout|не уложил|не ответил|не заверш|завис'
SSH_ERROR = 'ssh: connect to host exit port 22: Connection refused'


class ExitSshTests(unittest.TestCase):
    """Вход на экзит через hy2-mgmt: вывод .bashrc не в логе, exit в .bashrc не проход, общий предел времени."""

    def verify(self, bashrc, **env):
        """(вывод, код, строка отчёта, секунды прогона)."""
        started = time.monotonic()
        result = run_verify(extra_env={'FAKE_BASHRC': bashrc, 'SSH_PROBE_TIMEOUT': str(LIMIT),
                                       **{key: str(value) for key, value in env.items()}})
        rows = [r for r in (result.report or {}).get('checks', []) if r['check'] == LABEL]
        self.assertEqual(len(rows), 1, f'нет строки отчёта «{LABEL}»:\n{result.stdout}')
        for line in NOISE:
            self.assertNotIn(line, result.stdout, f'вывод .bashrc экзита попал в лог verify:\n{result.stdout}')
            self.assertNotIn(line, rows[0]['reason'])
        return result.stdout, result.returncode, rows[0], time.monotonic() - started

    def assert_pass(self, bashrc, **env):
        out, code, row, _ = self.verify(bashrc, **env)
        self.assertIn(f'✓ {LABEL}', out)
        self.assertEqual((row['result'], code), ('Работает', 0), out)

    def assert_failure(self, bashrc, reason, *, limit=False, **env):
        out, code, row, elapsed = self.verify(bashrc, **env)
        self.assertNotIn(f'✓ {LABEL}', out)
        self.assertIn(f'✗ {LABEL}', out)
        self.assertEqual(row['result'], 'Ошибка')
        self.assertNotEqual(code, 0)
        self.assertRegex(row['reason'], reason)
        if limit:
            self.assertRegex(row['reason'], rf'(?<!\d){LIMIT}\s*(с|сек\w*|s)(?!\w)')
            self.assertNotRegex(row['reason'], NO_MARK)
        else:
            self.assertNotRegex(row['reason'], LIMIT_REASON, 'код или отказ экзита принят за предел времени')
        return out, row, elapsed

    def test_clean_noisy_and_late_stderr_pass(self):
        for bashrc in ('', 'noise', 'late', 'erronly'):
            with self.subTest(bashrc=bashrc):
                self.assert_pass(bashrc)

    def test_bashrc_exit_is_not_a_pass(self):
        for bashrc in ('exit0', 'exit1', 'nomark'):
            with self.subTest(bashrc=bashrc):
                self.assert_failure(bashrc, BASHRC)

    def test_unreachable_node_shows_ssh_error(self):
        out, row, _ = self.assert_failure('conn', '255')
        self.assertIn(SSH_ERROR, out)
        self.assertIn(SSH_ERROR, row['reason'])
        self.assertNotRegex(row['reason'], BASHRC)

    def test_remote_code_124_137_is_not_the_limit(self):
        baseline = self.verify('')[3]
        for where, rc in ((w, rc) for w in ('before', 'after') for rc in (124, 137)):
            with self.subTest(where=where, rc=rc):
                *_, elapsed = self.assert_failure(f'code-{where}', NO_MARK if where == 'before' else rf'(?<!\d){rc}(?!\d)',
                                                 FAKE_RC=rc)
                self.assertLess(elapsed - baseline, LIMIT - 1, 'verify ждал предел при мгновенном отказе экзита')

    def test_remote_code_just_before_limit_is_not_the_limit(self):
        # Часы stage на дробной секунде: прошедшее в целых секундах набегает до предела раньше, чем он истёк.
        for where, rc, delay in ((w, rc, d) for w in ('before', 'after') for rc in (124, 137)
                                 for d in (LIMIT - 0.5, LIMIT - 0.1)):
            with self.subTest(where=where, rc=rc, delay=delay):
                row = late_exit_row(delay, FAKE_BASHRC=f'code-{where}', FAKE_RC=str(rc))
                self.assertEqual(row['result'], 'Ошибка')
                self.assertRegex(row['reason'], NO_MARK if where == 'before' else rf'(?<!\d){rc}(?!\d)')
                self.assertNotRegex(row['reason'], LIMIT_REASON, 'код экзита до истечения предела принят за предел')

    def test_hang_fails_by_limit_and_leaves_nothing(self):
        baseline = self.verify('')[3]
        for bashrc in ('hang-before', 'hang-after'):
            with self.subTest(bashrc=bashrc), tempfile.TemporaryDirectory() as directory:
                pids, tmp = Path(directory) / 'pids', Path(directory) / 'tmp'
                tmp.mkdir()
                *_, elapsed = self.assert_failure(bashrc, LIMIT_REASON, limit=True, FAKE_SLEEP=9, FAKE_PIDS=pids,
                                                 TMPDIR=tmp)
                self.assertLess(elapsed - baseline, LIMIT + 3, f'verify ждал зависший экзит {elapsed:.1f} с')
                pid_list = [int(p) for p in pids.read_text().split()]
                self.assertGreaterEqual(len(pid_list), 2, 'заглушка ssh не записала pid свой и sleep')
                deadline = time.monotonic() + 2
                while alive(pid_list) and time.monotonic() < deadline:
                    time.sleep(0.1)
                live = alive(pid_list)
                for pid in live:
                    os.kill(pid, 9)
                self.assertEqual(live, [], 'заглушка ssh или её дети живут после выхода по пределу')
                self.assertEqual(list(tmp.iterdir()), [], 'временный файл ошибок ssh не удалён')

    def test_bashrc_just_under_limit_passes(self):
        self.assert_pass('hang-before', FAKE_SLEEP=LIMIT - 1.5)


class LateExitClock:
    """Часы stage: заглушка ssh отвечает сразу, а по часам stage - через delay с от дробной секунды."""

    def __init__(self, delay):
        self.now, self.delay = 1_800_000_000.95, delay

    def monotonic(self):
        return self.now

    time = monotonic

    def sleep(self, seconds):
        self.now += seconds

    def popen(self):
        clock = self

        class Late(subprocess.Popen):
            def wait(self, timeout=None):
                rc = super().wait()
                clock.now += clock.delay
                if timeout is not None and clock.delay >= timeout:
                    raise subprocess.TimeoutExpired(self.args, timeout)
                return rc

        return Late


def late_exit_row(delay, **env):
    """Строка отчёта «ssh exit через hy2-mgmt»: экзит ответил через delay с по часам stage, предел LIMIT."""
    clock = LateExitClock(delay)
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / 'ssh').write_text(FAKE_SSH)
        (Path(directory) / 'ssh').chmod(0o755)
        node = verify_stage.Stage({'HOST_TAG': 'ru'}, Path(directory) / 'rows.jsonl')
        with mock.patch.dict(os.environ, {'PATH': f"{directory}:{os.environ['PATH']}", **env}), \
                mock.patch.object(verify_stage, 'time', clock), \
                mock.patch.object(verify_stage.subprocess, 'Popen', clock.popen()), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            node.check(LABEL, verify_stage.exit_ssh, 'exit', str(LIMIT))
        return node.read_rows()[0]


def alive(pids):
    live = []
    for pid in pids:
        try:
            state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            continue
        if state != 'Z':
            live.append(pid)
    return live


STANDBY = '''import sys
from pathlib import Path
from vpn2.verify import stage

root = Path(sys.argv[1])
stage.STAMPS = root
code, output = stage.emergency_standby(root / 'hy2.json', root / 'state/agent.json', not stage.after_stage())
print(output)
raise SystemExit(code)
'''


class StandbyBeforeStage23Tests(unittest.TestCase):
    """Entry восстановлен из бэкапа, экзиты живы: verify после 10-foreign встречает аварийку, которую entry гасит."""

    def standby(self, stage23_newer, active):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'bin').mkdir()
            (root / 'bin/ss').write_text('#!/bin/sh\necho "UNCONN 0 0 0.0.0.0:18443 0.0.0.0:*"\n')
            (root / 'bin/ss').chmod(0o755)
            (root / 'hy2.json').write_text(json.dumps({'server_ip': '192.0.2.2', 'port': 18443}))
            (root / 'state').mkdir()
            (root / 'state/agent.json').write_text(json.dumps({'leader': {'active': active}}))
            now = int(time.time()) - 10_000
            stamps = [root / '10-foreign', root / '23-emergency']
            for index, path in enumerate(stamps if stage23_newer else stamps[::-1]):
                path.touch()
                os.utime(path, (now + index, now + index))
            env = dict(os.environ, PATH=f"{root / 'bin'}:{os.environ['PATH']}", PYTHONPATH=str(ROOT / 'infra/vpn2/src'))
            return subprocess.run([sys.executable, '-B', '-c', STANDBY, str(root)], env=env, capture_output=True,
                                  text=True)

    def test_listening_port_only_during_emergency_before_stage_23(self):
        self.assertEqual(self.standby(stage23_newer=False, active=True).returncode, 0)
        for stage23_newer, active in ((True, True), (False, False), (True, False)):
            with self.subTest(stage23_newer=stage23_newer, active=active):
                result = self.standby(stage23_newer, active)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn('udp/18443 уже слушает', result.stdout)


if __name__ == '__main__':
    unittest.main()
