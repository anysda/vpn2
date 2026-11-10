"""Эталон текстов бота: любое изменение байта в показываемых строках краснит тест.

Перезапись эталона только явной командой:
    python3 telegram/tests/test_texts_frozen.py --write
"""
import ast
import collections
import json
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from support import import_bot  # noqa: E402

BOT_DIR = HERE.parent
FIXTURE = HERE / 'fixtures' / 'texts.json'
NAMED_CONSTANTS = ('ERROR_FALLBACK', 'NAV_DEVICES', 'NAV_ADD', 'REPLY_KB',
                   'UNEXPECTED_ERROR', 'ADMIN_START_TEXT')
# Обращения кода к общей таблице по постоянному ключу. Сам текст лежит в
# app/shared/error-text.json, в коде от него осталось только обращение: его
# пропажа - это пропажа показа текста, как раньше пропажа литерала.
TABLE_REFS = {"ERROR_TEXT['device_limit']": 1, "PLURAL['botDevice']": 1}
PLURAL_NUMBERS = (0, 1, 2, 5, 11, 21, 111)
SAMPLE_EXPIRES = ('2026-12-31T23:59:59Z', '2027-01-05T10:00:00+00:00', '', None, 'garbage')
SPECIAL = ('plain', 'a_b', '*x*', 'q[1]', 'back`tick', 'Имя_с*символом', 123)

VIEWS = {
    'empty': {'name': 'Аня', 'devices': []},
    'full': {
        'name': 'Иван_П', 'deviceLimit': 3, 'expiresAt': '2026-12-31T23:59:59Z',
        'accessStatus': 'Доступ включён',
        'devices': [{'id': 1, 'name': 'Телефон', 'hasWg': True},
                    {'id': 2, 'name': 'Ноут_2', 'hasOvpn': True}],
        'operation': {'status': 'done', 'protocols': {
            'wg': {'state': 'confirmed'}, 'openvpn': {'state': 'not_required'},
            'ikev2': {'state': 'failed', 'error': 'ошибка_x'}}},
    },
    'unlimited': {
        'name': 'Без лимита', 'deviceLimit': None, 'accessStatus': 'Состояние не подтверждено',
        'devices': [{'id': 3}],
        'operation': {'status': 'superseded', 'protocols': {
            'wg': {'state': 'confirmed'}, 'ikev2': {'state': 'confirmed'}}},
    },
    'bare': {},
}
DEVICES = {
    'none': {}, 'wg': {'name': 'A', 'hasWg': True}, 'ovpn': {'name': 'B_b', 'hasOvpn': True},
    'both': {'name': 'Оба', 'hasWg': True, 'hasOvpn': True},
}

CLIENTS = {
    'active': {'id': 'c1', 'name': 'Иван_П', 'status': 'active', 'deviceLimit': 3, 'deviceCount': 2,
               'expiresAt': '2026-12-31T23:59:59Z', 'tgLinked': True,
               'accessStatus': 'Доступ включён'},
    'frozen': {'id': 'c2', 'name': 'Анна', 'status': 'frozen', 'deviceLimit': None,
               'deviceCount': 5, 'expiresAt': 'garbage'},
    'pending': {'id': 'c3', 'name': 'Пётр', 'status': 'active', 'deviceLimit': 1, 'deviceCount': 1,
                'frozenManual': True, 'accessStatus': 'Состояние не подтверждено',
                'operation': {'status': 'pending', 'protocols': {
                    'wg': {'state': 'failed', 'error': 'ошибка_x'}}}},
    'superseded': {'id': 'c4', 'name': 'Заменён', 'status': 'active', 'deviceCount': 0,
                   'accessStatus': 'Состояние не подтверждено',
                   'operation': {'status': 'superseded', 'protocols': {
                       'wg': {'state': 'confirmed'}}}},
    'bare': {},
}


