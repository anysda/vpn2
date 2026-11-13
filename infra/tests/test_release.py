"""Исполнитель миграций панели (app/scripts/migrate.mjs) и сборка манифеста выпуска для тестов образов."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'infra/vpn2/src'), str(ROOT / 'infra/vpn2/tests')]
from test_release_state import build_manifest  # noqa: E402


class ReleaseTests(unittest.TestCase):
    def build_manifest(self, work, migrations_after_tag):
        return build_manifest(work, migrations_after_tag)

    def test_migration_is_explicit_idempotent_and_rejects_future_history(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'db.sqlite'
            env = dict(os.environ, NUXT_DATABASE_URL=f'file:{database}')

            def migrate():
                return subprocess.run(['node', 'scripts/migrate.mjs'], cwd=ROOT / 'app', env=env,
                                      capture_output=True, text=True, timeout=30)
            for _ in range(2):
                run = migrate()
                self.assertEqual(run.returncode, 0, run.stderr)
            with closing(sqlite3.connect(database)) as db, db:
                count = db.execute('SELECT count(*) FROM __drizzle_migrations').fetchone()[0]
                db.execute("INSERT INTO __drizzle_migrations(hash,created_at) VALUES ('future',9999999999999)")
            self.assertNotEqual(migrate().returncode, 0)
            with closing(sqlite3.connect(database)) as db, db:
                self.assertEqual(db.execute('SELECT count(*) FROM __drizzle_migrations').fetchone()[0], count + 1)


if __name__ == '__main__':
    unittest.main()
