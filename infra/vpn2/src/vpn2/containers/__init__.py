"""Контейнеры entry под compose: стадии 25-monitoring, 35-telegram, 30-frontend и откат панели."""
import grp
import json
import os
import pwd
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from vpn2 import fsutil, resources
from vpn2.adguard.stage import random_text
from vpn2.containers import compose, panel
from vpn2.containers.compose import EngineError, say

ERRORS = (EngineError, panel.StageError)
SCRAPE = '/etc/anysda/vmsingle-scrape.yml'
ETC = '/etc/anysda'
RUNTIME = '/var/lib/anysda-vpn2/runtime/telegram-runtime.json'
BOT_DATA = '/var/lib/anysda-tgbot'
TELEGRAM = ('bot_token', 'chat_id', 'admin_username')


def targets():
    try:
        with urllib.request.urlopen('http://127.0.0.1:8428/api/v1/targets', timeout=3) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError):
        return {}


def monitoring(environ, docker):
    """VictoriaMetrics; готова, когда каждая цель скрейпа (entry и экзиты) health=up."""
    tags = compose.exit_tags(environ)
    if not tags:
        raise EngineError('EXIT_TAGS пуст')
    text, skipped = compose.scrape_config(environ)
    for tag in skipped:
        say(f'ВНИМАНИЕ: MGMT_IP_{compose.upper(tag)} не задан — {tag} пропущен в scrape config')
    os.makedirs(ETC, exist_ok=True)
    os.makedirs('/var/lib/vmsingle', exist_ok=True)
    stamp = compose.prepare_host('25-monitoring')
    changed = fsutil.publish(SCRAPE, text, 0o644)
    image = f'victoriametrics/victoria-metrics:{environ["VM_VERSION"]}'
    changed |= docker.publish(compose.VM, compose.vm_service(image, resources.entry(environ), text))
    changed |= docker.up(compose.VM)
    reply = {}

    def ready():
        nonlocal reply
        reply = targets()
        active = (reply.get('data') or {}).get('activeTargets') or []
        return (reply.get('status') == 'success' and len(active) == 1 + len(tags)
                and all(target.get('health') == 'up' for target in active))

    ok = compose.wait(ready, 30, 1)
    say('scrape health:')
    for target in (reply.get('data') or {}).get('activeTargets') or []:
        print(f'  {(target.get("labels") or {}).get("host") or "?"} ({target.get("scrapeUrl", "")}): '
              f'{target.get("health", "")}')
    if not ok:
        raise EngineError(f'мониторинг не готов: ожидается {1 + len(tags)} доступных scrape-целей')
    stamp.touch()
    return changed


def bot_secret():
    """Общий секрет бота и панели: создаётся один раз. Вернуть (секрет, создан ли)."""
    path = Path(ETC, 'tgbot-secret.txt')
    created = not (path.is_file() and path.stat().st_size)
    if created:
        fsutil.atomic(path, random_text(32))
    os.chmod(path, 0o600)
    return path.read_text(), created


def sync_runtime(environ, previous_env):
    """Поля telegram из config.yaml в настройки бота, только сменившиеся с прошлого деплоя.

    Заданное в панели сильнее; снятое в config.yaml поле очищается, если панель его не меняла.
    Файл - ссылка в каталог панели: пишется сквозь неё, владелец и права цели прежние.
    """
    path, taken = Path(ETC, 'telegram-runtime.json'), Path(ETC, 'telegram-from-config.json')
    current = {field: environ.get('TELEGRAM_' + field.upper()) or '' for field in TELEGRAM}
    created = not path.is_file()
    if created:
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(json.dumps(current))
        os.chmod(path, 0o600)
    runtime = json.loads(path.read_text())
    try:
        previous = json.loads(taken.read_text())
    except (OSError, ValueError):
        previous = None
    recorded = isinstance(previous, dict)
    if not recorded:
        env = dict(item.split('=', 1) for item in previous_env if '=' in item)
        previous = {field: env.get('TELEGRAM_' + field.upper(), '') for field in TELEGRAM}
    edited = False
    for field in TELEGRAM:
        value, before = current[field], previous.get(field, '')
        if value and value != before:
            runtime[field], edited = value, True
        elif field != 'admin_username' and not value and before and runtime.get(field) == before:
            runtime[field], edited = '', True
    if edited:
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(json.dumps(runtime) + '\n')
    if previous != current or not recorded:
        fsutil.atomic(taken, json.dumps(current))
    return created or edited