def source_files():
    """Все .py под telegram/, кроме tests, каталогов с точки и __pycache__."""
    found = []
    for path in BOT_DIR.rglob('*.py'):
        parts = path.relative_to(BOT_DIR).parts[:-1]
        if any(p == 'tests' or p == '__pycache__' or p.startswith('.') for p in parts):
            continue
        found.append(path)
    return sorted(found)


def collect_table_refs():
    """Обращения ERROR_TEXT['ключ'] и PLURAL['ключ'] в коде бота со счётчиком."""
    counter = collections.Counter()
    for path in source_files():
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if not (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, str)):
                continue
            table = getattr(node.value, 'id', None) or getattr(node.value, 'attr', None)
            if table in ('ERROR_TEXT', 'PLURAL'):
                counter[f'{table}[{node.slice.value!r}]'] += 1
    return dict(sorted(counter.items()))


def ref_texts(bot, ref):
    """Тексты таблицы за обращением: один у ERROR_TEXT, три формы у PLURAL."""
    table, _, key = ref.partition('[')
    value = getattr(bot.texts, table)[ast.literal_eval(key.removesuffix(']'))]
    return [value] if isinstance(value, str) else list(value)


def collect_literals(bot):
    """Каждая строка с не-ASCII символом (кириллица, эмодзи, типографика) в telegram/**/*.py
    и каждый текст общей таблицы, к которому код обращается по постоянному ключу."""
    counter = collections.Counter()
    for ref, n in collect_table_refs().items():
        for text in ref_texts(bot, ref):
            counter[text] += n
    for path in source_files():
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and not node.value.isascii()):
                counter[node.value] += 1
    return [[text, n] for text, n in sorted(counter.items())]


def plural_forms(bot):
    """Triples passed to plural_ru: tuple literals and PLURAL['name'] from the table."""
    triples = set()
    for path in source_files():
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if not (isinstance(node, ast.Call) and getattr(node.func, 'id', None) == 'plural_ru'
                    and len(node.args) == 2):
                continue
            arg = node.args[1]
            if isinstance(arg, ast.Tuple):
                triples.add(tuple(ast.literal_eval(arg)))
            elif (isinstance(arg, ast.Subscript) and getattr(arg.value, 'id', None) == 'PLURAL'
                    and isinstance(arg.slice, ast.Constant)):
                triples.add(tuple(bot.texts.PLURAL[arg.slice.value]))
    return sorted(triples)


def table_strings(bot):
    """Strings of the shared table the bot loaded: bot, botFallback, plural forms."""
    table = bot.texts._TABLE
    forms = [form for triple in table['plural'].values() for form in triple]
    return [*table['bot'].values(), table['botFallback'], *forms]


def rows(markup):
    """Клавиатура как список рядов, кнопка - пара [text, callback_data]."""
    return [[[b['text'], b['callback_data']] for b in row] for row in markup['inline_keyboard']]


