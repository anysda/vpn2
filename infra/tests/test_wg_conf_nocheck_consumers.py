"""Nothing to check with reaches the operator as "not checked", not as a mismatch or a failure.

Review 12 of 729cda0 (`--check-bundle` exit 5, the backup copy exits 4 with the event
"сверка … не выполнена") found:
1 do_backup (infra/lib/deploy-backup.sh) printed "БД и конфиги VPN расходятся - перевыпусти" on every exit 4;
3 anysda-backup.sh put the reason into the event JSON as it came: tab, CR, \\x01, \\x1b
  and a non-UTF-8 byte broke the body, a reason over 200 characters made the panel
  (events.post.ts, zod max(200)) refuse the whole body, and curl hid it behind `|| true`;
5 free_addresses read wg0.conf as text: a latin-1 byte in a comment, which wg takes,
  raised UnicodeDecodeError and the check stopped.
Each consumer runs as it is in its script; only what it asks the node for is stubbed.
"""
import json
import os
from pathlib import Path
import signal
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest

from test_deploy import bash, function
from test_wg_conf_allowedips_prefix import ROOT, source

S = 'KLKQERtKGmYrCPaUJZYYoFjRRNizdBvaUWfrjPG5I1Q='
P = '0ivjKi39oR/BxsZp17GSMczPpE4CKHCaC1RbI3zedFg='
PSK = 'C' * 43 + '='
TEST_KEY = 'T' * 43 + '='
CONF = f'[Interface]\nPrivateKey = {S}\nListenPort = 51820\n\n[Peer]\nPublicKey = {P}\nPresharedKey = {PSK}\n' \
       'AllowedIPs = 10.66.66.2/32\n'
NOCHECK = 'wg0.conf: проверить нечем'
# events.post.ts line 12: the body the panel takes from the backup copy.
SCHEMA = "z.object({ type: z.literal('backup_warning'), detail: z.string().trim().max(200).default('') }),"


def result(stdout='', code=0):
    return types.SimpleNamespace(stdout=stdout, returncode=code)


class VerifyFreeAddressesTests(unittest.TestCase):
    """5 a latin-1 byte in a wg0.conf comment, LANG=C: free addresses as wg holds the file."""

    PROBE = '''
import importlib.util, json, sys, types
from pathlib import Path
import signal
spec = importlib.util.spec_from_file_location('vt', sys.argv[1])
vt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vt)
vt.WG_CONF = Path(sys.argv[2])
def run(*args, **_options):
    if args[0] == 'ip':
        return types.SimpleNamespace(stdout=json.dumps([{'addr_info': [{'local': '10.66.66.1', 'prefixlen': 29}]}]))
    return types.SimpleNamespace(stdout='51820' if 'listen-port' in args else '')
vt.run = run
print(json.dumps(vt.WireGuard().free))
'''

    def test_latin1_comment_is_read_as_wg_reads_it(self):
        with tempfile.TemporaryDirectory() as directory:
            conf = Path(directory) / 'wg0.conf'
            # wg takes the file: the byte is after `#`, wg drops it with the comment.
            conf.write_bytes(f'[Interface]\nPrivateKey = {S} # caf'.encode() + b'\xe9\n\n[Peer]\n' +
                             f'PublicKey = {P}\nAllowedIPs = 10.66.66.6/32\n'.encode())
            env = {'PATH': os.environ['PATH'], 'LANG': 'C', 'LC_ALL': 'C'}
            done = subprocess.run([sys.executable, '-I', '-c', self.PROBE, str(ROOT / 'infra/vpn2/src/vpn2/verify/traffic.py'),
                                   str(conf)], env=env, capture_output=True, text=True, timeout=30, check=False)
        self.assertNotIn('UnicodeDecodeError', done.stderr)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        # /29: hosts .1-.6, .1 is wg0 itself, .6 is the peer of wg0.conf (live wg shows none).
        self.assertEqual(json.loads(done.stdout), ['10.66.66.5', '10.66.66.4'])


