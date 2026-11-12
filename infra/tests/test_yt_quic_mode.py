import unittest

import yaml

from zapret_support import ROLE, tasks


class QuicModeTests(unittest.TestCase):
    def test_allow_removes_previous_block_and_refresh_jobs(self):
        off = yaml.safe_load((ROLE / 'defaults/main.yml').read_text())['zapret_quic_off']
        # Таймер и уже запущенное им обновление гаснут раньше удаления таблицы: иначе оно её вернёт.
        self.assertEqual(off['units'], ['anysda-yt-quic-refresh.timer', 'anysda-yt-quic-refresh.service',
                                        'anysda-yt-quic.service'])
        self.assertEqual(off['tables'], ['anysda_ytquic'])
        self.assertIn('/etc/nftables-yt-quic.conf', off['files'])

    def test_role_blocks_or_lifts_by_mode(self):
        found = {task['name']: task for task in tasks()}
        self.assertEqual(found['Блокировка QUIC к Google']['when'], "zapret_quic == 'block'")
        lifted = found['Блокировка QUIC снята']
        self.assertEqual((lifted['when'], lifted['vars']), ("zapret_quic != 'block'", {'zapret_off': '{{ zapret_quic_off }}'}))
        warning = found['Предупреждение о снятой блокировке QUIC']
        self.assertEqual(warning['when'], "zapret_quic != 'block'")
        self.assertEqual(warning['ansible.builtin.debug']['msg'],
                         'Видео YouTube может стартовать с задержкой, если провайдер режет QUIC.')
        self.assertEqual(found['Блокировка QUIC включена и перезалита']['when'], "zapret_quic == 'block'")


if __name__ == '__main__':
    unittest.main()
