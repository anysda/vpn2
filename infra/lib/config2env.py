#!/usr/bin/env python3
"""
config2env.py — читает config.yaml, пишет infra/envs/*.env

Использование:
    python3 infra/lib/config2env.py [repo_root]
"""

import base64
import re
import sys
from pathlib import Path


def shq(v):
    """Значение для *.env в одинарных кавычках: `source` вернёт его байт в байт."""
    return "'" + str(v).replace("'", "'\\''") + "'"


def unquote_scalar(v):
    """Снимает YAML-кавычки: '..' ('' внутри = '), ".." (escape-последовательности).

    Хвостовой комментарий после закрывающей кавычки отбрасывается. Незакрытая
    кавычка — ошибка: молча обрезанный пароль хуже упавшего деплоя.
    """
    q = v[0]
    i, out = 1, []
    while i < len(v):
        c = v[i]
        if q == "'" and c == "'":
            if v[i + 1:i + 2] == "'":
                out.append("'")
                i += 2
                continue
            return ''.join(out)
        if q == '"' and c == '\\':
            nxt = v[i + 1:i + 2]
            out.append({'n': '\n', 't': '\t', '"': '"', '\\': '\\', '/': '/'}.get(nxt, '\\' + nxt))
            i += 2
            continue
        if q == '"' and c == '"':
            return ''.join(out)
        out.append(c)
        i += 1
    print(f'ERROR: config.yaml: незакрытая кавычка в значении {v!r}', file=sys.stderr)
    sys.exit(1)


def parse_yaml(text):
    """Минимальный парсер для нашего фиксированного формата config.yaml."""
    cfg = {'exits': [], 'ports': {}, 'entry': {}, 'telegram': {}, 'admin': {},
           'panel': {}, 'tgbot': {}, 'backup': {}, 'youtube': {}, 'sso': {}}
    context = None
    current_exit = None
    backup_sub = None    # 's3' или 'local' внутри backup: блока

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith('#'):
            continue

        indent = len(line) - len(line.lstrip())
        content = line.lstrip()

        def kv(s):
            k, _, v = s.partition(':')
            v = v.strip()
            if v[:1] in ('"', "'"):
                v = unquote_scalar(v)
            else:
                # `us   # короткий тег` — комментарий после значения не часть значения
                v = re.sub(r'\s+#.*$', '', v)
            return k.strip(), v

        if indent == 0:
            if content.startswith('exits:'):
                context = 'exits'
                current_exit = None
            elif content.startswith('entry:'):
                context = 'entry'
            elif content.startswith('ports:'):
                context = 'ports'
            elif content.startswith('telegram:'):
                context = 'telegram'
            elif content.startswith('admin:'):
                context = 'admin'
            elif content.startswith('panel:'):
                context = 'panel'
            elif content.startswith('tgbot:'):
                context = 'tgbot'
            elif content.startswith('backup:'):
                context = 'backup'
                backup_sub = None
            elif content.startswith('youtube:'):
                context = 'youtube'
            elif content.startswith('sso:'):
                context = 'sso'
            else:
                k, v = kv(content)
                if v:
                    cfg[k] = v
        elif indent == 2:
            if context == 'exits':
                if content.startswith('- '):
                    k, v = kv(content[2:])
                    current_exit = {k: v}
                    cfg['exits'].append(current_exit)
                elif current_exit is not None:
                    k, v = kv(content)
                    current_exit[k] = v
            elif context == 'entry':
                k, v = kv(content)
                cfg['entry'][k] = v
            elif context == 'ports':
                k, v = kv(content)
                cfg['ports'][k] = v
            elif context == 'telegram':
                k, v = kv(content)
                cfg['telegram'][k] = v
            elif context == 'admin':
                k, v = kv(content)
                cfg['admin'][k] = v
            elif context == 'panel':
                k, v = kv(content)
                cfg['panel'][k] = v
            elif context == 'tgbot':
                k, v = kv(content)
                cfg['tgbot'][k] = v
            elif context == 'youtube':
                k, v = kv(content)
                cfg['youtube'][k] = v
            elif context == 'sso':
                k, v = kv(content)
                cfg['sso'][k] = v
            elif context == 'backup':
                # `local:` / `s3:` — суб-блок; иначе обычный ключ верхнего уровня
                if content.endswith(':') and ':' not in content[:-1]:
                    backup_sub = content[:-1]
                    cfg['backup'].setdefault(backup_sub, {})
                else:
                    k, v = kv(content)
                    cfg['backup'][k] = v
        elif indent == 4:
            if context == 'exits' and current_exit is not None:
                k, v = kv(content)
                current_exit[k] = v
            elif context == 'backup' and backup_sub is not None:
                k, v = kv(content)
                cfg['backup'][backup_sub][k] = v

    return cfg


