"""Why the panel is closed is written to the journal once per state change.

The installation closes the panel before the first certificate; the timer
(vpn2-tls.service, its stdout is the journal) and the installation itself must
leave "TLS: панель закрыта: <причина>" there, without repeating it every minute.
"""

import time
import unittest

from test_panel_https_only import DOMAIN, HOUR, ROOT, Entry

LINE = 'TLS: панель закрыта: '


class Journal:
    """Journal of an entry: vpn2-tls.service stdout plus `logger -t` records."""

    def __init__(self, entry):
        self.entry = entry
        self.log = entry.dir / 'logger.log'
        stub = entry.dir / 'bin' / 'logger'
        stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {self.log}\n')
        stub.chmod(0o755)
        self.service = []

    def install(self):
        """The --initial run of the installation: its stdout is the install output."""
        self.entry.tick(initial=True)

    def tick(self):
        self.service += self.entry.tick().splitlines()

    def closed_lines(self):
        logged = self.log.read_text().splitlines() if self.log.exists() else []
        return [line for line in logged + self.service if LINE in line]


class PanelClosedJournalTests(unittest.TestCase):
    def each_mode(self, check):
        for domain in ('', DOMAIN):
            with self.subTest(domain=domain or 'ip'):
                check(Entry(self, domain))

    def test_closed_by_installation_first_tick_logs_reason_once(self):
        def check(entry):
            journal = Journal(entry)
            journal.tick()
            self.assertEqual([line for line in journal.service if LINE in line],
                             [LINE + 'сертификат не выпущен; откроется сама после выпуска сертификата'],
                             journal.service)
            journal.tick()
            journal.tick()
            self.assertEqual(len(journal.closed_lines()), 1, journal.closed_lines())
        self.each_mode(check)

    def test_full_installation_leaves_one_journal_line_with_reason(self):
        def check(entry):
            journal = Journal(entry)
            journal.install()
            journal.tick()
            lines = journal.closed_lines()
            self.assertEqual(len(lines), 1, lines)
            # Written by the installation under the identifier of the timer unit.
            self.assertTrue(lines[0].startswith('-t vpn2-tls ' + LINE + 'сертификат не выпущен'), lines)
            journal.tick()
            self.assertEqual(journal.closed_lines(), lines)
        self.each_mode(check)

    def test_each_state_change_logs_again(self):
        entry = Entry(self, '')
        journal = Journal(entry)
        journal.tick()
        # A certificate that expires soon opens the panel. A loaded machine may
        # miss a timer run (openssl checks time out): the next minute retries.
        end = time.time() + 40
        entry.issue(time.time() - HOUR, end)
        gate = entry.dir / 'etc/caddy/vpn2-panel.conf'
        for _ in range(5):
            journal.tick()
            if gate.read_text() == 'import to_panel\n' or time.time() > end - 5:
                break
        self.assertEqual(gate.read_text(), 'import to_panel\n', 'certificate did not open the panel in time')
        self.assertEqual(len(journal.closed_lines()), 1, journal.closed_lines())
        time.sleep(max(0, end + 1 - time.time()))
        journal.tick()
        journal.tick()
        lines = journal.closed_lines()
        self.assertEqual(len(lines), 2, lines)
        self.assertIn(LINE + 'сертификат истёк', lines[-1])

    def test_timer_lines_share_the_logger_identifier(self):
        stage = (ROOT / 'infra/scripts/30-frontend.sh').read_text()
        unit = stage[stage.index('cat > /etc/systemd/system/vpn2-tls.service'):]
        unit = unit[:unit.index('\nEOF\n')]
        self.assertIn('\nSyslogIdentifier=vpn2-tls', unit)


if __name__ == '__main__':
    unittest.main()
