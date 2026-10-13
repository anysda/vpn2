"""Публичные списки выходов не выдают служебные каналы за упавшие экзиты."""
import unittest
from unittest.mock import AsyncMock, patch

from support import load_bot, update


SNAPSHOT = {'outbounds': [
    {'name': 'hy2-se-direct', 'rttMs': 42},
    {'name': 'hy2-se-warp', 'rttMs': None},
    {'name': 'hy2-us-2-direct', 'rttMs': 85},
    *[{'name': name, 'rttMs': None} for name in (
        'hy2-se-mgmt', 'hy2-fr-mgmt', 'hy2-de-mgmt', 'hy2-se-dns',
        'direct-ru', 'foreign-best', 'lane-01', 'pin-hy2-se-direct',
        'youtube-ru', 'youtube-auto', 'block', 'dns-out', 'hy2--direct',
    )],
]}
EXITS = '🟢 `SE`: 42 ms\n🔴 `SE WARP`: нет связи\n🟢 `US-2`: 85 ms'


class NodesTests(unittest.IsolatedAsyncioTestCase):
    async def test_handler_filters_service_outbounds_but_keeps_failed_exits(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        with patch.object(bot, 'api_status', AsyncMock(return_value=SNAPSHOT)), \
                patch.object(bot, 'tg_reply', AsyncMock()) as reply:
            await bot.cmd_nodes(update(chat_id=100, user_id=100), None)
        text = reply.await_args.args[1]
        print('/nodes:\n' + text)
        self.assertEqual(text, '*Exit-ноды*\n\n' + EXITS)

    async def test_status_and_deploy_summary_use_the_same_exit_list(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        with patch.object(bot, 'api_status', AsyncMock(return_value=SNAPSHOT)):
            text = await bot.build_status_text()
        self.assertEqual(text, '*Состояние системы*\n\n\n' + EXITS)

    async def test_only_service_outbounds_means_no_nodes(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        with patch.object(bot, 'api_status', AsyncMock(return_value={'outbounds': SNAPSHOT['outbounds'][3:]})), \
                patch.object(bot, 'tg_reply', AsyncMock()) as reply:
            await bot.cmd_nodes(update(chat_id=100, user_id=100), None)
        self.assertEqual(reply.await_args.args[1], 'Нет нод')


if __name__ == '__main__':
    unittest.main()
