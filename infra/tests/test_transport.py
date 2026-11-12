"""Проверки генерации и миграции настроек хаб ↔ экзит без изменения сети."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_deploy import write_envs


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT / 'infra/vpn2/src'))


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
    def test_reject_invalid_bandwidth_before_writing_env(self):
        for up, down in [('10', ''), ('', '10'), ('0', '10'), ('-1', '10'),
                         ('1.5', '10'), ('oops', '10'), ('1000001', '10')]:
            with self.subTest(up=up, down=down), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                base = (ROOT / 'config.example.yaml').read_text().replace(
                    '  passphrase: ""', '  passphrase: test pass')
                (root / 'config.yaml').write_text(base.replace('    host: 5.6.7.8\n',
                    f'    host: 5.6.7.8\n    hy2_up_mbps: {up}\n    hy2_down_mbps: {down}\n'))
                result = write_envs(tmp)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('нужны обе скорости', result.stderr)
                self.assertFalse((root / 'infra/envs').exists())


if __name__ == '__main__':
    unittest.main()
