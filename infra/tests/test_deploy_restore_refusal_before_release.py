"""Отказ restore до начала работ не открывает выпуск.

Возврат со стенда: `./deploy.sh restore latest` на узле с клиентами отказывал
«запусти повторно с --force» уже после записи выпуска в applying, и подсказанный
запуск с --force упирался в «unfinished deploy» до `./deploy.sh recover`. Пробный
проход --dry-run ловил только отсутствие архива. Тест гоняет настоящий
infra/deploy.sh из копии дерева; restore узла подставной и отказывает по
сценарию теми же кодами, что пакет: 3 архива нет, 4 расшифровка, 6 живая база
без --force, 9 версия.
"""
import unittest

from test_deploy_probe_timeout_before_release import FAKE_SSH, NOOP_STAGE, Tree

# Сценарий узла лежит в $FAKE_NODE/restore.mode, вызовы - в restore.log.
FAKE_RESTORE = r'''#!/bin/bash
mode=$(cat "$FAKE_NODE/restore.mode")
echo "$*" >> "$FAKE_NODE/restore.log"
has() { [[ " $ARGS " == *" '$1' "* ]]; }
ARGS="$*"
if has --preflight && [[ -e "$FAKE_NODE/old-package" ]]; then
  echo 'anysda-restore: неизвестный аргумент: --preflight' >&2; exit 2
fi
[[ $mode != no-archive ]] || { echo 'anysda-restore: архива нет' >&2; exit 3; }
if has --dry-run; then echo 'anysda-restore: dry-run'; exit 0; fi
case $mode in
  passphrase) echo 'anysda-restore: дешифровка провалилась (age exit=1)' >&2; exit 4 ;;
  no-force) has --force || { echo 'Запусти повторно с --force чтобы согласиться.' >&2; exit 6; } ;;
  version) has --allow-incompatible || { echo 'anysda-restore: ABORT - версия (обход: --allow-incompatible)' >&2; exit 9; } ;;
esac
has --preflight && exit 0
echo started >> "$FAKE_NODE/restore.log"
[[ $mode != dies ]] || { echo 'anysda-restore: сбой посреди замены' >&2; exit 10; }
echo 'anysda-restore: SUCCESS'
'''
RESTORE_CASE = '''  *anysda-restore.sh*)
    exec bash -c "$begin bash \"\$FAKE_NODE/restore.sh\" \"\$1\"" _ "${cmd#*anysda-restore.sh}" ;;
'''
REFUSALS = (
    ('no-archive', 3, 'архива нет', ('restore', 'latest'), 'ok'),
    ('passphrase', 4, 'дешифровка провалилась', ('restore', 'latest'), 'ok'),
    ('no-force', 6, 'Запусти повторно с --force', ('restore', 'latest', '--force'), 'no-force'),
    ('version', 9, 'обход: --allow-incompatible', ('restore', 'latest', '--allow-incompatible'), 'version'),
)


class RestoreTree(Tree):
    def __init__(self, mode, old_package=False):
        super().__init__()
        head, tail = FAKE_SSH.split('  *anysda-restore.sh*)\n')
        ssh = self.root / 'bin/ssh'
        ssh.write_text(head + RESTORE_CASE + tail.split('\n', 1)[1])
        (self.node / 'restore.sh').write_text(FAKE_RESTORE)
        if old_package:
            (self.node / 'old-package').touch()
        self.mode(mode)

    def mode(self, name):
        (self.node / 'restore.mode').write_text(name)

    def calls(self):
        log = self.node / 'restore.log'
        return log.read_text() if log.exists() else ''


class RestoreRefusalBeforeReleaseTests(unittest.TestCase):
    def assert_not_opened(self, tree, result):
        output = result.stdout + result.stderr
        self.assertFalse(tree.journal().exists(), output)
        self.assertEqual(tree.copies(), [], output)
        self.assertNotIn('operation journal retained', output)
        self.assertNotIn('started', tree.calls(), output)

    def test_each_refusal_leaves_journal_closed_and_hinted_command_works_at_once(self):
        for old_package in (False, True):
            for mode, code, text, hinted, mode_after in REFUSALS:
                with self.subTest(mode=mode, old_package=old_package), RestoreTree(mode, old_package) as tree:
                    refused = tree.deploy('5', 'restore', 'latest')
                    output = refused.stdout + refused.stderr
                    if old_package and mode != 'no-archive':
                        # Пакет узла старше дерева: отказ приходит уже от самого restore.
                        self.assertEqual(refused.returncode, code, output)
                        continue
                    self.assertEqual(refused.returncode, code, output)
                    self.assertEqual(refused.stderr.count(text), 1, output)
                    self.assertNotIn('неизвестный аргумент', output)
                    self.assert_not_opened(tree, refused)
                    tree.mode(mode_after)
                    again = tree.deploy('5', *hinted)
                    output = again.stdout + again.stderr
                    # Стадии entry после замены данных - не предмет теста: restore дошёл до работы.
                    self.assertNotIn('unfinished deploy', output)
                    self.assertIn('anysda-restore: SUCCESS', again.stdout, output)
                    self.assertIn('started', tree.calls())

    def test_refused_restore_does_not_block_next_stage(self):
        with RestoreTree('no-force') as tree:
            self.assertEqual(tree.deploy('5', 'restore', 'latest').returncode, 6)
            following = tree.deploy('5', NOOP_STAGE, 'ru')
            self.assertEqual(following.returncode, 0, following.stdout + following.stderr)

    def test_dry_run_stays_a_dry_run(self):
        with RestoreTree('no-force') as tree:
            done = tree.deploy('5', 'restore', 'latest', '--dry-run')
            self.assertEqual((done.returncode, done.stdout.count('anysda-restore: dry-run')), (0, 1),
                             done.stdout + done.stderr)
            self.assertNotIn('--preflight', tree.calls())
            self.assert_not_opened(tree, done)

    def test_restore_that_started_and_died_keeps_journal_and_recover_finishes(self):
        with RestoreTree('dies') as tree:
            died = tree.deploy('5', 'restore', 'latest')
            output = died.stdout + died.stderr
            self.assertEqual(died.returncode, 10, output)
            self.assertIn('started', tree.calls())
            self.assertIn('applying', tree.journal().read_text(), output)
            self.assertIn('operation journal retained', output)
            blocked = tree.deploy('5', NOOP_STAGE, 'ru')
            self.assertIn('unfinished deploy', blocked.stderr)
            recovered = tree.deploy('5', 'recover')
            self.assertNotIn('unfinished deploy', recovered.stdout + recovered.stderr)


if __name__ == '__main__':
    unittest.main()
