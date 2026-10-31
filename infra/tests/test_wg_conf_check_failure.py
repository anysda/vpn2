"""A check of the copy that fails with any code is "not checked", as nothing to check with.

Review 14 of 8cf026a: `--check-bundle` exiting with a code other than 0, 3 and 5 (1, 2, 127,
a python traceback) made anysda-backup.sh write only a WARN to stderr and exit 0: no
`warning:` line, no event for the panel, deploy.sh do_backup said "success".
The copy runs as it is up to the check, its loop over slices and its final part; only the
snapshot and the archive are stubbed. Its stdout goes to do_backup of deploy.sh as it is.
"""
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from test_deploy import bash, function
from test_wg_conf_allowedips_prefix import cut, source
from test_wg_conf_nocheck_consumers import NOCHECK

NOT_DONE = 'сверка БД с конфигами VPN не выполнена'
REISSUE = 'архив сохранён с предупреждением: БД и конфиги VPN расходятся - перевыпусти перечисленные выше устройства в панели'
FAILURES = {
    # name: (check_bundle, what the warning line names besides the code)
    'exit-1': ("check_bundle() { echo 'boom' >&2; return 1; }\n", 'boom'),
    'exit-2': ("check_bundle() { echo 'Unknown option: -P' >&2; return 2; }\n", 'Unknown option'),
    'exit-127': ('check_bundle() { python3-not-here -P - "$1"; }\n', 'not found'),
    # The real check on a db.sqlite that is not a database: sqlite3.DatabaseError, exit 1.
    'traceback': ('', 'DatabaseError'),
}


def copy(directory, check):
    """anysda-backup.sh with `check` as check_bundle (empty - its own); stdout, stderr, code, event body."""
    root = Path(directory)
    (root / 'etc').mkdir()
    (root / 'etc/tgbot-secret.txt').write_text('s3cret\n')
    bundle, archive = root / 'bundle', root / 'anysda-vpn2-x.tar.gz.age'
    bundle.mkdir()
    (bundle / 'db.sqlite').write_text('not a database\n')
    archive.write_bytes(b'age-encrypted')
    text = source('infra/lib/anysda-backup.sh').replace('/etc/anysda/', f'{root}/etc/')
    fake_curl = ('curl() { while (($#)); do [[ $1 == -d ]] && printf "%s" "$2" > body; shift; done; '
                 'cat > header; }\n')
    head = text.split('# Сверка среза с БД того же среза', 1)[0].replace('umask 077\n', 'umask 077\n' + fake_curl, 1)
    own = cut(text, 'check_bundle() {', '\nPY\n}\n') + '\nPY\n}\n'
    loop = cut(text, 'for attempt in 1 2 3 4 5 6 7 8 9 10; do', '\ndone\n') + '\ndone\n'
    final = cut(text, '# ── Финал ──', '\n  exit 4\nfi\n') + '\n  exit 4\nfi\n'
    script = head + own + check + f"BUNDLE_DIR='{bundle}'\nsnapshot_access() {{ :; }}\nsleep() {{ :; }}\n" + loop + \
        f"FINAL='{archive}'\nSIZE=13\nHASH=0\nBACKUP_BACKEND=local\nRETAIN=7\n" + final + 'exit 0\n'
    done = subprocess.run(['bash', '-c', script], cwd=directory, text=True, capture_output=True, timeout=15,
                          check=False)
    body = root / 'body'
    event = json.loads(body.read_text()) if body.exists() else None
    return done, event, archive


def deploy(directory, done):
    """deploy.sh do_backup with ssh_exec printing what the copy printed and exiting with its code."""
    root = Path(directory)
    (root / 'copy.out').write_text(done.stdout)
    (root / 'copy.err').write_text(done.stderr)
    tmp = root / 'tmp'
    tmp.mkdir()
    script = f'export TMPDIR={tmp}\n' + function('do_backup') + \
        'load_env() { :; }\nwarn() { printf "[ru] %s\\n" "$*" >&2; }\n' + \
        f'ssh_exec() {{\ncat {root}/copy.out\ncat {root}/copy.err >&2\nreturn {done.returncode}\n}}\ndo_backup\n'
    run = bash(script, directory)
    return run.stdout + run.stderr, run.returncode