class DeployBackupTests(unittest.TestCase):
    """1 do_backup (infra/lib/deploy-backup.sh) on exit 4 of the copy: nothing to check with vs a real mismatch.

    Review 13 of 77ceeec: exit 4 without a `warning:` line (sshpass "parse error" is 4 too,
    the copy passes it as it is) was "архив сохранён … перевыпусти" and 0, and the mktemp
    file of the copy stayed behind on Ctrl-C. do_backup runs as `backup) do_backup ;;`
    of deploy.sh: last command under set -euo pipefail, its own TMPDIR.
    """

    OLD = 'архив сохранён с предупреждением: БД и конфиги VPN расходятся - перевыпусти перечисленные выше устройства в панели'

    @staticmethod
    def script(tmp, copy):
        # Копию на entry снимает anysda-backup.sh; здесь его место занимает функция copy. Временные
        # файлы живут в $TMP деплоя, его убирает on_exit с ловушками из infra/deploy.sh.
        traps = '\n'.join(re.findall(r'(?m)^trap .*$', source('infra/deploy.sh')))
        return f'export TMPDIR={tmp}\nTMP=$(mktemp -d) BEAT= PLAY= MUTATING=false OPENED=false\n' + \
            function('on_exit') + traps + '\n' + function('do_backup').replace('/usr/local/bin/anysda-backup.sh', 'copy') + \
            'warn() { printf "[ru] %s\\n" "$*" >&2; }\n' + \
            'copy() {\n' + copy + '\n}\ndo_backup\n'

    def do_backup(self, copy, code=4):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory, 'tmp')
            tmp.mkdir()
            done = bash(self.script(tmp, f'{copy}\nreturn {code}'), directory)
            left = sorted(path.name for path in tmp.iterdir())
        output = done.stdout + done.stderr
        self.assertEqual(left, [], output)
        return output, f'rc={done.returncode}'

    def test_exit_4_without_warning_line_is_a_failure(self):
        for name, copy in {
            'silent': ':',
            'sshpass-parse-error': "echo 'sshpass: parse error' >&2",
            'backup-line-only': "printf '%-12s %s\\n' 'backup:' /var/backups/anysda/anysda-vpn2-x.tar.gz.age",
            'warning-not-at-start': "echo 'anysda-backup: WARN: warning: сверка БД с конфигами VPN не выполнена' >&2\n"
                                    "echo ' warning: БД и конфиги VPN расходятся'",
        }.items():
            with self.subTest(name):
                output, rc = self.do_backup(copy)
                self.assertNotEqual(rc, 'rc=0', output)
                self.assertNotIn('архив сохранён', output)
                self.assertNotIn('перевыпусти', output)

    def test_success_and_exit_1_are_as_before(self):
        output, rc = self.do_backup("printf '%-12s %s\\n' 'backup:' /x.tar.gz.age", 0)
        self.assertEqual(rc, 'rc=0', output)
        self.assertNotIn('перевыпусти', output)
        self.assertNotIn('не проверено', output)
        output, rc = self.do_backup("echo 'boom' >&2", 1)
        self.assertEqual(rc, 'rc=1', output)
        self.assertNotIn('архив сохранён', output)

    def test_signal_during_copy_leaves_no_temp_file(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(sig.name), tempfile.TemporaryDirectory() as directory:
                tmp, copying = Path(directory, 'tmp'), Path(directory, 'copying')
                tmp.mkdir()
                copy = f"printf '%-12s %s\\n' 'backup:' /x.tar.gz.age\n: > {copying}\nsleep 30\nreturn 0"
                # Ctrl-C reaches the whole foreground group: bash, the copy, tee.
                with subprocess.Popen(['bash', '-euo', 'pipefail', '-c', self.script(tmp, copy)], cwd=directory,
                                      text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      start_new_session=True) as done:
                    for _ in range(100):
                        if copying.exists():
                            break
                        time.sleep(0.1)
                    during = sorted(path.name for path in tmp.iterdir())
                    os.killpg(done.pid, sig)
                    stdout, stderr = done.communicate(timeout=15)
                left = sorted(path.name for path in tmp.iterdir())
                output = stdout + stderr
                self.assertTrue(copying.exists(), output)
                self.assertEqual(len(during), 1, output)
                self.assertNotEqual(done.returncode, 0, output)
                self.assertEqual(left, [], output)
                self.assertNotIn('архив сохранён', output)

    def test_nothing_to_check_with_is_not_a_mismatch(self):
        reason = NOCHECK + ' - wg не ответил за 30 с'
        # What anysda-backup.sh of 729cda0 prints on `--check-bundle` exit 5.
        output, rc = self.do_backup(
            f"echo 'anysda-backup: WARN: сверка БД с конфигами VPN не выполнена: {reason}' >&2\n"
            f"printf '\\n%-12s %s\\n' 'backup:' /var/backups/anysda/anysda-vpn2-x.tar.gz.age\n"
            f"printf '%-12s %s\\n' 'warning:' 'сверка БД с конфигами VPN не выполнена: {reason}'")
        self.assertEqual(rc, 'rc=0', output)
        self.assertNotIn('расходятся', output)
        self.assertNotIn('перевыпусти', output)
        self.assertIn(reason, output)

    def test_real_mismatch_keeps_the_reissue_advice(self):
        output, rc = self.do_backup(
            "echo 'anysda-backup: WARN: БД и конфиги VPN расходятся в 10 срезах подряд — сохраняю архив как есть; "
            "перевыпусти эти устройства в панели:' >&2\necho 'устройство 7: нет пира WireGuard' >&2\n"
            "printf '%-12s %s\\n' 'warning:' 'БД и конфиги VPN расходятся (устройства: 7) — перевыпусти в панели'")
        self.assertEqual(rc, 'rc=0', output)
        self.assertIn(self.OLD, output)


class BackupEventTests(unittest.TestCase):
    """3 the event of the copy: the schema the package cuts the detail for (vpn2.notify.clean_detail)."""

    def test_schema_copy_is_the_panel_schema(self):
        self.assertIn(SCHEMA, source('app/server/resources/settings.ts'))


if __name__ == '__main__':
    unittest.main()
