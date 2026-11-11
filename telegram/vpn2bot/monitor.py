"""Фоновый опрос панели: тревоги в админский чат о нодах, нагрузке и самой панели."""
import asyncio
import os
import time

from . import api, config, texts
from .config import log

INTERVAL_SEC = 30
# Столько проверок подряд — тревога: короткий рестарт сбора метрик не считается.
HITS = 2
# Нода «лежит», когда её метрикам больше стольки секунд или их нет вовсе;
# столько же панель должна не отвечать до тревоги о ней.
DOWN_SEC = 300
# Тревога о нагрузке снимается, когда метрика ушла ниже порога на столько.
HYSTERESIS = 10.0
# Пороги считаются по размеру узла: на 1 vCPU / 2 ГБ обычная нагрузка VPN
# держится у 80-90%, и общий порог 80% будил по многу раз в день. Малому узлу
# — CPU 95%, RAM 90% и окно 5 минут. Явная переменная окружения перекрывает расчёт.
ENV_CPU = os.environ.get('ALERT_CPU_PCT')
ENV_RAM = os.environ.get('ALERT_RAM_PCT')
ENV_WINDOW = os.environ.get('ALERT_WINDOW_SEC')
SMALL_MEM_BYTES = 2 * 1024 ** 3
SMALL_WINDOW_SEC = 300

streak: dict[tuple, int] = {}   # (нода, признак) → сколько проверок подряд плохо
alerted: set[tuple] = set()     # о чём тревога уже отправлена
# Последний известный размер узла: cores/memTotalBytes приходят не в каждом
# снимке, и без памяти малый узел получил бы на таком снимке пороги большого.
node_size: dict[str, dict] = {}


def thresholds(size: dict) -> tuple[float, float, int]:
    """Пороги CPU и RAM и число проверок подряд для узла такого размера."""
    cores, mem = size.get('cores'), size.get('memTotalBytes')
    small_cpu = isinstance(cores, (int, float)) and cores <= 1
    small_ram = isinstance(mem, (int, float)) and mem <= SMALL_MEM_BYTES
    cpu = float(ENV_CPU) if ENV_CPU is not None else 95.0 if small_cpu else 80.0
    ram = float(ENV_RAM) if ENV_RAM is not None else 90.0 if small_ram else 80.0
    if ENV_WINDOW is not None:
        return cpu, ram, max(1, int(ENV_WINDOW) // INTERVAL_SEC)
    return cpu, ram, SMALL_WINDOW_SEC // INTERVAL_SEC if small_cpu or small_ram else HITS


async def say(text: str) -> None:
    await api.send(config.CHAT_ID, text, 'Markdown')


async def check_load(tag: str, label: str, value, threshold: float, hits: int) -> None:
    key = (tag, label)
    if not isinstance(value, (int, float)):
        return
    if value >= threshold:
        streak[key] = streak.get(key, 0) + 1
        if streak[key] >= hits and key not in alerted:
            await say(texts.LOAD_HIGH.format(tag.upper(), label, threshold, value))
            alerted.add(key)
        return
    # Гистерезис удерживает уже отправленную тревогу, но не серию пиков:
    # любое значение ниже порога прерывает счёт превышений.
    streak[key] = 0
    if value < threshold - HYSTERESIS and key in alerted:
        await say(texts.LOAD_OK.format(tag.upper(), label, value))
        alerted.discard(key)


async def check_nodes(nodes: list) -> None:
    for n in nodes:
        tag, stale = n.get('tag', '?'), n.get('staleSec')
        key = (tag, 'down')
        if stale is None or stale >= DOWN_SEC:
            streak[key] = streak.get(key, 0) + 1
            if streak[key] >= HITS and key not in alerted:
                await say(texts.NODE_DOWN.format(tag.upper(), DOWN_SEC // 60))
                alerted.add(key)
            continue
        if key in alerted:
            await say(texts.NODE_UP.format(tag.upper()))
            alerted.discard(key)
        streak[key] = 0
    for n in nodes:
        tag = n.get('tag', '?')
        size = node_size.setdefault(tag, {})
        size.update({field: n[field] for field in ('cores', 'memTotalBytes')
                     if isinstance(n.get(field), (int, float))})
        cpu_high, ram_high, hits = thresholds(size)
        await check_load(tag, 'CPU', n.get('cpu'), cpu_high, hits)
        await check_load(tag, 'RAM', n.get('ram'), ram_high, hits)


async def monitor_loop() -> None:
    log.info('monitor started: every %ds, cpu=%s, ram=%s, window=%s (None - by node size)',
             INTERVAL_SEC, ENV_CPU, ENV_RAM, ENV_WINDOW)
    failed_since, panel_alerted = None, False
    while True:
        await asyncio.sleep(INTERVAL_SEC)
        try:
            data = await api.status()
        except Exception as e:
            log.warning('panel poll error: %s', e)
            now = time.monotonic()
            failed_since = now if failed_since is None else failed_since
            if now - failed_since >= DOWN_SEC and not panel_alerted:
                try:
                    await api.send(config.CHAT_ID, texts.PANEL_DOWN)
                    panel_alerted = True
                except Exception as error:
                    log.warning('panel alert delivery error: %s', error)
            continue
        # Сбой отправки в Telegram или разбора метрик не значит, что панель
        # недоступна: успешный опрос обрывает серию сразу.
        failed_since = None
        try:
            if panel_alerted:
                await api.send(config.CHAT_ID, texts.PANEL_UP)
                panel_alerted = False
            await check_nodes(data.get('nodes', []))
        except Exception as e:
            log.warning('monitor error: %s', e)
