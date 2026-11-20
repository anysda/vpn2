"""Что видит оператор: шапка и итог полного деплоя, заметки IKEv2 и SSO, событие боту, backup и restore.
Деплой идёт с entry, поэтому состояние entry читается с диска этой же машины."""
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from vpn2 import config, creds
from vpn2.backup import archive, env as backup_env, keys, restore as node_restore
from vpn2.deploy import B, E, G, CONFIG, elapsed, install_prereqs, prep_keys, read, warn

ETC = Path('/etc/anysda')
BACKUP, RESTORE = '/usr/local/bin/anysda-backup.sh', '/usr/local/bin/anysda-restore.sh'


def banner(deploy):
    rows = ['anysda-vpn2 ·  deployment orchestrator', '', 'RU entry    →  sing-box router',
            *(f'            →  {tag.upper()} экзит' for tag in deploy.exit_tags())]
    print(f'\n{B}  ┌{"─" * 42}┐{E}\n' + ''.join(f'{B}  │{E}  {row:<40}{B}│{E}\n' for row in rows)
          + f'{B}  └{"─" * 42}┘{E}\n', flush=True)


def columns(lines):
    """Как `column -t -s TAB`: колонки через два пробела, последняя без добивки."""
    cells = [line.split('\t') for line in lines]
    widths = [max(len(row[i]) for row in cells if len(row) > i) for i in range(max(map(len, cells), default=0))]
    return ['  '.join(cell.ljust(widths[i]) if i < len(row) - 1 else cell for i, cell in enumerate(row)) for row in cells]


def summary(deploy, t0):
    env, timing = deploy.env(), elapsed(t0)
    url = f'http://{env["SSH_HOST"]}/'
    if read('/etc/vpn2/tls/panel/secure') == 'true':
        url = f'https://{env.get("ENTRY_HOST") or env["SSH_HOST"]}/'
    if env.get('PANEL_DOMAIN'):
        url = f'https://{env["PANEL_DOMAIN"]}/'
    password = ('показан ниже, один раз' if deploy.admin_generated else 'задан в config.yaml (secrets.admin_password)'
                if os.environ.get('ADMIN_PASSWORD') else 'прежний; новый - в профиле панели или secrets.admin_password')
    warned = deploy.verify_warnings or deploy.deploy_warnings or deploy.skipped
    title = f'  {"⚠ деплой завершён с предупреждениями" if warned else "деплой завершён"}  ·  {timing}  '
    rows = [title, '-', *(f'  {row}' for row in columns(deploy.stage_log)), '-',
            f'  Panel:    {url}', f'  Login:    {env.get("ADMIN_USER") or "admin"}', f'  Password: {password}']
    width = max(len(row) for row in rows) + 12
    bar = '─' * width
    print(f'\n{G}  ┌{bar}┐{E}\n' + ''.join(f'{G}  ├{bar}┤{E}\n' if row == '-' else f'{G}  │{E}{row:<{width}}{G}│{E}\n'
                                         for row in rows) + f'{G}  └{bar}┘{E}\n', flush=True)
    ikev2_notice()
    sso_notice(deploy)
    if deploy.verify_warnings:
        warn('99-verify: установка завершена, но не все проверки измерены')
        print('\n'.join(deploy.verify_warnings), flush=True)
    if deploy.deploy_warnings:
        print('\n'.join(deploy.deploy_warnings), flush=True)
    if deploy.skipped:
        warn(f'pre-flight: экзиты пропущены из-за недоступного SSH: {" ".join(deploy.skipped)}')


def ikev2_notice():
    try:
        mode, host = ((ETC / f'ikev2-{name}').read_text().strip() for name in ('mode', 'server-host'))
    except OSError:
        warn('IKEv2: не удалось прочитать состояние сервера', tag='ru')
        return
    domain = read(ETC / 'ikev2-domain')
    print(f'IKEv2: mode={mode} server-host={host}', flush=True)
    if domain and (mode != 'letsencrypt' or host != domain):
        print(f'IKEv2: ожидается сертификат Caddy для {domain}; автоматическая проверка каждые 30 с.', flush=True)


def sso_notice(deploy):
    env = deploy.env()
    if (env.get('SSO_REQUESTED') or env.get('SSO_ENABLED') or 'false') == 'true' and not creds.read('sso_client_secret') \
            and not (ETC / 'sso-runtime.json').exists():
        print('SSO не включён, потому что не задан секрет. Входите по паролю администратора. Чтобы включить SSO, '
              'заполните secrets.sso_client_secret в config.yaml и запустите ./deploy.sh 30-frontend ru.', flush=True)


