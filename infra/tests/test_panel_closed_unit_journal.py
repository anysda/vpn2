"""The reason the panel is closed is in `journalctl -u vpn2-tls`.

Only what vpn2-tls.service itself writes (its stdout) belongs to the unit:
a `logger -t vpn2-tls` call from the installation lands in the ssh session
scope and `-u vpn2-tls` does not show it. The installation output keeps the
line, the unit gets it from the service run once per state change.
"""

import re
import time
import unittest

from test_panel_https_only import DOMAIN, HOUR, ROOT, Entry

LINE = 'TLS: панель закрыта: '
OPEN = 'import to_panel\n'


class Unit:
    """vpn2-tls.service journal of an entry plus whatever `logger` was asked to write."""

    def __init__(self, entry):
        self.entry = entry
        self.logger = entry.dir / 'logger.log'
        stub = entry.dir / 'bin' / 'logger'
        stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {self.logger}\n')
        stub.chmod(0o755)
        self.journal = []

    def install(self):
        """The --initial run of ./deploy.sh: its stdout is the terminal, not the unit."""
        return self.entry.tick(initial=True)

    def run(self):
        """One run of vpn2-tls.service, started by the installation or by the timer."""
        self.journal += self.entry.tick().splitlines()

    def closed(self):
        return [line for line in self.journal if LINE in line]

    def logged_outside(self):
        return self.logger.read_text().splitlines() if self.logger.exists() else []


class PanelClosedUnitJournalTests(unittest.TestCase):
    def each_mode(self, check):
        for domain in ('', DOMAIN):
            with self.subTest(domain=domain or 'ip'):
                check(Entry(self, domain))

    def test_installation_with_closed_panel_leaves_one_line_in_the_unit(self):
        def check(entry):
            unit = Unit(entry)
            output = unit.install()
            self.assertIn(LINE + 'сертификат не выпущен', output)
            unit.run()
            self.assertEqual(len(unit.closed()), 1, unit.journal)
            self.assertIn(LINE + 'сертификат не выпущен', unit.closed()[0])
            # Restarts and timer runs without a state change stay silent.
            unit.run()
            unit.run()
            self.assertEqual(len(unit.closed()), 1, unit.journal)
            self.assertEqual(unit.logged_outside(), [])
        self.each_mode(check)

    def test_installation_starts_the_service_after_the_initial_run(self):
        stage = (ROOT / 'infra/scripts/30-frontend.sh').read_text()
        tail = stage[stage.index('vpn2_tls.py --initial'):]
        tail = tail[:tail.index('Панель:')]
        start = re.search(r'^systemctl start vpn2-tls\.service\b(?!.*--no-block)', tail, re.M)
        self.assertIsNotNone(start, tail)

    def test_state_changed_between_installation_and_service(self):
        entry = Entry(self, '')
        unit = Unit(entry)
        self.assertIn(LINE + 'сертификат не выпущен', unit.install())
        # Issued before the service run: the panel opens, the stale reason is not written.
        end = time.time() + 40
        entry.issue(time.time() - HOUR, end)
        gate = entry.dir / 'etc/caddy/vpn2-panel.conf'
        for _ in range(5):
            unit.run()
            if gate.read_text() == OPEN or time.time() > end - 5:
                break
        self.assertEqual(gate.read_text(), OPEN, 'certificate did not open the panel in time')
        self.assertEqual(unit.closed(), [], unit.journal)
        # Closed again by expiry: one new line with the new reason.
        time.sleep(max(0, end + 1 - time.time()))
        unit.run()
        unit.run()
        self.assertEqual(len(unit.closed()), 1, unit.journal)
        self.assertIn(LINE + 'сертификат истёк', unit.closed()[0])
        self.assertEqual(unit.logged_outside(), [])


if __name__ == '__main__':
    unittest.main()
