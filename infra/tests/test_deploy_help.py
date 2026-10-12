from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from test_deploy import ROOT


class DeployHelpTests(unittest.TestCase):
    def test_help_without_config_or_env_generation(self):
        for flag in ('--help', '-h'):
            for config in (None, 'invalid config: ['):
                with self.subTest(flag=flag, config=config), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    (root / 'infra/lib').mkdir(parents=True)
                    for name in ('deploy.sh', 'infra/deploy.sh', 'infra/lib/ssh.sh', 'infra/lib/anysda-svc.sh'):
                        shutil.copy2(ROOT / name, root / name)
                    # A converter call must fail even when config.yaml exists.
                    (root / 'infra/lib/config2env.py').write_text('raise RuntimeError("converter called")\n')
                    if config is not None:
                        (root / 'config.yaml').write_text(config)
                    result = subprocess.run(['bash', str(root / 'deploy.sh'), flag],
                                            input='', capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn('./deploy.sh <stage> <group>', result.stdout)
                    self.assertNotIn('converter called', result.stderr)
                    self.assertFalse((root / 'infra/envs').exists())
