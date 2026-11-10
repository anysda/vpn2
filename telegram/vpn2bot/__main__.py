import asyncio
import os
import time

from aiohttp import web
from telegram.error import InvalidToken, NetworkError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from . import client, config, events, monitor, transport
from .config import log


async def _fetch_bot_username() -> None:
    """getMe → BOT_USERNAME, для построения диплинков-приглашений."""
    try:
        r = await transport._tg.get('/getMe', timeout=20)
        body = r.json()
        if body.get('ok'):
            config.BOT_USERNAME = body.get('result', {}).get('username')
            log.info('BOT_USERNAME=%s', config.BOT_USERNAME)
        else:
            log.warning('getMe не ok: %s', body)
    except Exception as e:
        log.warning('getMe failed: %s — диплинки будут с фолбэк-инструкцией', e)


# Если UI редактирует /etc/anysda/telegram-runtime.json — exit, docker
# рестартит контейнер с новыми token/chat_id.
async def _config_watcher():
    initial_mtime = config.RUNTIME_PATH.stat().st_mtime if config.RUNTIME_PATH.exists() else None
    while True:
        await asyncio.sleep(5)
        mtime = config.RUNTIME_PATH.stat().st_mtime if config.RUNTIME_PATH.exists() else None
        if mtime != initial_mtime:
            log.info('telegram-runtime.json изменён — выхожу для рестарта')
            os._exit(0)


async def main() -> None:
    if not config.TOKEN or config.CHAT_ID == 0:
        # Бот ставится и без токена: ждём «Сохранить» в панели, не выходя, иначе
        # docker --restart always крутит его по кругу.
        log.warning('Токен бота или Chat ID не заданы: жду их в панели, раздел «Боты»')
        # Только /health: без него контейнер unhealthy. Токен приходит выходом
        # процесса из _config_watcher, порт освобождается вместе с ним.
        idle = web.Application()
        idle.router.add_get('/health', events.handle_health_waiting)
        idle_runner = web.AppRunner(idle)
        await idle_runner.setup()
        await web.TCPSite(idle_runner, '127.0.0.1', config.EVENT_PORT).start()
        await _config_watcher()
    # Telegram egress — через TG_PROXY (HTTP-CONNECT прокси sing-box →
    # foreign-best → экзит): RU-нода в Москве api.telegram.org напрямую не
    # достаёт. Используем HTTP-CONNECT, а не SOCKS5: httpx-socks давал
    # ложные PoolTimeout, тогда как CONNECT-туннели httpx пулит надёжно.
    # connection_pool_size + pool_timeout default'ы в HTTPXRequest очень
    # маленькие (1 и 1.0с) — при любой задержке handler-команд получаем
    # PoolTimeout. Поднимаем с запасом, чтобы команды + monitor_loop +
    # event_server жили вместе.
    poll_req = transport._WatchedRequest(connection_pool_size=4, read_timeout=40, proxy=config.TG_PROXY)
    send_req = HTTPXRequest(
        connection_pool_size=50,
        pool_timeout=20.0,
        connect_timeout=10.0,
        read_timeout=20.0,
        proxy=config.TG_PROXY,
    )
    tgapp = (
        Application.builder()
        .token(config.TOKEN)
        .request(send_req)
        .get_updates_request(poll_req)
        .build()
    )
    tgapp.add_handler(CommandHandler('start',   client.cmd_start))
    tgapp.add_handler(CommandHandler('help',    client.cmd_start))
    tgapp.add_handler(CommandHandler('status',  monitor.cmd_status))
    tgapp.add_handler(CommandHandler('nodes',   monitor.cmd_nodes))
    tgapp.add_handler(CommandHandler('clients', monitor.cmd_clients))
    # Клиентское инлайн-меню + ввод названия устройства.
    tgapp.add_handler(CallbackQueryHandler(client.on_callback))
    tgapp.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, client.on_text))

    # aiohttp event server for Nuxt callbacks
    webapp = web.Application()
    webapp.cleanup_ctx.append(events.event_outbox)
    webapp['tgapp'] = tgapp
    webapp.router.add_post('/event', events.handle_event)
    webapp.router.add_get('/health', events.handle_health)
    runner = web.AppRunner(webapp)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', config.EVENT_PORT).start()
    log.info('Event server on 127.0.0.1:%d', config.EVENT_PORT)

    async def _supervised_monitor():
        try:
            await monitor.monitor_loop()
        except Exception:
            log.exception('monitor_loop crashed')

    tasks = [asyncio.create_task(_config_watcher()), asyncio.create_task(transport.poll_watchdog())]
    try:
        # Accept durable events even when Telegram is unavailable at startup.
        while True:
            try:
                await tgapp.initialize()
                break
            except NetworkError:
                log.warning('Telegram initialization unavailable; retry in 5s')
                await asyncio.sleep(5)
        await _fetch_bot_username()
        async with tgapp:
            await tgapp.start()
            tasks.append(asyncio.create_task(_supervised_monitor()))
            await tgapp.updater.start_polling(drop_pending_updates=True)
            log.info('Bot started, polling Telegram via configured TG_PROXY')
            try:
                await asyncio.Event().wait()  # run forever
            finally:
                await tgapp.updater.stop()
                await tgapp.stop()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runner.cleanup()


def _wait_token_fix(limit_sec: int) -> None:
    """Пауза перед перезапуском с отвергнутым токеном.

    Без неё docker --restart поднимает бот через секунды, и тот снова бьётся
    в Telegram с тем же токеном. Новый токен из панели приходит правкой
    telegram-runtime.json: тогда выходим сразу, не досиживая паузу.
    """
    def mtime():
        return config.RUNTIME_PATH.stat().st_mtime if config.RUNTIME_PATH.exists() else None

    initial = mtime()
    deadline = time.monotonic() + limit_sec
    while time.monotonic() < deadline:
        time.sleep(5)
        if mtime() != initial:
            return


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except InvalidToken:
        log.error(
            'Telegram отверг токен бота (InvalidToken): проверьте токен в панели, '
            'раздел «Боты». Перезапуск через %d с или сразу после сохранения токена',
            config.INVALID_TOKEN_PAUSE_SEC,
        )
        _wait_token_fix(config.INVALID_TOKEN_PAUSE_SEC)
        raise SystemExit(1) from None
    except Exception:
        # Через логгер, а не голой трассировкой в stderr: так токен вырезается.
        log.exception('бот остановлен ошибкой')
        raise SystemExit(1) from None
