"""Поздний beat сторожа фаервола: дошёл до узла после disarm - ничего не ломает.

kill "$beat" в deploy.sh не гасит уже начатый ssh с `fw-guard.sh beat`, и он
может дойти до узла до 30 с позже: после disarm стадии, наперегонки с ним или
после arm следующей стадии.

VPN2_TEST_ROOT позволяет прогнать те же проверки против другой ревизии.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
GUARD = ROOT / 'infra/lib/fw-guard.sh'

FAKE_UFW = '''#!/bin/bash
echo "ufw $*" >> "$FAKE/calls"
case "$1 $2" in
  'status '*|'status ') [ -e "$FAKE/ufw-active" ] && echo 'Status: active' || echo 'Status: inactive' ;;
  '--force enable') touch "$FAKE/ufw-active" ;;
  '--force disable') rm -f "$FAKE/ufw-active" ;;
esac
'''

FAKE_NFT = '''#!/bin/bash
echo "nft $*" >> "$FAKE/calls"
case "$1" in
  list) [ -f "$FAKE/table-$4" ] && cat "$FAKE/table-$4" || exit 1 ;;
  delete) rm -f "$FAKE/table-$4" ;;
  -f) name=$(sed -n 's/^table inet \\([a-z_]*\\) {$/\\1/p' "$2"); cp "$2" "$FAKE/table-$name" ;;
esac
'''

FAKE_LOG = '''#!/bin/bash
echo "$(basename "$0") $*" >> "$FAKE/calls"
'''

# Откат сторожа: эти вызовы ufw/nft делает только restore().
ROLLBACK = ('ufw --force', 'ufw reload', 'nft delete', 'nft -f')

RACE_ROUNDS = 200


class LateBeatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake = self.root / 'fake'
        self.bin = self.root / 'bin'
        self.etc = self.root / 'etc'
        self.dir = self.root / 'run/vpn2-fw-guard'
        for path in (self.fake, self.bin, self.etc / 'ufw', self.etc / 'default'):
            path.mkdir(parents=True)
        for name, body in (('ufw', FAKE_UFW), ('nft', FAKE_NFT),
                           ('systemd-run', FAKE_LOG), ('systemctl', FAKE_LOG)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        (self.etc / 'ufw/user.rules').write_text('-A ufw-user-input -p tcp --dport 22 -j ACCEPT\n')
        (self.etc / 'default/ufw').write_text('DEFAULT_INPUT_POLICY="DROP"\n')
        (self.fake / 'ufw-active').touch()
        (self.fake / 'table-ytzapret').write_text('table inet ytzapret {\n}\n')
        self.env = dict(os.environ, PATH=f'{self.bin}:{os.environ["PATH"]}', FAKE=str(self.fake),
                        FW_GUARD_DIR=str(self.dir), FW_GUARD_ETC=str(self.etc))

    def tearDown(self):
        self.tmp.cleanup()

    def guard(self, *args):
        return subprocess.run(['bash', str(GUARD), *args], env=self.env, text=True,
                              capture_output=True, timeout=30)

    def calls(self):
        path = self.fake / 'calls'
        return path.read_text().splitlines() if path.exists() else []

    def ok(self, result, code=0):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)

    def test_beat_after_disarm_before_next_arm_leaves_nothing(self):
        self.ok(self.guard('arm', '28-wireguard', '30'))
        self.ok(self.guard('disarm', '28-wireguard'))
        after_disarm = len(self.calls())

        self.ok(self.guard('beat'))
        self.assertFalse(self.dir.exists(), 'поздний beat создал каталог сторожа')
        self.assertEqual(self.calls()[after_disarm:], [], 'поздний beat тронул ufw/nft')

        self.ok(self.guard('arm', '29-openvpn', '30'))
        self.assertEqual((self.dir / 'stage').read_text().strip(), '29-openvpn')
        self.ok(self.guard('disarm', '29-openvpn'))
        self.assertFalse(self.dir.exists())

    def test_beat_racing_disarm_never_fails_it(self):
        # beat в цикле параллельно с disarm: окно между проверкой каталога и
        # touch в beat попадает на rm -rf в disarm.
        loop = ('while :; do bash "$1" beat >/dev/null 2>&1 || :; done')
        for round_ in range(RACE_ROUNDS):
            self.ok(self.guard('arm', f'28-race-{round_}', '30'))
            beats = [subprocess.Popen(['bash', '-c', loop, 'beat', str(GUARD)], env=self.env)
                     for _ in range(3)]
            try:
                time.sleep(0.05)
                disarm = self.guard('disarm', f'28-race-{round_}')
            finally:
                for proc in beats:
                    proc.kill()
                for proc in beats:
                    proc.wait(timeout=10)
            self.assertEqual(disarm.returncode, 0,
                             f'круг {round_}: {disarm.stdout}{disarm.stderr}')
            self.assertFalse(self.dir.exists(), f'круг {round_}: каталог сторожа остался')
            self.assertFalse([c for c in self.calls() if c.startswith(ROLLBACK)],
                             f'круг {round_}: был откат')

    def test_late_beat_after_next_arm_delays_rollback_by_its_lateness_only(self):
        window, late = 3, 2.0
        armed = time.monotonic()
        self.ok(self.guard('arm', '29-openvpn', str(window)))
        # Сторож напрямую, не через systemd-run (заглушка его не запускает).
        watch = subprocess.Popen(['bash', str(GUARD), 'watch', str(window)], env=self.env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        time.sleep(late - (time.monotonic() - armed))
        beat_at = time.monotonic() - armed
        self.ok(self.guard('beat'))

        out, _ = watch.communicate(timeout=window + late + 15)
        rolled_at = time.monotonic() - armed
        self.assertEqual(watch.returncode, 0, out)
        self.assertIn('откатываю фаервол к снимку до стадии 29-openvpn', out)
        self.assertEqual((self.dir / 'restored').read_text().strip(), '0')
        # Отсчёт окна идёт от позднего beat: откат не раньше beat + окно и не
        # позже beat + окно + шаг опроса 2 с + 1 с (mtime в целых секундах).
        self.assertGreater(rolled_at, beat_at + window)
        self.assertLessEqual(rolled_at, beat_at + window + 2 + 1)
        print(f'\nпоздний beat через {beat_at:.1f} с после arm, окно {window} с: '
              f'откат через {rolled_at:.1f} с после arm '
              f'(граница {beat_at + window + 3:.1f} = beat + окно + 3)')

        self.ok(self.guard('disarm', '29-openvpn'), 3)
        self.assertFalse(self.dir.exists())


if __name__ == '__main__':
    unittest.main()
