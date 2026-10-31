#!/usr/bin/env bash
# anysda-vpn2 backup — снимает зашифрованный архив всего необходимого для
# полного восстановления entry-ноды с нуля. Запускается:
#   - вручную: /usr/local/bin/anysda-backup.sh
#   - systemd таймером anysda-backup.timer (если backup.schedule: daily)
#   - через `./deploy.sh backup` (диспетчится в ssh_exec на entry)
#
# Архив содержит:
#   - db.sqlite        — через sqlite3 .backup (без даунтайма панели)
#   - etc-anysda/      — секреты панели/бота (admin-password, tgbot-secret,
#                        clash-secret, telegram-runtime.json, telegram-from-config.json,
#                        manual-routes, session-secret, anysda-config.yaml)
#   - etc-wireguard/   — wg0.conf + server.priv/server.pub (ключи для панели)
#   - etc-openvpn/     — CA + серверный сертификат + ccd/ + crl.pem
#   - etc-strongswan/  — IKEv2 CA + server cert/key
#   - etc-swanctl/     — IKEv2 conn-конфиг + загруженные creds
#   - caddy-storage/   — сертификаты, приватные ключи и ACME-аккаунты Caddy
#   - manifest.json    — версии + SHA-256 каждого компонента
#
# Plaintext tar.gz живёт ТОЛЬКО в mktemp-dir chmod 700, удаляется trap EXIT.
# На диск/в S3 попадает только *.tar.gz.age (age symmetric encrypt).
#
# Источник passphrase: /etc/anysda/backup.env → ANYSDA_BACKUP_PASSPHRASE.
# Файл (chmod 600) рендерится стадией 26-backup из config.yaml.
#
# Код выхода: 0 — архив снят; 4 — архив снят с предупреждением (строка
# warning:): БД и конфиги VPN расходятся во всех срезах (перечень устройств
# в выводе) или сверка не выполнена — нечем спросить wg; остальное — архива нет.

set -euo pipefail
umask 077

# Клон, из которого шёл деплой (отметку пишет стадия 26-backup); старые
# установки без отметки держат исходники в /opt/anysda-vpn2.
SOURCE_ROOT=$(cat /etc/anysda/source-root 2>/dev/null || true)
SOURCE_ROOT=${SOURCE_ROOT:-/opt/anysda-vpn2}

# Неудачный бэкап — тревога в ленту «События» панели, архив с расхождением
# (код 4) — предупреждение. Best-effort: панели может и не быть, на исход
# бэкапа это не влияет. Секрет идёт в curl через stdin, чтобы не светиться
# в списке процессов.
FAIL_REASON=
WARNING=
report_failure() {
  local rc=$1 secret=/etc/anysda/tgbot-secret.txt type=backup_failed detail=${FAIL_REASON:-код выхода $1} event
  [[ $rc -ne 0 && -r $secret ]] || return 0
  if [[ $rc -eq 4 && -n $WARNING ]]; then type=backup_warning; detail=$WARNING; fi
  # Причина бывает чужим выводом (wg, unshare): управляющие и не-UTF-8 байты
  # заменяются, длина - не больше 200 знаков UTF-16, как в схеме панели
  # (app/server/api/bot/events.post.ts), иначе панель отвергнет всё событие.
  event=$(python3 -I -c '
import json, os, sys
text = os.fsencode(sys.argv[2]).decode("utf-8", "replace")
text = " ".join("".join(c if c.isprintable() else " " for c in text).split())[:200]
while len(text.encode("utf-16-le")) > 400:
    text = text[:-1]
event = {"type": sys.argv[1], "detail": text.strip()}
sys.stdout.buffer.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode())' "$type" "$detail") || return 0
  printf 'Authorization: Bearer %s\n' "$(cat "$secret")" | curl -fsS --max-time 5 -H @- \
    -H 'Content-Type: application/json' -d "$event" \
    http://127.0.0.1:51821/api/bot/events >/dev/null 2>&1 || true
}
trap 'report_failure $?' EXIT