class CheckFailureTests(unittest.TestCase):
    def test_any_failed_check_is_not_checked(self):
        """1 exit 1, 2, 127 and a traceback: exit 4, `warning:` line, event, archive, deploy "не проверено"."""
        for name, (check, hint) in FAILURES.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                done, event, archive = copy(directory, check)
                output = done.stdout + done.stderr
                if name == 'traceback':
                    self.assertIn('Traceback', done.stderr, output)
                self.assertEqual(done.returncode, 4, output)
                lines = [line for line in done.stdout.splitlines() if line.strip()]
                self.assertTrue(lines[-1].startswith('warning:'), output)
                self.assertIn(NOT_DONE, lines[-1])
                code = re.search(r'код (\d+)', lines[-1])
                self.assertTrue(code or hint in lines[-1], lines[-1])
                if code:
                    self.assertNotIn(code.group(1), ('0', '3', '5'), lines[-1])
                self.assertTrue(archive.is_file())
                self.assertIn(f'backup:      {archive}', done.stdout)
                self.assertIsNotNone(event, 'no event reached the panel')
                self.assertEqual(set(event), {'type', 'detail'})
                self.assertEqual(event['type'], 'backup_warning')
                self.assertTrue(event['detail'].startswith(NOT_DONE), event)
                self.assertLessEqual(len(event['detail'].encode('utf-16-le')) // 2, 200, event)
                said, rc = deploy(directory, done)
                self.assertEqual(rc, 0, said)
                self.assertIn('не проверено', said)
                self.assertNotIn('перевыпусти', said)
                self.assertNotIn('расходятся', said)

    def test_check_passing_is_success_without_warning(self):
        """2 exit 0 (the real check, no db.sqlite in the slice): success, no warning, no event."""
        with tempfile.TemporaryDirectory() as directory:
            done, event, archive = copy(directory, 'rm -f bundle/db.sqlite\n')
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertNotIn('warning:', done.stdout)
            self.assertIsNone(event)
            self.assertTrue(archive.is_file())
            said, rc = deploy(directory, done)
            self.assertEqual(rc, 0, said)
            self.assertNotIn('не проверено', said)
            self.assertNotIn('перевыпусти', said)

    def test_mismatch_in_ten_slices_keeps_the_reissue(self):
        """2 exit 3 in all 10 slices: "расходятся", reissue in the panel."""
        with tempfile.TemporaryDirectory() as directory:
            check = "check_bundle() { echo x >> slices; echo 'устройство 7: нет пира WireGuard'; return 3; }\n"
            done, event, archive = copy(directory, check)
            output = done.stdout + done.stderr
            self.assertEqual(done.returncode, 4, output)
            self.assertEqual(len(Path(directory, 'slices').read_text().split()), 10)
            lines = [line for line in done.stdout.splitlines() if line.strip()]
            self.assertTrue(lines[-1].startswith('warning:'), output)
            self.assertIn('расходятся (устройства: 7)', lines[-1])
            self.assertNotIn(NOT_DONE, output)
            self.assertTrue(archive.is_file())
            self.assertEqual(event, {'type': 'backup_warning',
                                     'detail': 'БД и конфиги VPN расходятся (устройства: 7) — перевыпусти в панели'})
            said, rc = deploy(directory, done)
            self.assertEqual(rc, 0, said)
            self.assertIn(REISSUE, said)
            self.assertNotIn('не проверено', said)

    def test_nothing_to_check_with_names_the_reason(self):
        """2 exit 5: "не выполнена: <причина>", not checked, no reissue."""
        reason = NOCHECK + ' - wg не ответил за 30 с'
        with tempfile.TemporaryDirectory() as directory:
            done, event, archive = copy(directory, f"check_bundle() {{ echo '{reason}'; return 5; }}\n")
            output = done.stdout + done.stderr
            self.assertEqual(done.returncode, 4, output)
            lines = [line for line in done.stdout.splitlines() if line.strip()]
            self.assertEqual(lines[-1], f'warning:     {NOT_DONE}: {reason}')
            self.assertTrue(archive.is_file())
            self.assertEqual(event, {'type': 'backup_warning', 'detail': f'{NOT_DONE}: {reason}'})
            said, rc = deploy(directory, done)
            self.assertEqual(rc, 0, said)
            self.assertIn('не проверено', said)
            self.assertNotIn('перевыпусти', said)


if __name__ == '__main__':
    unittest.main()