def materialize_orchestrator_key(cfg):
    """Раскладывает orchestrator SSH-ключ из config.yaml в ~/.ssh/id_ed25519.

    Ключ хранится в config.yaml (поля orchestrator_key / orchestrator_pubkey),
    поэтому переживает пересоздание entry-ноды: новый entry с тем же config.yaml
    получит тот же ключ, и забутстрапленные ранее экзиты его уже знают.
    Если ключа в config.yaml нет — ничего не делаем, его сгенерит deploy.sh.
    """
    key_b64 = cfg.get('orchestrator_key', '')
    if not key_b64:
        return
    ssh_dir = Path.home() / '.ssh'
    ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    priv = ssh_dir / 'id_ed25519'
    priv.write_bytes(base64.b64decode(key_b64))
    priv.chmod(0o600)
    pub_line = cfg.get('orchestrator_pubkey', '')
    if pub_line:
        pub = ssh_dir / 'id_ed25519.pub'
        pub.write_text(pub_line.rstrip() + '\n')
        pub.chmod(0o644)
    print('orchestrator-ключ восстановлен из config.yaml')


def load_excluded_exits(repo_root):
    """Читает infra/state/excluded-exits.txt — список тегов экзитов, которые
    probe_udp_or_exclude() (в deploy.sh) пометил как недостижимые по UDP с
    RU направления. Эти экзиты исключаются из всех env-файлов на каждой
    регенерации envs (одиночные стадии тоже подхватывают)."""
    state_file = repo_root / 'infra' / 'state' / 'excluded-exits.txt'
    if not state_file.exists():
        return set()
    excluded = set()
    for line in state_file.read_text().splitlines():
        tag = line.strip()
        if tag and not tag.startswith('#'):
            excluded.add(tag)
    return excluded


def assign_mgmt_octets(repo_root, exits):
    """Закрепляет за тегом экзита последний октет MGMT_IP (10.99.0.N).

    Октет хранится в infra/state/mgmt-ips.txt (`тег октет`) и от порядка
    exits в config.yaml больше не зависит: убрали экзит из середины — у
    остальных адреса и mon-порты (10100+N) те же. Файла нет (первый деплой
    или обновление со старой версии) — засеваем прежней формулой «позиция в
    списке + 2», поэтому уже поставленные установки адресов не меняют.
    Новый тег берёт наименьший свободный октет; октет убранного тега
    остаётся за ним и никому не переходит.
    """
    state_file = repo_root / 'infra' / 'state' / 'mgmt-ips.txt'
    pinned = {}
    if state_file.exists():
        for line in state_file.read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and not parts[0].startswith('#') and parts[1].isdigit():
                pinned[parts[0]] = int(parts[1])
    else:
        pinned = {e['tag']: i for i, e in enumerate(exits, start=2)}

    changed = not state_file.exists()
    for e in exits:
        if e['tag'] not in pinned:
            used = set(pinned.values())
            pinned[e['tag']] = next(n for n in range(2, 255) if n not in used)
            changed = True
        e['_mgmt_idx'] = pinned[e['tag']]

    if changed:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            '# тег → последний октет MGMT_IP (10.99.0.N), пишет config2env.py.\n'
            '# Не перенумеровывать: на октет завязаны метки метрик и mon-порт.\n'
            + ''.join(f'{t} {n}\n' for t, n in sorted(pinned.items(), key=lambda kv: kv[1])))


