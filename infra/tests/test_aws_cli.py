"""AWS installer regression; optional real release via AWS_CLI_TEST_ARCHIVE."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
import zipfile


ROOT = Path(os.environ.get("VPN2_TEST_ROOT", Path(__file__).resolve().parents[2]))
STAGE = (ROOT / "infra/scripts/26-backup.sh").read_text()
START = STAGE.index('if [[ "$BACKUP_BACKEND" == "s3" ]] &&')
BLOCK = STAGE[START:STAGE.index("# ── 2.", START)]
ARCHIVE = os.environ.get("AWS_CLI_TEST_ARCHIVE")


class AwsCliTests(unittest.TestCase):
    def run_installer(self, directory, *, backend="s3", installed=False, signature=True):
        directory = Path(directory)
        temporary = directory / "tmp"
        temporary.mkdir(exist_ok=True)
        script = r'''
set -euo pipefail
command() {
  if [[ "$*" == '-v aws' ]]; then
    [[ "$INSTALLED" == 1 ]]
  else
    builtin command "$@"
  fi
}
curl() {
  echo "$2" >> "$CASE/downloads"
  if [[ "$2" == *.sig ]]; then
    [[ "$SIGNATURE" == 1 ]] || return 22
    cp "$CASE/archive.zip.sig" "$4"
  else
    cp "$CASE/archive.zip" "$4"
  fi
}
unzip() {
  touch "$CASE/unpacked"
  builtin command unzip "$@"
}
aws() { "$CASE/bin/aws" "$@"; }
'''
        # Execute the unchanged stage block except for installation destinations.
        block = BLOCK.replace("/usr/local/bin", '"$CASE/bin"').replace(
            "/usr/local/aws-cli", '"$CASE/aws-cli"'
        )
        result = subprocess.run(
            ["bash", "-c", script + block + '\ntouch "$CASE/completed"\n'],
            env={**os.environ, "CASE": str(directory), "TMPDIR": str(temporary),
                 "HOST_TAG": "ru", "BACKUP_BACKEND": backend,
                 "INSTALLED": str(int(installed)), "SIGNATURE": str(int(signature))},
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(list(temporary.iterdir()), [], result.stderr)
        return result

    def forged_archive(self, directory):
        archive = Path(directory) / "archive.zip"
        installer = Path(directory) / "install"
        installer.write_text('#!/bin/sh\ntouch "$CASE/executed"\n')
        installer.chmod(0o755)
        with zipfile.ZipFile(archive, "w") as output:
            output.write(installer, "aws/install")
        Path(str(archive) + ".sig").write_text("forged signature\n")

    def assert_rejected(self, directory, result):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        for marker in ("executed", "unpacked", "completed"):
            self.assertFalse((Path(directory) / marker).exists(), marker + result.stderr)

    def test_substituted_zip_rejected_before_unpacking(self):
        with tempfile.TemporaryDirectory() as directory:
            self.forged_archive(directory)
            result = self.run_installer(directory)
            self.assert_rejected(directory, result)
            self.assertIn("signature verification failed", result.stderr)

    def test_missing_signature_stops_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            self.forged_archive(directory)
            self.assert_rejected(directory, self.run_installer(directory, signature=False))

    def test_existing_aws_skips_download(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_installer(directory, installed=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((Path(directory) / "downloads").exists())

    def test_local_backend_skips_download(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_installer(directory, backend="local")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((Path(directory) / "downloads").exists())

    @unittest.skipUnless(ARCHIVE, "set AWS_CLI_TEST_ARCHIVE to a signed AWS ZIP")
    def test_official_release_and_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "archive.zip"
            archive.symlink_to(ARCHIVE)
            Path(str(archive) + ".sig").symlink_to(ARCHIVE + ".sig")
            result = self.run_installer(directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            version = re.search(r"AWS_CLI_VERSION=([\d.]+)", BLOCK).group(1)
            self.assertIn("aws-cli/" + version + " ", result.stdout)
            downloads = (Path(directory) / "downloads").read_text().splitlines()
            self.assertEqual(downloads, [
                f"https://awscli.amazonaws.com/awscli-exe-linux-x86_64-{version}.zip",
                f"https://awscli.amazonaws.com/awscli-exe-linux-x86_64-{version}.zip.sig",
            ])
            result = self.run_installer(directory, installed=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((Path(directory) / "downloads").read_text().splitlines(), downloads)
            print(f"official release: aws-cli/{version}; repeat: no downloads")

    @unittest.skipUnless(ARCHIVE, "set AWS_CLI_TEST_ARCHIVE to a signed AWS ZIP")
    def test_substituted_zip_with_official_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            self.forged_archive(directory)
            signature = Path(directory) / "archive.zip.sig"
            signature.unlink()
            signature.symlink_to(ARCHIVE + ".sig")
            result = self.run_installer(directory)
            self.assert_rejected(directory, result)
            self.assertIn("BAD signature", result.stderr)
            print("substituted ZIP + official signature: rejected before unzip/install")


if __name__ == "__main__":
    unittest.main(verbosity=2)
