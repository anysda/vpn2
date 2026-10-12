from pathlib import Path
import shutil
import tempfile
import unittest

from test_deploy import ROOT, bash, source


class QuicModeTests(unittest.TestCase):
    def test_allow_removes_previous_block_and_refresh_jobs(self):
        stage = source('infra/scripts/19-yt-zapret.sh')
        block = stage.split('echo "[$HOST_TAG] [5b/7]', 1)[1]
        block = 'echo "[$HOST_TAG] [5b/7]' + block.split('\n# ----------------------------------------------------------------------------', 1)[0]
        # Вызов части настоящей стадии, которая устанавливает/снимает QUIC.
        helper = ''
        if 'quic_teardown() {' in stage:
            helper = 'quic_teardown() {' + stage.split('quic_teardown() {', 1)[1].split('\n}', 1)[0] + '\n}\n'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in ('etc/systemd/system', 'usr/local/sbin', 'tmp/anysda', 'var/lib/anysda'):
                (root / path).mkdir(parents=True, exist_ok=True)
            for name in ('gen-yt-quic-nft.py', 'google-prefixes.json'):
                shutil.copy(ROOT / 'infra/lib' / name, root / 'tmp/anysda' / name)
            stale = root / 'etc/nftables-yt-quic.conf'
            stale.write_text('old block\n')
            script = '''
HOST_TAG=ru
YT_QUIC=allow
QUIC_PREFIXES='/var/lib/anysda/google-prefixes.json'
QUIC_NFT_CONF='/etc/nftables-yt-quic.conf'
systemctl() { echo "systemctl $*" >> calls; }
nft() { echo "nft $*" >> calls; echo 'udp dport 443'; }
''' + helper + block
            for prefix in ('/etc/', '/usr/local/', '/var/lib/', '/tmp/anysda/'):
                script = script.replace(prefix, directory + prefix)
            result = bash(script, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = (root / 'calls').read_text()
            self.assertNotIn('restart anysda-yt-quic.service', calls)
            self.assertNotIn('enable --now anysda-yt-quic', calls)
            self.assertIn('stop anysda-yt-quic-refresh.service', calls)
            self.assertIn('delete table inet anysda_ytquic', calls)
            self.assertFalse(stale.exists())


if __name__ == '__main__':
    unittest.main()
