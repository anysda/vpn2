import unittest
from unittest.mock import patch

from zapret_support import stage, tasks


class QuicModeTests(unittest.TestCase):
    def test_allow_removes_previous_block_and_refresh_jobs(self):
        calls = []
        with patch.object(stage, '_quiet', side_effect=lambda *argv: calls.append(' '.join(argv)) or True), \
                patch.object(stage, '_remove', return_value=True) as remove, \
                patch.object(stage.subprocess, 'run') as run:
            self.assertTrue(stage.quic_off())
        # Таймер и уже запущенное им обновление гаснут раньше удаления таблицы: иначе оно её вернёт.
        self.assertEqual(calls, ['systemctl disable --now anysda-yt-quic-refresh.timer',
                                 'systemctl stop anysda-yt-quic-refresh.service',
                                 'systemctl disable --now anysda-yt-quic.service',
                                 'nft delete table inet anysda_ytquic'])
        self.assertIn('/etc/nftables-yt-quic.conf', remove.call_args.args[0])
        self.assertEqual(run.call_args.args[0], ['systemctl', 'daemon-reload'])

    def test_role_blocks_or_lifts_by_mode(self):
        found = {task['name']: task for task in tasks()}
        self.assertEqual(found['Блокировка QUIC к Google']['when'], "zapret_quic == 'block'")
        lifted = found['Блокировка QUIC снята']
        self.assertEqual(lifted['when'], "zapret_quic != 'block'")
        self.assertIn("'quic-off'", lifted['ansible.builtin.command']['argv'])
        warning = found['Предупреждение о снятой блокировке QUIC']
        self.assertEqual(warning['when'], "zapret_quic != 'block'")
        self.assertEqual(warning['ansible.builtin.debug']['msg'],
                         'Видео YouTube может стартовать с задержкой, если провайдер режет QUIC.')
        for name in ('Блокировка QUIC включена', 'Таблица QUIC перезалита'):
            self.assertEqual(found[name]['when'], "zapret_quic == 'block'")


if __name__ == '__main__':
    unittest.main()