# Сверка среза с БД того же среза: каждый действующий сертификат OpenVPN не
# отозван в CRL, у каждого действующего устройства есть пир WireGuard и учётка
# IKEv2, у отключённого и удалённого их нет. Иначе восстановленный клиент
# получит «certificate revoked», не подключится или подключится без доступа.
# Зовёт и restore: anysda-backup.sh --check-bundle <распакованный бандл>.
# Пиры wg0.conf - wg_conf.py из ANYSDA_LIB_DIR, без неё - из каталога библиотек
# узла, куда его ставит 26-backup (не рядом со скриптом и не из текущего
# каталога, `python3 -P`: в /usr/local/bin старый).
check_bundle() {
  python3 -P - "$1" "${ANYSDA_LIB_DIR:-/usr/local/lib/anysda}" <<'PY'
import base64, calendar, json, pathlib, re, sqlite3, sys, time

root = pathlib.Path(sys.argv[1])
sys.path[:0] = [sys.argv[2]]
db = root / 'db.sqlite'
if not db.is_file():
    sys.exit(0)
con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)


def columns(table):
    return {row[1] for row in con.execute(f'PRAGMA table_info({table})')}


device_columns = columns('devices')
deleting = 'd.deleting' if 'deleting' in device_columns else '0'
bad, nocheck = [], []
names = dict(con.execute('SELECT id, name FROM devices')) if 'name' in device_columns else {}


def label(device):
    """Номер и название устройства, как их ищут в панели."""
    name = ' '.join(str(names.get(device) or '').split())
    return f'устройство {device} «{name}»' if name else f'устройство {device}'


def tlv(data, i=0):
    tag, size = data[i], data[i + 1]
    i += 2
    if size & 0x80:
        width = size & 0x7f
        size, i = int.from_bytes(data[i:i + width], 'big'), i + width
    return tag, data[i:i + size], i + size


def children(data):
    i = 0
    while i < len(data):
        tag, value, i = tlv(data, i)
        yield tag, value


def der(pem, label):
    match = re.search(f'-----BEGIN {label}-----(.*?)-----END {label}-----', pem or '', re.S)
    return base64.b64decode(''.join(match.group(1).split())) if match else None


def serial(pem):
    data = der(pem, 'CERTIFICATE')
    if data is None:
        return None
    tbs = tlv(tlv(data)[1])[1]
    items = list(children(tbs))
    return int.from_bytes(items[1 if items[0][0] == 0xa0 else 0][1], 'big')


crl = root / 'etc-openvpn/server/pki/crl.pem'
if crl.is_file() and 'ovpn_cert' in device_columns:
    revoked = set()
    tbs = tlv(tlv(der(crl.read_text(), 'X509 CRL'))[1])[1]
    for tag, value in children(tbs):
        # Список отзыва — единственная SEQUENCE из SEQUENCE{INTEGER серийник, ...}.
        entries = [next(children(v)) for t, v in children(value) if t == 0x30] if tag == 0x30 else []
        if entries and len(entries) == len(list(children(value))) and all(t == 0x02 for t, _ in entries):
            revoked |= {int.from_bytes(v, 'big') for _, v in entries}
    for device, cert in con.execute(
            f'SELECT d.id, d.ovpn_cert FROM devices d WHERE d.ovpn_cert IS NOT NULL AND {deleting} = 0'):
        if (number := serial(cert)) in revoked:
            bad.append(f'{label(device)}: сертификат {number:X} отозван в CRL')

# Пиры WireGuard и учётки IKEv2 панель пишет из БД тем же правилом: устройство
# не удаляется, клиент не заморожен и срок не истёк, ключи выданы целиком.
# Срок считается на момент среза (у restore — время из манифеста). Переходные
# устройства не сверяются: клиент с незавершённой операцией доступа (очередь в
# БД доведёт её после восстановления) и срок, истекающий в пределах двух минут
# от среза (отключение по сроку ставит в очередь cron раз в минуту).
now = time.time()
try:
    now = calendar.timegm(time.strptime(
        json.loads((root / 'manifest.json').read_text())['timestamp'], '%Y-%m-%dT%H%M%SZ'))