def main():
    repo_root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    config_path = repo_root / 'config.yaml'

    if not config_path.exists():
        print('ERROR: config.yaml не найден. Запусти ./setup.sh', file=sys.stderr)
        sys.exit(1)

    cfg = parse_yaml(config_path.read_text())
    materialize_orchestrator_key(cfg)

    entry = cfg.get('entry', {})
    exits = cfg.get('exits', [])
    ports = cfg.get('ports', {})

    # MGMT_IP закреплён за тегом (до исключения по UDP-фильтру), а не за
    # позицией в списке: см. assign_mgmt_octets.
    assign_mgmt_octets(repo_root, exits)

    # Применить persistent UDP-фильтр от verify_and_rotate_ports. Эти экзиты
    # деплой не настраивает ни в одной стадии, пока verify не подтвердит
    # обратное (для re-include — удалить тег из excluded-exits.txt и
    # перезапустить ./deploy.sh; verify прогонится заново при do_all).
    excluded = load_excluded_exits(repo_root)
    if excluded:
        kept = [e for e in exits if e.get('tag') not in excluded]
        skipped = [e['tag'] for e in exits if e.get('tag') in excluded]
        if skipped:
            print(f'config2env: исключены по UDP-фильтру: {", ".join(skipped)} '
                  f'(infra/state/excluded-exits.txt) — '
                  f'их MGMT_IP-индексы зарезервированы и не переиспользуются')
        exits = kept

    if not entry.get('host'):
        print('ERROR: entry.host не задан в config.yaml', file=sys.stderr)
        sys.exit(1)
    if not exits:
        print('ERROR: exits пустой — нужна хотя бы одна выходная нода', file=sys.stderr)
        sys.exit(1)

    entry_host = entry['host']

    # Дефолтные порты
    hy2_direct = ports.get('hy2_direct', '443')
    hy2_warp   = ports.get('hy2_warp',   '8443')
    # Служебный Hysteria2 (скрейп node_exporter из-за границы вместо WG-mesh).
    hy2_mgmt   = ports.get('hy2_mgmt',   '8444')

    # Ключ человека-админа: если задан, 00-bootstrap отключает парольный вход;
    # если пусто — пароль остаётся (VPN2-31, чтобы не запереть себя без ключа).
    admin_cfg = cfg.get('admin', {}) or {}
    admin_ssh_pubkey = (admin_cfg.get('ssh_pubkey') or '').strip()

    envs_dir = repo_root / 'infra' / 'envs'
    envs_dir.mkdir(parents=True, exist_ok=True)

    exit_tags = ' '.join(e['tag'] for e in exits)

    # ------------------------------------------------------------------
    # all.env
    # ------------------------------------------------------------------
    lines = [
        f'ENTRY_HOST={shq(entry_host)}',
        '',
        'AGH_PORT=3000',
        '',
        f'HY2_DIRECT_PORT={hy2_direct}',
        f'HY2_WARP_PORT={hy2_warp}',
        f'HY2_MGMT_PORT={hy2_mgmt}',
        '',
        f'ADMIN_SSH_PUBKEY={shq(admin_ssh_pubkey)}',
        '',
        f'EXIT_TAGS="{exit_tags}"',
    ]

    # Адрес сервера в клиентских WG/OVPN-конфигах. Домен вместо IP: при смене
    # IP entry хватает A-записи, конфиги у клиентов не перевыпускаются.
    public_host = str(entry.get('public_host') or '').strip()
    if public_host:
        lines += ['', f'WG_PUBLIC_HOST={shq(public_host)}', f'OVPN_PUBLIC_HOST={shq(public_host)}']

    # Per-exit server IP (используется как адрес для подключения Hysteria2)
    for ex in exits:
        tag = ex['tag'].upper()
        lines.append(f"DOMAIN_{tag}={ex['host']}")

    # Per-host MGMT IPs (октет закреплён за тегом, см. assign_mgmt_octets).
    lines.append('')
    lines.append('MGMT_IP_RU=10.99.0.1')
    for ex in exits:
        tag = ex['tag'].upper()
        lines.append(f"MGMT_IP_{tag}=10.99.0.{ex['_mgmt_idx']}")

    # Per-exit Hysteria2 direct port
    lines.append('')
    for ex in exits:
        tag = ex['tag'].upper()
        port = ex.get('hy2_direct_port', hy2_direct)
        lines.append(f'{tag}_HY2_DIRECT_PORT={port}')

    (envs_dir / 'all.env').write_text('\n'.join(lines) + '\n')

    # ------------------------------------------------------------------
    # ru.env (entry node)
    # ------------------------------------------------------------------
    entry_pass = entry.get('password', '')
    if not entry_pass:
        print('ERROR: entry.password не задан в config.yaml — перезапусти ./setup.sh',
              file=sys.stderr)
        sys.exit(1)

    admin = cfg.get('admin', {})
    admin_user = admin.get('user', 'anysda') or 'anysda'
    admin_password = admin.get('password', '')  # пусто → auto-gen в 22-adguard

    panel_domain = cfg.get('panel', {}).get('domain', '')
    # Образ панели. Пусто → дефолт стадии 30 (…/panel:dev). Пин нужен, когда на
    # проде осознанно крутится не dev-линия: без него обычный прогон 30-frontend
    # молча утащит панель обратно на :dev.
    panel_image = cfg.get('panel', {}).get('image', '')
    # Образ бота — то же для стадии 35-telegram (…/tgbot:dev).
    tgbot_image = cfg.get('tgbot', {}).get('image', '')
    # Адрес кнопки «AdGuard Home» в панели. Пусто → https://<домен>:3001.
    # Нужен, когда UI AdGuard открыт только изнутри VPN (напр. http://10.99.0.1:3080).
    agh_ui_url = cfg.get('panel', {}).get('adguard_url', '')

    lines = [
        "HOST_TAG='ru'",
        '',
        "SSH_USER='root'",
        f"SSH_HOST={shq(entry_host)}",
        f"SSH_PASS={shq(entry_pass)}",
        '',
        f"PUB_IP_OUT={shq(entry_host)}",
        '',
        "MGMT_IP='10.99.0.1'",
        '',
        f"ADMIN_USER={shq(admin_user)}",
        f"ADMIN_PASSWORD={shq(admin_password)}",
        f"PANEL_DOMAIN={shq(panel_domain)}",
    ]
    if panel_image:
        lines.append(f"PANEL_IMAGE={shq(panel_image)}")
    if tgbot_image:
        lines.append(f"TGBOT_IMAGE={shq(tgbot_image)}")
    if agh_ui_url:
        lines.append(f"AGH_UI_URL={shq(agh_ui_url)}")
    tg = cfg.get('telegram', {})
    if tg.get('bot_token') and tg.get('chat_id'):
        lines += [
            '',
            f"TELEGRAM_BOT_TOKEN={shq(tg['bot_token'])}",
            f"TELEGRAM_CHAT_ID={shq(tg['chat_id'])}",
        ]
        if tg.get('admin_username'):
            lines.append(f"TELEGRAM_ADMIN_USERNAME={shq(tg['admin_username'])}")


    # YouTube-секция. Читают обе стадии: 20-ru-router (gen-router-config.py
    # строит outbound youtube-ru и правила) и 19-yt-zapret (десинк). Пустая
    # секция → YT_ROUTE=off, поведение ровно как до появления фичи.
    yt = cfg.get('youtube', {})
    yt_route = (yt.get('route', '') or 'off').lower()
    if yt_route not in ('off', 'zapret', 'direct'):
        print(f"ERROR: youtube.route='{yt_route}' — допустимо off | zapret | direct",
              file=sys.stderr)
        sys.exit(1)
    yt_quic = (yt.get('quic', '') or 'block').lower()
    if yt_quic not in ('block', 'allow'):
        print(f"ERROR: youtube.quic='{yt_quic}' — допустимо block | allow", file=sys.stderr)
        sys.exit(1)
    lines += [
        '',
        f"YT_ROUTE={shq(yt_route)}",
        f"YT_QUIC={shq(yt_quic)}",
        f"YT_MARK={shq(yt.get('mark', '256') or '256')}",
        f"YT_ZAPRET_OFFLOAD={shq((yt.get('offload', '') or 'keep').lower())}",
    ]
    # Необязательные переопределения — пишем только если заданы, чтобы дефолты
    # жили в одном месте (в стадии 19), а не размножались по конфигам.
    if yt.get('strategy'):
        lines.append(f"YT_ZAPRET_STRATEGY={shq(yt['strategy'])}")
    if yt.get('zapret_ref'):
        lines.append(f"YT_ZAPRET_REF={shq(yt['zapret_ref'])}")
    if yt.get('domains'):
        lines.append(f"YT_DOMAINS={shq(yt['domains'])}")

    # SSO-секция (Authentik / OIDC). Читает стадия 30-frontend. Пустая секция
    # или enabled=false → SSO_ENABLED='false', панель ведёт себя ровно как до
    # появления фичи (форма с логином и паролем).
    sso = cfg.get('sso', {})
    sso_on = (sso.get('enabled', '') or '').lower() in ('true', 'yes', 'y', '1')
    lines += ['', f"SSO_ENABLED={shq('true' if sso_on else 'false')}"]
    if sso_on:
        # discovery_url можно задать целиком; иначе собираем из адреса IdP и
        # slug'а приложения — у Authentik ручка ВСЕГДА такая.
        discovery = sso.get('discovery_url', '')
        if not discovery:
            base = (sso.get('base_url', '') or '').rstrip('/')
            slug = sso.get('app_slug', '') or 'vpn2'
            if not base:
                print('ERROR: sso.enabled=true, но не задан ни sso.discovery_url, '
                      'ни sso.base_url', file=sys.stderr)
                sys.exit(1)
            discovery = f'{base}/application/o/{slug}/.well-known/openid-configuration'
        client_id = sso.get('client_id', '') or 'vpn2'
        client_secret = sso.get('client_secret', '')
        allowed_subs = sso.get('allowed_subs', '')
        if not client_secret:
            print('ERROR: sso.client_secret не задан — панель не сможет обменять '
                  'код на токен', file=sys.stderr)
            sys.exit(1)
        if not allowed_subs:
            # Пустой список — не «всех пустить», а «никого». Молча выкатывать
            # такое нельзя: вход просто перестанет работать, и искать причину
            # будешь в Authentik, а она здесь.
            print('ERROR: sso.allowed_subs пуст — войти не сможет никто. Укажи uuid '
                  'пользователя Authentik (sub), см. docs/sso.md', file=sys.stderr)
            sys.exit(1)
        lines += [
            f'SSO_DISCOVERY_URL={shq(discovery)}',
            f'SSO_CLIENT_ID={shq(client_id)}',
            f'SSO_CLIENT_SECRET={shq(client_secret)}',
            f'SSO_ALLOWED_SUBS={shq(allowed_subs)}',
            f"SSO_LABEL={shq(sso.get('label', '') or 'Authentik')}",
            # Бесшовность (форму не видно вообще). false — форма остаётся, но с
            # кнопкой входа через IdP.
            f"SSO_AUTO_REDIRECT={shq('false' if (sso.get('auto_redirect', '') or '').lower() in ('false', 'no', 'n', '0') else 'true')}",
            # Парольный вход. Стандарт флота — «спрятан, но жив», поэтому по
            # умолчанию true; false рубит его наглухо (и break-glass тоже!).
            f"SSO_PASSWORD_LOGIN={shq('false' if (sso.get('password_login', '') or '').lower() in ('false', 'no', 'n', '0') else 'true')}",
        ]

    # Backup-секция. Выгружаем как BACKUP_* переменные; пустая backup секция /
    # enabled=false → ничего не пишем, 26-backup увидит отсутствие и пропустится.
    backup = cfg.get('backup', {})
    if backup.get('enabled', '').lower() in ('true', 'yes', 'y', '1'):
        lines += [
            '',
            "BACKUP_ENABLED='true'",
            f"BACKUP_BACKEND={shq(backup.get('backend', 'local'))}",
            f"BACKUP_PASSPHRASE={shq(backup.get('passphrase', ''))}",
            f"BACKUP_RETENTION={shq(backup.get('retention', '10'))}",
            f"BACKUP_SCHEDULE={shq(backup.get('schedule', 'daily'))}",
            f"BACKUP_LOCAL_DIR={shq(backup.get('local', {}).get('dir', '/var/backups/anysda-vpn2'))}",
        ]
        s3 = backup.get('s3', {})
        if backup.get('backend') == 's3' and s3:
            lines += [
                f"BACKUP_S3_ENDPOINT={shq(s3.get('endpoint', ''))}",
                f"BACKUP_S3_BUCKET={shq(s3.get('bucket', ''))}",
                # region — обязателен для cloud.ru/yandex/selectel и т.п. (SigV4).
                # Пустое значение оставит дефолт aws-cli (us-east-1).
                f"BACKUP_S3_REGION={shq(s3.get('region', ''))}",
                f"BACKUP_S3_ACCESS_KEY={shq(s3.get('access_key', ''))}",
                f"BACKUP_S3_SECRET_KEY={shq(s3.get('secret_key', ''))}",
            ]

    ru_env = envs_dir / 'ru.env'
    ru_env.write_text('\n'.join(lines) + '\n')
    ru_env.chmod(0o600)

    # ------------------------------------------------------------------
    # Per-exit env files (октет закреплён за тегом, см. assign_mgmt_octets).
    # ------------------------------------------------------------------
    for ex in exits:
        tag      = ex['tag']
        host     = ex['host']
        password = ex.get('password', '')
        mgmt_ip  = f"10.99.0.{ex['_mgmt_idx']}"
        hy2_port = ex.get('hy2_direct_port', hy2_direct)

        if not password:
            print(f'ERROR: exits[{tag}].password не задан — перезапусти ./setup.sh',
                  file=sys.stderr)
            sys.exit(1)

        lines = [
            f"HOST_TAG={shq(tag)}",
            '',
            "SSH_USER='root'",
            f"SSH_HOST={shq(host)}",
            f"SSH_PASS={shq(password)}",
            '',
            f"PUB_IP={shq(host)}",
            '',
            f"MGMT_IP={shq(mgmt_ip)}",
        ]
        if str(hy2_port) != str(hy2_direct):
            lines.append(f'HY2_DIRECT_PORT={hy2_port}')

        ex_env = envs_dir / f'{tag}.env'
        ex_env.write_text('\n'.join(lines) + '\n')
        ex_env.chmod(0o600)

    # ------------------------------------------------------------------
    # exits.env
    # ------------------------------------------------------------------
    (envs_dir / 'exits.env').write_text(f'EXIT_TAGS="{exit_tags}"\n')

    print(f'Сгенерировано: all.env, ru.env, '
          f'{", ".join(e["tag"] + ".env" for e in exits)}, exits.env')


if __name__ == '__main__':
    main()
