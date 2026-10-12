"""Смена порта WG не требует пересоздания интерфейса."""
import tempfile
import unittest

from test_deploy import bash, source


class WireguardApplyTests(unittest.TestCase):
    def test_port_change_keeps_live_interface(self):
        with tempfile.TemporaryDirectory() as directory:
            result = bash(source('infra/lib/anysda-svc.sh') + '''
systemctl() { if [[ $1 != is-active ]]; then echo "systemctl $*"; fi; }
ip() {
  if [[ $1 == -o ]]; then
    echo '4: wg0 inet 10.66.66.1/24 scope global wg0'
    echo '4: wg0 inet6 fd66:66::1/64 scope global'
  fi
}
wg() { if [[ $1 == show ]]; then echo 51820; else echo "wg $*"; fi; }
wg-quick() { echo '[Interface]'; }
awk() {
  if [[ ${*: -1} == /etc/wireguard/wg0.conf ]]; then
    command awk "${@:1:$#-1}" <<'CONF'
[Interface]
Address = 10.66.66.1/24, fd66:66::1/64
ListenPort = 51821
CONF
  else command awk "$@"; fi
}
wg0_apply
''', directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('wg syncconf wg0', result.stdout)
            self.assertNotIn('systemctl restart', result.stdout)


if __name__ == '__main__':
    unittest.main()