def collect_rendered(bot):
    out = {name: getattr(bot.texts, name) for name in NAMED_CONSTANTS}
    out['ERROR_TEXT'] = dict(bot.texts.ERROR_TEXT)
    out['plural_ru'] = {' / '.join(forms): {str(n): bot.texts.plural_ru(n, forms)
                                            for n in PLURAL_NUMBERS}
                        for forms in plural_forms(bot)}
    out['_limit_label'] = {str(v): bot.texts._limit_label(v) for v in (None, 0, 1, 25)}
    out['_expiry_label'] = {str(v): bot.texts._expiry_label(v) for v in SAMPLE_EXPIRES}
    out['md'] = {str(v): bot.texts.md(v) for v in SPECIAL}
    out['md_bold'] = {str(v): bot.texts.md_bold(v) for v in SPECIAL}
    out['_menu_text'] = {k: bot.texts._menu_text(v) for k, v in VIEWS.items()}
    out['_devices_text'] = {k: bot.texts._devices_text(v) for k, v in VIEWS.items()}
    out['_device_card_text'] = {k: bot.texts._device_card_text(v) for k, v in DEVICES.items()}
    out['devices_markup'] = {k: rows(bot.texts._devices_markup(v)) for k, v in VIEWS.items()}
    out['device_card_markup'] = {i: rows(bot.texts._device_card_markup(i)) for i in ('7', 'a_b')}
    out['confirm_markup'] = rows(bot.texts._confirm_markup('yes:1', 'no:1'))
    out['admin_list_markup'] = {
        'none': rows(bot.admin._admin_list_markup([])),
        'all': rows(bot.admin._admin_list_markup(list(CLIENTS.values()))),
    }
    out['admin_card_markup'] = {k: rows(bot.admin._admin_card_markup(v)) for k, v in CLIENTS.items()}
    out['admin_card_text'] = {k: bot.admin._admin_card_text(v) for k, v in CLIENTS.items()}
    saved = bot.config.ADMIN_USERNAME
    try:
        out['_no_access_text'] = {}
        for username in ('', 'admin_user'):
            bot.config.ADMIN_USERNAME = username
            out['_no_access_text'][username or 'no_admin'] = bot.texts._no_access_text()
    finally:
        bot.config.ADMIN_USERNAME = saved
    out['_admin_list_text'] = {str(n): bot.admin._admin_list_text([{}] * n) for n in (0, 1, 2, 5, 21)}
    return out


def build(bot):
    return {'literals': collect_literals(bot), 'rendered': collect_rendered(bot)}


def dump(data):
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + '\n'


class FrozenTextsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bot = import_bot()
        cls.data = build(cls.bot)

    def test_texts_match_fixture(self):
        self.assertTrue(FIXTURE.exists(), f'нет эталона {FIXTURE}; создать: --write')
        frozen = json.loads(FIXTURE.read_text(encoding='utf-8'))
        table = table_strings(self.bot)

        def in_table(text):
            return isinstance(text, str) and any(text in s for s in table)

        referenced = {text for ref in TABLE_REFS for text in ref_texts(self.bot, ref)}
        code = {text: n for text, n in self.data['literals']}
        fixed = {text: n for text, n in frozen['literals']}
        for text, n in fixed.items():
            if text in code:
                self.assertEqual(code[text], n, text)
            else:
                self.assertNotIn(text, referenced, f'table text is no longer sent: {text!r}')
                self.assertTrue(in_table(text), f'literal gone and not in table: {text!r}')
        for text in code:
            self.assertTrue(text in fixed, f'new literal: {text!r}')
        rendered = self.data['rendered']
        for name, value in frozen['rendered'].items():
            if name in rendered:
                self.assertEqual(dump(rendered[name]), dump(value), name)
            else:
                self.assertTrue(in_table(value), f'constant gone and not in table: {name}')
        for name in rendered:
            self.assertTrue(name in frozen['rendered'], f'new rendered entry: {name}')

    def test_table_references_are_frozen(self):
        self.assertEqual(collect_table_refs(), TABLE_REFS)

    def test_rendered_is_complete(self):
        rendered = self.data['rendered']
        for key in self.bot.texts.ERROR_TEXT:
            self.assertIn(key, rendered['ERROR_TEXT'])
        for name in NAMED_CONSTANTS + ('ERROR_TEXT',):
            self.assertIn(name, rendered)
        for name in ('devices_markup', 'device_card_markup', 'confirm_markup', 'admin_list_markup',
                     'admin_card_markup', 'admin_card_text'):
            self.assertTrue(rendered[name], name)
        self.assertTrue(rendered['plural_ru'])
        self.assertTrue(self.data['literals'])


if __name__ == '__main__':
    if '--write' in sys.argv:
        FIXTURE.parent.mkdir(exist_ok=True)
        FIXTURE.write_text(dump(build(import_bot())), encoding='utf-8')
        print(f'записан {FIXTURE}')
    else:
        unittest.main()
