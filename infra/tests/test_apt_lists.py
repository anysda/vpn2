"""APT update runs when fresh list directories do not contain usable indexes."""
from pathlib import Path
import os
import tempfile
import time
import unittest

from test_deploy import bash, source


class AptListsTests(unittest.TestCase):
    def run_update(self, index=None, source_is_newer=False, stale_index=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lists = root / 'lists'
            sources = root / 'sources.list.d'
            source_list = root / 'sources.list'
            sources.mkdir()
            source_list.write_text('deb https://example.test stable main\n')
            source_file = sources / 'extra.sources'
            source_file.write_text('Types: deb\n')
            (lists / 'partial').mkdir(parents=True)
            now = time.time()
            source_time = now - 120
            if source_is_newer:
                source_time = now + 1
            for path in (source_list, source_file, sources):
                os.utime(path, (source_time, source_time))
            if index:
                # apt ставит индексу Last-Modified зеркала, не время скачивания
                (lists / index).write_text('index\n')
                os.utime(lists / index, (now - 30 * 86400, now - 30 * 86400))
            lists_time = now - 3660 if stale_index else now - 60
            os.utime(lists, (lists_time, lists_time))
            library = source('infra/lib/anysda-svc.sh')
            library = library.replace('/var/lib/apt/lists', str(lists))
            library = library.replace('/etc/apt/sources.list.d', str(sources))
            library = library.replace('/etc/apt/sources.list', str(source_list))
            result = bash(library + '''
apt_get() { printf '%s\\n' "$*"; }
apt_update_if_stale
''', directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

    def test_empty_lists_with_partial_runs_update(self):
        self.assertEqual(self.run_update(), 'update -qq\n')

    def test_fresh_packages_index_skips_update(self):
        self.assertEqual(self.run_update('mirror_Packages.lz4'), '')

    def test_fresh_inrelease_skips_update(self):
        self.assertEqual(self.run_update('mirror_InRelease'), '')

    def test_source_newer_than_lists_runs_update(self):
        self.assertEqual(self.run_update('mirror_Packages.lz4', source_is_newer=True), 'update -qq\n')

    def test_stale_lists_run_update(self):
        self.assertEqual(self.run_update('mirror_Packages.lz4', stale_index=True), 'update -qq\n')

    def test_stage_installers_use_shared_list_check(self):
        for stage in ('26-backup', '27-ikev2', '29-openvpn'):
            with self.subTest(stage=stage):
                text = source(f'infra/scripts/{stage}.sh')
                self.assertTrue('apt_update_if_stale' in text, f'{stage} bypasses shared check')
                self.assertFalse('find /var/lib/apt/lists' in text, f'{stage} has local list check')


if __name__ == '__main__':
    unittest.main()
