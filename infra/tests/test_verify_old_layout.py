"""Остатки v1.1.5, которые deploy.sh сносит после зелёного verify: список и отсутствие писателей."""
from pathlib import Path
import unittest

import yaml

ROLES = Path(__file__).resolve().parents[1] / 'ansible/roles'
DEFAULTS = yaml.safe_load((ROLES / 'verify/defaults/main.yml').read_text(encoding='utf-8'))
# Стадии сети больше не пишут отметок; от v1.1.5 они остаются на entry.
DROPPED_STAMPS = [f'/var/anysda/.stamps/{stage}'
                  for stage in ('19-yt-zapret', '20-ru-router', '27-ikev2', '28-wireguard', '29-openvpn')]


class OldLayoutTests(unittest.TestCase):
    def test_dropped_stage_stamps_are_removed_on_entry(self):
        for stamp in DROPPED_STAMPS:
            with self.subTest(stamp=stamp):
                self.assertIn(stamp, DEFAULTS['verify_old_layout_entry'])

    def test_no_role_writes_a_removed_path(self):
        # Путь, который роль снова пишет, сносился бы после каждого деплоя.
        removed = DEFAULTS['verify_old_layout_entry'] + DEFAULTS['verify_old_layout_exit']
        writers = []
        for source in sorted(ROLES.glob('*/t*/**/*')):
            name = source.relative_to(ROLES)
            if not source.is_file() or name.parts[0] == 'verify':
                continue
            text = source.read_text(encoding='utf-8', errors='replace')
            writers += [f'{name}: {path}' for path in removed if path in text]
        self.assertEqual(writers, [])


if __name__ == '__main__':
    unittest.main()
