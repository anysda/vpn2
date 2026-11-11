import asyncio
import os
import time

from aiohttp import web
from telegram.error import InvalidToken, NetworkError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest

from . import api, config, events, handlers, monitor
from .config import log


async def serve(routes: list) -> web.AppRunner:
    app = web.Application()
    if any(route.path == '/event' for route in routes):
        app.cleanup_ctx.append(events.event_outbox)
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', config.EVENT_PORT).start()
    return runner


async def runtime_changed(limit_sec: float = float('inf')) -> bool:
    """Дождаться правки telegram-runtime.json: панель сохранила новые токен или
    Chat ID. False — за limit_sec секунд файл не менялся."""
    def mtime():
        return config.RUNTIME_PATH.stat().st_mtime if config.RUNTIME_PATH.exists() else None

    initial, deadline = mtime(), time.monotonic() + limit_sec
    while time.monotonic() < deadline:
        await asyncio.sleep(5)
        if mtime() != initial:
            return True
    return False


async def restart_on_new_runtime() -> None:
    await runtime_changed()
    log.info('telegram-runtime.json изменён — выхожу для рестарта')
    os._exit(0)


async def main() -> None:
    if not config.TOKEN or config.CHAT_ID == 0:
        # Бот ставится и без токена: ждём «Сохранить» в панели, не выходя, иначе
        # docker --restart крутит его по кругу. /health нужен, чтобы контейнер
        # не стал unhealthy; порт освобождается вместе с процессом.
        log.warning('Токен бота или Chat ID не заданы: жду их в панели, раздел «Боты»')
        await serve([web.get('/health', events.health())])
        await restart_on_new_runtime()
    # Пул и таймауты выше умолчаний HTTPXRequest (1 соединение, 1 с), иначе
    # PoolTimeout при любой задержке прокси.
    tgapp = (Application.builder().token(config.TOKEN)
             .request(HTTPXRequest(connection_pool_size=50, pool_timeout=20.0, connect_timeout=10.0,
                                   read_timeout=20.0, proxy=config.TG_PROXY))
             .get_updates_request(api.WatchedRequest(connection_pool_size=4, read_timeout=40,
                                                     proxy=config.TG_PROXY))
             .build())
    tgapp.add_handler(CommandHandler(handlers.COMMANDS, handlers.dispatch))
    tgapp.add_handler(CallbackQueryHandler(handlers.dispatch))
    tgapp.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.dispatch))

    runner = await serve([web.post('/event', events.handle_event),
                          web.get('/health', events.handle_health)])
    log.info('Event server on 127.0.0.1:%d', config.EVENT_PORT)
    tasks = [asyncio.create_task(restart_on_new_runtime()), asyncio.create_task(api.poll_watchdog())]
    try:
        # События панели принимаем в очередь, даже пока Telegram недоступен.
        while True:
            try:
                await tgapp.initialize()
                break
            except NetworkError:
                log.warning('Telegram initialization unavailable; retry in 5s')
                await asyncio.sleep(5)
        # Имя бота нужно для ссылок-приглашений и «Настроек» панели.
        config.BOT_USERNAME = tgapp.bot.username
        async with tgapp:
            await tgapp.start()
            tasks.append(asyncio.create_task(monitor.monitor_loop()))
            await tgapp.updater.start_polling(drop_pending_updates=True)
            log.info('Bot started as @%s, polling Telegram via TG_PROXY', config.BOT_USERNAME)
            try:
                await asyncio.Event().wait()
            finally:
                await tgapp.updater.stop()
                await tgapp.stop()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runner.cleanup()


async def invalid_token_pause() -> None:
    """Пауза перед перезапуском с отвергнутым токеном: без неё docker поднимает
    бот через секунды, и тот снова бьётся в Telegram с тем же токеном. Новый
    токен из панели обрывает паузу сразу. /health живёт всю паузу."""
    runner = await serve([web.get('/health', events.health(invalid_token=True))])
    try:
        # Строка пишется после старта сервера: по ней узнают, что пауза началась.
        log.error('Telegram отверг токен бота (InvalidToken): проверьте токен в панели, '
                  'раздел «Боты». Перезапуск через %d с или сразу после сохранения токена',
                  config.INVALID_TOKEN_PAUSE_SEC)
        await runtime_changed(config.INVALID_TOKEN_PAUSE_SEC)
    finally:
        await runner.cleanup()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except InvalidToken:
        asyncio.run(invalid_token_pause())
        raise SystemExit(1) from None
    except Exception:
        # Через логгер, а не голой трассировкой в stderr: так токен вырезается.
        log.exception('бот остановлен ошибкой')
        raise SystemExit(1) from None