def place_runtime():
    """Данные бота - его пользователю; настройки бота в каталоге панели, в /etc/anysda - ссылка."""
    os.makedirs(BOT_DATA, mode=0o700, exist_ok=True)
    bot = pwd.getpwnam('vpn2-bot')
    for root, dirs, files in os.walk(BOT_DATA):
        for name in [root, *(os.path.join(root, item) for item in dirs + files)]:
            os.chown(name, bot.pw_uid, grp.getgrnam('vpn2-bot').gr_gid, follow_symlinks=False)
    os.chmod(BOT_DATA, 0o700)
    os.makedirs(Path(RUNTIME).parent, mode=0o700, exist_ok=True)
    shutil.chown(Path(RUNTIME).parent, 'vpn2-panel', 'vpn2-panel')
    link = Path(ETC, 'telegram-runtime.json')
    if not link.is_symlink():
        shutil.copy2(link, RUNTIME)
        temporary = link.with_name('.telegram-runtime.link')
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(RUNTIME)
        temporary.replace(link)
    shutil.chown(RUNTIME, 'vpn2-panel', 'vpn2-panel')
    os.chmod(RUNTIME, 0o640)


def telegram(environ, docker):
    """Бот работает и без токена: токен, чат и админа панель пишет в telegram-runtime.json."""
    os.makedirs(ETC, exist_ok=True)
    value, created = bot_secret()
    say('  новый секрет создан' if created else '  секрет уже существует — не перегенерируем')
    before = docker.inspect(compose.BOT)
    changed = created | sync_runtime(environ, (before or {}).get('Config', {}).get('Env') or [])
    image = compose.release_image(environ, 'tgbot', docker)
    say('bot image digest verified')
    current = docker.inspect(compose.PANEL)
    # Полная установка ставит панель после бота, и она сама берёт новый секрет.
    if (environ.get('VPN2_PANEL_FOLLOWS', 'false') != 'true' and current is not None
            and compose.env_of(current).get('NUXT_TGBOT_SECRET', '') != value):
        say('пересоздание anysda-vpn2 для передачи TGBOT_SECRET...')
        development = environ.get('VPN2_DEVELOPMENT', 'false') == 'true'
        panel.run({**environ, 'PANEL_IMAGE': current['Image'] if development else '', 'PANEL_IMAGE_PULL': 'false'},
                  docker)
        changed = True
    place_runtime()
    stamp = compose.prepare_host('35-telegram')
    bot = pwd.getpwnam('vpn2-bot')
    service = compose.bot_service(environ, image, resources.entry(environ), secret=value,
                                  user=f'{bot.pw_uid}:{bot.pw_gid}', panel_gid=grp.getgrnam('vpn2-panel').gr_gid)
    changed |= docker.publish(compose.BOT, service)
    if docker.up(compose.BOT):
        changed = True
        say('жду пока бот запустится...')
        compose.wait(lambda: 'Event server on' in docker.logs(compose.BOT), 12, 0.5)
    say('последние логи:')
    for line in docker.logs(compose.BOT, tail=6).splitlines():
        print(f'  {line}')
    stamp.touch()
    return changed


STAGES = {'monitoring': monitoring, 'telegram': telegram, 'panel': panel.run}


def run(stage, environ):
    """Стадия целиком; вернуть, менялся ли узел."""
    return STAGES[stage](environ, compose.Docker())


def rollback():
    """Вернуть прежний образ панели; занятый замок деплоя - отказ."""
    transaction = panel.Transaction(compose.Docker())
    transaction.lock()
    return transaction.rollback()
