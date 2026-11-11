"""Роль control: владелец файлов базы панели не падает на исчезнувшем временном файле SQLite."""
import getpass
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(os.environ.get('VPN2_AUDIT_ROOT', Path(__file__).resolve().parents[2]))
ROLE = ROOT / 'infra/ansible/roles/control'
FIND, OWNER = 'Файлы базы панели', 'Владелец базы - пользователь панели'
FILES = yaml.safe_load((ROLE / 'defaults/main.yml').read_text())['control_panel_files']
TEMPORARY = [name for name in FILES if name.endswith(('-wal', '-shm', '-journal'))]


def tasks():
    """Две задачи роли как есть; владелец заменён текущим пользователем - chown без root."""
    found = {task['name']: task for task in yaml.safe_load((ROLE / 'tasks/data.yml').read_text())}
    owner = found[OWNER]
    me = getpass.getuser()
    owner['ansible.builtin.file'].update(owner=me, group=str(os.getgid()))
    return found[FIND], owner


@unittest.skipUnless(shutil.which('ansible-playbook'), 'нужен ansible-playbook')
class ControlDataOwnerTests(unittest.TestCase):
    def play(self, present, vanish=(), owner_name=None):
        """Прогон find -> (файлы vanish исчезают) -> file по каталогу с файлами present."""
        find, owner = tasks()
        if owner_name:
            owner['ansible.builtin.file']['owner'] = owner_name
        between = [{'name': 'Файл исчез', 'ansible.builtin.file': {'path': '{{ control_data }}/' + name, 'state': 'absent'}}
                   for name in vanish]
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / 'data'
            data.mkdir()
            for name in present:
                (data / name).write_bytes(b'x')
            play = [{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                     'vars': {'control_data': str(data), 'control_panel_files': FILES},
                     'tasks': [find, *between, owner]}]
            book = Path(directory) / 'play.yml'
            book.write_text(yaml.safe_dump(play, allow_unicode=True))
            config = Path(directory) / 'ansible.cfg'
            config.write_text('[defaults]\n')
            env = {**os.environ, 'ANSIBLE_CONFIG': str(config), 'ANSIBLE_NOCOLOR': '1'}
            done = subprocess.run(['ansible-playbook', '-i', 'localhost,', str(book)], env=env, text=True,
                                  capture_output=True, timeout=300, check=False)
            left = sorted(path.name for path in data.iterdir())
        return done, left

    def test_names_cover_the_temporary_files_of_sqlite(self):
        self.assertEqual(sorted(TEMPORARY), ['access-worker.sqlite-journal', 'db.sqlite-shm', 'db.sqlite-wal'])

    def test_file_vanished_between_find_and_file_does_not_fail(self):
        # Исчезает каждый файл по очереди: и временный, и сама база (перенос, restore).
        for name in FILES:
            with self.subTest(vanished=name):
                done, left = self.play(FILES, vanish=[name])
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertIn('failed=0', done.stdout)
                self.assertEqual(left, sorted(set(FILES) - {name}))

    def test_all_temporary_files_vanish_at_once(self):
        done, _left = self.play(FILES, vanish=TEMPORARY)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_wal_and_shm_present_get_the_owner(self):
        done, left = self.play(FILES)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(left, sorted(FILES))
        for name in FILES:
            self.assertRegex(done.stdout, rf'ok: \[localhost\] => \(item=\S+/{name}\)')

    def test_base_absent_on_first_install(self):
        done, left = self.play([])
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(left, [])

    def test_real_failure_is_still_a_failure(self):
        # Терпим только исчезновение: чужой владелец, которого нет, остаётся ошибкой.
        done, _left = self.play(['db.sqlite'], owner_name='vpn2-no-such-user')
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn('failed=1', done.stdout)


if __name__ == '__main__':
    unittest.main()
