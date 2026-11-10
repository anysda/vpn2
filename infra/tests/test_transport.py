"""Проверки генерации и миграции настроек хаб ↔ экзит без изменения сети."""

import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from router_support import exit_cert, exit_secrets, stage
import watchdog_src


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT / 'infra/vpn2/src'))
from vpn2 import exitnode  # noqa: E402


class QueueTests(unittest.TestCase):
    def test_bootstrap_hub_and_exit(self):
        role = ROOT / 'infra/ansible/roles/base'
        unit = (role / 'templates/anysda-fq.service.j2').read_text()
        self.assertIn('ExecStart=/usr/local/sbin/anysda-fq {{ base_wan_qdisc }}', unit)
        defaults = (role / 'defaults/main.yml').read_text()
        self.assertIn("""base_wan_qdisc: "{{ 'pfifo_fast' if base_entry else 'fq' }}\"""", defaults)
        for host, expected in [('ru', 'pfifo_fast'), ('test', 'fq flow_limit 1000')]:
            with self.subTest(host=host), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / 'bin').mkdir()
                mocks = {
                    'ip': '#!/bin/sh\necho "default via 192.0.2.1 dev wan-test proto static"\n',
                    'tc': '''#!/bin/sh
if [ "$2" = show ]; then
  cat "$QUEUE_STATE"
else
  echo "$*" >> "$QUEUE_CALLS"
  case "$*" in
    *pfifo_fast) echo 'qdisc pfifo_fast 0: root refcnt 2 bands 3' > "$QUEUE_STATE";;
    *) echo 'qdisc fq 0: root refcnt 2 limit 10000p flow_limit 1000p' > "$QUEUE_STATE";;
  esac
fi
''',
                }
                for name, body in mocks.items():
                    path = root / 'bin' / name
                    path.write_text(body)
                    path.chmod(0o755)
                state, calls = root / 'state', root / 'calls'
                state.write_text('qdisc fq 0: root refcnt 2 flow_limit 100p\n')
                env = dict(os.environ, HOST_TAG=host, PATH=f'{root}/bin:{os.environ["PATH"]}',
                           QUEUE_STATE=str(state), QUEUE_CALLS=str(calls))
                queue = ['sh', str(role / 'files/anysda-fq'), expected.split()[0]]
                for _ in range(2):
                    subprocess.run(queue, env=env, check=True, capture_output=True, text=True)
                self.assertEqual(calls.read_text().replace(' pacing', '').splitlines(),
                                 [f'qdisc replace dev wan-test root {expected}'])
                state.write_text('qdisc pfifo_fast 0: root\n' if host != 'ru'
                                 else 'qdisc fq 0: root flow_limit 1000p\n')
                subprocess.run(queue, env=env, check=True, capture_output=True)
                self.assertEqual(calls.read_text().replace(' pacing', '').splitlines()[-1],
                                 f'qdisc replace dev wan-test root {expected}')
        sysctl = (role / 'files/99-anysda.conf').read_text()
        for setting in ('net.core.default_qdisc = fq',
                        'net.ipv4.tcp_congestion_control = bbr',
                        'net.core.rmem_default = 8388608',
                        'net.core.wmem_default = 8388608',
                        'net.core.netdev_max_backlog = 5000',
                        'net.ipv4.tcp_slow_start_after_idle = 0',
                        'net.ipv4.tcp_mtu_probing = 1',
                        'net.ipv4.tcp_notsent_lowat = 131072'):
            self.assertIn(setting, sysctl)


