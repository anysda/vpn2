from pathlib import Path
import tempfile
import unittest

from test_deploy import bash
from zapret_support import render, role_file, tasks


class StartupTests(unittest.TestCase):
    def test_conntrack_is_loaded_before_sysctl_and_at_boot(self):
        names = [task['name'] for task in tasks()]
        boot, load, apply = (names.index(name) for name in (
            'Модуль conntrack при загрузке', 'Модуль conntrack загружен', 'Настройка conntrack применена'))
        self.assertLess(boot, apply)
        self.assertLess(load, apply)
        found = tasks()
        self.assertEqual(found[boot]['ansible.builtin.copy']['content'], 'nf_conntrack\n')
        self.assertEqual(found[boot]['ansible.builtin.copy']['dest'], '/etc/modules-load.d/anysda-yt-zapret.conf')
        self.assertEqual(found[load]['ansible.builtin.command']['argv'], ['modprobe', 'nf_conntrack'])
        self.assertEqual(found[apply]['ansible.builtin.command']['argv'],
                         ['sysctl', '-p', '/etc/sysctl.d/97-anysda-yt-zapret.conf'])

    def test_router_waits_for_owned_queue(self):
        dropin = role_file('sing-box-90-anysda-youtube.conf')
        self.assertIn('Wants=anysda-yt-nfqws.service', dropin)
        self.assertIn('After=anysda-yt-nfqws.service', dropin)
        self.assertIn('ExecStartPost=/usr/local/sbin/anysda-yt-nfqws-ready', render('anysda-yt-nfqws.service'))
        ready = render('anysda-yt-nfqws-ready')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for queue, pid, expected in ((200, 123, 0), (200, 456, 1), (201, 123, 1)):
                with self.subTest(queue=queue, pid=pid):
                    path = root / 'queue'
                    path.write_text(f'{queue} {pid} 0 2 65531 0 0 1 1\n')
                    script = ready.replace('/proc/net/netfilter/nfnetlink_queue', str(path))
                    result = bash('sleep() { :; }; set -- 123\n' + script, directory)
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
