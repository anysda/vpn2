"""Polls the panel and alerts the admin chat about nodes, their load and the panel itself."""
import asyncio
import os
import time

from . import api, config, texts
from .config import log

INTERVAL_SEC = 30
HITS = 2
# A node is down when its metrics are older than this or missing; the panel after this long silent.
DOWN_SEC = 300
# A load alert clears once the metric is this far below the threshold.
HYSTERESIS = 10.0
# On 1 vCPU or 2 GB a VPN node sits at 80-90 %: CPU 95 %, RAM 90 % and a 5 minute window there.
ENV_CPU = os.environ.get('ALERT_CPU_PCT')
ENV_RAM = os.environ.get('ALERT_RAM_PCT')
ENV_WINDOW = os.environ.get('ALERT_WINDOW_SEC')
SMALL_MEM_BYTES = 2 * 1024 ** 3
SMALL_WINDOW_SEC = 300

streak: dict[tuple, int] = {}
alerted: set[tuple] = set()
# cores and memTotalBytes do not come in every snapshot.
node_size: dict[str, dict] = {}


def thresholds(size: dict) -> tuple[float, float, int]:
    """CPU and RAM thresholds and checks in a row for a node of this size."""
    cores, mem = size.get('cores'), size.get('memTotalBytes')
    small_cpu = isinstance(cores, (int, float)) and cores <= 1
    small_ram = isinstance(mem, (int, float)) and mem <= SMALL_MEM_BYTES
    cpu = float(ENV_CPU) if ENV_CPU is not None else 95.0 if small_cpu else 80.0
    ram = float(ENV_RAM) if ENV_RAM is not None else 90.0 if small_ram else 80.0
    if ENV_WINDOW is not None:
        return cpu, ram, max(1, int(ENV_WINDOW) // INTERVAL_SEC)
    return cpu, ram, SMALL_WINDOW_SEC // INTERVAL_SEC if small_cpu or small_ram else HITS


async def edge(key: tuple, bad: bool, recovered: bool, hits: int, alert: str, recovery: str) -> None:
    """Alert after hits bad checks in a row, once; any good check restarts the count."""
    if bad:
        streak[key] = streak.get(key, 0) + 1
        if streak[key] >= hits and key not in alerted:
            await api.send(config.CHAT_ID, alert, 'Markdown')
            alerted.add(key)
        return
    streak[key] = 0
    if recovered and key in alerted:
        await api.send(config.CHAT_ID, recovery, 'Markdown')
        alerted.discard(key)


async def check_nodes(nodes: list) -> None:
    for n in nodes:
        tag, stale = n.get('tag', '?'), n.get('staleSec')
        down = stale is None or stale >= DOWN_SEC
        await edge((tag, 'down'), down, not down, HITS,
                   texts.NODE_DOWN.format(tag.upper(), DOWN_SEC // 60), texts.NODE_UP.format(tag.upper()))
    for n in nodes:
        tag = n.get('tag', '?')
        size = node_size.setdefault(tag, {})
        size.update({field: n[field] for field in ('cores', 'memTotalBytes')
                     if isinstance(n.get(field), (int, float))})
        cpu_high, ram_high, hits = thresholds(size)
        for label, value, high in (('CPU', n.get('cpu'), cpu_high), ('RAM', n.get('ram'), ram_high)):
            if isinstance(value, (int, float)):
                await edge((tag, label), value >= high, value < high - HYSTERESIS, hits,
                           texts.LOAD_HIGH.format(tag.upper(), label, high, value),
                           texts.LOAD_OK.format(tag.upper(), label, value))


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
        # A failed send or a broken snapshot does not make the panel unreachable.
        failed_since = None
        try:
            if panel_alerted:
                await api.send(config.CHAT_ID, texts.PANEL_UP)
                panel_alerted = False
            await check_nodes(data.get('nodes', []))
        except Exception as e:
            log.warning('monitor error: %s', e)
