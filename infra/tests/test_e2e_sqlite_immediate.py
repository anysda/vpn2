"""Транзакции e2e-скриптов в базу панели открываются BEGIN IMMEDIATE.

Отложенная транзакция сначала читает, потом пишет. Если таймер bot-outbox
панели успел записать между ними, переход к записи получает SQLITE_BUSY
сразу, без ожидания busy_timeout.
"""

from pathlib import Path
import re
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
E2E = ROOT / 'app' / 'e2e'
SCRIPTS = ('.ts', '.mjs', '.cjs', '.js', '.sh')

QUOTES = '\'"`'
# BEGIN как отдельная инструкция SQL: в начале строки SQL, после кавычки
# или ';', и сразу за ним конец инструкции. Так не ловятся тело триггера
# (BEGIN SELECT ... END) и заголовки PEM (-----BEGIN CERTIFICATE-----).
BEGIN = re.compile(
    r'(?:^|(?<=[;\'"`]))[ \t]*BEGIN'
    r'(?:[ \t]+(DEFERRED|IMMEDIATE|EXCLUSIVE))?'
    r'(?:[ \t]+TRANSACTION)?[ \t]*(?=;|[\'"`]|$)',
    re.IGNORECASE | re.MULTILINE,
)
READ_ONLY = re.compile(r'\s*SELECT\b[^;]*;?\s*', re.IGNORECASE)
WRITE = re.compile(r'\b(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b', re.IGNORECASE)


def read_only_tail(text, end):
    """После 'BEGIN;' в той же строке SQL только SELECT: до записи не дойдёт."""
    if text[end:end + 1] != ';':
        return False
    rest = text[end + 1:]
    close = min((i for i in (rest.find(q) for q in QUOTES) if i >= 0), default=-1)
    if close < 0:
        return False
    tail = rest[:close]
    return bool(READ_ONLY.fullmatch(tail)) and not WRITE.search(tail)


def bare_begins(text):
    """Номера строк, где транзакция открыта без IMMEDIATE или EXCLUSIVE."""
    lines = []
    for match in BEGIN.finditer(text):
        mode = (match.group(1) or '').upper()
        if mode in ('IMMEDIATE', 'EXCLUSIVE') or read_only_tail(text, match.end()):
            continue
        lines.append(text.count('\n', 0, match.start()) + 1)
    return lines


def scan(root):
    found = []
    for path in sorted(root.rglob('*')):
        if path.suffix in SCRIPTS and path.is_file():
            rel = path.relative_to(root).as_posix()
            found += [f'{rel}:{line}' for line in bare_begins(path.read_text(encoding='utf-8'))]
    return found


class E2eSqliteImmediateTests(unittest.TestCase):
    def test_e2e_transactions_begin_immediate(self):
        self.assertEqual(scan(E2E), [], 'голый BEGIN в e2e: нужен BEGIN IMMEDIATE')

    def test_detects_every_bare_form(self):
        cases = {
            "db.exec('BEGIN')": [1],
            'db.exec("BEGIN")': [1],
            "db.exec('BEGIN; UPDATE t SET a = 1')": [1],
            'db.exec(`BEGIN DEFERRED TRANSACTION`)': [1],
            "sqlite3 db 'BEGIN; DELETE FROM t; COMMIT;'": [1],
            'sqlite3 db <<SQL\nBEGIN;\nDELETE FROM t;\nCOMMIT;\nSQL': [2],
            "x()\n  db.exec('begin transaction')": [2],
        }
        for text, lines in cases.items():
            with self.subTest(text=text):
                self.assertEqual(bare_begins(text), lines)

    def test_immediate_exclusive_trigger_and_pem_are_not_flagged(self):
        for text in (
            "db.exec('BEGIN IMMEDIATE')",
            "db.exec('PRAGMA busy_timeout=30000; BEGIN IMMEDIATE')",
            'db.exec("BEGIN EXCLUSIVE TRANSACTION")',
            "db.run(\"CREATE TRIGGER t BEFORE INSERT ON a BEGIN SELECT RAISE(ABORT, 'x'); END\")",
            "expect(text).toMatch(/^-----BEGIN [A-Z ]+-----/)",
            "expect(key).toContain('BEGIN OpenVPN Static key V1')",
            "db.exec('BEGIN; SELECT * FROM probe')",
        ):
            with self.subTest(text=text):
                self.assertEqual(bare_begins(text), [])

    def test_trigger_body_in_repo_is_not_flagged(self):
        found = scan(E2E)
        for name in ('access-operations.spec.ts', 'clients.spec.ts', 'traffic.spec.ts'):
            with self.subTest(name=name):
                self.assertIn('CREATE TRIGGER', (E2E / name).read_text(encoding='utf-8'))
                self.assertFalse([f for f in found if f.startswith(name + ':')])

    def test_immediate_on_copy_removes_file_from_list(self):
        bare = "db.exec('BEGIN')\n"
        for suffix in SCRIPTS:
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / 'support').mkdir()
                sample = root / 'support' / f'sample{suffix}'
                sample.write_text(bare, encoding='utf-8')
                self.assertEqual(scan(root), [f'support/sample{suffix}:1'])
                sample.write_text(bare.replace('BEGIN', 'BEGIN IMMEDIATE'), encoding='utf-8')
                self.assertEqual(scan(root), [])

if __name__ == '__main__':
    unittest.main()
