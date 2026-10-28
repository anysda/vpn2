"""The client guide promises only the import ways the bot's real bundle delivers."""

import html
import importlib
from pathlib import Path
import re
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'infra/emergency'))
tgqueue = importlib.import_module('vpn2_emergency.tgqueue')
GUIDE = ROOT / 'app/emergency/instructions.html'
STAMP = '2026-01-01T00:00:00+00:00'
QR = re.compile(r'\bQR\b', re.I)
FILE = re.compile(r'файл', re.I)
LONG = re.compile(r'длинн', re.I)
FILE_ACTION = re.compile(r'импорт|скопир|сохран|открой', re.I)


TOKEN = re.compile(r'<!--.*?-->|<![^>]*>|<(/?)([a-zA-Z][\w-]*)[^>]*>|([^<]+)', re.S)


def guide_blocks():
    """Visible text split into blocks (<p>, <li>), each tagged with its section."""
    blocks, section, text, summary = [], 'общий текст', None, None
    for match in TOKEN.finditer(GUIDE.read_text(encoding='utf-8')):
        closing, tag, data = match.group(1), (match.group(2) or '').lower(), match.group(3)
        if data is not None:
            for sink in (text, summary):
                if sink is not None:
                    sink.append(html.unescape(data))
        elif not closing and tag in ('p', 'li'):
            text = []
        elif not closing and tag == 'summary':
            summary = []
        elif closing and tag in ('p', 'li') and text is not None:
            blocks.append((section, ' '.join(''.join(text).split())))
            text = None
        elif closing and tag == 'summary':
            section, summary = ''.join(summary).strip(), None
        elif closing and tag == 'details':
            section = 'общий текст'
    return blocks


def bundle(uri):
    snapshot = {'bot_token': 'TOKEN', 'clients': [{
        'id': 'c1', 'allowed': True, 'telegram_chat_id': 42,
        'devices': [{'id': 'd1', 'name': 'телефон'}]}]}
    row = tgqueue._client_row(snapshot, 'c1', emergency_id='e1', generation=1,
                              bundle_version=1, exit_name='exit-a', issued_at=STAMP,
                              exit_url='https://exit-a.example', profiles={'d1': {'uri': uri}},
                              event='initial', now=0)
    parts, reason = row[7], row[8]
    assert not reason, reason
    return parts


def delivered(parts):
    """Import ways the client really receives, derived from the Bot API methods."""
    ways = set()
    for part in parts:
        if part['method'] == 'sendMessage' and 'hysteria2://' in part['text']:
            ways.add('link')
        elif part['method'] == 'sendDocument':
            ways.add('file')
        elif part['method'] == 'sendPhoto':
            ways.add('qr')
    return ways


SHORT = 'hysteria2://secret@exit-a.example:443/?sni=exit-a.example#d1'
LONG_URI = 'hysteria2://secret@exit-a.example:443/?obfs-password=' + 'a' * 5000


class InstructionsPromiseTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.blocks = guide_blocks()
        self.short = delivered(bundle(SHORT))
        self.long = delivered(bundle(LONG_URI))
        self.sections = {section for section, _ in self.blocks}
        # Positive control: the parser sees the app steps and the bundle the link.
        for app in ('Npv Tunnel', 'Egern', 'Hiddify'):
            self.assertTrue(any(app in section for section in self.sections), (app, self.sections))
        self.assertIn('link', self.short)

    def test_edge1_qr_promised_only_if_bot_sends_qr(self):
        if 'qr' in self.short | self.long:
            return
        promises = [f'{section}: {text}' for section, text in self.blocks if QR.search(text)]
        self.assertEqual(promises, [], 'бот не шлёт QR (нет sendPhoto), инструкция его обещает')

    def test_edge2_short_profile_no_unconditional_file(self):
        if 'file' in self.short:
            return
        promises = [f'{section}: {text}' for section, text in self.blocks
                    if FILE.search(text) and not LONG.search(text)]
        self.assertEqual(promises, [], 'короткий профиль приходит без файла, инструкция '
                                       'упоминает файл не как случай длинной ссылки')

    def test_edge3_long_profile_file_explained(self):
        self.assertIn('file', self.long)
        self.assertNotIn('link', self.long)
        told = [text for _, text in self.blocks if FILE.search(text) and FILE_ACTION.search(text)]
        self.assertTrue(told, 'длинная ссылка приходит файлом, инструкция не говорит, что с ним делать')


if __name__ == '__main__':
    unittest.main()
