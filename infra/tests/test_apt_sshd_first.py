"""openssh обновляется до включения сторожа фаервола.

Первый apt стадии 00-bootstrap обновляет сотни пакетов одним заходом. prerm
openssh-server останавливает sshd, а запускает его configure в конце всего
захода: на медленном диске это дольше окна сторожа, и сторож откатывал фаервол
из-за нашего же обновления. apt_sshd_first обновляет один openssh отдельным
коротким заходом в подготовке стадии, когда сторож ещё не взведён.
"""
from pathlib import Path
import tempfile
import unittest

from infra_support import bash, source


class AptSshdFirstTests(unittest.TestCase):
    def run_first(self, versions='1:9.6p1-3 1:9.6p1-3ubuntu13.5', stamp=False, installed=True,
                  answers='true', update_rc=0):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            if stamp:
                (root / 'stamp').write_text('')
            result = bash(source('infra/lib/anysda-svc.sh') + f'''
APT_SSHD_STAMP={root}/stamp
APT_SSHD_WAIT=1
apt_have() {{ {'true' if installed else 'false'}; }}
apt_update_if_stale() {{ echo update >> calls; return {update_rc}; }}
apt_sshd_versions() {{ echo '{versions}'; }}
apt_get() {{ echo "apt_get $*" >> calls; }}
sshd_answers() {{ echo answers >> calls; {answers}; }}
sleep() {{ :; }}
rc=0
apt_sshd_first || rc=$?
echo "rc=$rc"
''', directory)
            calls = root / 'calls'
            return result, calls.read_text().splitlines() if calls.exists() else []

    def test_newer_candidate_upgrades_openssh_with_certificates_and_waits_for_sshd(self):
        result, calls = self.run_first()
        self.assertEqual(result.stdout.splitlines()[-1], 'rc=0', result.stderr)
        self.assertEqual(calls[0], 'update')
        self.assertTrue(calls[1].startswith('apt_get install -y -qq --only-upgrade '), calls)
        self.assertTrue(calls[1].endswith(' openssh-server openssh-client openssh-sftp-server ca-certificates'), calls)
        self.assertEqual(calls[2:], ['answers'])
        self.assertIn('1:9.6p1-3 -> 1:9.6p1-3ubuntu13.5', result.stdout)

    def test_same_version_runs_no_install_and_no_wait(self):
        result, calls = self.run_first(versions='1:9.6p1-3 1:9.6p1-3')
        self.assertEqual(result.stdout, 'rc=0\n', result.stderr)
        self.assertEqual(calls, ['update'])

    def test_repeat_deploy_does_nothing(self):
        result, calls = self.run_first(stamp=True)
        self.assertEqual(result.stdout, 'rc=0\n', result.stderr)
        self.assertEqual(calls, [])

    def test_node_without_openssh_server_does_nothing(self):
        result, calls = self.run_first(installed=False)
        self.assertEqual(result.stdout, 'rc=0\n', result.stderr)
        self.assertEqual(calls, [])

    def test_unknown_candidate_leaves_the_error_to_the_stage(self):
        for versions in ('1:9.6p1-3 (none)', '1:9.6p1-3', ''):
            with self.subTest(versions=versions):
                result, calls = self.run_first(versions=versions, update_rc=100)
                self.assertEqual(result.stdout, 'rc=0\n', result.stderr)
                self.assertEqual(calls, ['update'])

    def test_dead_sshd_after_upgrade_fails_the_stage_before_the_guard(self):
        result, calls = self.run_first(answers='false')
        self.assertEqual(result.stdout.splitlines()[-1], 'rc=1', result.stderr)
        self.assertIn('sshd не отвечает', result.stdout)
        self.assertIn('answers', calls)


if __name__ == '__main__':
    unittest.main()
