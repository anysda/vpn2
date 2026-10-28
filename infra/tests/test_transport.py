"""Проверки генерации и миграции настроек хаб ↔ экзит без изменения сети."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from router_support import exit_cert


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))


class QueueTests(unittest.TestCase):
    def test_bootstrap_hub_and_exit(self):
        source = (ROOT / 'infra/scripts/00-bootstrap.sh').read_text()
        section = source[source.index('echo "[$HOST_TAG] [2/6] sysctl"'):
                         source.index('# systemd-networkd при каждом')]
        for host, expected in [('ru', 'pfifo_fast'), ('test', 'fq flow_limit 1000')]:
            with self.subTest(host=host), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for folder in ('etc/sysctl.d', 'etc/systemd/system', 'usr/local/sbin', 'bin'):
                    (root / folder).mkdir(parents=True)
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
                    'sysctl': '#!/bin/sh\nexit 0\n',
                    'systemctl': '#!/bin/sh\nexit 0\n',
                }
                for name, body in mocks.items():
                    path = root / 'bin' / name
                    path.write_text(body)
                    path.chmod(0o755)
                state, calls = root / 'state', root / 'calls'
                state.write_text('qdisc fq 0: root refcnt 2 flow_limit 100p\n')
                env = dict(os.environ, HOST_TAG=host, PATH=f'{root}/bin:{os.environ["PATH"]}',
                           QUEUE_STATE=str(state), QUEUE_CALLS=str(calls))
                script = section.replace('/etc/', f'{root}/etc/').replace(
                    '/usr/local/', f'{root}/usr/local/')
                for _ in range(2):
                    subprocess.run(['bash', '-eu', '-o', 'pipefail', '-c', script],
                                   env=env, check=True, capture_output=True, text=True)
                self.assertEqual(calls.read_text().replace(' pacing', '').splitlines(),
                                 [f'qdisc replace dev wan-test root {expected}'])
                unit = (root / 'etc/systemd/system/anysda-fq.service').read_text()
                start = next(line.removeprefix('ExecStart=') for line in unit.splitlines()
                             if line.startswith('ExecStart='))
                state.write_text('qdisc pfifo_fast 0: root\n' if host != 'ru'
                                 else 'qdisc fq 0: root flow_limit 1000p\n')
                subprocess.run(['sh', '-c', start], env=env, check=True, capture_output=True)
                self.assertEqual(calls.read_text().replace(' pacing', '').splitlines()[-1],
                                 f'qdisc replace dev wan-test root {expected}')
                sysctl = (root / 'etc/sysctl.d/99-anysda.conf').read_text()
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
                               check=True, capture_output=True, text=True)
                stage = (ROOT / 'infra/scripts/20-ru-router.sh').read_text()
                exports = stage[stage.index('export RU_CLASH_SECRET EXIT_TAGS'):
                                stage.index('# TLS pinning Hysteria2 outbound:')]
                script = '''source "$1/infra/envs/all.env"
source "$1/infra/envs/ru.env"
for tag in $EXIT_TAGS; do
  upper=${tag^^}
  for key in DIRECT WARP MGMT OBFS; do
    printf -v "${upper}_${key}" '%s' test-password
  done
done
export WG_OUT_IFACE=wan-test
RU_CLASH_SECRET=test-clash-secret
''' + exports + '''for tag in $EXIT_TAGS; do
  export "${tag^^}_HY2_CERT_PATH=$3"
done
python3 "$2/infra/lib/gen-router-config.py"'''
                result = subprocess.run(['bash', '-eu', '-c', script, 'test', tmp, str(ROOT), exit_cert()],
                                        check=True, capture_output=True, text=True,
                                        env={'PATH': os.environ['PATH']})
                router = json.loads(result.stdout)
                for out in router['outbounds']:
                    if out['type'] != 'hysteria2':
                        continue
                    actual = {k: out[k] for k in ('up_mbps', 'down_mbps') if k in out}
                    expected = {'up_mbps': 120, 'down_mbps': 240} if settings and out[
                        'tag'].removeprefix('transport-') in ('hy2-us-direct', 'hy2-us-warp') else {}
                    self.assertEqual(actual, expected, out['tag'])

                stage = (ROOT / 'infra/scripts/10-foreign.sh').read_text()
                exports = stage[stage.index('export HY2_UP_MBPS='):
                                stage.index('SB_CONFIG_TMP=')]
                exports = exports.replace('/tmp/anysda/hy2_bandwidth.py',
                                          str(ROOT / 'infra/lib/hy2_bandwidth.py'))
                env = dict(os.environ, HY2_PWD_DIRECT='test-direct', HY2_PWD_WARP='test-warp',
                           HY2_PWD_MGMT='test-mgmt', HY2_OBFS_PWD='test-obfs', CLASH_SECRET='test',
                           WGCF_PEER_ENDPOINT_HOST='192.0.2.20', WGCF_PEER_ENDPOINT_PORT='2408',
                           WGCF_LOCAL_IPV4='192.0.2.21', WGCF_RESERVED_JSON='[0,0,0]',
                           WGCF_PRIVKEY='AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
                           WGCF_PEER_PUBKEY='AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
                for tag in ('us', 'gb'):
                    script = '''source "$1/infra/envs/all.env"
source "$1/infra/envs/$3.env"
export HY2_DIRECT_PORT HY2_WARP_PORT HY2_MGMT_PORT
''' + exports + '\nenvsubst < "$2/infra/configs/sing-box-server.json.tpl"'
                    result = subprocess.run(['bash', '-eu', '-c', script, 'test', tmp, str(ROOT), tag],
                                            env=env, check=True, capture_output=True, text=True)
                    server = json.loads(result.stdout)
                    for inbound in server['inbounds']:
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
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('нужны обе скорости', result.stderr)
                self.assertFalse((root / 'infra/envs').exists())
                result = subprocess.run(['python3', str(ROOT / 'infra/lib/hy2_bandwidth.py')],
                                        env=dict(os.environ, HY2_UP_MBPS=up, HY2_DOWN_MBPS=down),
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