except (OSError, ValueError, KeyError, TypeError):
    pass
pending, retired_keys, retired_devices = set(), set(), set()
if {'client_id', 'status', 'payload'} <= columns('access_operations'):
    for client, payload in con.execute(
            "SELECT client_id, payload FROM access_operations WHERE status = 'pending'"):
        pending.add(client)
        try:
            retired = json.loads(payload).get('retired') or []
        except (ValueError, AttributeError):
            retired = []
        retired_keys |= {old.get('wgPublicKey') for old in retired if isinstance(old, dict)}
        retired_devices |= {old.get('id') for old in retired if isinstance(old, dict)}
joined = 'client_id' in device_columns and {'id', 'frozen_manual', 'expires_at'} <= columns('clients')


def devices(*fields):
    """(устройство, должен быть в конфиге: True/False, None — переходное, поля)."""
    names = ', '.join('d.' + field for field in fields)
    query = (f'SELECT d.id, {deleting}, c.frozen_manual, c.expires_at, c.id, {names} '
             'FROM devices d LEFT JOIN clients c ON c.id = d.client_id') if joined else (
             f'SELECT d.id, {deleting}, 0, NULL, 0, {names} FROM devices d')
    for device, removing, frozen, expires, client, *values in con.execute(query):
        if client in pending or (expires is not None and abs(expires - now) <= 120):
            yield device, None, values
        else:
            active = client is not None and not removing and not frozen and (expires is None or expires > now)
            yield device, bool(active and all(values)), values


wg = root / 'etc-wireguard/wg0.conf'
if wg.is_file() and {'wg_public_key', 'wg_preshared_key', 'wg_ip'} <= device_columns:
    # Пиры берёт сам wg во временном netns, одним путём с verify-traffic.py - wg_conf.py.
    # Файл, который wg отвергает, - жалоба вместо сверки; нечем спросить wg - сверка не выполнена.
    from wg_conf import conf_peers
    addresses = {}
    try:
        peers = {peer['key']: peer for peer in conf_peers(wg.read_bytes())}
    except ValueError as error:
        (nocheck if 'проверить нечем' in str(error) else bad).append(str(error))
    else:
        addresses = {key: set(peer['allowed']) for key, peer in peers.items()}
        known = set()
        for device, wanted, (key, psk, ip) in devices('wg_public_key', 'wg_preshared_key', 'wg_ip'):
            known.add(key)
            # Ключ из середины цепочки перевыпусков в retired не хранится: пир на
            # адресе переходного устройства очередь заменит сама.
            if wanted is None and ip:
                known |= {other for other, allowed in addresses.items() if f'{ip}/32' in allowed}
            if wanted and key not in peers:
                bad.append(f'{label(device)}: нет пира WireGuard')
            elif wanted and peers[key]['preshared_key'] != psk:
                bad.append(f'{label(device)}: ключ пира WireGuard не совпадает с БД')
            elif wanted is False and key in peers:
                bad.append(f'{label(device)}: пир WireGuard у отключённого устройства')
        for key in sorted(set(peers) - known - retired_keys):
            bad.append(f'пир WireGuard {key}: устройства нет в БД')

swan = root / 'etc-swanctl/conf.d/anysda-clients.conf'
if swan.is_file() and {'ikev2_username', 'ikev2_password', 'ikev2_ip'} <= device_columns:
    text = swan.read_text()
    conns = {int(n) for n in re.findall(r'(?m)^\s*anysda-dev-(\d+)\s*:\s*connections\.', text)}
    secrets = {int(n): (user, password) for n, user, password in
               re.findall(r'eap-(\d+)\s*\{\s*id\s*=\s*"([^"]*)"\s*secret\s*=\s*"([^"]*)"', text)}
    known = set()
    for device, wanted, (user, password, _) in devices('ikev2_username', 'ikev2_password', 'ikev2_ip'):
        known.add(device)
        if wanted and device not in conns:
            bad.append(f'{label(device)}: нет учётки IKEv2')
        elif wanted and secrets.get(device) != (user, password):
            bad.append(f'{label(device)}: учётка IKEv2 не совпадает с БД')
        elif wanted is False and device in conns:
            bad.append(f'{label(device)}: учётка IKEv2 у отключённого устройства')
    for device in sorted(conns - known - retired_devices):
        bad.append(f'{label(device)}: учётка IKEv2 есть, устройства нет в БД')

