"""A signal held by apply keeps its code when a second comes as the hold ends and the first is taken up."""
from itertools import permutations
import os
import signal
import unittest
from unittest import mock

from test_apply import apply


def poke():
    """A Python call: the interpreter runs the handlers of the signals waiting so far."""


class HeldSignalWinsTests(unittest.TestCase):
    def setUp(self):
        before = {number: signal.getsignal(number) for number in apply.SIGNALS}
        self.addCleanup(lambda: [signal.signal(number, handler) for number, handler in before.items()])

    def stop(self, first, second, end):
        """FIRST comes while apply holds; SECOND as the hold ENDs and apply reads the held FIRST."""
        sent = []

        class Held(dict):
            def __getitem__(self, key):
                value = super().__getitem__(key)
                # The hold is over and the held signal is about to stop apply: the second lands here.
                if key == 'first' and value and not super().__getitem__('on') and not sent:
                    sent.append(second)
                    os.kill(os.getpid(), second)
                    poke()
                return value

        state = Held(on=False, first=None)
        with self.assertRaises(apply.Stopped) as caught, mock.patch.object(apply, 'HOLD', state), apply.stopping():
            with apply.held():
                os.kill(os.getpid(), first)
                poke()
                if end == 'released':
                    with apply.released():
                        self.fail('apply went on inside the release')
            self.fail('apply went on after the hold')
        self.assertEqual(sent, [second], 'the second signal was not sent as the hold ended')
        return caught.exception.code

    def test_code_is_the_held_signal_s(self):
        for end in ('hold ends', 'released'):
            for first, second in permutations(apply.SIGNALS, 2):
                with self.subTest(first=first.name, second=second.name, end=end):
                    self.assertEqual(self.stop(first, second, end), 128 + first)


if __name__ == '__main__':
    unittest.main()
