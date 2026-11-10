import asyncio
import os
import re
import time

from telegram import Update
from telegram.ext import ContextTypes

from . import admin, client, config, panel, transport
from .config import log
from .panel import BotApiError
from .texts import UNEXPECTED_ERROR


# Счётчики «нода лежит» — по tag ноды: сколько проверок подряд она down
# и для каких уже отправлен алерт (чтобы не спамить).
_node_down: dict[str, int] = {}
_node_alerted: set[str] = set()


def _ob_label(name: str) -> str:
    """hy2-us-direct → US, hy2-de-warp → DE WARP."""
    s = name[4:] if name.startswith('hy2-') else name
    if s.endswith('-warp'):
        return s[:-5].upper() + ' WARP'
    if s.endswith('-direct'):
        return s[:-7].upper()
    return s.upper()


def _exit_outbounds(data: dict) -> list:
    # Контракт app/shared/outbounds.ts; защита и при старой версии панели.
    return [o for o in data.get('outbounds', [])
            if re.fullmatch(r'hy2-([a-z](?:[a-z0-9-]*[a-z0-9])?)-(direct|warp)',
                            o.get('name') or '')]


def _pct(v) -> str:
    return f'{v:.0f}%' if isinstance(v, (int, float)) else '—'


async def build_status_text(header: str = '*Состояние системы*') -> str:
    data = await panel.api_status()
    lines = [header + '\n']
    for n in data.get('nodes', []):
        net = (n.get('rxMbps') or 0) + (n.get('txMbps') or 0)
        lines.append(
            f"🖥 *{n.get('tag', '?').upper()}*: CPU {_pct(n.get('cpu'))}"
            f" | RAM {_pct(n.get('ram'))}"
            f" | ↕ {net:.1f} Mbps"
        )
    lines.append('')
    for o in _exit_outbounds(data):
        rtt = o.get('rttMs')
        icon = '🟢' if rtt else '🔴'
        rtt_txt = f'{rtt} ms' if rtt else 'нет связи'
        lines.append(f"{icon} `{_ob_label(o.get('name', '?'))}`: {rtt_txt}")
    return '\n'.join(lines)