# Код 3 - расхождение, 5 - сверку нечем выполнить (не расхождение).
for line in bad + nocheck:
    print(line)
sys.exit(3 if bad else 5 if nocheck else 0)
PY
}
if [[ ${1:-} == --check-bundle ]]; then
  trap - EXIT
  # Бандл узнаётся по manifest.json (он есть в архиве любой версии, db.sqlite —
  # нет). Без проверки путь мимо бандла давал «расхождений нет».
  if [[ ! -f ${2:-}/manifest.json ]]; then
    echo "anysda-backup: --check-bundle: '${2:-}' — не распакованный бандл (нет manifest.json); укажи каталог anysda-vpn2-* из архива" >&2
    exit 2
  fi
  check_bundle "$2"
  exit
fi

ENV_FILE=/etc/anysda/backup.env
[[ -r "$ENV_FILE" ]] || { echo "anysda-backup: $ENV_FILE отсутствует или unreadable" >&2; exit 2; }
# shellcheck disable=SC1090
source "$ENV_FILE"

: "${BACKUP_BACKEND:?BACKUP_BACKEND не задан в $ENV_FILE}"
: "${BACKUP_LOCAL_DIR:=/var/backups/anysda-vpn2}"
: "${BACKUP_RETENTION:=10}"

# ── fail-loud: passphrase отсутствует ───────────────────────────────────────
# Должно произойти ДО любых tar/age операций. Падаем с человеческим сообщением,
# отправляем Telegram alert если бот сконфигурирован, exit non-zero.
if [[ -z "${ANYSDA_BACKUP_PASSPHRASE:-}" ]]; then
  cat >&2 <<-EOM
	anysda-backup FATAL: ANYSDA_BACKUP_PASSPHRASE отсутствует или пуста.

	Ожидается в $ENV_FILE. Источник:
	  config.yaml (backup.passphrase)  →  config2env.py  →
	  стадия 26-backup рендерит /etc/anysda/backup.env (chmod 600)  →
	  source в Bash-скрипте anysda-backup.sh (вручную или из systemd).

	Действия:
	  1. Запусти ./setup.sh (на оркестраторе) — заполни backup.passphrase.
	  2. ./deploy.sh 26-backup ru — стадия пересоздаст $ENV_FILE.
	  3. Перезапусти бэкап вручную: /usr/local/bin/anysda-backup.sh
	EOM
  # Бот алерт — best-effort, не блокирует exit
  if [[ -r /etc/anysda/tgbot-secret.txt ]]; then
    curl -fsS --max-time 5 \
      -H 'Content-Type: application/json' \
      -H "X-Tgbot-Secret: $(cat /etc/anysda/tgbot-secret.txt)" \
      -d '{"type":"system_alert","subject":"backup failed","detail":"ANYSDA_BACKUP_PASSPHRASE missing"}' \
      http://127.0.0.1:8877/event >/dev/null 2>&1 || true
  fi
  FAIL_REASON='нет пароля шифрования'
  exit 2
fi

# ── Подготовка ──────────────────────────────────────────────────────────────
mkdir -p "$BACKUP_LOCAL_DIR"
chmod 700 "$BACKUP_LOCAL_DIR"

# WORK = encrypted-only scratch. shred всё перед rm, trap не пропускает падения.
WORK=$(mktemp -d /var/lib/anysda-vpn2/.backup-tmp.XXXXXX)
chmod 700 "$WORK"
cleanup() {
  if [[ -d "$WORK" ]]; then
    # shred всё внутри, потом rm. shred из coreutils есть везде.
    find "$WORK" -type f -exec shred -uz {} + 2>/dev/null || true
    rm -rf "$WORK"
  fi
}
trap 'rc=$?; cleanup; report_failure $rc' EXIT

