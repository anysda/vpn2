"""Every command, button, text, access rule and notification of the bot.

The fixture holds what the bot sends to Telegram and to the panel in each
scenario, byte for byte. After a deliberate change of a text, regenerate it:
    python telegram/tests/test_dialogue.py --write
"""
import asyncio
import json
from pathlib import Path
import sys
import unittest

import harness
import scenarios

FIXTURE = Path(__file__).with_name('fixtures') / 'dialogue.json'


def played(name):
    record = asyncio.run(harness.play(scenarios.SCENARIOS[name], scenarios.PANEL))
    return json.loads(json.dumps(record, ensure_ascii=False))


class DialogueTest(unittest.TestCase):
    maxDiff = None

    def test_bot_says_and_does_the_same(self):
        expected = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(sorted(expected), sorted(scenarios.SCENARIOS))
        for name in scenarios.SCENARIOS:
            with self.subTest(scenario=name):
                self.assertEqual(played(name), expected[name])


if __name__ == '__main__':
    if sys.argv[1:] == ['--write']:
        FIXTURE.write_text(json.dumps({name: played(name) for name in scenarios.SCENARIOS},
                                      ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    else:
        unittest.main()
