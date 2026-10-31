"""Nothing to check with reaches the operator as "not checked", not as a mismatch or a failure.

Review 12 of 729cda0 (`--check-bundle` exit 5, the backup copy exits 4 with the event
"сверка … не выполнена") found:
1 deploy.sh do_backup printed "БД и конфиги VPN расходятся - перевыпусти" on every exit 4;
2 verify-traffic.py wg_apply_check made the ValueError "проверить нечем" a FAIL (code 1);
3 anysda-backup.sh put the reason into the event JSON as it came: tab, CR, \\x01, \\x1b
  and a non-UTF-8 byte broke the body, a reason over 200 characters made the panel
  (events.post.ts, zod max(200)) refuse the whole body, and curl hid it behind `|| true`;
4 restore_wg_apply re-read the saved wg0.conf before restoring the files: when that read
  failed, wg0.conf and control-wg.json stayed as the check left them;
5 free_addresses read wg0.conf as text: a latin-1 byte in a comment, which wg takes,
  raised UnicodeDecodeError and the check stopped.
Each consumer runs as it is in its script; only what it asks the node for is stubbed.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from test_deploy import bash, function
from test_wg_conf_allowedips_prefix import ROOT, VT, cut, source

S = 'KLKQERtKGmYrCPaUJZYYoFjRRNizdBvaUWfrjPG5I1Q='
P = '0ivjKi39oR/BxsZp17GSMczPpE4CKHCaC1RbI3zedFg='
PSK = 'C' * 43 + '='
TEST_KEY = 'T' * 43 + '='
CONF = f'[Interface]\nPrivateKey = {S}\nListenPort = 51820\n\n[Peer]\nPublicKey = {P}\nPresharedKey = {PSK}\n' \
       'AllowedIPs = 10.66.66.2/32\n'
NOCHECK = 'wg0.conf: проверить нечем'
# events.post.ts line 12: the body the panel takes from the backup copy.
SCHEMA = "z.object({ type: z.literal('backup_warning'), detail: z.string().trim().max(200).default('') }),"
# String.prototype.trim: WhiteSpace and LineTerminator of ECMAScript.
JS_SPACE = '\t\n\v\f\r   ' + ''.join(map(chr, range(0x2000, 0x200b))) + '    　﻿'


def result(stdout='', code=0):
    return types.SimpleNamespace(stdout=stdout, returncode=code)


def node(live, addresses='10.66.66.2/32\n', prefix=24):
    """`run` of verify-traffic.py as wg and ip on the node answer; `live` - peers of wg0."""
    def run(*args, **_options):
        if args[:4] == ('wg', 'show', 'wg0', 'peers'):
            return result('\n'.join(sorted(live)) + '\n')
        if args[:4] == ('wg', 'show', 'wg0', 'allowed-ips'):
            return result(addresses)
        if args[0] == 'ip':
            return result(json.dumps([{'addr_info': [{'local': '10.66.66.1', 'prefixlen': prefix}]}]))
        if args[:2] == ('wg', 'pubkey'):
            return result(TEST_KEY + '\n')
        if args[:2] in (('wg', 'genkey'), ('wg', 'genpsk')):
            return result('G' * 43 + '=\n')
        return result()
    return run


class VerifyApplyCheckTests(unittest.TestCase):
    """2 wg_apply_check: nothing to check with - UNKNOWN, code 2; wg refusing the file - FAIL."""

    def check(self, directory, asked, path):
        root = Path(directory)
        (root / 'control.sock').touch()
        (root / 'wg0.conf').write_text(CONF + 'Bogus = 1\n')
        bin_dir = root / path
        bin_dir.mkdir()
        for name in ('wg', 'ip', 'unshare', 'setpriv'):
            if path == 'no-wg' and name == 'wg':
                continue
            (bin_dir / name).write_text('#!/bin/sh\nexit 0\n')
            (bin_dir / name).chmod(0o755)
        process = types.SimpleNamespace(run=asked, TimeoutExpired=subprocess.TimeoutExpired)
        report = VT.Report('test')
        with mock.patch.object(VT, 'CONTROL_SOCKET', root / 'control.sock'), \
                mock.patch.object(VT, 'WG_CONF', root / 'wg0.conf'), \
                mock.patch.object(VT, 'run', node({P})), mock.patch.object(VT.wg_conf, 'subprocess', process), \
                mock.patch.dict(os.environ, {'PATH': str(bin_dir)}):
            VT.wg_apply_check(report)
        return [(row['result'], row['reason']) for row in report.rows], report.code()

    def test_nothing_to_check_with_is_not_checked(self):
        def denied(args, **_options):
            return subprocess.CompletedProcess(args, 1, b'', b'unshare: unshare failed: Operation not permitted\n')

        def hangs(args, **_options):
            raise subprocess.TimeoutExpired(args, 30)

        def never(*_args, **_options):
            raise AssertionError('wg asked without wg')

        for case, asked, path in (('no-wg', never, 'no-wg'), ('no-rights', denied, 'bin'),
                                  ('endpoint-name', hangs, 'bin')):
            with self.subTest(case), tempfile.TemporaryDirectory() as directory:
                rows, code = self.check(directory, asked, path)
                self.assertEqual(len(rows), 1, rows)
                self.assertEqual(rows[0][0], VT.STATUSES[VT.UNKNOWN], rows)
                self.assertIn(NOCHECK, rows[0][1])
                self.assertEqual(code, 2, rows)

    def test_wg_refusing_the_file_is_still_a_failure(self):
        calls = []

        def refuses(args, **_options):
            calls.append(args)
            if len(calls) == 1:
                return subprocess.CompletedProcess(args, 121, b'', b"Line unrecognized: `Bogus=1'\n"
                                                                    b'Configuration parsing error\n')
            return subprocess.CompletedProcess(args, 0, b'', b'')

        with tempfile.TemporaryDirectory() as directory:
            rows, code = self.check(directory, refuses, 'bin')
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0][0], VT.STATUSES[VT.FAIL], rows)
        self.assertIn('wg0.conf: wg отвергает файл', rows[0][1])
        self.assertEqual(code, 1)


class VerifyRestoreTests(unittest.TestCase):
    """4 the re-read of the saved wg0.conf fails: wg0.conf and control-wg.json come back byte for byte."""

    def test_failed_reread_still_restores_both_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'work').mkdir()
            (root / 'control.sock').touch()
            conf, state = root / 'wg0.conf', root / 'wg.json'
            conf.write_bytes(CONF.encode())
            state.write_bytes(b'{"version": 7}\n')
            before = conf.read_bytes(), state.read_bytes()
            live = {P}
            reads = []

            def read(_data):
                reads.append(1)
                if len(reads) > 1:
                    raise ValueError(NOCHECK + ' - wg не ответил за 30 с')
                return {'PrivateKey': S}, [{'key': P, 'preshared_key': PSK, 'allowed': ['10.66.66.2/32']}]

            def control_call(_method, params):
                # The service writes wg0.conf and its version state as wg.apply does.
                live.clear()
                live.update(peer['key'] for peer in params['peers'])
                conf.write_text('[Interface]\n' + ''.join(f'\n[Peer]\nPublicKey = {peer["key"]}\n'
                                                          for peer in params['peers']))
                state.write_text(json.dumps({'version': params['version']}))

            with mock.patch.object(VT, 'CONTROL_SOCKET', root / 'control.sock'), \
                    mock.patch.object(VT, 'WG_CONF', conf), mock.patch.object(VT, 'CONTROL_WG_STATE', state), \
                    mock.patch.object(VT, 'WORK', root / 'work'), mock.patch.object(VT, 'run', node(live)), \
                    mock.patch.object(VT, 'control_call', control_call), mock.patch.object(VT.wg_conf, 'read', read):
                try:
                    VT.wg_apply_check(VT.Report('test'))
                except Exception:  # what verify does with the failure is not this test's
                    pass
            self.assertGreater(len(reads), 1, 'the saved wg0.conf was not read again')
            self.assertEqual((conf.read_bytes(), state.read_bytes()), before)


class VerifyFreeAddressesTests(unittest.TestCase):
    """5 a latin-1 byte in a wg0.conf comment, LANG=C: free addresses as wg holds the file."""

    PROBE = '''
import importlib.util, json, sys, types
from pathlib import Path
spec = importlib.util.spec_from_file_location('vt', sys.argv[1])
vt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vt)
vt.WG_CONF = Path(sys.argv[2])
def run(*args, **_options):
    if args[0] == 'ip':
        return types.SimpleNamespace(stdout=json.dumps([{'addr_info': [{'local': '10.66.66.1', 'prefixlen': 29}]}]))
    return types.SimpleNamespace(stdout='')
vt.run = run
print(json.dumps(vt.WireGuard.free_addresses(3)))
'''

    def test_latin1_comment_is_read_as_wg_reads_it(self):
        with tempfile.TemporaryDirectory() as directory:
            conf = Path(directory) / 'wg0.conf'
            # wg takes the file: the byte is after `#`, wg drops it with the comment.
            conf.write_bytes(f'[Interface]\nPrivateKey = {S} # caf'.encode() + b'\xe9\n\n[Peer]\n' +
                             f'PublicKey = {P}\nAllowedIPs = 10.66.66.6/32\n'.encode())
            env = {'PATH': os.environ['PATH'], 'LANG': 'C', 'LC_ALL': 'C'}
            done = subprocess.run([sys.executable, '-I', '-c', self.PROBE, str(ROOT / 'infra/lib/verify-traffic.py'),
                                   str(conf)], env=env, capture_output=True, text=True, timeout=30, check=False)
        self.assertNotIn('UnicodeDecodeError', done.stderr)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        # /29: hosts .1-.6, .1 is wg0 itself, .6 is the peer of wg0.conf (live wg shows none).
        self.assertEqual(json.loads(done.stdout), ['10.66.66.5', '10.66.66.4', '10.66.66.3'])


class DeployBackupTests(unittest.TestCase):
    """1 deploy.sh do_backup on exit 4 of the copy: nothing to check with vs a real mismatch."""

    OLD = 'архив сохранён с предупреждением: БД и конфиги VPN расходятся - перевыпусти перечисленные выше устройства в панели'

    def do_backup(self, copy):
        script = function('do_backup') + 'load_env() { :; }\nwarn() { printf "[ru] %s\\n" "$*" >&2; }\n' + \
            'ssh_exec() {\n' + copy + '\nreturn 4\n}\nrc=0; do_backup || rc=$?; echo "rc=$rc"\n'
        with tempfile.TemporaryDirectory() as directory:
            done = bash(script, directory)
        return done.stdout + done.stderr, done.stdout.strip().splitlines()[-1]

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
    """3 the event of the copy for any reason: valid UTF-8 JSON the panel schema takes."""

    REASONS = {
        'tab': b'tab\there',
        'cr': b'cr\rhere',
        'ctl-01': b'ctl\x01here',
        'esc': b'esc\x1b[31mhere',
        'not-utf8': b'latin1 \xff\xfe byte',
        'long-1000': ('Операция не позволена ' * 46).encode()[:1000],
    }

    def event(self, directory, reason, locale):
        """anysda-backup.sh as it is up to the check, its loop over slices, the exit; the check says `reason`, exit 5."""
        root = Path(directory)
        (root / 'etc').mkdir()
        (root / 'etc/tgbot-secret.txt').write_text('s3cret\n')
        (root / 'reason').write_bytes(NOCHECK.encode() + b' - unshare --net: ' + reason)
        text = source('infra/lib/anysda-backup.sh').replace('/etc/anysda/', f'{root}/etc/')
        fake_curl = ('curl() { while (($#)); do [[ $1 == -d ]] && printf "%s" "$2" > body; shift; done; '
                     'cat > header; }\n')
        head = text.split('# Сверка среза с БД того же среза', 1)[0].replace('umask 077\n', 'umask 077\n' + fake_curl, 1)
        loop = cut(text, 'for attempt in 1 2 3 4 5 6 7 8 9 10; do', '\ndone\n') + '\ndone\n'
        script = head + 'BUNDLE_DIR=.\nsnapshot_access() { :; }\ncheck_bundle() { cat reason; return 5; }\n' + loop + \
            '[[ -z $WARNING ]] || exit 4\n'
        env = {'PATH': os.environ['PATH'], 'LC_ALL': locale, 'LANG': locale}
        done = subprocess.run(['bash', '-c', script], cwd=directory, env=env, capture_output=True, timeout=15,
                              check=False)
        self.assertEqual(done.returncode, 4, done.stderr[-2000:])
        body = root / 'body'
        self.assertTrue(body.exists(), 'no event reached the panel')
        return body.read_bytes()

    def test_schema_copy_is_the_panel_schema(self):
        self.assertIn(SCHEMA, source('app/server/api/bot/events.post.ts').splitlines()[11])

    def test_any_reason_gives_a_body_the_panel_takes(self):
        for locale in ('C', 'C.UTF-8'):
            for case, reason in self.REASONS.items():
                with self.subTest(case=case, locale=locale), tempfile.TemporaryDirectory() as directory:
                    raw = self.event(directory, reason, locale)
                    try:
                        text = raw.decode('utf-8')
                    except UnicodeDecodeError as error:
                        self.fail(f'body is not UTF-8: {error}: {raw[-80:]!r}')
                    try:
                        body = json.loads(text)  # strict: a raw control character is not JSON, as for JSON.parse
                    except ValueError as error:
                        self.fail(f'body is not JSON: {error}: {text[-80:]!r}')
                    self.assertEqual(set(body), {'type', 'detail'})
                    self.assertEqual(body['type'], 'backup_warning')
                    self.assertIsInstance(body['detail'], str)
                    detail = body['detail'].strip(JS_SPACE)
                    # zod max counts String length: UTF-16 code units.
                    self.assertLessEqual(len(detail.encode('utf-16-le')) // 2, 200, detail)
                    self.assertTrue(detail.startswith('сверка БД с конфигами VPN не выполнена'), detail)
                    self.assertIn('проверить нечем', detail)


if __name__ == '__main__':
    unittest.main()
