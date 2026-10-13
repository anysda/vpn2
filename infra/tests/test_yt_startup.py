from pathlib import Path
import tempfile
import unittest

from test_deploy import bash, source


class StartupTests(unittest.TestCase):
    def test_conntrack_is_loaded_before_sysctl_and_at_boot(self):
        stage = source('infra/scripts/19-yt-zapret.sh')
        body = stage.split('echo "[$HOST_TAG] [5/7] nftables"\n', 1)[1].split('# Поправка окна.', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'etc/sysctl.d').mkdir(parents=True)
            script = '''
MODULES_CONF=/etc/modules-load.d/anysda-yt-zapret.conf
SYSCTL_CONF=/etc/sysctl.d/97-anysda-yt-zapret.conf
modprobe() { [[ "$*" == nf_conntrack ]]; touch conntrack-loaded; }
sysctl() { test -f conntrack-loaded; }
''' + body
            result = bash(script.replace('/etc/', directory + '/etc/'), directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'etc/modules-load.d/anysda-yt-zapret.conf').read_text(),
                             'nf_conntrack\n')

    def test_router_waits_for_owned_queue(self):
        stage = source('infra/scripts/19-yt-zapret.sh')
        self.assertIn('Wants=anysda-yt-nfqws.service', stage)
        self.assertIn('After=anysda-yt-nfqws.service', stage)
        self.assertIn('ExecStartPost=/usr/local/sbin/anysda-yt-nfqws-ready', stage)
        body = stage.split('cat > /usr/local/sbin/anysda-yt-nfqws-ready <<EOF\n', 1)[1].split('\nEOF', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            render = bash('YT_ZAPRET_QUEUE=200\ncat <<EOF\n' + body + '\nEOF\n', directory)
            self.assertEqual(render.returncode, 0, render.stderr)
            for queue, pid, expected in ((200, 123, 0), (200, 456, 1), (201, 123, 1)):
                with self.subTest(queue=queue, pid=pid):
                    path = root / 'queue'
                    path.write_text(f'{queue} {pid} 0 2 65531 0 0 1 1\n')
                    script = render.stdout.replace('/proc/net/netfilter/nfnetlink_queue', str(path))
                    result = bash('sleep() { :; }; set -- 123\n' + script, directory)
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