# ── Снимок данных ───────────────────────────────────────────────────────────
TS=$(date -u +%Y-%m-%dT%H%M%SZ)
BUNDLE="anysda-vpn2-$TS"
BUNDLE_DIR="$WORK/$BUNDLE"
mkdir -p "$BUNDLE_DIR"

# 2. /etc/anysda — все секреты
if [[ -d /etc/anysda ]]; then
  cp -a /etc/anysda "$BUNDLE_DIR/etc-anysda"
  # App runtime moved out of /etc. Keep archive format portable and compatible
  # with old restorers: copy only these two regular app files, never symlink targets.
  for name in sso-runtime.json telegram-runtime.json; do
    if [[ -L "$BUNDLE_DIR/etc-anysda/$name" ]]; then
      rm -f "$BUNDLE_DIR/etc-anysda/$name"
      runtime=/var/lib/anysda-vpn2/runtime/$name
      [[ ! -L "$runtime" ]] || { echo 'backup: runtime must be a regular file' >&2; exit 1; }
      [[ ! -f "$runtime" ]] || cp -p "$runtime" "$BUNDLE_DIR/etc-anysda/$name"
    fi
  done
fi

# 3. WireGuard: снимается вместе с БД (1), PKI OpenVPN (4) и IKEv2 (5) одним
# срезом под замком PKI, который держат выдача и отзыв сертификатов в vpn2-control.
# Выдача, отзыв и перевыпуск в панели — запись в БД и применение конфига
# отдельными шагами, поэтому срез ещё сверяется (БД против CRL, пиров WireGuard
# и учёток IKEv2) и при расхождении снимается заново.
DB=/var/lib/anysda-vpn2/db.sqlite
PKI_LOCK=/etc/openvpn/server/.pki.lock
snapshot_access() {
  local lock=
  rm -rf "$BUNDLE_DIR/db.sqlite" "$BUNDLE_DIR/etc-wireguard" "$BUNDLE_DIR/etc-openvpn" \
    "$BUNDLE_DIR/etc-strongswan" "$BUNDLE_DIR/etc-swanctl"
  if [[ -e "$PKI_LOCK" ]]; then
    exec {lock}<"$PKI_LOCK"
    flock -w 60 -s "$lock" || { FAIL_REASON='замок PKI OpenVPN занят дольше минуты'; exit 1; }
  fi
  # SQLite — консистентный снимок (без даунтайма)
  if [[ -r "$DB" ]]; then
    sqlite3 "$DB" ".timeout 10000" ".backup '$BUNDLE_DIR/db.sqlite'"
  else
    echo "anysda-backup: WARN: $DB не найден — пропускаю db.sqlite" >&2
  fi
  # WireGuard: конфиг и исходная пара ключей стадии 28, которую читает панель.
  mkdir -p "$BUNDLE_DIR/etc-wireguard"
  for file in wg0.conf server.priv server.pub; do
    [[ ! -f /etc/wireguard/$file ]] || cp -aL "/etc/wireguard/$file" "$BUNDLE_DIR/etc-wireguard/"
  done
  # OpenVPN PKI + конфиг + CCD + CRL
  if [[ -d /etc/openvpn ]]; then
    cp -a /etc/openvpn "$BUNDLE_DIR/etc-openvpn"
  fi
  # strongSwan (IKEv2) — CA key/cert + server cert/key + swanctl conf'ы с
  # учётками устройств. Без CA клиенты не подключатся (он зашит в их
  # .mobileconfig'ах).
  if [[ -d /etc/strongswan ]]; then
    cp -a /etc/strongswan "$BUNDLE_DIR/etc-strongswan"
  fi
  if [[ -d /etc/swanctl ]]; then
    cp -a /etc/swanctl "$BUNDLE_DIR/etc-swanctl"
  fi
  if [[ -n $lock ]]; then exec {lock}<&-; fi
}
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  snapshot_access
  rc=0
  mismatch=$(check_bundle "$BUNDLE_DIR") || rc=$?
  [[ $rc != 0 ]] || break
  if [[ $rc == 5 ]]; then
    # Нечем спросить wg - не расхождение: архив сохраняем, перевыпуск не нужен.
    echo "anysda-backup: WARN: сверка БД с конфигами VPN не выполнена: $mismatch" >&2
    WARNING="сверка БД с конфигами VPN не выполнена: ${mismatch//$'\n'/; }"
    break
  fi
  if [[ $rc != 3 ]]; then
    echo "anysda-backup: WARN: сверка БД с конфигами VPN не выполнена (код $rc)" >&2
    WARNING="сверка БД с конфигами VPN не выполнена (код $rc)"
    break
  fi
  if [[ $attempt == 10 ]]; then
    # Устойчивое расхождение (упала синхронизация конфигов) — не повод
    # остаться без бэкапов: архив сохраняем с предупреждением, устройства
    # перевыпускают в панели.
    echo "anysda-backup: WARN: БД и конфиги VPN расходятся в 10 срезах подряд — сохраняю архив как есть; перевыпусти эти устройства в панели:" >&2
    printf '%s\n' "$mismatch" >&2
    # В ленту событий — только номера (панель режет подробности длиннее 200 символов).
    mapfile -t ids < <(grep -oE '^устройство [0-9]+' <<< "$mismatch" | awk '{print $2}' | sort -un || true)
    MISMATCH_IDS=$(printf '%s, ' "${ids[@]:0:10}"); MISMATCH_IDS=${MISMATCH_IDS%, }
    (( ${#ids[@]} <= 10 )) || MISMATCH_IDS+=" и ещё $(( ${#ids[@]} - 10 ))"
    WARNING="БД и конфиги VPN расходятся${MISMATCH_IDS:+ (устройства: $MISMATCH_IDS)} — перевыпусти в панели"
    break
  fi
  echo "anysda-backup: срез $attempt: БД и конфиги VPN расходятся (идёт выдача или отзыв?), снимаю заново:" >&2
  printf '%s\n' "$mismatch" >&2
  sleep 3
done

# 5. Настройки starter/charon и автозагрузка swanctl лежат вне этих каталогов.
for name in strongswan.conf strongswan.d ipsec.conf ipsec.secrets; do
  if [[ -e "/etc/$name" ]]; then
    cp -a "/etc/$name" "$BUNDLE_DIR/etc-$name"
  fi
done

# 6. Всё постоянное хранилище Caddy, в том числе ACME account keys.
# Без домена/до первого запуска каталога может не быть. Runtime-locks
# другого процесса на новом хосте не нужны.
if [[ -d /var/lib/caddy/.local/share/caddy ]]; then
  cp -a /var/lib/caddy/.local/share/caddy "$BUNDLE_DIR/caddy-storage"
  rm -rf -- "$BUNDLE_DIR/caddy-storage/locks"
fi

# cp -a переносит и сокеты (mgmt.sock OpenVPN), и FIFO, а tar их молча
# пропускает: манифест считал файл, которого в архиве нет, и restore свежего
# бэкапа падал на проверке. В бандле остаются только то, что ляжет в архив.
find "$BUNDLE_DIR" \( -type s -o -type p -o -type b -o -type c \) -delete

# ── Manifest с SHA-256 каждого компонента ───────────────────────────────────
# Версию панель отдаёт сама на /api/version (контейнер в сети хоста). Ни
# /app/package.json, ни .output/.config/.json в образ не попадают.
APP_VERSION=$(curl -fsS --max-time 10 http://127.0.0.1:51821/api/version 2>/dev/null \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["version"])' 2>/dev/null || echo unknown)
GIT_COMMIT=$(git -C "$SOURCE_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)
SCHEMA_VER=0
if [[ -f "$BUNDLE_DIR/db.sqlite" ]]; then
  SCHEMA_VER=$(sqlite3 "$BUNDLE_DIR/db.sqlite" 'PRAGMA schema_version;')
fi

python3 - "$BUNDLE_DIR" "$APP_VERSION" "$GIT_COMMIT" "$SCHEMA_VER" "$TS" <<'PY' > "$BUNDLE_DIR/manifest.json"
import hashlib, json, os, subprocess, sys, tarfile, io
bdir, ver, commit, schema, ts = sys.argv[1:6]

def sha_file(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1<<20), b''):
            h.update(chunk)
    return 'sha256:' + h.hexdigest()

def sha_dir(p):
    # Детерминированный SHA: список файлов отсортирован, для каждого — путь + hash содержимого.
    h = hashlib.sha256()
    for root, dirs, files in os.walk(p):
        dirs.sort(); files.sort()
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, p)
            h.update(rel.encode())
            h.update(b'\0')
            try:
                h.update(sha_file(full).encode())
            except Exception:
                h.update(b'<unreadable>')
            h.update(b'\n')
    return 'sha256:' + h.hexdigest()

components = {}
for name in sorted(os.listdir(bdir)):
    if name == 'manifest.json': continue
    p = os.path.join(bdir, name)
    if os.path.isdir(p):
        components[name] = sha_dir(p)
    else:
        components[name] = sha_file(p)

print(json.dumps({
    'version': ver,
    'git_commit': commit,
    'schema_version': int(schema),
    'timestamp': ts,
    'components': components,
    'archive_format': 'tar.gz.age',
    'spec_version': 1,
}, indent=2, ensure_ascii=False))
PY

# ── Архив tar.gz внутри WORK (НЕ в /var/backups!) ───────────────────────────
ARCHIVE_PLAIN="$WORK/$BUNDLE.tar.gz"
tar -C "$WORK" -czf "$ARCHIVE_PLAIN" "$BUNDLE"

# ── Шифрование age с passphrase из ENV через expect (age читает /dev/tty) ───
ARCHIVE_AGE="$WORK/$BUNDLE.tar.gz.age"
export ANYSDA_BACKUP_PASSPHRASE   # expect читает через $env(...)
# Heredoc unquoted чтобы bash подставил $ARCHIVE_AGE / $ARCHIVE_PLAIN.
# Expect-внутренние переменные ($pass, $result) экранированы как \$.
expect <<EXPECT > /dev/null
log_user 0
set timeout 60
set pass \$env(ANYSDA_BACKUP_PASSPHRASE)
spawn -noecho age -p -o "$ARCHIVE_AGE" "$ARCHIVE_PLAIN"
expect {
  "Enter passphrase*"   { send -- "\$pass\r"; exp_continue }
  "Confirm passphrase*" { send -- "\$pass\r"; exp_continue }
  eof
}
catch wait result
exit [lindex \$result 3]
EXPECT

# Сразу же удаляем plaintext архив (shred + rm)
shred -uz "$ARCHIVE_PLAIN" 2>/dev/null || rm -f "$ARCHIVE_PLAIN"

if [[ ! -s "$ARCHIVE_AGE" ]]; then
  echo "anysda-backup FATAL: шифрование age не дало файла $ARCHIVE_AGE" >&2
  exit 3
fi

# ── Финальный путь в локальном backend ──────────────────────────────────────
FINAL="$BACKUP_LOCAL_DIR/$BUNDLE.tar.gz.age"
mv "$ARCHIVE_AGE" "$FINAL"
chmod 600 "$FINAL"
SIZE=$(stat -c %s "$FINAL")
HASH=$(sha256sum "$FINAL" | awk '{print $1}')

# ── Ротация: keep last N, защищаем последний успешный ──────────────────────
# Используем find -printf '%T@\t%p\n' | sort -n чтобы не парсить ls.
RETAIN="${BACKUP_RETENTION:-10}"
mapfile -t ALL < <(find "$BACKUP_LOCAL_DIR" -maxdepth 1 -type f -name 'anysda-vpn2-*.tar.gz.age' \
  -printf '%T@\t%p\n' | sort -n | awk -F'\t' '{print $2}')
COUNT=${#ALL[@]}
if (( COUNT > RETAIN )); then
  TO_REMOVE=$(( COUNT - RETAIN ))
  # Никогда не удаляем последний (самый свежий) — гарантируем что свежесозданный
  # FINAL не попадёт под нож. Он и так в конце списка — мы отрезаем с начала.
  for ((i=0; i<TO_REMOVE; i++)); do
    [[ "${ALL[i]}" == "$FINAL" ]] && continue   # ровно никогда (FINAL свежайший)
    rm -f -- "${ALL[i]}"
    echo "anysda-backup: rotated out $(basename "${ALL[i]}")"
  done
fi

# ── S3 upload + S3-ротация (если backend: s3) ──────────────────────────────
if [[ "$BACKUP_BACKEND" == "s3" ]]; then
  : "${BACKUP_S3_ENDPOINT:?BACKUP_S3_ENDPOINT не задан}"
  : "${BACKUP_S3_BUCKET:?BACKUP_S3_BUCKET не задан}"
  : "${ANYSDA_S3_ACCESS_KEY:?ANYSDA_S3_ACCESS_KEY не задан (из $ENV_FILE)}"
  : "${ANYSDA_S3_SECRET_KEY:?ANYSDA_S3_SECRET_KEY не задан (из $ENV_FILE)}"
  # Region обязателен у cloud.ru/yandex/selectel (SigV4). Пусто → дефолт.
  export AWS_DEFAULT_REGION="${BACKUP_S3_REGION:-us-east-1}"

  S3_KEY="$BUNDLE.tar.gz.age"
  echo "anysda-backup: uploading to s3://$BACKUP_S3_BUCKET/$S3_KEY"
  AWS_ACCESS_KEY_ID="$ANYSDA_S3_ACCESS_KEY" \
  AWS_SECRET_ACCESS_KEY="$ANYSDA_S3_SECRET_KEY" \
  aws --endpoint-url "$BACKUP_S3_ENDPOINT" s3 cp \
      "$FINAL" "s3://$BACKUP_S3_BUCKET/$S3_KEY" --only-show-errors

  # S3-ротация: list, отсортировать по имени (TS встроен), удалить старые
  AWS_ACCESS_KEY_ID="$ANYSDA_S3_ACCESS_KEY" \
  AWS_SECRET_ACCESS_KEY="$ANYSDA_S3_SECRET_KEY" \
  aws --endpoint-url "$BACKUP_S3_ENDPOINT" s3 ls "s3://$BACKUP_S3_BUCKET/" \
    | awk '{print $NF}' \
    | grep -E '^anysda-vpn2-.*\.tar\.gz\.age$' \
    | sort \
    | head -n "-${RETAIN}" \
    | while read -r key; do
        [[ -z "$key" || "$key" == "$S3_KEY" ]] && continue
        AWS_ACCESS_KEY_ID="$ANYSDA_S3_ACCESS_KEY" \
        AWS_SECRET_ACCESS_KEY="$ANYSDA_S3_SECRET_KEY" \
        aws --endpoint-url "$BACKUP_S3_ENDPOINT" s3 rm "s3://$BACKUP_S3_BUCKET/$key" --only-show-errors
        echo "anysda-backup: s3 rotated out $key"
      done
fi

# ── Финал ───────────────────────────────────────────────────────────────────
printf '\n%-12s %s\n' 'backup:'    "$FINAL"
printf '%-12s %s bytes\n' 'size:'  "$SIZE"
printf '%-12s sha256:%s\n' 'hash:' "$HASH"
printf '%-12s %s\n' 'backend:'     "$BACKUP_BACKEND"
printf '%-12s %s\n' 'retention:'   "keep last $RETAIN"
if [[ -n $WARNING ]]; then
  printf '%-12s %s\n' 'warning:'   "$WARNING"
  # Предупреждение в ленту «События» уходит из report_failure по коду выхода.
  exit 4
fi
