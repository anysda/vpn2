"""The packaged nft loader must coexist with rules owned by other services."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


def source(path):
    ref = os.environ.get('NFT_REF')
    if ref:
        return subprocess.check_output(
            ['git', 'show', f'{ref}:{path}'], cwd=ROOT, text=True)
    return (ROOT / path).read_text()


class NftablesPreserveTests(unittest.TestCase):
    def prepare(self, root):
        etc = root / 'etc'
        etc.mkdir()
        conf = etc / 'nftables.conf'
        conf.write_text('#!/usr/sbin/nft -f\nflush ruleset\ntable inet filter {}\n')
        script = root / 'install.sh'
        script.write_text(source('infra/lib/anysda-svc.sh').replace('/etc/', str(etc) + '/') + '''
systemctl() { :; }
if declare -F nftables_preserve_rules >/dev/null; then
    nftables_preserve_rules
    nftables_preserve_rules
fi
''')
        subprocess.run(['bash', '-e', str(script)], check=True, capture_output=True, text=True)
        return conf

    def test_migration_is_idempotent_and_keeps_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conf = self.prepare(root)
            self.assertNotIn('\nflush ruleset\n', conf.read_text())
            self.assertIn('table inet filter {}', conf.read_text())
            self.assertIn('\nflush ruleset\n', Path(str(conf) + '.before-vpn2').read_text())
            dropin = root / 'etc/systemd/system/nftables.service.d/10-anysda-preserve.conf'
            self.assertIn('\nExecStop=\n', dropin.read_text())

    def test_watchdog_repairs_lost_chains_with_policy_rule_still_present(self):
        stage = source('infra/scripts/21-failover-watchdog.sh')
        watchdog = stage.split("<<'WDEOF'\n", 1)[1].split('\nWDEOF', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            restored = Path(directory) / 'restored'
            mocks = r'''
systemctl() {
    if [[ $1 == is-active ]]; then [[ ${@: -1} == anysda-wg-routing ]];
    elif [[ $1 == restart ]]; then echo "$2" > "$RESTORED";
    else return 1; fi
}
iptables() {
    [[ -f "$RESTORED" ]] || return 1
    if [[ $* == *'-S PREROUTING'* ]]; then echo '-A PREROUTING -i wg0 -j ANYSDA_WG_TPROXY'; fi
}
ip() {
    if [[ $1 == rule ]]; then echo '32765: from all fwmark 0x42 lookup 101';
    elif [[ $1 == route ]]; then echo 'local default dev lo'; fi
}
'''
            result = subprocess.run(['bash', '-c', mocks + watchdog], text=True,
                                    capture_output=True, timeout=5,
                                    env={**os.environ, 'RESTORED': str(restored)})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(restored.exists(), 'watchdog silently ignored missing TPROXY chains')
            self.assertEqual(restored.read_text(), 'anysda-wg-routing\n')
            self.assertIn('цепочка TPROXY пропала', result.stderr)

    @unittest.skipUnless(os.environ.get('RUN_NETNS_TESTS') == '1', 'set RUN_NETNS_TESTS=1')
    def test_restart_reload_preserve_dynamic_rules_and_bans(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conf = self.prepare(root)
            dropin = root / 'etc/systemd/system/nftables.service.d/10-anysda-preserve.conf'
            stop = ':' if dropin.exists() and '\nExecStop=\n' in dropin.read_text() else 'nft flush ruleset'
            script = r'''
set -euo pipefail
nft -f "$1"
iptables -N ufw-user-input
iptables -A INPUT -j ufw-user-input
iptables -t nat -A POSTROUTING -s 198.51.100.0/24 -j MASQUERADE
iptables -t mangle -N ANYSDA_WG_TPROXY
iptables -t mangle -A PREROUTING -j ANYSDA_WG_TPROXY
iptables -t mangle -A ANYSDA_WG_TPROXY -p tcp -j TPROXY --on-port 7898 --on-ip 127.0.0.1 --tproxy-mark 0x42/0x42
nft 'add table inet f2b_table'
nft 'add set inet f2b_table banned { type ipv4_addr; }'
nft 'add element inet f2b_table banned { 192.0.2.99 }'
before=$(nft -s list ruleset)
for event in restart reload restart reload; do
    if [[ "$event" == restart ]]; then eval "$2"; fi
    nft -f "$1"
    after=$(nft -s list ruleset)
    printf '%s: tables before=%s after=%s\n' "$event" "$(grep -c '^table ' <<<"$before")" "$(grep -c '^table ' <<<"$after")"
    test "$before" = "$after"
done
echo 'ufw, MASQUERADE, TPROXY 0x42 and fail2ban ban preserved'
'''
            result = subprocess.run(['sudo', '-n', 'unshare', '--net', 'bash', '-c', script,
                                     'nft-lifecycle', str(conf), stop],
                                    capture_output=True, text=True, timeout=30)
            print(result.stdout, end='')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
