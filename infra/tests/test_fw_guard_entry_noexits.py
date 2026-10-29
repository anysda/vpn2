"""Пульс сторожа на entry, когда внешнюю точку проверки взять негде.

Экзитов нет или они ещё не пускают по ssh (00-bootstrap на чистой ОС): пульс
идёт через lo, как раньше, а журнал один раз за стадию говорит, что вход
снаружи не проверяется. Ложного отката быть не должно.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_fw_guard_entry_beat import ENTRY, EXIT, ROOT, STUBS, functions


EXIT_DOWN = f'''
ssh_fresh() {{
  if [[ $SSH_HOST == {EXIT} ]]; then echo "exit-down $*" >> calls; return 255; fi
  node_cmd "$@"
}}
'''


class EntryBeatWithoutExitsTests(unittest.TestCase):
    def run_entry_stage(self, exit_tags, extra='', skipped='', stage='00-bootstrap'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in ('scripts', 'envs', 'lib'):
                (root / path).mkdir()
            (root / 'scripts' / f'{stage}.sh').touch()
            (root / 'envs/all.env').write_text(f'ENTRY_HOST={ENTRY}\n')
            (root / 'envs/ru.env').write_text('HOST_TAG=ru\n')
            (root / 'envs/se.env').write_text('HOST_TAG=se\n')
            (root / 'envs/exits.env').write_text(f"EXIT_TAGS='{exit_tags}'\n")
            source = (ROOT / 'infra/deploy.sh').read_text()
            settings = ''.join(line + '\n' for line in source.splitlines()
                               if line.startswith(('FW_GUARD_STAGES=', 'FW_GUARD_WINDOW=')))
            script = (settings + functions(ROOT / 'infra/lib/ssh.sh')
                      + functions(ROOT / 'infra/deploy.sh') + STUBS + extra + f'''
DEPLOY_ROOT=$PWD
STAGE={stage}
SKIPPED_EXIT_TAGS='{skipped}'
HOST_TAG=ru
if run_stage_on_host {stage} ru; then echo RESULT=0; else echo RESULT=1; fi
''')
            env = dict(os.environ, VPN2_FW_GUARD_WINDOW='1')
            result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=directory,
                                    env=env, text=True, capture_output=True, timeout=90)
            beats = (root / 'beats').read_text().split() if (root / 'beats').exists() else []
            calls = (root / 'calls').read_text() if (root / 'calls').exists() else ''
            return result, beats, calls

    def test_entry_without_exits_beats_over_lo_and_warns_once(self):
        result, beats, calls = self.run_entry_stage('')
        detail = result.stdout + result.stderr + calls
        self.assertIn('entry', beats, detail)
        self.assertGreater(beats.count('entry'), 1, detail)
        self.assertEqual(result.stdout.count('вход на entry снаружи не проверяется: экзитов нет'), 1, detail)
        self.assertNotIn('откатан', result.stdout, detail)
        self.assertIn('RESULT=0', result.stdout, detail)

    def test_exits_not_up_yet_do_not_roll_back_entry(self):
        result, beats, calls = self.run_entry_stage('se', EXIT_DOWN)
        detail = result.stdout + result.stderr + calls
        self.assertIn('exit-down', calls, detail)
        self.assertIn('entry', beats, detail)
        self.assertEqual(result.stdout.count('вход на entry снаружи не проверяется: экзиты пока не отвечают'), 1,
                         detail)
        self.assertNotIn('откатан', result.stdout, detail)
        self.assertIn('RESULT=0', result.stdout, detail)

    def test_skipped_exit_counts_as_no_exits(self):
        result, beats, calls = self.run_entry_stage('se', skipped='se')
        detail = result.stdout + result.stderr + calls
        self.assertNotRegex(calls, r'(?m)^exit ', detail)
        self.assertIn('entry', beats, detail)
        self.assertIn('RESULT=0', result.stdout, detail)


if __name__ == '__main__':
    unittest.main()
