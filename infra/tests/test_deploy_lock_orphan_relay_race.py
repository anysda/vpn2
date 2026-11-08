"""Шаг теста осиротевшей сессии кончается, даже если клиент ssh опоздал.

На загруженном раннере процессы подставного ssh стартуют в любом порядке.
Занятый замок flock отвергает сразу, и команда на entry может кончиться
раньше, чем пересылка stdout клиента откроет fifo вывода сессии. Настоящий
ssh так не теряет конец команды: шаг обязан получить отказ «deploy already
running», а не ждать писателя fifo вечно.
"""
import fcntl
import subprocess
import unittest

from test_deploy_lock_orphan_session import NEXT, OrphanEntry
from test_deploy_lock_ssh_failure import verdict

# Предел шага: без нагрузки отказ по занятому замку приходит за ~1 с.
STEP = 8
# Каждый cat опаздывает на старт, как под полной загрузкой CPU.
LATE_CAT = '#!/bin/bash\n/usr/bin/sleep 1\nexec /usr/bin/cat "$@"\n'


class OrphanRelayRaceTests(unittest.TestCase):
    def test_busy_lock_is_reported_when_client_relay_starts_late(self):
        with OrphanEntry() as entry:
            shim = entry.root / 'late'
            shim.mkdir()
            (shim / 'cat').write_text(LATE_CAT)
            (shim / 'cat').chmod(0o755)
            # Замок держит другой deploy.
            with open(entry.node / 'lock' / 'deploy.lock', 'w') as busy:
                fcntl.flock(busy, fcntl.LOCK_EX)
                entry.env['PATH'] = f'{shim}:{entry.env["PATH"]}'
                try:
                    result = entry.run(NEXT, timeout=STEP)
                except subprocess.TimeoutExpired:
                    self.fail(f'шаг при занятом замке не кончился за {STEP} с: '
                              'команда на entry кончилась раньше, чем клиент ssh '
                              'открыл её вывод')
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('deploy-locked', result.stdout)
            self.assertIn('deploy already running', verdict(result.stderr), result.stderr)


if __name__ == '__main__':
    unittest.main()