async def cmd_status(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not await client._admin_only(update):
        return
    try:
        await transport.tg_reply(update, await build_status_text(), 'Markdown')
    except Exception as e:
        log.error('cmd_status failed (%s)', type(e).__name__)
        await transport.tg_reply(update, f'❌ {UNEXPECTED_ERROR}')


async def cmd_nodes(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not await client._admin_only(update):
        return
    try:
        data = await panel.api_status()
        lines = ['*Exit-ноды*\n']
        for o in _exit_outbounds(data):
            rtt = o.get('rttMs')
            icon = '🟢' if rtt else '🔴'
            rtt_txt = f'{rtt} ms' if rtt else 'нет связи'
            lines.append(f"{icon} `{_ob_label(o.get('name', '?'))}`: {rtt_txt}")
        await transport.tg_reply(
            update,
            '\n'.join(lines) if len(lines) > 1 else 'Нет нод',
            'Markdown',
        )
    except Exception as e:
        log.error('cmd_nodes failed (%s)', type(e).__name__)
        await transport.tg_reply(update, f'❌ {UNEXPECTED_ERROR}')


async def cmd_clients(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not await client._admin_only(update):
        return
    try:
        # Интерактивный список: каждый клиент — инлайн-кнопка с drill-down
        # в карточку управления (заморозка / лимит / срок / приглашение).
        await admin.cmd_clients_interactive()
    except BotApiError as e:
        await transport.tg_reply(update, f'❌ {e.message}')
    except Exception as e:
        log.error('cmd_clients failed (%s)', type(e).__name__)
        await transport.tg_reply(update, f'❌ {UNEXPECTED_ERROR}')


# Алерты: проверки каждые MONITOR_INTERVAL сек, ALERT_HITS подряд = триггер.
# Снятие — когда метрика опускается ниже (порог - HYSTERESIS).
# По умолчанию: 30с * 2 = ~60с до CPU/RAM-алерта.
MONITOR_INTERVAL = int(os.environ.get('ALERT_INTERVAL_SEC', '30'))
ALERT_HITS       = int(os.environ.get('ALERT_HITS',         '2'))
# Пороги CPU/RAM считаются по размеру узла (cores, memTotalBytes из статуса
# панели): на 1 vCPU / 2 ГБ обычная нагрузка VPN держится у 80-90%, и тревога
# по старым порогам приходила по многу раз в день. Явный env перекрывает
# расчёт, у каждой переменной своё.
CPU_HIGH = float(os.environ.get('ALERT_CPU_PCT', '80'))
RAM_HIGH = float(os.environ.get('ALERT_RAM_PCT', '80'))
SMALL_CPU_HIGH = 95.0
SMALL_RAM_HIGH = 90.0
SMALL_WINDOW_SEC = 300
SMALL_CORES = 1
SMALL_MEM_BYTES = 2 * 1024 ** 3
_ENV_CPU = 'ALERT_CPU_PCT' in os.environ
_ENV_RAM = 'ALERT_RAM_PCT' in os.environ
_ENV_WINDOW = os.environ.get('ALERT_WINDOW_SEC')
HYSTERESIS = 10.0
# Нода считается «лежит», когда её метрики устарели больше чем на столько
# секунд (или их нет вовсе). По умолчанию 5 минут.
NODE_DOWN_SEC = int(os.environ.get('ALERT_NODE_DOWN_SEC', '300'))

_load_hits: dict[str, int] = {}     # key = f'{tag}:cpu' / f'{tag}:ram'
_load_alerted: set[str] = set()
_load_logged: dict[str, tuple] = {}  # пороги узла, последними записанные в журнал
# Последний известный размер узла: запрос cores/memTotal к VM может не успеть,
# и без памяти малый узел на этом сэмпле получил бы пороги большого.
_node_size: dict[str, dict] = {}


def _window_hits(window_sec: int) -> int:
    return max(1, window_sec // MONITOR_INTERVAL)


def node_thresholds(n: dict) -> tuple[float, float, int, int]:
    """Пороги CPU и RAM, число сэмплов подряд и окно в секундах для узла.

    Малый узел - не больше 1 ядра (CPU 95%) или не больше 2 ГБ памяти
    (RAM 90% по MemAvailable); у малого узла окно 5 мин, иначе ALERT_HITS
    сэмплов. Узел без размера в статусе - пороги как раньше."""
    cores = n.get('cores')
    mem = n.get('memTotalBytes')
    small_cpu = isinstance(cores, (int, float)) and cores <= SMALL_CORES
    small_ram = isinstance(mem, (int, float)) and mem <= SMALL_MEM_BYTES
    cpu = CPU_HIGH if _ENV_CPU or not small_cpu else SMALL_CPU_HIGH
    ram = RAM_HIGH if _ENV_RAM or not small_ram else SMALL_RAM_HIGH
    if _ENV_WINDOW is not None:
        window = int(_ENV_WINDOW)
    elif small_cpu or small_ram:
        window = SMALL_WINDOW_SEC
    else:
        return cpu, ram, ALERT_HITS, ALERT_HITS * MONITOR_INTERVAL
    return cpu, ram, _window_hits(window), window


async def _check_load(tag: str, metric: str, value: float, threshold: float,
                       label: str, unit: str = '%', hits: int | None = None) -> None:
    key = f'{tag}:{metric}'
    need = ALERT_HITS if hits is None else hits
    if value >= threshold:
        _load_hits[key] = _load_hits.get(key, 0) + 1
        if _load_hits[key] >= need and key not in _load_alerted:
            await transport.tg_send(
                config.CHAT_ID,
                f'⚠️ *{tag.upper()}*: нагрузка на {label} выше '
                f'{threshold:.0f}{unit} ({value:.0f}{unit})',
                'Markdown',
            )
            _load_alerted.add(key)
    else:
        # Гистерезис удерживает уже отправленный алерт, но не серию пиков.
        # Любое значение ниже порога прерывает последовательные превышения.
        _load_hits[key] = 0
        if value < threshold - HYSTERESIS and key in _load_alerted:
            await transport.tg_send(
                config.CHAT_ID,
                f'✅ *{tag.upper()}*: {label} вернулся в норму ({value:.0f}{unit})',
                'Markdown',
            )
            _load_alerted.discard(key)


async def monitor_loop() -> None:
    log.info('monitor_loop started: interval=%ds, hits=%d, cpu_high=%s, ram_high=%s, window=%s',
             MONITOR_INTERVAL, ALERT_HITS,
             f'{CPU_HIGH:.0f} (env)' if _ENV_CPU else 'по размеру узла',
             f'{RAM_HIGH:.0f} (env)' if _ENV_RAM else 'по размеру узла',
             f'{_ENV_WINDOW}s (env)' if _ENV_WINDOW is not None else 'по размеру узла')
    panel_failed_since = None
    panel_alerted = False
    while True:
        await asyncio.sleep(MONITOR_INTERVAL)
        try:
            data = await panel.api_status()
        except Exception as e:
            log.warning('panel poll error: %s', e)
            now = time.monotonic()
            if panel_failed_since is None:
                panel_failed_since = now
            if now - panel_failed_since >= 300 and not panel_alerted:
                try:
                    await transport.tg_send(config.CHAT_ID, '🔴 Панель недоступна — мониторинговые данные не поступают')
                    panel_alerted = True
                except Exception as error:
                    log.warning('panel alert delivery error: %s', error)
            continue
        # Ошибка отправки в Telegram или обработки метрик не означает,
        # что сама панель недоступна. Успешный опрос прерывает серию сразу.
        panel_failed_since = None
        try:
            if panel_alerted:
                await transport.tg_send(config.CHAT_ID, '🟢 Панель снова доступна')
                panel_alerted = False
            nodes = data.get('nodes', [])

            # Нода лежит: метрики устарели > NODE_DOWN_SEC (или их нет вовсе —
            # staleSec=None, VM уже не отдаёт серию). staleSec — реальный
            # возраст последнего сэмпла node_exporter. ALERT_HITS подряд —
            # отсекаем кратковременный рестарт VM, когда staleSec мигает в None.
            for n in nodes:
                tag = n.get('tag', '?')
                stale = n.get('staleSec')
                down = stale is None or stale >= NODE_DOWN_SEC
                if down:
                    _node_down[tag] = _node_down.get(tag, 0) + 1
                    if _node_down[tag] >= ALERT_HITS and tag not in _node_alerted:
                        log.info('ALERT: нода %s лежит — отправляю алерт', tag)
                        await transport.tg_send(
                            config.CHAT_ID,
                            f'🔴 *Нода {tag.upper()} лежит* — '
                            f'метрик нет больше {NODE_DOWN_SEC // 60} мин',
                            'Markdown',
                        )
                        _node_alerted.add(tag)
                else:
                    if tag in _node_alerted:
                        log.info('нода %s восстановилась — отправляю алерт', tag)
                        await transport.tg_send(
                            config.CHAT_ID,
                            f'🟢 *Нода {tag.upper()} восстановилась*',
                            'Markdown',
                        )
                        _node_alerted.discard(tag)
                    _node_down[tag] = 0

            # CPU / RAM на каждой ноде
            for n in nodes:
                tag = n.get('tag', '?')
                cpu = n.get('cpu')
                ram = n.get('ram')
                size = _node_size.setdefault(tag, {})
                for field in ('cores', 'memTotalBytes'):
                    if isinstance(n.get(field), (int, float)):
                        size[field] = n[field]
                cpu_high, ram_high, hits, window = node_thresholds(size)
                if _load_logged.get(tag) != (cpu_high, ram_high, hits):
                    log.info('пороги узла %s: cores=%s, mem=%s, cpu=%.0f%%, ram=%.0f%%, '
                             'окно %ds (%d сэмпл.)', tag, size.get('cores'), size.get('memTotalBytes'),
                             cpu_high, ram_high, window, hits)
                    _load_logged[tag] = (cpu_high, ram_high, hits)
                if isinstance(cpu, (int, float)):
                    await _check_load(tag, 'cpu', float(cpu), cpu_high, 'CPU', hits=hits)
                if isinstance(ram, (int, float)):
                    await _check_load(tag, 'ram', float(ram), ram_high, 'RAM', hits=hits)
        except Exception as e:
            log.warning('monitor error: %s', e)
