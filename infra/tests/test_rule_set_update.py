import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from router_support import SB, stage_source


@unittest.skipUnless(SB, 'set SING_BOX_BIN to sing-box 1.12.25')
class RuleSetUpdateTests(unittest.TestCase):
    def test_download_is_validated_before_replace(self):
        for payload in [b'', b'upstream error page', b'SRS\x02broken']:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                source = directory / 'valid.json'
                source.write_text(json.dumps({'version': 2, 'rules': [{'domain': ['example.com']}]}))
                live = directory / 'fixture.srs'
                subprocess.run([SB, 'rule-set', 'compile', '-o', str(live), str(source)],
                               check=True, capture_output=True)
                old = time.time() - 10 * 86400
                os.utime(live, (old, old))
                before = (live.read_bytes(), live.stat().st_mtime_ns)
                (directory / 'download').write_bytes(payload)
                result = self.update(directory)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual((live.read_bytes(), live.stat().st_mtime_ns), before)
                self.assertFalse((directory / 'fixture.srs.new').exists())

    def test_fresh_corrupt_cache_is_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = directory / 'valid.json'
            source.write_text('{"version":2,"rules":[]}')
            downloaded = directory / 'download'
            subprocess.run([SB, 'rule-set', 'compile', '-o', str(downloaded), str(source)],
                           check=True, capture_output=True)
            live = directory / 'fixture.srs'
            live.write_text('corrupted cache')
            result = self.update(directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(live.read_bytes(), downloaded.read_bytes())
            before = live.stat().st_mtime_ns
            result = self.update(directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(live.stat().st_mtime_ns, before)

    @staticmethod
    def update(directory):
        source = stage_source().split('for _rs in ', 1)[1].split('\ndone', 1)[0]
        source = 'for _rs in ' + source + '\ndone\n'
        source = source.replace('$(python3 /tmp/anysda/gen-router-config.py --rule-sets)', 'fixture')
        source = source.replace('/usr/local/bin/sing-box', '"$SING_BOX_BIN"')
        # Only the network download is replaced; validation uses the real binary.
        shell = '''set -euo pipefail
curl() {
  while [[ "$1" != '-o' ]]; do shift; done
  cp "$DOWNLOAD" "$2"
}
''' + source
        return subprocess.run(['bash', '-c', shell], text=True, capture_output=True,
                              env={**os.environ, 'RS_DIR': str(directory), 'HOST_TAG': 'test',
                                   'SING_BOX_BIN': SB, 'DOWNLOAD': str(directory / 'download')})


if __name__ == '__main__':
    unittest.main()
