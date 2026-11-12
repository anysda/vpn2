import asyncio
import os
import time

from aiohttp import web
from telegram.error import InvalidToken
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from . import api, config, events, handlers, monitor
from .config import log

# The event loop holds tasks only weakly.
watchers: list[asyncio.Task] = []


async def serve(*routes, outbox: bool = False) -> web.AppRunner:
    app = web.Application()
    if outbox:
        app.cleanup_ctx.append(events.event_outbox)
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', config.EVENT_PORT).start()
    return runner


async def runtime_changed(limit_sec: float = float('inf')) -> bool:
    """True once the panel saved telegram-runtime.json, False after limit_sec without a change."""
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


def run() -> None:
    """/event and /health first: panel events are queued while Telegram is still unreachable."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    if not config.TOKEN or config.CHAT_ID == 0:
        log.warning('Токен бота или Chat ID не заданы: жду их в панели, раздел «Боты»')
        loop.run_until_complete(serve(web.get('/health', events.health(waiting_token=True))))
        loop.run_until_complete(restart_on_new_runtime())
    runner = loop.run_until_complete(serve(web.post('/event', events.handle_event),
                                           web.get('/health', events.handle_health), outbox=True))
    log.info('Event server on 127.0.0.1:%d', config.EVENT_PORT)
    watchers.extend(loop.create_task(job()) for job in (restart_on_new_runtime, api.poll_watchdog, monitor.monitor_loop))
    tgapp = Application.builder().bot(api.bot).post_init(started).build()
    tgapp.add_handler(CommandHandler(handlers.COMMANDS, handlers.dispatch))
    tgapp.add_handler(CallbackQueryHandler(handlers.dispatch))
    tgapp.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.dispatch))
    try:
        tgapp.run_polling(drop_pending_updates=True, bootstrap_retries=-1, close_loop=False)
    finally:
        # The port is free again before invalid_token_pause; the stopped loop no longer runs the watchers.
        loop.run_until_complete(runner.cleanup())


async def started(tgapp: Application) -> None:
    config.BOT_USERNAME = tgapp.bot.username
    log.info('Bot started as @%s, polling Telegram via TG_PROXY', config.BOT_USERNAME)


async def invalid_token_pause() -> None:
    await serve(web.get('/health', events.health(waiting_token=True, invalid_token=True)))
    log.error('Telegram отверг токен бота (InvalidToken): проверьте токен в панели, '
              'раздел «Боты». Перезапуск через %d с или сразу после сохранения токена',
              config.INVALID_TOKEN_PAUSE_SEC)
    await runtime_changed(config.INVALID_TOKEN_PAUSE_SEC)


if __name__ == '__main__':
    try:
        run()
    except InvalidToken:
        asyncio.run(invalid_token_pause())
        raise SystemExit(1) from None
    except Exception:
        # Through the logger, not a bare traceback: the token gets cut out.
        log.exception('бот остановлен ошибкой')
        raise SystemExit(1) from None
