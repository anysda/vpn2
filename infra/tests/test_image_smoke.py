"""Exercise the image workflow command and check its non-root storage contract."""

import ast
import os
from pathlib import Path
import shlex
import subprocess
import unittest

import yaml


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))


class ImageSmokeTests(unittest.TestCase):
    def test_panel_smoke_mounts_database_for_image_user(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/images.yml').read_text())
        step = next(step for step in workflow['jobs']['build']['steps']
                    if step.get('name') == 'smoke panel')
        result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', '''
docker() { if [[ $1 == run ]]; then printf '%s\\n' "$@"; fi; }
curl() { return 0; }
''' + step['run']], capture_output=True, text=True, check=True)
        args = result.stdout.splitlines()
        image = args.index('smoke/panel')
        self.assertNotIn('--user', args[:image], 'smoke must use the image default user')
        user = (ROOT / 'app/Dockerfile').read_text().split('USER ')[-1].splitlines()[0]
        uid, gid = user.split(':')
        self.assertNotEqual(uid, '0')
        environment = dict(args[i + 1].split('=', 1) for i, arg in enumerate(args[:image])
                           if arg == '-e')
        self.assertIn('NUXT_DATABASE_URL', environment, 'smoke falls back to root-owned /app')
        database = environment['NUXT_DATABASE_URL'].removeprefix('file:')
        self.assertEqual(database, '/var/lib/anysda-vpn2/db.sqlite')
        mounts = [args[i + 1] for i, arg in enumerate(args[:image]) if arg == '--tmpfs']
        mount = next((mount for mount in mounts if mount.split(':')[0] == str(Path(database).parent)), '')
        self.assertTrue(mount, 'database needs a writable mount')
        self.assertIn(f'uid={uid}', mount)
        self.assertIn(f'gid={gid}', mount)
        self.assertIn('node scripts/migrate.mjs', args[-1])
        self.assertIn('exec node .output/server/index.mjs', args[-1])

    def test_upgrade_preserves_database_owner(self):
        source = (ROOT / 'app/e2e/pool-upgrade.sh').read_text()
        # docker cp defaults to root ownership in the destination container,
        # even when its configured runtime user is unprivileged.
        copy = next(line.strip() for line in source.splitlines()
                    if 'dk cp' in line and '"$NAME:' in line)
        self.assertIn('-a', shlex.split(copy))
        seed = next(line for line in source.splitlines() if ' - seed ' in line)
        self.assertIn('--user 10001:10001', seed)

    def test_tls_probe_migrates_its_fresh_database(self):
        tree = ast.parse((ROOT / 'infra/tests/probe_tls_live.py').read_text())
        scenario = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == 'panel_scenario')
        start = next(node for node in ast.walk(scenario)
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                     and node.func.id == 'docker' and ast.literal_eval(node.args[0]) == 'run')
        command = ast.literal_eval(start.args[-1])
        self.assertIn('node scripts/migrate.mjs;', command)
        self.assertLess(command.index('node scripts/migrate.mjs;'), command.index('exec node'))


if __name__ == '__main__':
    unittest.main()
