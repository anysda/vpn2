"""Число устройств в кнопке админского списка согласовано с существительным."""
import unittest

from support import load_bot


class PluralTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bot = load_bot()

    def test_device_counts(self):
        cases = (
            (0, 'девайсов'), (1, 'девайс'), (2, 'девайса'), (3, 'девайса'),
            (4, 'девайса'), (5, 'девайсов'), (11, 'девайсов'),
            (12, 'девайсов'), (14, 'девайсов'), (20, 'девайсов'),
            (21, 'девайс'), (22, 'девайса'), (24, 'девайса'),
            (25, 'девайсов'), (101, 'девайс'), (111, 'девайсов'),
            (112, 'девайсов'), (114, 'девайсов'),
        )
        for count, noun in cases:
            for limit in (None, 25):
                with self.subTest(count=count, limit=limit):
                    client = {'id': 1, 'name': 'Тест', 'status': 'active',
                              'deviceCount': count, 'deviceLimit': limit}
                    text = self.bot._admin_list_markup([client])['inline_keyboard'][0][0]['text']
                    print(text)
                    label = '∞' if limit is None else str(limit)
                    self.assertEqual(text, f'✅ Тест — {count} {noun} · лимит: {label}')


if __name__ == '__main__':
    unittest.main()