def announce(deploy):
    """Бот включён, если есть токен в хранилище и чат в config.yaml или в панели (telegram-runtime.json)."""
    if not deploy.env().get('TELEGRAM_CHAT_ID') and not re.search(r'"chat_id": *("[^"]|-?[1-9])', read(ETC / 'telegram-runtime.json')):
        return
    secret = creds.read('tgbot_secret')
    if not (secret and creds.read('telegram_bot_token')):
        return
    body = json.dumps({'type': 'deploy_done', 'verify_unmeasured': bool(deploy.verify_warnings)}).encode()
    request = urllib.request.Request('http://127.0.0.1:8877/event', body, {'Content-Type': 'application/json', 'X-Tgbot-Secret': secret})
    try:
        urllib.request.urlopen(request, timeout=5).close()
    except (OSError, ValueError):
        pass


def backup():
    """./deploy.sh backup: код 4 со строкой warning - архив годный, без неё - сбой."""
    with subprocess.Popen([BACKUP], stdout=subprocess.PIPE, text=True) as done:
        lines = [print(line, end='', flush=True) or line for line in done.stdout]
    warning = next((line for line in lines if line.startswith('warning:')), '')
    if done.returncode != 4 or not warning:
        return done.returncode
    if 'сверка БД с конфигами VPN не выполнена' in warning:
        warn('архив сохранён, сверка с конфигами VPN не выполнена - не проверено, причина выше')
    else:
        warn('архив сохранён с предупреждением: БД и конфиги VPN расходятся - перевыпусти перечисленные выше устройства в панели')
    return 0


RESTAGES = ('29-openvpn', '20-ru-router', '21-failover-watchdog', '26-backup', '27-ikev2', '35-telegram', '30-frontend')


def restore(deploy, args, capture=False):
    """restore [<имя>|latest|<файл>] [--force] [--dry-run]: архив разворачивает anysda-restore.sh,
    затем стадии пересобирают всё, что читает восстановленное (ключи и PKI стадии не трогают)."""
    archive, flags = (args[0], args[1:]) if args and not args[0].startswith('-') else ('latest', args)
    output = {'stdout': subprocess.PIPE, 'stderr': subprocess.STDOUT, 'text': True} if capture else {}
    if archive != 'latest' and os.path.isfile(archive):
        base = os.path.basename(archive)
        if '--dry-run' in flags:
            text = f'restore: dry-run — загрузил бы на entry и восстановил локальный архив {base}, ничего не изменено\n'
            if not capture:
                print(text, end='', flush=True)
            return 0, text
        env = subprocess.run(['bash', '-c', 'source /etc/anysda/backup.env 2>/dev/null; echo "${BACKUP_LOCAL_DIR:-}"'],
                             capture_output=True, text=True).stdout.strip()
        target = Path(env or '/var/backups/anysda-vpn2') / base
        if not (target.exists() and os.path.samefile(archive, target)):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(Path(archive).read_bytes())
            target.chmod(0o600)
        archive = base
    # Ключ архива спрошен деплоем один раз: проверке и самому restore он идёт в stdin.
    secret = getattr(deploy, 'restore_secret', None)
    done = subprocess.run([RESTORE, archive, *flags], **{**output, **({'input': secret[0] + '\n', 'text': True} if secret else {})})
    if done.returncode or '--dry-run' in flags or '--preflight' in flags:
        return done.returncode, done.stdout or ''
    # Пароль панели - в восстановленной базе: стадии после restore его не задают.
    os.environ['VPN2_PRESERVE_ADMIN'] = '1'
    # Ключи ssh из архива: открытые части и authorized_keys entry - по ним.
    prep_keys()
    deploy.load_nodes()
    for stage in RESTAGES:
        deploy.run_stage(stage, 'ru')
    return 0, ''


def fresh_settings(cfg):
    """Настройки бэкапа нового сервера: backup.env ещё нет, всё - из config.yaml и хранилища."""
    backup = cfg.raw.get('backup') or {}
    s3, local = backup.get('s3') or {}, backup.get('local') or {}
    return backup_env.Settings(backend=backup.get('backend') or 'local',
                               local_dir=local.get('dir') or backup_env.Settings.local_dir, s3_endpoint=s3.get('endpoint') or '',
                               s3_bucket=s3.get('bucket') or '', s3_region=s3.get('region') or backup_env.Settings.s3_region,
                               s3_access_key=creds.read('s3_access_key'), s3_secret_key=creds.read('s3_secret_key'))


def restore_input(deploy, args, fresh):
    """Архив restore - локальным файлом, его ключ или пароль - один раз и в самом начале: дальше их ждут деплой
    нового сервера, проверка архива и сам restore. Вернуть args с путём к архиву вместо имени."""
    name, flags = (args[0], args[1:]) if args and not args[0].startswith('-') else ('latest', args)
    try:
        if fresh:
            settings = fresh_settings(config.load(CONFIG))
            install_prereqs('age', *(['rclone'] if settings.backend == 's3' else []))
        else:
            settings = backup_env.settings(backup_env.read_env(), lazy_s3=True)
        if os.path.isfile(name):
            path = Path(name).resolve()
        else:
            if name == 'latest':
                name, _ = node_restore.resolve_latest(settings)
            path = Path(settings.local_dir) / os.path.basename(name)
            if not path.is_file():
                node_restore.s3_pull(settings, path.name)
                deploy.restore_pulled = path
        deploy.restore_secret = node_restore.secret_for(path, tell=True)
    except (node_restore.RestoreError, backup_env.BackupEnvError) as error:
        restore_refused(deploy)
        print(str(error), file=sys.stderr, flush=True)
        raise SystemExit(error.code) from None
    return [str(path), *flags]


