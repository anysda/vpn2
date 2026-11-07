"""Two signals at once stop apply with the code of the first, whichever comes inside its handler."""
from itertools import permutations
import os
import signal
import time
import unittest
from unittest import mock

from test_apply import apply

ROUNDS = 200


def poke():
    """A Python call: the interpreter runs the handlers of the signals waiting so far."""


class FirstSignalWinsTests(unittest.TestCase):
    def setUp(self):
        before = {number: signal.getsignal(number) for number in apply.SIGNALS}
        self.addCleanup(lambda: [signal.signal(number, handler) for number, handler in before.items()])

    def stop(self, first, second, hold):
        """apply stopped by FIRST; SECOND comes while the handler of FIRST still runs."""
        real, sent = signal.signal, []

        def chase():
            if not sent:
                sent.append(second)
                os.kill(os.getpid(), second)
                poke()

        def swapping(number, handler):
            # The handler of the first signal swaps the handlers: the second lands there.
            chase()
            return real(number, handler)

        class Held(dict):
            def __getitem__(self, key):
                # The handler of the first signal, holding: the second lands as it reads.
                if key == 'first':
                    chase()
                return super().__getitem__(key)

        state = Held(on=False, first=None)
        with self.assertRaises(apply.Stopped) as caught, mock.patch.object(apply, 'HOLD', state), apply.stopping():
            if hold:
                with apply.held():
                    os.kill(os.getpid(), first)
                    poke()
                    self.assertEqual(sent, [second], 'the second signal was not sent inside the handler')
            else:
                with mock.patch.object(apply.signal, 'signal', swapping):
                    os.kill(os.getpid(), first)
                    time.sleep(5)
            self.fail('apply went on after the signals')
        self.assertEqual(sent, [second], 'the second signal was not sent inside the handler')
        return caught.exception.code

    def test_code_is_the_first_signal_s(self):
        for hold in (False, True):
            for first, second in permutations(apply.SIGNALS, 2):
                with self.subTest(first=first.name, second=second.name, hold=hold):
                    codes = {self.stop(first, second, hold) for _ in range(ROUNDS)}
                    self.assertEqual(codes, {128 + first})


if __name__ == '__main__':
    unittest.main()
