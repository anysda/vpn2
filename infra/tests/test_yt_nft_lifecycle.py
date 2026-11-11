from pathlib import Path
import tempfile
import unittest

from test_deploy import bash
from zapret_support import role_file


class NftLifecycleTests(unittest.TestCase):
    def test_nft_start_and_reload_restore_only_existing_tables(self):
        dropin = role_file('nftables-90-anysda-youtube.conf')
        for event in ('ExecStartPost', 'ExecReload'):
            command = next(line.split('=', 1)[1] for line in dropin.splitlines()
                           if line.startswith(event + '='))
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for mode in ('block', 'allow'):
                    with self.subTest(event=event, mode=mode):
                        (root / 'nftables-yt-zapret.conf').touch()
                        quic = root / 'nftables-yt-quic.conf'
                        if mode == 'block':
                            quic.touch()
                        else:
                            quic.unlink()
                        script = command.replace('/etc/', directory + '/')
                        script = script.replace('/usr/sbin/nft', '/bin/echo nft')
                        result = bash(script, directory)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn('nft -f ' + str(root / 'nftables-yt-zapret.conf'), result.stdout)
                        self.assertEqual('nftables-yt-quic.conf' in result.stdout, mode == 'block')
                        self.assertNotIn('flush ruleset', result.stdout)


if __name__ == '__main__':
    unittest.main()
