"""Параллельный старт VPN-служб не дублирует общее policy rule."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_deploy import source


class RoutingRaceTests(unittest.TestCase):
    def test_parallel_protocols_share_one_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_ip = root / 'ip'
            fake_ip.write_text('''#!/bin/bash
if [[ $1 == rule && $2 == list ]]; then
  snapshot=$(cat "$RULES" 2>/dev/null || true)
  sleep 0.2
  echo "$snapshot"
elif [[ $1 == rule && $2 == add ]]; then
  echo '32765: from all fwmark 0x42 lookup 101' >> "$RULES"
elif [[ $1 == route ]]; then
  echo 'local default dev lo'
fi
''')
            fake_ip.chmod(0o755)
            processes = []
            env = dict(os.environ, PATH=f'{root}:{os.environ["PATH"]}', RULES=str(root / 'rules'))
            for stage in ('28-wireguard', '29-openvpn'):
                routing = source(f'infra/scripts/{stage}.sh').split("<<'IPTSEOF'\n", 1)[1].split('\nIPTSEOF', 1)[0]
                routing = routing.replace('/run/anysda-vpn-routing.lock', str(root / 'routing.lock'))
                script = 'iptables() { return 0; }\nip6tables() { [[ $1 != -C ]]; }\n' + routing
                processes.append(subprocess.Popen(['bash', '-c', script], env=env,
                                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            try:
                for process in processes:
                    out, err = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 0, out + err)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()
            self.assertEqual(len((root / 'rules').read_text().splitlines()), 1)


if __name__ == '__main__':
    unittest.main()