def restore_refused(deploy):
    """Отказ до изменений: архив, скачанный этим прогоном из S3, не остаётся."""
    if pulled := getattr(deploy, 'restore_pulled', None):
        Path(pulled).unlink(missing_ok=True)


def fresh_secrets(deploy, path):
    """restore на новом сервере, до деплоя: секреты и ключи ssh архива - в хранилище. Экзиты знают только ключ
    из архива, поэтому деплой нового сервера входит на них им, без строки ./deploy.sh -key. Неверный ключ или
    битый архив - отказ здесь, до деплоя."""
    try:
        with tempfile.TemporaryDirectory() as work:
            plain = Path(work) / 'archive.tar.gz'
            archive.decrypt(path, plain, *deploy.restore_secret)
            with tarfile.open(plain) as tar:
                member = next((item for item in tar.getmembers() if item.name.endswith('/secrets.json')), None)
                if member is not None:
                    (Path(work) / 'secrets.json').write_bytes(tar.extractfile(member).read())
            # Архив 1.2.0 без secrets.json: ключ оркестратора уже взят из orchestrator_key config.yaml 1.2.0.
            if member is not None:
                print(f'  секреты и ключи ssh из {Path(path).name} - в хранилище нового сервера', flush=True)
                if note := creds.replaced_note(creds.unbundle(Path(work) / 'secrets.json')):
                    print(f'  {note}', flush=True)
    except (archive.ArchiveError, tarfile.TarError) as error:
        restore_refused(deploy)
        print(str(error), file=sys.stderr, flush=True)
        raise SystemExit(getattr(error, 'code', 5)) from None


def restore_preflight(deploy, args):
    """Отказ restore (архива нет, не тот пароль, версия, живая база без --force) - до открытия выпуска.
    Пакет узла старше дерева флага --preflight не знает - тогда --dry-run."""
    code, out = restore(deploy, [*args, '--preflight'], capture=True)
    if code == 2 and 'неизвестный аргумент: --preflight' in out:
        code, out = restore(deploy, [*args, '--dry-run'], capture=True)
    if code:
        restore_refused(deploy)
        print(out, end='', file=sys.stderr)
    return code


# Прогоны, после которых у включённых бэкапов есть действующая пара ключей.
KEY_ACTIONS = ('', 'recover', 'restore', '26-backup')


def backup_key_start(manager):
    """Пары нет или её никто не видел - новая; закрытая часть - в памяти до show_backup_key в конце прогона.
    Задание менеджера пару только заводит, если её нет: показ - дело мастера (vpn2.backup.keys.issue()).
    Вернуть (закрытый ключ, открытый, архивы с непоказанной парой) или False.
    Пароль архивов 1.2.0 из backup.env - сразу в хранилище: снимки отката деплоя берут /etc/anysda целиком."""
    if (old := keys.forget_password()) and not creds.read('backup_passphrase'):
        creds.write('backup_passphrase', old)
    keys.scrub_rollback()
    try:
        cfg = config.load(CONFIG).raw.get('backup') or {}
    except (OSError, config.ConfigError):
        return False
    if str(cfg.get('enabled', '')).lower() not in config.TRUE_WORDS:
        return False
    if keys.recipient() and (manager or not keys.unseen()):
        return False
    lost = keys.since_unseen((cfg.get('local') or {}).get('dir') or backup_env.Settings.local_dir)
    install_prereqs('age')
    private = keys.issue()
    return False if manager else (private, keys.recipient(), lost)


def show_backup_key(issued):
    """Конец прогона в терминале: новый ключ бэкапов, если он ещё действует, и пароль архивов 1.2.0 - один раз.
    Пароль после показа уходит с сервера: из хранилища, backup.env и config.yaml."""
    if issued and keys.recipient() == issued[1]:
        print(keys.shown_text(issued[0], issued[2]), end='', flush=True)
        keys.seen()
    old = creds.read('backup_passphrase')
    old = keys.forget_password() or old
    if not old:
        return
    print(f'Пароль архивов, сделанных до перехода на ключ: {old}\n'
          '  Сохраните его рядом с ключом: restore таких архивов спросит его; на сервере его больше нет.\n', flush=True)
    creds.write('backup_passphrase', '')
    if CONFIG.is_file():
        config.wipe_secrets(CONFIG, keep=tuple({*config.LEGACY_SECRETS.values(), *config.AREA_NAMES, ''} - {'backup_passphrase'}))
