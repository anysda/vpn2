"""Шаг стадии 35: бот Telegram на entry.

Работает и без токена: токен, чат и админа можно задать позже в панели, она
пишет их в telegram-runtime.json, бот перечитывает файл сам.
"""

import grp
import json
import os
import pwd
import secrets
import shutil
import string
from pathlib import Path

from vpn2 import fsutil
from vpn2.containers import compose, engine

STAGE = '35-telegram'
ETC = '/etc/anysda'
RUNTIME_DIR = '/var/lib/anysda-vpn2/runtime'
BOT_DATA = '/var/lib/anysda-tgbot'
FIELDS = ('bot_token', 'chat_id', 'admin_username')
ALPHABET = string.ascii_letters + string.digits


def secret(etc=ETC):
    """Общий секрет бота и панели: создаётся один раз. Вернуть (секрет, создан ли)."""
    path = Path(etc, 'tgbot-secret.txt')
    created = not (path.is_file() and path.stat().st_size)
    if created:
        fsutil.atomic(path, ''.join(secrets.choice(ALPHABET) for _ in range(32)), mode=0o600,
                      encoding='utf-8')
    os.chmod(path, 0o600)
    return path.read_text(), created


def _from_config(environ):
    return {field: environ.get('TELEGRAM_' + field.upper()) or '' for field in FIELDS}


def sync_runtime(environ, previous_env, etc=ETC):
    """Перенести в настройки бота поля config.yaml, сменившиеся с прошлого деплоя.

    Заданное в панели сильнее: поле из config.yaml применяется, только когда его
    значение сменилось; снятое в config.yaml поле очищается, если панель его не меняла.
    """
    path, taken = Path(etc, 'telegram-runtime.json'), Path(etc, 'telegram-from-config.json')
    current = _from_config(environ)
    changed = False
    if not path.is_file():
        # Роль control ставит ссылку в каталог панели раньше, чем появится цель: пишем сквозь неё.
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(json.dumps(current))
        os.chmod(path, 0o600)
        changed = True
    runtime = json.loads(path.read_text())
    try:
        previous = json.loads(taken.read_text())
    except (OSError, ValueError):
        previous = None
    recorded = isinstance(previous, dict)
    if not recorded:
        env = dict(item.split('=', 1) for item in previous_env if '=' in item)
        previous = {field: env.get('TELEGRAM_' + field.upper(), '') for field in FIELDS}
    edited = False
    for field in FIELDS:
        value, before = current[field], previous.get(field, '')
        if value and value != before:
            runtime[field] = value
            edited = True
        elif field != 'admin_username' and not value and before and runtime.get(field) == before:
            runtime[field] = ''
            edited = True
    if edited:
        # Файл может быть ссылкой в каталог панели: пишем в цель, владелец и права прежние.
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(json.dumps(runtime) + '\n')
    if previous != current or not recorded:
        fsutil.atomic(taken, json.dumps(current), mode=0o600, encoding='utf-8')
    return changed or edited


def place_runtime(etc=ETC, runtime_dir=RUNTIME_DIR, data=BOT_DATA, chown=shutil.chown):
    """Настройки бота лежат в каталоге панели (она их пишет), в /etc/anysda - ссылка."""
    os.makedirs(data, mode=0o700, exist_ok=True)
    for root, dirs, files in os.walk(data):
        for name in [root, *(os.path.join(root, item) for item in dirs + files)]:
            os.chown(name, pwd.getpwnam('vpn2-bot').pw_uid, grp.getgrnam('vpn2-bot').gr_gid,
                     follow_symlinks=False)
    os.chmod(data, 0o700)
    os.makedirs(runtime_dir, mode=0o700, exist_ok=True)
    chown(runtime_dir, 'vpn2-panel', 'vpn2-panel')
    os.chmod(runtime_dir, 0o700)
    link, target = Path(etc, 'telegram-runtime.json'), Path(runtime_dir, 'telegram-runtime.json')
    if not link.is_symlink():
        shutil.copy2(link, target)
        temporary = link.with_name('.telegram-runtime.link')
        temporary.unlink(missing_ok=True)
        os.symlink(target, temporary)
        os.replace(temporary, link)
    chown(target, 'vpn2-panel', 'vpn2-panel')
    os.chmod(target, 0o640)


def panel_secret(docker):
    panel = docker.inspect(compose.PANEL)
    if panel is None:
        return None, ''
    env = dict(item.split('=', 1) for item in panel['Config'].get('Env') or [] if '=' in item)
    return env.get('NUXT_TGBOT_SECRET', ''), panel['Image']


def run(environ, *, docker=None, say=engine.say, panel_stage=None, sleep=None):
    docker = docker or engine.Docker()
    os.makedirs(ETC, exist_ok=True)
    value, created = secret()
    say('  новый секрет создан' if created else '  секрет уже существует — не перегенерируем')
    before = docker.inspect(compose.BOT)
    changed = created | sync_runtime(environ, (before or {}).get('Config', {}).get('Env') or [])
    image = engine.release_image(environ, 'tgbot', environ.get('TGBOT_IMAGE') or '',
                                 (environ.get('TGBOT_IMAGE_PULL') or 'true') != 'false', docker=docker)
    say('bot image digest verified')
    # Панель уже стоит, а секрет другой: она пересоздаётся с ним. При полной
    # установке стадия 30 идёт после 35 и берёт секрет сама.
    current, panel_image = panel_secret(docker)
    if environ.get('VPN2_PANEL_FOLLOWS', 'false') != 'true' and current is not None and current != value:
        say('пересоздание anysda-vpn2 для передачи TGBOT_SECRET...')
        if panel_stage is None:
            from vpn2.containers import panel as panel_module
            panel_stage = panel_module.run
        development = environ.get('VPN2_DEVELOPMENT', 'false') == 'true'
        panel_stage({**environ, 'PANEL_IMAGE': panel_image if development else '',
                     'PANEL_IMAGE_PULL': 'false'})
        changed = True
    place_runtime()
    engine.drop_timezone_dir()
    bot = pwd.getpwnam('vpn2-bot')
    service = compose.bot_service(environ, image, engine.resource_text(environ, 'bot'), secret=value,
                                  user=f'{bot.pw_uid}:{bot.pw_gid}',
                                  panel_gid=grp.getgrnam('vpn2-panel').gr_gid)
    changed |= docker.publish(compose.BOT, service)
    created = docker.up(compose.BOT)
    if created:
        say('жду пока бот запустится...')
        kwargs = {'sleep': sleep} if sleep else {}
        engine.wait(lambda: 'Event server on' in docker.logs(compose.BOT), 12, 0.5, **kwargs)
    say('последние логи:')
    for line in docker.logs(compose.BOT, tail=6).splitlines():
        print(f'  {line}')
    engine.stamp(STAGE)
    return changed or created
