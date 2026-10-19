import json
from pathlib import Path
import tempfile
import unittest

from test_deploy import bash, source

PANEL = 'http://127.0.0.1:51821/api/bot/events'


def run_backup(directory, env_file, finish=None):
    """Настоящий anysda-backup.sh с /etc/anysda во временном каталоге и curl-записью."""
    root = Path(directory)
    (root / 'etc').mkdir()
    (root / 'etc' / 'tgbot-secret.txt').write_text('s3cret\n')
    if env_file is not None:
        (root / 'etc' / 'backup.env').write_text(env_file)
    script = source('infra/lib/anysda-backup.sh').replace('/etc/anysda/', f'{root}/etc/')
    fake_curl = ('curl() { local url=${*: -1}; [[ $url == ' + PANEL + ' ]] || return 0; '
                 'cat > header; for a in "$@"; do printf "%s\\n" "$a"; done > args; }\n')
    script = script.replace('umask 077\n', 'umask 077\n' + fake_curl, 1)
    if finish is not None:
        # Конец скрипта вместо съёмки: только отчёт в панель по коду выхода.
        script = script.split('# Сверка среза с БД того же среза', 1)[0] + finish
    result = bash(script, directory)
    args = (root / 'args').read_text().splitlines() if (root / 'args').exists() else None
    header = (root / 'header').read_text() if (root / 'header').exists() else None
    return result, args, header


class BackupReportTests(unittest.TestCase):
    def test_missing_passphrase_reports_failure_to_panel_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            result, args, header = run_backup(directory, 'BACKUP_BACKEND=local\n')
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIsNotNone(args, 'сбой бэкапа не дошёл до панели')
            body = json.loads(args[args.index('-d') + 1])
            self.assertEqual(body, {'type': 'backup_failed', 'detail': 'нет пароля шифрования'})
            # Секрет только через stdin, не аргументом процесса.
            self.assertEqual(header, 'Authorization: Bearer s3cret\n')
            self.assertFalse(any('s3cret' in a for a in args))

    def test_any_failed_exit_reports_its_code(self):
        with tempfile.TemporaryDirectory() as directory:
            result, args, _ = run_backup(directory, None)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIsNotNone(args, 'сбой бэкапа не дошёл до панели')
            self.assertEqual(json.loads(args[args.index('-d') + 1])['detail'], 'код выхода 2')


    def test_archive_saved_with_warning_reports_warning_not_failure(self):
        warning = 'БД и конфиги VPN расходятся (устройства: 7) — перевыпусти в панели'
        with tempfile.TemporaryDirectory() as directory:
            result, args, header = run_backup(directory, None, f"WARNING='{warning}'\nexit 4\n")
            self.assertEqual(result.returncode, 4, result.stderr)
            self.assertIsNotNone(args, 'предупреждение бэкапа не дошло до панели')
            self.assertEqual(json.loads(args[args.index('-d') + 1]), {'type': 'backup_warning', 'detail': warning})
            self.assertEqual(header, 'Authorization: Bearer s3cret\n')

    def test_successful_backup_reports_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            result, args, _ = run_backup(directory, None, 'exit 0\n')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNone(args)

if __name__ == '__main__':
    unittest.main()
