import json
from pathlib import Path
import tempfile
import unittest

from test_deploy import bash, source


class YoutubeCheckTests(unittest.TestCase):
    def run_check(self, scenario):
        stage = source('infra/scripts/19-yt-zapret.sh')
        header = stage.split('cat > /usr/local/bin/anysda-yt-check <<EOF\n', 1)[1].split('\nEOF', 1)[0]
        body = stage.split("cat >> /usr/local/bin/anysda-yt-check <<'EOF'\n", 1)[1].split('\nEOF', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            render = bash('''
DIAG_UID=42
DIAG_USER=ytzdiag
YT_MARK=256
YT_ZAPRET_QUEUE=200
YT_ZAPRET_BASE=/opt/zapret2
id() { echo 42; }
cat <<EOF
''' + header + '\nEOF\n', directory)
            self.assertEqual(render.returncode, 0, render.stderr)
            (root / 'queue').write_text('200 123 0 2 65531 0 0 1 1\n' if scenario != 'no_queue' else '')
            for name, count in (('before', 0), ('after', 12 if scenario != 'no_packets' else 0)):
                (root / name).write_text(json.dumps({'nftables': [{'rule': {'expr': [
                    {'counter': {'packets': count}}, {'queue': {'num': 200}}
                ]}}]}))
            mocks = '''
systemctl() {
  if [[ $1 == is-active ]]; then [[ $SCENARIO != inactive ]]; else echo 123; fi
}
nft() {
  [[ $SCENARIO != no_table ]] || return 1
  if [[ -f sampled ]]; then cat after; else cat before; touch sampled; fi
}
curl() { printf 200; [[ $SCENARIO != partial ]]; }
setpriv() { while [[ $1 != curl ]]; do shift; done; "$@"; }
'''
            script = (render.stdout + body + '\n').replace('/proc/net/netfilter/nfnetlink_queue', str(root / 'queue'))
            return bash(f'SCENARIO={scenario}\n' + mocks + 'set +e\n' + script, directory)

    def test_healthy_path(self):
        result = self.run_check('healthy')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_http_200_is_not_enough(self):
        for scenario in ('inactive', 'no_queue', 'no_table', 'no_packets', 'partial'):
            with self.subTest(scenario=scenario):
                result = self.run_check(scenario)
                self.assertNotEqual(result.returncode, 0, result.stdout)


if __name__ == '__main__':
    unittest.main()
