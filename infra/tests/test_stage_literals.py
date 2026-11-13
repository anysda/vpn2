"""Скрипт проверки YouTube из шаблона роли zapret: значения конфигурации остаются данными."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from zapret_support import VPN2_MOCK, render

VALUES = ['operator"name', "operator'name", r'operator\name', 'operator$HOME$(touch INJECTED)', 'operator\nname\n']


def run(script, directory, **values):
    return subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=directory, env=dict(os.environ, **values),
                          text=True, capture_output=True, timeout=15)


class YoutubeLiteralTests(unittest.TestCase):
    def test_generated_diagnostic_shell(self):
        for value in VALUES:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as work:
                check = render('anysda-yt-check', base=value, diag_uid=123, diag_gid=123)
                check = check.replace('/proc/net/netfilter/nfnetlink_queue', work + '/queue')
                (Path(work) / 'check.sh').write_text(check)
                (Path(work) / 'queue').write_text('200 123 0 2 65531 0 0 1 1\n')
                # Очередь готова и счётчик растёт: вердикт решают коды HTTP.
                checked = run(VPN2_MOCK + 'curl() { printf 000; }; setpriv() { printf 000; }; '
                              'systemctl() { [[ $1 == is-active ]] || echo 123; }; '
                              'nft() { n=$(cat n 2>/dev/null || echo 0); echo $((n + 1)) > n; '
                              'printf \'{"nftables":[{"rule":{"expr":[{"counter":{"packets":%s}},'
                              '{"queue":{"num":200}}]}}]}\' "$n"; }; '
                              'export -f curl setpriv systemctl nft vpn2; bash check.sh', work)
                self.assertEqual(checked.returncode, 1, checked.stderr)
                self.assertIn('  подбор стратегии: ' + value + '/blockcheck2.sh\n', checked.stdout)
                self.assertFalse((Path(work) / 'INJECTED').exists())


if __name__ == '__main__':
    unittest.main()
