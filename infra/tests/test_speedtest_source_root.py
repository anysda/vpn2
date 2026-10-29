"""Список экзитов speedtest берётся из config.yaml клона по отметке source-root."""
from pathlib import Path
import argparse
import runpy
import subprocess
import tempfile
import unittest

from test_deploy import ROOT


class Stop(Exception):
    """Список экзитов получен, дальше run() не нужен."""


CONFIG = 'exits:\n  - tag: exit-a\n  - tag: exit-b\n'


class ExitListFromSourceRoot(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='speedtest-root-')
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)
        self.mark = self.tmp / 'source-root'
        self.opt = self.tmp / 'opt'
        self.opt.mkdir()

    def exits(self):
        """Команда списка экзитов из run(), выполненная локальным bash."""
        speedtest = runpy.run_path(str(ROOT / 'infra/tools/speedtest.py'))
        ran = []

        def sh(_target, cmd, **kwargs):
            if kwargs.get('via') or ran:
                raise Stop
            cmd = (cmd.replace('/etc/anysda/source-root', str(self.mark))
                      .replace('/opt/anysda-vpn2', str(self.opt)))
            res = subprocess.run(['bash', '-c', cmd], capture_output=True, text=True)
            ran.append(res)
            if not res.stdout.split():
                raise Stop
            return res.stdout

        speedtest['run'].__globals__['sh'] = sh
        args = argparse.Namespace(client=[], hub='hub', exits=None)
        with self.assertRaises(Stop):
            speedtest['run'](args)
        self.assertEqual(ran[0].returncode, 0, ran[0].stderr)
        return ran[0].stdout.split()

    def test_clone_path_with_space_and_quote(self):
        clone = self.tmp / "my vpn'2"
        clone.mkdir()
        (clone / 'config.yaml').write_text(CONFIG)
        self.mark.write_text(f'{clone}\n')
        self.assertEqual(self.exits(), ['exit-a', 'exit-b'])

    def test_no_mark_reads_opt(self):
        (self.opt / 'config.yaml').write_text(CONFIG)
        self.assertEqual(self.exits(), ['exit-a', 'exit-b'])


if __name__ == '__main__':
    unittest.main()