class HysteriaTests(unittest.TestCase):
    def test_config_to_stages_bbr_brutal_and_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = (ROOT / 'config.example.yaml').read_text().replace(
                '  passphrase: ""', '  passphrase: test pass')
            # Разные направления выявляют случайную перестановку скоростей;
            # второй экзит и mgmt должны оставаться на BBR.
            for settings in ('', '    hy2_up_mbps: 240\n    hy2_down_mbps: 120\n', ''):
                (root / 'config.yaml').write_text(base.replace(
                    '    host: 5.6.7.8\n', '    host: 5.6.7.8\n' + settings))
                subprocess.run(['python3', str(ROOT / 'infra/lib/config2env.py'), tmp],
                               check=True, capture_output=True, text=True,
                               env={**os.environ, 'VPN2_STATE_DIR': f'{tmp}/state'})
                # Как run_play: переменные стадии уходят в окружение шага целиком.
                script = 'set -a; source "$1/infra/envs/all.env"; source "$1/infra/envs/ru.env"; env -0'
                dumped = subprocess.run(['bash', '-eu', '-c', script, 'test', tmp], check=True,
                                        capture_output=True, text=True, env={'PATH': os.environ['PATH']})
                environ = dict(item.split('=', 1) for item in dumped.stdout.split('\0') if item)
                tags = environ['EXIT_TAGS'].split()
                for tag in tags:
                    shutil.copyfile(exit_cert(), root / f'{tag}.pem')
                env, _ = stage.exit_environment(environ, exit_secrets(tags), root, root / 'pinned', 'wan-test')
                env['RU_CLASH_SECRET'] = 'test-clash-secret'
                router = json.loads(stage.render_config(env))
                for out in router['outbounds']:
                    if out['type'] != 'hysteria2':
                        continue
                    actual = {k: out[k] for k in ('up_mbps', 'down_mbps') if k in out}
                    expected = {'up_mbps': 120, 'down_mbps': 240} if settings and out[
                        'tag'].removeprefix('transport-') in ('hy2-us-direct', 'hy2-us-warp') else {}
                    self.assertEqual(actual, expected, out['tag'])

                template = (ROOT / 'infra/configs/sing-box-server.json.tpl').read_text()
                values = dict(HY2_PWD_DIRECT='test-direct', HY2_PWD_WARP='test-warp',
                              HY2_PWD_MGMT='test-mgmt', HY2_OBFS_PWD='test-obfs', CLASH_SECRET='test',
                              REALITY_PRIVKEY='test', REALITY_SHORT_ID='abcd',
                              REALITY_SNI='www.apple.com', REALITY_PORT='4443',
                              **exitnode.WARP_PLACEHOLDER)
                names = ('HY2_DIRECT_PORT', 'HY2_WARP_PORT', 'HY2_MGMT_PORT', 'HY2_UP_MBPS', 'HY2_DOWN_MBPS')
                for tag in ('us', 'gb'):
                    script = ('source "$1/infra/envs/all.env"\nsource "$1/infra/envs/$2.env"\n'
                              + ''.join(f'echo "${{{name}:-}}"\n' for name in names))
                    result = subprocess.run(['bash', '-eu', '-c', script, 'test', tmp, tag],
                                            check=True, capture_output=True, text=True)
                    env = dict(zip(names, result.stdout.split('\n')))
                    server = json.loads(exitnode.render_config(
                        template, {**values, **env, 'HY2_BANDWIDTH_FIELDS': exitnode.bandwidth_fragment(env)}))
                    hy2 = [inbound for inbound in server['inbounds'] if inbound['type'] == 'hysteria2']
                    self.assertEqual(len(hy2), 3)
                    for inbound in hy2:
                        actual = {k: inbound[k] for k in ('up_mbps', 'down_mbps') if k in inbound}
                        expected = {'up_mbps': 240, 'down_mbps': 120} if settings and tag == 'us' and (
                            inbound['tag'] in ('hy2-direct-in', 'hy2-warp-in')) else {}
                        self.assertEqual(actual, expected, inbound['tag'])

    def test_reject_invalid_bandwidth_before_writing_env(self):
        for up, down in [('10', ''), ('', '10'), ('0', '10'), ('-1', '10'),
                         ('1.5', '10'), ('oops', '10'), ('1000001', '10')]:
            with self.subTest(up=up, down=down), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                base = (ROOT / 'config.example.yaml').read_text().replace(
                    '  passphrase: ""', '  passphrase: test pass')
                (root / 'config.yaml').write_text(base.replace('    host: 5.6.7.8\n',
                    f'    host: 5.6.7.8\n    hy2_up_mbps: {up}\n    hy2_down_mbps: {down}\n'))
                result = subprocess.run(['python3', str(ROOT / 'infra/lib/config2env.py'), tmp],
                                        capture_output=True, text=True,
                                        env={**os.environ, 'VPN2_STATE_DIR': f'{tmp}/state'})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('нужны обе скорости', result.stderr)
                self.assertFalse((root / 'infra/envs').exists())
                result = subprocess.run(watchdog_src.command('hy2'),
                                        env=dict(os.environ, HY2_UP_MBPS=up, HY2_DOWN_MBPS=down),
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
