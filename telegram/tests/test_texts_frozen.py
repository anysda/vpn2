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
from support import load_bot  # noqa: E402

BOT_DIR = HERE.parent
FIXTURE = HERE / 'fixtures' / 'texts.json'
NAMED_CONSTANTS = ('_ADMIN', '_LATER', 'ERROR_FALLBACK', 'NAV_DEVICES', 'NAV_ADD', 'REPLY_KB',
                   'UNEXPECTED_ERROR', 'ADMIN_START_TEXT')
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


def collect_literals():
    """Каждая строка с не-ASCII символом (кириллица, эмодзи, типографика) в telegram/*.py."""
    counter = collections.Counter()
    for path in sorted(BOT_DIR.glob('*.py')):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and not node.value.isascii()):
                counter[node.value] += 1
    return [[text, n] for text, n in sorted(counter.items())]


def plural_forms():
    """Тройки форм, которые код передаёт в plural_ru."""
    triples = set()
    for path in sorted(BOT_DIR.glob('*.py')):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if (isinstance(node, ast.Call) and getattr(node.func, 'id', None) == 'plural_ru'
                    and len(node.args) == 2 and isinstance(node.args[1], ast.Tuple)):
                triples.add(tuple(ast.literal_eval(node.args[1])))
    return sorted(triples)


def collect_rendered(bot):
    out = {name: getattr(bot, name) for name in NAMED_CONSTANTS}
    out['ERROR_TEXT'] = dict(bot.ERROR_TEXT)
    out['plural_ru'] = {' / '.join(forms): {str(n): bot.plural_ru(n, forms)
                                            for n in PLURAL_NUMBERS}
                        for forms in plural_forms()}
    out['_limit_label'] = {str(v): bot._limit_label(v) for v in (None, 0, 1, 25)}
    out['_expiry_label'] = {str(v): bot._expiry_label(v) for v in SAMPLE_EXPIRES}
    out['md'] = {str(v): bot.md(v) for v in SPECIAL}
    out['md_bold'] = {str(v): bot.md_bold(v) for v in SPECIAL}
    out['_menu_text'] = {k: bot._menu_text(v) for k, v in VIEWS.items()}
    out['_devices_text'] = {k: bot._devices_text(v) for k, v in VIEWS.items()}
    out['_device_card_text'] = {k: bot._device_card_text(v) for k, v in DEVICES.items()}
    saved = bot.ADMIN_USERNAME
    try:
        out['_no_access_text'] = {}
        for username in ('', 'admin_user'):
            bot.ADMIN_USERNAME = username
            out['_no_access_text'][username or 'no_admin'] = bot._no_access_text()
    finally:
        bot.ADMIN_USERNAME = saved
    out['_admin_list_text'] = {str(n): bot._admin_list_text([{}] * n) for n in (0, 1, 2, 5, 21)}
    return out


def build(bot):
    return {'literals': collect_literals(), 'rendered': collect_rendered(bot)}


def dump(data):
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + '\n'


class FrozenTextsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bot = load_bot()
        cls.data = build(cls.bot)

    def test_texts_match_fixture(self):
        self.assertTrue(FIXTURE.exists(), f'нет эталона {FIXTURE}; создать: --write')
        self.assertEqual(dump(self.data), FIXTURE.read_text(encoding='utf-8'))

    def test_rendered_is_complete(self):
        rendered = self.data['rendered']
        for key in self.bot.ERROR_TEXT:
            self.assertIn(key, rendered['ERROR_TEXT'])
        for name in NAMED_CONSTANTS + ('ERROR_TEXT',):
            self.assertIn(name, rendered)
        self.assertTrue(rendered['plural_ru'])
        self.assertTrue(self.data['literals'])


if __name__ == '__main__':
    if '--write' in sys.argv:
        FIXTURE.parent.mkdir(exist_ok=True)
        FIXTURE.write_text(dump(build(load_bot())), encoding='utf-8')
        print(f'записан {FIXTURE}')
    else:
        unittest.main()
