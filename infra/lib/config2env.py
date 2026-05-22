#!/usr/bin/env python3
"""
config2env.py — читает config.yaml, пишет infra/envs/*.env

Использование:
    python3 infra/lib/config2env.py [repo_root]
"""

import base64
import sys
from pathlib import Path


def parse_yaml(text):
    """Минимальный парсер для нашего фиксированного формата config.yaml."""
    cfg = {'exits': [], 'ports': {}, 'entry': {}, 'telegram': {}, 'admin': {}, 'panel': {}}
    context = None
    current_exit = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith('#'):
            continue

        indent = len(line) - len(line.lstrip())
        content = line.lstrip()

        def kv(s):
            k, _, v = s.partition(':')
            return k.strip(), v.strip().strip("'\"")

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
        elif indent == 4:
            if context == 'exits' and current_exit is not None:
                k, v = kv(content)
                current_exit[k] = v

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
    mgmt_port  = ports.get('mgmt',       '51900')

    envs_dir = repo_root / 'infra' / 'envs'
    envs_dir.mkdir(parents=True, exist_ok=True)

    exit_tags = ' '.join(e['tag'] for e in exits)

    # ------------------------------------------------------------------
    # all.env
    # ------------------------------------------------------------------
    lines = [
        f'ENTRY_HOST={entry_host}',
        '',
        f'MGMT_NET=10.99.0.0/24',
        f'MGMT_PORT={mgmt_port}',
        '',
        f'AGH_PORT=3000',
        '',
        f'HY2_DIRECT_PORT={hy2_direct}',
        f'HY2_WARP_PORT={hy2_warp}',
        '',
        f'EXIT_TAGS="{exit_tags}"',
    ]

    # Per-exit server IP (используется как адрес для подключения Hysteria2)
    for ex in exits:
        tag = ex['tag'].upper()
        lines.append(f"DOMAIN_{tag}={ex['host']}")

    # Per-host MGMT IPs
    lines.append('')
    lines.append('MGMT_IP_RU=10.99.0.1')
    for i, ex in enumerate(exits, start=2):
        tag = ex['tag'].upper()
        lines.append(f'MGMT_IP_{tag}=10.99.0.{i}')

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

    lines = [
        "HOST_TAG='ru'",
        "HOST_LABEL='RU entry'",
        '',
        "SSH_USER='root'",
        f"SSH_HOST='{entry_host}'",
        f"SSH_PASS='{entry_pass}'",
        '',
        f"PUB_IP_IN='{entry_host}'",
        f"PUB_IP_OUT='{entry_host}'",
        '',
        "MGMT_IP='10.99.0.1'",
        '',
        f"ADMIN_USER='{admin_user}'",
        f"ADMIN_PASSWORD='{admin_password}'",
        f"PANEL_DOMAIN='{panel_domain}'",
    ]
    tg = cfg.get('telegram', {})
    if tg.get('bot_token') and tg.get('chat_id'):
        lines += [
            '',
            f"TELEGRAM_BOT_TOKEN='{tg['bot_token']}'",
            f"TELEGRAM_CHAT_ID='{tg['chat_id']}'",
        ]
        if tg.get('admin_username'):
            lines.append(f"TELEGRAM_ADMIN_USERNAME='{tg['admin_username']}'")

    ru_env = envs_dir / 'ru.env'
    ru_env.write_text('\n'.join(lines) + '\n')
    ru_env.chmod(0o600)

    # ------------------------------------------------------------------
    # Per-exit env files
    # ------------------------------------------------------------------
    for i, ex in enumerate(exits, start=2):
        tag      = ex['tag']
        host     = ex['host']
        password = ex.get('password', '')
        mgmt_ip  = f'10.99.0.{i}'
        hy2_port = ex.get('hy2_direct_port', hy2_direct)

        if not password:
            print(f'ERROR: exits[{tag}].password не задан — перезапусти ./setup.sh',
                  file=sys.stderr)
            sys.exit(1)

        lines = [
            f"HOST_TAG='{tag}'",
            f"HOST_LABEL='{tag.upper()} exit'",
            '',
            "SSH_USER='root'",
            f"SSH_HOST='{host}'",
            f"SSH_PASS='{password}'",
            '',
            f"PUB_IP='{host}'",
            '',
            f"MGMT_IP='{mgmt_ip}'",
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
