"""`./deploy.sh restore ./<архив>`: архив ложится туда, где его ищет anysda-restore.sh.

Настоящие infra/deploy.sh и anysda-restore.sh. Транспорт до entry подставной:
команды ssh исполняются локально, корень entry - временный каталог (пути
/etc/anysda, /var/backups, /var/lib/anysda-vpn2, /tmp/anysda, /usr/local/bin
переписаны внутрь него). age подменён: подставной expect записывает, какой
архив restore взялся расшифровывать, - дальше расшифровки тест не идёт.
"""
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
DEFAULT_DIR = '/var/backups/anysda-vpn2'
NAME = 'anysda-vpn2-2026-11-09T000000Z.tar.gz.age'
ENTRY_PATHS = r'(^|[^\w./-])/(etc/anysda|var/backups|var/lib/anysda-vpn2|tmp/anysda|usr/local/bin)'


def into_entry(text, entry):
    return re.sub(ENTRY_PATHS, lambda m: f'{m.group(1)}{entry}/{m.group(2)}', text)


class TestRestoreLocalArchivePath(unittest.TestCase):
    def restore(self, backup_env_dir, backend='local'):
        """Возвращает (результат deploy.sh, корень entry, что расшифровывал restore)."""
        directory = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ['rm', '-rf', directory])
        root = Path(directory) / 'clone'
        entry = Path(directory) / 'entry'
        for sub in ('lib', 'bin', 'scripts', 'envs'):
            (root / sub).mkdir(parents=True)
        for sub in ('etc/anysda', 'var/lib/anysda-vpn2', 'usr/local/bin', 'stub'):
            (entry / sub).mkdir(parents=True)
        for path in ('infra/deploy.sh', 'infra/lib/deploy-release.sh', 'infra/lib/ansible-venv.sh'):
            (root / path.removeprefix('infra/')).write_text((ROOT / path).read_text())
        (root / 'lib/anysda-svc.sh').write_text('')
        for tool in ('ssh', 'scp', 'sshpass', 'envsubst', 'curl', 'rsync', 'openssl'):
            (root / 'bin' / tool).write_text('#!/bin/sh\nexit 0\n')
            (root / 'bin' / tool).chmod(0o755)
        restore_sh = entry / 'usr/local/bin/anysda-restore.sh'
        restore_sh.write_text(into_entry((ROOT / 'infra/lib/anysda-restore.sh').read_text(), entry))
        restore_sh.chmod(0o755)
        expect = entry / 'stub/expect'
        expect.write_text(f'#!/bin/sh\ncat > {entry}/decrypting\nexit 1\n')
        expect.chmod(0o755)
        local_dir = into_entry(backup_env_dir, entry)
        (entry / 'etc/anysda/backup.env').write_text(
            "BACKUP_ENABLED='true'\n"
            f"BACKUP_BACKEND='{backend}'\n"
            "BACKUP_PASSPHRASE='pass'\n"
            "ANYSDA_BACKUP_PASSPHRASE='pass'\n"
            f"BACKUP_LOCAL_DIR='{local_dir}'\n")
        (root / 'lib/ssh.sh').write_text(f'''
C_R= C_G= C_Y= C_B= C_END=
log() {{ :; }}; ok() {{ :; }}; warn() {{ echo "warn: $*" >&2; }}
die() {{ echo "die: $*" >&2; exit 1; }}
load_env() {{ HOST_TAG=$1; }}
expand_hosts() {{ case $1 in ru|all) echo ru ;; *) die "unknown host group: $1" ;; esac; }}
# Выпуск (release.py, deploy_state.py) тесту не нужен: исполняются только
# команды про архив и каталог бэкапов.
push() {{
  [[ "${{2:-$1}}" == *.tar.gz.age ]] || return 0
  local target='{entry}/tmp/anysda/'"${{2:-$(basename "$1")}}"
  mkdir -p '{entry}/tmp/anysda'
  cp -r "$1" "$target"
  chmod "$(stat -c %a "$1")" "$target"
}}
ssh_exec() {{
  if [[ "$1" == *'exec flock'* ]]; then echo locked; read -r unlock; return 0; fi
  [[ "$1" == *.tar.gz.age* || "$1" == *BACKUP_LOCAL_DIR* || "$1" == *backups* || "$1" == *backup.env* ]] || return 0
  local cmd; cmd=$(printf '%s' "$1" | python3 -c 'import re, sys
print(re.sub(sys.argv[1], lambda m: m.group(1) + sys.argv[2] + "/" + m.group(2), sys.stdin.read()))' \\
    '{ENTRY_PATHS}' '{entry}')
  PATH='{entry}/stub':$PATH bash -c "$cmd"
}}
''')
        archive = Path(directory) / NAME
        archive.write_bytes(b'archive')
        archive.chmod(0o644)
        environment = dict(os.environ, PATH=f'{root / "bin"}:{os.environ["PATH"]}',
                           VPN2_DEVELOPMENT='true')
        result = subprocess.run(['bash', str(root / 'deploy.sh'), 'restore', str(archive), '--force'],
                                cwd=root, env=environment, text=True, capture_output=True, timeout=30)
        decrypting = entry / 'decrypting'
        return result, entry, decrypting.read_text() if decrypting.exists() else ''

    def assert_restored_from(self, backup_env_dir, backend='local'):
        result, entry, decrypting = self.restore(backup_env_dir, backend)
        output = result.stdout + result.stderr
        placed = Path(into_entry(backup_env_dir, entry)) / NAME
        self.assertNotIn('не найден', output)
        self.assertTrue(placed.is_file(), f'архива нет в {placed}\n{output}')
        self.assertEqual(stat.S_IMODE(placed.stat().st_mode), 0o600)
        self.assertIn(f'anysda-restore: source = {placed}', output)
        # restore дошёл до расшифровки именно этого файла
        self.assertIn('age -d -o', decrypting)
        self.assertIn(f'"{placed}"', decrypting)
        return entry

    def test_custom_local_dir_receives_archive(self):
        entry = self.assert_restored_from('/var/backups/srv-anysda-backups/nested')
        self.assertFalse((entry / DEFAULT_DIR.lstrip('/') / NAME).exists())

    def test_default_local_dir_unchanged(self):
        self.assert_restored_from(DEFAULT_DIR)

    def test_s3_backend_takes_uploaded_archive_locally(self):
        self.assert_restored_from(DEFAULT_DIR, backend='s3')


if __name__ == '__main__':
    unittest.main()
