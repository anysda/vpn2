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
#                        clash-secret, telegram-runtime.json, manual-routes,
#                        session-secret, anysda-config.yaml)
#   - etc-wireguard/wg0.conf       — серверный приватный ключ WG
#   - etc-openvpn/     — CA + серверный сертификат + ccd/ + crl.pem
#   - manifest.json    — версии + SHA-256 каждого компонента
#
# Plaintext tar.gz живёт ТОЛЬКО в mktemp-dir chmod 700, удаляется trap EXIT.
# На диск/в S3 попадает только *.tar.gz.age (age symmetric encrypt).
#
# Источник passphrase: /etc/anysda/backup.env → ANYSDA_BACKUP_PASSPHRASE.
# Файл (chmod 600) рендерится стадией 26-backup из config.yaml.

set -euo pipefail

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
	  systemd EnvironmentFile= для anysda-backup.service.

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
trap cleanup EXIT

# ── Снимок данных ───────────────────────────────────────────────────────────
TS=$(date -u +%Y-%m-%dT%H%M%SZ)
BUNDLE="anysda-vpn2-$TS"
BUNDLE_DIR="$WORK/$BUNDLE"
mkdir -p "$BUNDLE_DIR"

# 1. SQLite — консистентный снимок (без даунтайма)
DB=/var/lib/anysda-vpn2/db.sqlite
if [[ -r "$DB" ]]; then
  sqlite3 "$DB" ".backup $BUNDLE_DIR/db.sqlite"
else
  echo "anysda-backup: WARN: $DB не найден — пропускаю db.sqlite" >&2
fi

# 2. /etc/anysda — все секреты
if [[ -d /etc/anysda ]]; then
  cp -a /etc/anysda "$BUNDLE_DIR/etc-anysda"
fi

# 3. WireGuard server key — wg0.conf
mkdir -p "$BUNDLE_DIR/etc-wireguard"
[[ -r /etc/wireguard/wg0.conf ]] && cp -a /etc/wireguard/wg0.conf "$BUNDLE_DIR/etc-wireguard/"

# 4. OpenVPN PKI + конфиг + CCD + CRL
if [[ -d /etc/openvpn ]]; then
  cp -a /etc/openvpn "$BUNDLE_DIR/etc-openvpn"
fi

# ── Manifest с SHA-256 каждого компонента ───────────────────────────────────
APP_VERSION=$(docker exec anysda-vpn2 sh -c 'cat /app/.output/.config/.json 2>/dev/null; node -e "console.log(require(\"/app/package.json\").version)" 2>/dev/null' 2>/dev/null | grep -oE '"version":[[:space:]]*"[^"]+"' | head -1 | sed 's/.*"\([^"]*\)"/\1/' || echo unknown)
GIT_COMMIT=$(cd /opt/anysda-vpn2 2>/dev/null && git rev-parse --short HEAD 2>/dev/null || echo unknown)
SCHEMA_VER=$(sqlite3 "$DB" 'PRAGMA schema_version;' 2>/dev/null || echo 0)

# Hash файла или директории (тар-stream sha256 для директории).
_hash() {
  local p="$1"
  if [[ -d "$p" ]]; then
    (cd "$(dirname "$p")" && tar -cf - "$(basename "$p")" 2>/dev/null | sha256sum | awk '{print $1}')
  elif [[ -f "$p" ]]; then
    sha256sum "$p" | awk '{print $1}'
  else
    echo ""
  fi
}

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
SIZE_PLAIN=$(stat -c %s "$ARCHIVE_PLAIN")

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
