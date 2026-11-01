"""Ключи failover.* и перезапуск sing-box сторожем описаны в README и docs/failover.md."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / 'config.example.yaml'
DOC = ROOT / 'docs' / 'failover.md'
README = ROOT / 'README.md'

RESTART = re.compile(r'перезапус|перезагру|рестарт|restart', re.IGNORECASE)
SING_BOX = re.compile(r'sing-box', re.IGNORECASE)
DISABLE = re.compile(r'выключ|отключ|disabl', re.IGNORECASE)


def failover_keys():
    """Ключ -> умолчание из закомментированного блока failover: примера."""
    keys = {}
    inside = False
    for line in CONFIG.read_text(encoding='utf-8').splitlines():
        if re.fullmatch(r'#\s?failover:\s*(#.*)?', line.strip()):
            inside = True
            continue
        if not inside:
            continue
        m = re.match(r'#\s{2,}([a-z_][a-z0-9_]*):\s*([^#\s]+)', line)
        if not m:
            break
        keys[m.group(1)] = m.group(2)
    return keys


def paragraphs(text):
    return [p for p in re.split(r'\n\s*\n', text) if p.strip()]


def readme_section(title):
    lines = README.read_text(encoding='utf-8').splitlines()
    for i, line in enumerate(lines):
        m = re.match(r'(#+)\s+(.*?)\s*$', line)
        if m and m.group(2) == title:
            level = len(m.group(1))
            body = []
            for nxt in lines[i + 1:]:
                h = re.match(r'(#+)\s', nxt)
                if h and len(h.group(1)) <= level:
                    break
                body.append(nxt)
            return '\n'.join(body)
    return None


class FailoverDocsTests(unittest.TestCase):
    def setUp(self):
        self.keys = failover_keys()
        self.assertTrue(self.keys, 'в config.example.yaml не найден блок failover: с ключами')
        self.doc = DOC.read_text(encoding='utf-8')

    def test_every_config_key_is_in_doc(self):
        missing = [k for k in self.keys
                   if not re.search(rf'\b{re.escape(k)}\b', self.doc)]
        self.assertEqual(missing, [], f'{DOC.relative_to(ROOT)} не описывает ключи failover.*')

    def test_doc_gives_defaults_and_zero_disables(self):
        for key, default in self.keys.items():
            with self.subTest(key=key):
                near = [p for p in paragraphs(self.doc)
                        if re.search(rf'\b{re.escape(key)}\b', p)
                        and re.search(rf'(?<![\d.]){re.escape(default)}(?![\d.])', p)]
                self.assertTrue(near, f'нет умолчания {default} рядом с {key}')
        zero = [p for p in paragraphs(self.doc)
                if any(re.search(rf'\b{re.escape(k)}\b', p) for k in self.keys)
                and re.search(r'(?<![\d.])0(?![\d.])', p) and DISABLE.search(p)]
        self.assertTrue(zero, 'не сказано, что 0 выключает перезапуск')

    def test_readme_section_links_doc_and_mentions_restart(self):
        section = readme_section('Сторож экзитов')
        self.assertIsNotNone(section, 'в README нет раздела «Сторож экзитов»')
        self.assertIn('docs/failover.md', section)
        restart = [p for p in paragraphs(section)
                   if SING_BOX.search(p) and RESTART.search(p)]
        self.assertTrue(restart, 'раздел «Сторож экзитов» молчит о перезапуске sing-box')


if __name__ == '__main__':
    unittest.main()
