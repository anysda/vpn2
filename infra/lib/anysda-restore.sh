#!/usr/bin/env bash
# anysda-vpn2 restore — восстанавливает entry-ноду из *.tar.gz.age бэкапа.
#
# Использование:
#   /usr/local/bin/anysda-restore.sh [<archive-name|latest>] [--force] [--dry-run]
#     archive-name — имя файла из $BACKUP_LOCAL_DIR (только basename) или 'latest'
#     --force      — overwrite не-пустой db.sqlite без вопросов
#     --dry-run    — только показать, какой архив и откуда будет взят
#     --allow-incompatible — восстановить архив неизвестного формата или схемы БД
#
# Безопасность (требования TZ §3.6):
#   - SHA-256 каждого компонента проверяется ПЕРЕД применением (manifest)
#   - проверка совместимости версии
#   - refuse без --force если db.sqlite не пустая
#   - pre-restore safety snapshot в .pre-restore/ (исключён из ротации)
#   - стоп → swap → start affected services, стадии 22/30/35 с паролями из бэкапа
#   - 99-verify по завершению (если найден)

set -euo pipefail
umask 077

ENV_FILE=/etc/anysda/backup.env
[[ -r "$ENV_FILE" ]] || { echo "anysda-restore: $ENV_FILE не доступен" >&2; exit 2; }
# shellcheck disable=SC1090
source "$ENV_FILE"

: "${BACKUP_BACKEND:?BACKUP_BACKEND не задан}"
: "${BACKUP_LOCAL_DIR:=/var/backups/anysda-vpn2}"
# Region обязателен у cloud.ru/yandex/selectel (SigV4). Если backend=local —
# переменная просто игнорируется.
[[ "$BACKUP_BACKEND" == "s3" ]] && export AWS_DEFAULT_REGION="${BACKUP_S3_REGION:-us-east-1}"

# Имя архива необязательно, как в `./deploy.sh restore`: `--dry-run` — это latest с флагом.
ARCHIVE_REQ=latest
if [[ -n "${1:-}" && "$1" != -* ]]; then ARCHIVE_REQ=$1; shift; fi
FORCE=0
DRY_RUN=0
ALLOW_INCOMPATIBLE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force|-f) FORCE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --allow-incompatible) ALLOW_INCOMPATIBLE=1 ;;
    *) echo "anysda-restore: неизвестный аргумент: $1" >&2; exit 2 ;;
  esac
  shift
done

if [[ -z "${ANYSDA_BACKUP_PASSPHRASE:-}" ]]; then
  echo "anysda-restore FATAL: ANYSDA_BACKUP_PASSPHRASE пуст в $ENV_FILE." >&2
  echo "  Заполни backup.passphrase в config.yaml и прогони ./deploy.sh 26-backup ru" >&2
  exit 2
fi

s3() {
  AWS_ACCESS_KEY_ID="${ANYSDA_S3_ACCESS_KEY:?}" AWS_SECRET_ACCESS_KEY="${ANYSDA_S3_SECRET_KEY:?}" \
    aws --endpoint-url "${BACKUP_S3_ENDPOINT:?}" s3 "$@"
}

s3_pull() {
  mkdir -p "$BACKUP_LOCAL_DIR"
  s3 cp "s3://${BACKUP_S3_BUCKET:?}/$1" "$BACKUP_LOCAL_DIR/$1" --only-show-errors || return
  chmod 600 "$BACKUP_LOCAL_DIR/$1"   # aws s3 cp следует umask → выставляем явно
}

# Время создания — метка из имени: бэкап пишет её же в manifest.timestamp,
# а mtime меняется при копировании и скачивании.
archive_ts() { [[ "$1" =~ ^anysda-vpn2-([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z)\.tar\.gz\.age$ ]] && echo "${BASH_REMATCH[1]}"; }
ts_epoch() { date -u -d "${1:0:10} ${1:11:2}:${1:13:2}:${1:15:2}" +%s; }
age_text() {
  local s=$1
  if (( s >= 86400 )); then echo "$((s / 86400)) д $((s % 86400 / 3600)) ч"
  elif (( s >= 3600 )); then echo "$((s / 3600)) ч $((s % 3600 / 60)) мин"
  else echo "$((s / 60)) мин"; fi
}

# latest: самый свежий по времени создания среди локальных архивов и S3.
resolve_latest() {
  local name ts best_local="" best_s3="" s3_list
  while IFS= read -r name; do
    ts=$(archive_ts "$name") || continue
    [[ -z "$best_local" || "$ts" > "$(archive_ts "$best_local")" ]] && best_local=$name
  done < <(find "$BACKUP_LOCAL_DIR" -maxdepth 1 -type f -name 'anysda-vpn2-*.tar.gz.age' -printf '%f\n' 2>/dev/null)
  if [[ "$BACKUP_BACKEND" == "s3" ]]; then
    if s3_list=$(s3 ls "s3://${BACKUP_S3_BUCKET:?}/" 2>&1); then
      while IFS= read -r name; do
        name=${name##* }
        ts=$(archive_ts "$name") || continue
        [[ -z "$best_s3" || "$ts" > "$(archive_ts "$best_s3")" ]] && best_s3=$name
      done <<< "$s3_list"
    else
      echo "anysda-restore: WARN: S3 недоступен — выбираю только среди локальных архивов, в облаке может лежать более свежий:" >&2
      printf '%s\n' "$s3_list" | tail -n 3 | sed 's/^/  /' >&2
    fi
  fi
  if [[ -z "$best_local" && -z "$best_s3" ]]; then
    echo "anysda-restore: ни одного backup'а ни в $BACKUP_LOCAL_DIR, ни в S3" >&2
    exit 3
  fi
  local pick=$best_local from=local other="" other_from=""
  if [[ -n "$best_s3" && ( -z "$best_local" || "$(archive_ts "$best_s3")" > "$(archive_ts "$best_local")" ) ]]; then
    pick=$best_s3 from=S3 other=$best_local other_from=local
  elif [[ -n "$best_s3" && "$best_s3" != "$best_local" ]]; then
    other=$best_s3 other_from=S3
  fi
  local pick_epoch; pick_epoch=$(ts_epoch "$(archive_ts "$pick")")
  echo "anysda-restore: latest = $pick ($from), создан $(archive_ts "$pick"), $(age_text $(( $(date -u +%s) - pick_epoch ))) назад" >&2
  if [[ -n "$other" ]]; then
    echo "anysda-restore:   самый свежий $other_from: $other — старше выбранного на $(age_text $(( pick_epoch - $(ts_epoch "$(archive_ts "$other")") )))" >&2
  fi
  ARCHIVE_NAME=$pick ARCHIVE_FROM=$from
}

if [[ "$ARCHIVE_REQ" == "latest" ]]; then
  resolve_latest
else
  ARCHIVE_NAME=$(basename "$ARCHIVE_REQ") ARCHIVE_FROM=local
  if [[ ! -f "$BACKUP_LOCAL_DIR/$ARCHIVE_NAME" ]]; then
    if [[ "$BACKUP_BACKEND" != "s3" ]]; then
      echo "anysda-restore: архив $ARCHIVE_NAME не найден в $BACKUP_LOCAL_DIR" >&2
      exit 3
    fi
    ARCHIVE_FROM=S3
  fi
fi
if (( DRY_RUN )); then
  echo "anysda-restore: dry-run — восстановил бы $ARCHIVE_NAME ($ARCHIVE_FROM), ничего не изменено"
  exit 0
fi
if [[ "$ARCHIVE_FROM" == S3 ]] && ! s3_pull "$ARCHIVE_NAME"; then
  echo "anysda-restore: $ARCHIVE_NAME не скачан из S3" >&2
  exit 3
fi
ARCHIVE="$BACKUP_LOCAL_DIR/$ARCHIVE_NAME"
echo "anysda-restore: source = $ARCHIVE"

# ── Расшифровка во временную dir ────────────────────────────────────────────
WORK=$(mktemp -d /var/lib/anysda-vpn2/.restore-tmp.XXXXXX)
chmod 700 "$WORK"
cleanup() {
  if [[ -d "$WORK" ]]; then
    find "$WORK" -type f -exec shred -uz {} + 2>/dev/null || true
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

ARCHIVE_PLAIN="$WORK/restore.tar.gz"
export ANYSDA_BACKUP_PASSPHRASE
# expect может вернуть non-zero (age decrypt failed) — не даём set -e оборвать
# тихо, ловим код и печатаем человеческое сообщение.
set +e
expect <<EXPECT > /dev/null
log_user 0
set timeout 60
set pass \$env(ANYSDA_BACKUP_PASSPHRASE)
spawn -noecho age -d -o "$ARCHIVE_PLAIN" "$ARCHIVE"
expect {
  "Enter passphrase*" { send -- "\$pass\r"; exp_continue }
  eof
}
catch wait result
exit [lindex \$result 3]
EXPECT
AGE_RC=$?
set -e
if [[ $AGE_RC -ne 0 || ! -s "$ARCHIVE_PLAIN" ]]; then
  echo "anysda-restore: дешифровка провалилась (age exit=$AGE_RC) — неверный passphrase или повреждённый архив" >&2
  exit 4
fi

# ── Распаковка ──────────────────────────────────────────────────────────────
if ! tar -C "$WORK" -xzf "$ARCHIVE_PLAIN" 2>/tmp/anysda-restore-tar.err; then
  echo "anysda-restore: tar -xzf провалился — архив повреждён (после успешной дешифровки)" >&2
  sed 's/^/  /' /tmp/anysda-restore-tar.err >&2 || true
  rm -f /tmp/anysda-restore-tar.err
  exit 5
fi
rm -f /tmp/anysda-restore-tar.err
shred -uz "$ARCHIVE_PLAIN" 2>/dev/null || rm -f "$ARCHIVE_PLAIN"

BUNDLE_DIR=$(find "$WORK" -maxdepth 1 -mindepth 1 -type d -name 'anysda-vpn2-*' -print -quit)
[[ -d "$BUNDLE_DIR" ]] || { echo "anysda-restore: в архиве нет ожидаемой папки anysda-vpn2-*" >&2; exit 5; }

# ── Проверка manifest.json + SHA-256 каждого компонента ────────────────────
[[ -r "$BUNDLE_DIR/manifest.json" ]] || { echo "anysda-restore: manifest.json отсутствует — corrupted архив" >&2; exit 5; }

python3 - "$BUNDLE_DIR" "$ARCHIVE_NAME" <<'PY' || exit 5
import hashlib, json, os, sys
bdir = sys.argv[1]
with open(os.path.join(bdir, 'manifest.json')) as f:
    m = json.load(f)
# latest выбирался по метке в имени: она должна совпадать с манифестом.
if sys.argv[2] != f'anysda-vpn2-{m.get("timestamp")}.tar.gz.age':
    print(f'anysda-restore: WARN: имя архива не совпадает с manifest.timestamp={m.get("timestamp")}',
          file=sys.stderr)

def sha_file(p):
    h = hashlib.sha256()
    with open(p, 'rb') as fp:
        for chunk in iter(lambda: fp.read(1<<20), b''):
            h.update(chunk)
    return 'sha256:' + h.hexdigest()

def sha_dir(p):
    h = hashlib.sha256()
    for root, dirs, files in os.walk(p):
        dirs.sort(); files.sort()
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, p)
            h.update(rel.encode()); h.update(b'\0')
            try: h.update(sha_file(full).encode())
            except Exception: h.update(b'<unreadable>')
            h.update(b'\n')
    return 'sha256:' + h.hexdigest()

fail = []
for name, expected in m.get('components', {}).items():
    full = os.path.join(bdir, name)
    if not os.path.exists(full):
        fail.append(f'  {name}: missing'); continue
    actual = sha_dir(full) if os.path.isdir(full) else sha_file(full)
    if actual != expected:
        fail.append(f'  {name}: expected {expected}, got {actual}')

if fail:
    print('manifest verification FAILED:', file=sys.stderr)
    for f in fail: print(f, file=sys.stderr)
    sys.exit(1)
print(f'manifest verified: {len(m["components"])} components,'
      f' archive={m.get("timestamp")} app_v={m.get("version")} git={m.get("git_commit")}')
PY

# ── Совместимость: формат архива и схема БД против текущей версии ──────────
# До любых изменений: неизвестный формат или схема новее текущего образа
# оставили бы ноду с данными, которые панель не запустит.
incompatible() {
  if (( ALLOW_INCOMPATIBLE )); then
    echo "anysda-restore: WARN: $1 — продолжаю по --allow-incompatible" >&2
    return
  fi
  echo "anysda-restore: ABORT — $1; данные не изменены (обход: --allow-incompatible)" >&2
  exit 9
}
MANIFEST_SPEC=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("spec_version", 1))' "$BUNDLE_DIR/manifest.json")
if [[ "$MANIFEST_SPEC" != "1" ]]; then
  incompatible "формат архива spec_version=$MANIFEST_SPEC, этот restore понимает только 1"
fi
if [[ -e "$BUNDLE_DIR/db.sqlite" ]]; then
  # Журнал миграций образа, который поднимет панель; клон репозитория — запасной.
  MIGRATIONS=""
  if docker cp anysda-vpn2:/app/server/database/migrations "$WORK/migrations" >/dev/null 2>&1 \
     && [[ -r "$WORK/migrations/meta/_journal.json" ]]; then
    MIGRATIONS="$WORK/migrations"
  elif [[ -r /opt/anysda-vpn2/app/server/database/migrations/meta/_journal.json ]]; then
    MIGRATIONS=/opt/anysda-vpn2/app/server/database/migrations
  fi
  if [[ -z "$MIGRATIONS" ]]; then
    incompatible "не найден журнал миграций текущей версии (ни в контейнере anysda-vpn2, ни в /opt/anysda-vpn2)"
  elif ! SCHEMA_RESULT=$(python3 - "$BUNDLE_DIR/db.sqlite" "$MIGRATIONS" 2>&1 <<'PY'
import hashlib, json, os, sqlite3, sys
database, folder = sys.argv[1:]
entries = json.load(open(os.path.join(folder, 'meta/_journal.json')))['entries']
journal = [(int(e['when']), hashlib.sha256(open(os.path.join(folder, e['tag'] + '.sql'), 'rb').read()).hexdigest())
           for e in entries]
with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as db:
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if '__drizzle_migrations' not in tables:
        if tables:
            sys.exit('в базе архива нет истории миграций (__drizzle_migrations)')
        history = []
    else:
        history = [(int(when), digest) for when, digest in
                   db.execute('SELECT created_at, hash FROM __drizzle_migrations ORDER BY created_at')]
tags = [e['tag'] for e in entries]
if len(history) > len(journal) and history[:len(journal)] == journal:
    sys.exit(f'схема БД архива новее текущей версии: {len(history)} миграций против {len(journal)}')
for index, row in enumerate(history):
    if index >= len(journal) or row != journal[index]:
        name = tags[index] if index < len(journal) else f'#{index + 1}'
        sys.exit(f'история миграций архива расходится с текущей версией на {name}')
behind = len(journal) - len(history)
print(f'схема БД архива: {len(history)} из {len(journal)} миграций текущей версии'
      + (f', {behind} догонит стадия 30' if behind else ''))
PY
  ); then
    incompatible "$SCHEMA_RESULT"
  else
    echo "anysda-restore: $SCHEMA_RESULT"
  fi
fi

# Архивы до общего среза могли поймать выдачу, отзыв или перевыпуск: сертификат
# в БД уже отозван в CRL архива, у устройства нет пира WireGuard или учётки
# IKEv2, у отключённого они остались. Восстанавливать можно, но такие
# устройства перевыпустить: перевыпуск заново пишет конфиги из БД.
CHECK=/usr/local/bin/anysda-backup.sh
if [[ -x "$CHECK" ]] && ! MISMATCH=$("$CHECK" --check-bundle "$BUNDLE_DIR" 2>&1); then
  WHAT=
  [[ $MISMATCH != *CRL* ]] || WHAT+=', CRL OpenVPN'
  [[ $MISMATCH != *WireGuard* ]] || WHAT+=', пиры WireGuard'
  [[ $MISMATCH != *IKEv2* ]] || WHAT+=', учётки IKEv2'
  WHAT=${WHAT#, }
  echo "anysda-restore: WARN: в архиве БД и ${WHAT:-конфиги VPN} расходятся — после восстановления перевыпусти эти устройства в панели:" >&2
  printf '%s\n' "$MISMATCH" >&2
fi

# Старые архивы содержали только wg0.conf. Восстанавливаем исходный ключ
# для стадии 28 и панели ДО остановки/применения, не берём ключ свежего деплоя.
# Проверка манифеста выше должна пройти до изменения содержимого бандла.
WG_BUNDLE="$BUNDLE_DIR/etc-wireguard"
if [[ -L "$WG_BUNDLE/server.priv" && ! -f "$WG_BUNDLE/wg0.conf" ]]; then
  echo "anysda-restore: ссылка на ключ WG без wg0.conf — приватный ключ восстановить невозможно" >&2
  exit 5
fi
# Старые архивы переносили ссылки в keys/, но не сам каталог. Не читаем и
# не пишем через ссылки архива (в том числе абсолютные на живой хост).
for key in server.priv server.pub; do
  if [[ -L "$WG_BUNDLE/$key" ]]; then
    rm -- "$WG_BUNDLE/$key"
  fi
done
if [[ -f "$WG_BUNDLE/wg0.conf" || -f "$WG_BUNDLE/server.priv" ]]; then
  if [[ ! -s "$WG_BUNDLE/server.priv" ]]; then
    awk '
      /^[[:space:]]*\[/ { interface = ($0 ~ /^[[:space:]]*\[Interface\][[:space:]]*$/) }
      interface && /^[[:space:]]*PrivateKey[[:space:]]*=/ {
        sub(/^[^=]*=[[:space:]]*/, ""); sub(/[[:space:]]*$/, ""); print
      }
    ' "$WG_BUNDLE/wg0.conf" > "$WG_BUNDLE/server.priv"
  fi
  # pub выводим заново: не оставляем ключ B даже при отсутствии pub в архиве.
  if ! wg pubkey < "$WG_BUNDLE/server.priv" > "$WG_BUNDLE/server.pub"; then
    echo "anysda-restore: неверный или отсутствующий приватный ключ WG в архиве" >&2
    exit 5
  fi
  chmod 600 "$WG_BUNDLE/server.priv" "$WG_BUNDLE/server.pub"
fi

# Стадия 30 ждёт шаблон в staging-каталоге run_stage. /tmp может быть tmpfs:
# готовим его из установленного дерева на каждом restore, ещё до swap.
FRONTEND_TPL=/opt/anysda-vpn2/infra/configs/anysda-config.yaml.tpl
if [[ ! -r "$FRONTEND_TPL" ]]; then
  echo "anysda-restore: нет шаблона $FRONTEND_TPL — данные не изменены" >&2
  exit 5
fi
install -d -m 700 /tmp/anysda
install -m 600 "$FRONTEND_TPL" /tmp/anysda/anysda-config.yaml.tpl

# ── Защита от перетирания не-пустой БД ─────────────────────────────────────
CADDY_STORAGE=/var/lib/caddy/.local/share/caddy
if [[ -d "$BUNDLE_DIR/caddy-storage" ]] && ! id caddy >/dev/null 2>&1; then
  echo "anysda-restore: нет пользователя caddy — сначала выполни деплой без panel.domain; данные не изменены" >&2
  exit 5
fi
LIVE_DB=/var/lib/anysda-vpn2/db.sqlite
if [[ -f "$LIVE_DB" ]]; then
  LIVE_DB_SIZE=$(stat -c %s "$LIVE_DB")
  LIVE_CLIENTS=$(sqlite3 "$LIVE_DB" 'SELECT COUNT(*) FROM clients;' 2>/dev/null || echo 0)
  if (( LIVE_DB_SIZE > 0 )) && (( LIVE_CLIENTS > 0 )); then
    if (( FORCE == 0 )); then
      cat >&2 <<-MSG
	anysda-restore: ABORT — текущая db.sqlite не пустая ($LIVE_CLIENTS клиентов, $LIVE_DB_SIZE байт).
	Будут перетёрты: db.sqlite, /etc/anysda/*, /etc/wireguard/wg0.conf, /etc/openvpn/*, конфиги и ключи IKEv2, хранилище Caddy (если есть в архиве)
	Запусти повторно с --force чтобы согласиться.
	MSG
      exit 6
    fi
  fi
fi

# ── Pre-restore safety snapshot — отдельная директория, исключена из ротации ─
PRESNAP_DIR="$BACKUP_LOCAL_DIR/.pre-restore"
mkdir -p "$PRESNAP_DIR"; chmod 700 "$PRESNAP_DIR"
PRESNAP_TS=$(date -u +%Y-%m-%dT%H%M%S.%NZ)
PRESNAP_TAR="$WORK/pre-restore-$PRESNAP_TS.tar.gz"
PRESNAP_FINAL="$PRESNAP_DIR/pre-restore-$PRESNAP_TS.tar.gz.age"

echo "anysda-restore: pre-restore snapshot → $PRESNAP_FINAL"
# Панель ещё пишет в WAL: архивировать один db.sqlite нельзя. Сначала
# собираем самостоятельную SQLite-копию и все заменяемые файлы в WORK.
# Ошибка копирования/архивирования прерывает restore ДО остановки и swap.
PRESNAP_ROOT="$WORK/pre-restore"
mkdir -p "$PRESNAP_ROOT/var/lib/anysda-vpn2"
if [[ -f "$LIVE_DB" ]]; then
  sqlite3 "$LIVE_DB" ".timeout 10000" ".backup '$PRESNAP_ROOT/var/lib/anysda-vpn2/db.sqlite'"
  LIVE_DB_OWNER=$(stat -c %u:%g "$LIVE_DB") LIVE_DB_MODE=$(stat -c %a "$LIVE_DB")
fi
for path in \
  etc/anysda etc/wireguard etc/openvpn \
  etc/strongswan etc/swanctl etc/strongswan.conf etc/strongswan.d \
  etc/ipsec.conf etc/ipsec.secrets \
  etc/caddy var/lib/caddy/.local/share/caddy \
  var/lib/anysda-vpn2/runtime var/lib/vpn2-control \
  opt/AdGuardHome/AdGuardHome.yaml var/anysda/.stamps; do
  if [[ -e "/$path" || -L "/$path" ]]; then
    (cd / && cp -a --parents "$path" "$PRESNAP_ROOT/")
  fi
done
# Runtime-сокеты/FIFO не являются конфигурацией и не попадают в tar.
find "$PRESNAP_ROOT" \( -type s -o -type p -o -type b -o -type c \) -delete
tar -C "$PRESNAP_ROOT" -czf "$PRESNAP_TAR" .
# Шифруем (тот же passphrase)
expect <<EXPECT > /dev/null
log_user 0
set timeout 60
set pass \$env(ANYSDA_BACKUP_PASSPHRASE)
spawn -noecho age -p -o "$WORK/pre-restore.tar.gz.age" "$PRESNAP_TAR"
expect {
  "Enter passphrase*"   { send -- "\$pass\r"; exp_continue }
  "Confirm passphrase*" { send -- "\$pass\r"; exp_continue }
  eof
}
catch wait result
exit [lindex \$result 3]
EXPECT
[[ -s "$WORK/pre-restore.tar.gz.age" ]] || { echo "anysda-restore: пустой страховочный архив" >&2; exit 5; }
mv "$WORK/pre-restore.tar.gz.age" "$PRESNAP_FINAL"
shred -uz "$PRESNAP_TAR" 2>/dev/null || rm -f "$PRESNAP_TAR"
chmod 600 "$PRESNAP_FINAL"
echo "anysda-restore: pre-restore snapshot saved ($(stat -c %s "$PRESNAP_FINAL") bytes)"

# ── Откат при сбое после остановки служб ───────────────────────────────────
# С этой точки любая ошибка или сигнал возвращают заменённое из страховочной
# копии ($PRESNAP_ROOT ещё на диске) и запускают службы прежнего состояния.
STEP="остановка служб"
TOUCHED=()
DB_SWAPPED=0
CONTROL_STOPPED=0
# Содержимое каталога поверх живого. `cp -a src/. dst/` переносит на dst и
# владельца с режимом самого src (каталоги в WORK — root:root 0700, панель
# от vpn2-panel теряет доступ к БД): у живого каталога они остаются прежними.
# Нового каталога не было — берёт их из src.
copy_into() {
  local src=$1 dst=$2 owner="" mode=""
  if [[ -d "$dst" ]]; then
    owner=$(stat -Lc %u:%g "$dst") && mode=$(stat -Lc %a "$dst") || return
  else
    mkdir -p "$dst" || return
  fi
  # Без `|| return`: под ловушкой ERR сбой назовёт саму команду cp, а в
  # откате (ловушка снята) код вернёт проверка ниже.
  cp -a "$src/." "$dst/"
  local rc=$?
  (( rc == 0 )) || return "$rc"
  [[ -z "$owner" ]] || { chown "$owner" "$dst" && chmod "$mode" "$dst"; }
}
rollback_path() {
  local rel=$1
  rm -rf -- "/${rel:?}" || return
  [[ -e "$PRESNAP_ROOT/$rel" || -L "$PRESNAP_ROOT/$rel" ]] || return 0
  mkdir -p "/$(dirname "$rel")" && cp -a "$PRESNAP_ROOT/$rel" "/$(dirname "$rel")/"
}
rollback() {
  local rc=$1 cause=$2 item back=() failed=() down=()
  local -A seen=()
  trap - ERR INT TERM
  set +e
  echo "anysda-restore: FAILED на шаге «$STEP»: $cause (код $rc) — возвращаю прежнее состояние" >&2
  for item in "${TOUCHED[@]}"; do
    [[ -z "${seen[$item]:-}" ]] || continue
    seen[$item]=1
    case "$item" in
      db)
        if (( DB_SWAPPED )); then
          if [[ -f "$PRESNAP_ROOT/var/lib/anysda-vpn2/db.sqlite" ]]; then
            install -m "$LIVE_DB_MODE" -o "${LIVE_DB_OWNER%:*}" -g "${LIVE_DB_OWNER#*:}" \
              "$PRESNAP_ROOT/var/lib/anysda-vpn2/db.sqlite" /var/lib/anysda-vpn2/.db-rollback.sqlite \
              && rm -f /var/lib/anysda-vpn2/db.sqlite-wal /var/lib/anysda-vpn2/db.sqlite-shm /var/lib/anysda-vpn2/db.sqlite-journal \
              && mv /var/lib/anysda-vpn2/.db-rollback.sqlite /var/lib/anysda-vpn2/db.sqlite
          else
            rm -f /var/lib/anysda-vpn2/db.sqlite
          fi
        else
          # Основной файл не заменён: возвращаем только отложенные WAL/SHM.
          [[ ! -d "$WORK/db-before" ]] || copy_into "$WORK/db-before" /var/lib/anysda-vpn2
          rm -f /var/lib/anysda-vpn2/.db-restore.sqlite
        fi && back+=(/var/lib/anysda-vpn2/db.sqlite) || failed+=(/var/lib/anysda-vpn2/db.sqlite)
        if [[ -d "$WORK/control-before-restore" ]]; then
          { rm -rf /var/lib/vpn2-control && mv "$WORK/control-before-restore" /var/lib/vpn2-control; } \
            && back+=(/var/lib/vpn2-control) || failed+=(/var/lib/vpn2-control)
        fi
        ;;
      *)
        rollback_path "$item" && back+=("/$item") || failed+=("/$item") ;;
    esac
  done
  for item in sing-box openvpn-server@server anysda-failover-watchdog strongswan-starter anysda-ikev2-routing; do
    systemctl cat "$item" >/dev/null 2>&1 || continue
    systemctl start "$item" >/dev/null 2>&1 || down+=("$item")
  done
  if [[ -f /etc/wireguard/wg0.conf ]]; then
    systemctl restart wg-quick@wg0 >/dev/null 2>&1 || down+=(wg-quick@wg0)
  fi
  command -v swanctl >/dev/null 2>&1 && swanctl --load-all >/dev/null 2>&1
  if [[ -d "$BUNDLE_DIR/caddy-storage" ]]; then
    for item in caddy "${CADDY_SYNC_UNITS[@]}"; do
      systemctl start "$item" >/dev/null 2>&1 || down+=("$item")
    done
  fi
  if (( CONTROL_STOPPED )); then
    systemctl start vpn2-control.socket vpn2-control.service >/dev/null 2>&1 || down+=(vpn2-control)
  fi
  for item in anysda-vpn2 anysda-tgbot; do
    if docker container inspect "$item" >/dev/null 2>&1; then
      docker start "$item" >/dev/null 2>&1 || down+=("$item")
    fi
  done
  echo "anysda-restore: возвращено из страховочной копии: ${back[*]:-ничего не заменялось}" >&2
  (( ${#failed[@]} == 0 )) || echo "anysda-restore: НЕ возвращено: ${failed[*]} — бери из $PRESNAP_FINAL" >&2
  if (( ${#down[@]} )); then
    echo "anysda-restore: не запустились: ${down[*]}" >&2
  else
    echo "anysda-restore: службы и контейнеры прежнего состояния запущены" >&2
  fi
  echo "anysda-restore: страховочный снимок: $PRESNAP_FINAL" >&2
  exit 10
}
set -E
trap 'rollback $? "$BASH_COMMAND"' ERR
trap 'rollback 130 "получен сигнал INT"' INT
trap 'rollback 143 "получен сигнал TERM"' TERM

# Сбой запуска существующей службы — тоже сбой восстановления.
start_unit() {
  STEP="запуск службы $1"
  if systemctl cat "$1" >/dev/null 2>&1; then
    systemctl start "$1"
  fi
}

# ── Остановка сервисов перед swap ───────────────────────────────────────────
echo "anysda-restore: останавливаю сервисы…"
# Watcher не должен копировать промежуточную пару cert/key в swanctl.
# Старый бандл без storage оставляет Caddy и его данные как есть.
CADDY_SYNC_UNITS=()
if [[ -d "$BUNDLE_DIR/caddy-storage" ]]; then
  for unit in anysda-ikev2-cert-sync.path anysda-ikev2-cert-sync.timer; do
    if systemctl is-active --quiet "$unit"; then
      CADDY_SYNC_UNITS+=("$unit")
      systemctl stop "$unit"
    fi
  done
  if systemctl cat anysda-ikev2-cert-sync.service >/dev/null 2>&1; then
    systemctl stop anysda-ikev2-cert-sync.service
  fi
  # Ошибка stop недопустима: Caddy мог бы перезаписать восстановленный cert.
  systemctl stop caddy
fi
systemctl stop anysda-failover-watchdog 2>/dev/null || true
systemctl stop sing-box 2>/dev/null || true
systemctl stop openvpn-server@server 2>/dev/null || true
systemctl stop anysda-ikev2-routing 2>/dev/null || true
systemctl stop strongswan-starter 2>/dev/null || true
for container in anysda-vpn2 anysda-tgbot; do
  # Отсутствующий контейнер допустим на новой ноде; работающий писатель — нет.
  if docker container inspect "$container" >/dev/null 2>&1; then
    docker stop "$container" >/dev/null
  else
    # Отличаем отсутствие контейнера от недоступного Docker daemon.
    docker info >/dev/null
  fi
done

# ── Применение ──────────────────────────────────────────────────────────────
# Каждый блок защищён от отсутствия файла в архиве — допускаем частичные бэкапы
STEP="установка БД"
if [[ -f "$BUNDLE_DIR/db.sqlite" ]]; then
  TOUCHED+=(db)
  if id vpn2-control >/dev/null 2>&1; then
    # An explicitly restored DB starts a new control epoch. Its persisted
    # versions may precede the current service journal (including old backups).
    # Keep the previous journal in the encrypted safety snapshot, not active.
    CONTROL_STOPPED=1
    systemctl stop vpn2-control.service vpn2-control.socket
    [[ ! -d /var/lib/vpn2-control ]] || mv /var/lib/vpn2-control "$WORK/control-before-restore"
    install -d -o vpn2-control -g vpn2-control -m 0700 /var/lib/vpn2-control
  fi
  mkdir -p /var/lib/anysda-vpn2
  # После остановки писателей старые WAL/SHM не должны примениться к новой
  # базе. Сначала готовим файл рядом, затем атомарно заменяем основной файл;
  # WAL/SHM откладываем в WORK, чтобы откат вернул базу байт в байт.
  install -m 600 "$BUNDLE_DIR/db.sqlite" /var/lib/anysda-vpn2/.db-restore.sqlite
  mkdir -p "$WORK/db-before"
  for suffix in wal shm journal; do
    [[ ! -e /var/lib/anysda-vpn2/db.sqlite-$suffix ]] || mv "/var/lib/anysda-vpn2/db.sqlite-$suffix" "$WORK/db-before/"
  done
  mv /var/lib/anysda-vpn2/.db-restore.sqlite /var/lib/anysda-vpn2/db.sqlite
  DB_SWAPPED=1
  chown root:root /var/lib/anysda-vpn2/db.sqlite
fi
STEP="восстановление /etc/anysda"
if [[ -d "$BUNDLE_DIR/etc-anysda" ]]; then
  TOUCHED+=(etc/anysda)
  copy_into "$BUNDLE_DIR/etc-anysda" /etc/anysda
  # Старые архивы не содержат настроек SSO из профиля. Их отсутствие тоже
  # восстанавливаем, иначе настройки текущей панели перекроют архивный засев.
  if [[ ! -f "$BUNDLE_DIR/etc-anysda/sso-runtime.json" ]]; then
    rm -f /etc/anysda/sso-runtime.json
  fi
  # backup.env мог не быть в архиве (старый формат) — оставляем текущий нетронутым
fi
STEP="восстановление WireGuard"
if [[ -f "$BUNDLE_DIR/etc-wireguard/wg0.conf" ]]; then
  TOUCHED+=(etc/wireguard)
  mkdir -p /etc/wireguard
  cp -a "$BUNDLE_DIR/etc-wireguard/wg0.conf" /etc/wireguard/wg0.conf
  chmod 600 /etc/wireguard/wg0.conf
fi
if [[ -f "$WG_BUNDLE/server.priv" ]]; then
  # Стадия 28 переиспользует keys/. Если обновить только верхний уровень,
  # следующий деплой вернёт ключ, который был на хосте до restore.
  TOUCHED+=(etc/wireguard)
  for dir in /etc/wireguard /etc/wireguard/keys; do
    [[ -d "$dir" ]] || install -d -m 700 "$dir"
  done
  for key in server.priv server.pub; do
    install -m 600 "$WG_BUNDLE/$key" "/etc/wireguard/keys/$key"
    ln -sfn "keys/$key" "/etc/wireguard/$key"
  done
fi
STEP="распаковка PKI OpenVPN"
if [[ -d "$BUNDLE_DIR/etc-openvpn" ]]; then
  TOUCHED+=(etc/openvpn)
  copy_into "$BUNDLE_DIR/etc-openvpn" /etc/openvpn
fi
# IKEv2 (strongSwan + swanctl). Восстанавливаем CA, server cert/key, conf'ы.
# Если IP entry поменялся — server cert умрёт по SAN; стадия 27-ikev2 при
# следующем прогоне перевыпустит его тем же CA.
STEP="восстановление IKEv2"
if [[ -d "$BUNDLE_DIR/etc-strongswan" ]]; then
  TOUCHED+=(etc/strongswan)
  copy_into "$BUNDLE_DIR/etc-strongswan" /etc/strongswan
fi
if [[ -d "$BUNDLE_DIR/etc-swanctl" ]]; then
  TOUCHED+=(etc/swanctl)
  copy_into "$BUNDLE_DIR/etc-swanctl" /etc/swanctl
fi
for name in strongswan.conf strongswan.d ipsec.conf ipsec.secrets; do
  if [[ -d "$BUNDLE_DIR/etc-$name" ]]; then
    TOUCHED+=("etc/$name")
    copy_into "$BUNDLE_DIR/etc-$name" "/etc/$name"
  elif [[ -f "$BUNDLE_DIR/etc-$name" ]]; then
    TOUCHED+=("etc/$name")
    cp -a "$BUNDLE_DIR/etc-$name" "/etc/$name"
  fi
done

# Применяем до любого старта Caddy (в том числе из стадии 30). UID/GID на
# новом хосте отличаются; архивные числовые владельцы не годятся.
STEP="восстановление хранилища Caddy"
if [[ -d "$BUNDLE_DIR/caddy-storage" ]]; then
  TOUCHED+=("${CADDY_STORAGE#/}" etc/anysda)
  install -d -m 700 -o caddy -g caddy /var/lib/caddy/.local /var/lib/caddy/.local/share
  rm -rf -- "$CADDY_STORAGE"
  cp -a "$BUNDLE_DIR/caddy-storage" "$CADDY_STORAGE"
  rm -rf -- "$CADDY_STORAGE/locks"
  chown -R caddy:caddy "$CADDY_STORAGE"
  find "$CADDY_STORAGE" -type d -exec chmod 700 {} +
  find "$CADDY_STORAGE" -type f -exec chmod 600 {} +
  # Маркер из архива не доказывает, что creds загружены в текущий charon.
  rm -f /etc/anysda/ikev2-cert.sha256
  echo "anysda-restore: Caddy storage restored (caddy:caddy, dirs=700 files=600)"
else
  echo "anysda-restore: Caddy storage отсутствует в архиве — оставляю текущее"
fi

# ── Старт сервисов обратно ──────────────────────────────────────────────────
echo "anysda-restore: запускаю сервисы…"
STEP="запуск служб"
start_unit sing-box
# wg0 не останавливаем: рестарт обнуляет рукопожатия всем WG-клиентам. При
# прежних адресах и порте wg0_apply кладёт ключи и пиры `wg syncconf`.
if [[ -f /etc/wireguard/wg0.conf ]]; then
  if [[ -r /usr/local/lib/anysda/anysda-svc.sh ]]; then
    # shellcheck source=infra/lib/anysda-svc.sh
    source /usr/local/lib/anysda/anysda-svc.sh
    wg0_apply || echo "anysda-restore: WARN: wg0 не применился" >&2
  else
    systemctl restart wg-quick@wg0 2>/dev/null || true
  fi
fi
start_unit openvpn-server@server
start_unit anysda-failover-watchdog
start_unit strongswan-starter
start_unit anysda-ikev2-routing
# Reload swanctl creds/conns/pools после восстановления конфигов.
command -v swanctl >/dev/null 2>&1 && swanctl --load-all >/dev/null 2>&1 || true
if [[ -d "$BUNDLE_DIR/caddy-storage" ]]; then
  systemctl start caddy
  if [[ -s /etc/anysda/ikev2-domain ]] && systemctl cat anysda-ikev2-cert-sync.service >/dev/null 2>&1; then
    systemctl start anysda-ikev2-cert-sync.service
  fi
  if (( ${#CADDY_SYNC_UNITS[@]} )); then
    systemctl start "${CADDY_SYNC_UNITS[@]}"
  fi
fi
if id vpn2-panel >/dev/null 2>&1; then
  TOUCHED+=(var/lib/anysda-vpn2/runtime etc/anysda)
  install -d -o vpn2-panel -g vpn2-panel -m 0700 /var/lib/anysda-vpn2/runtime
  for name in sso-runtime.json telegram-runtime.json; do
    runtime=/var/lib/anysda-vpn2/runtime/$name
    if [[ -f /etc/anysda/$name && ! -L /etc/anysda/$name ]]; then
      install -o vpn2-panel -g vpn2-panel -m 0640 "/etc/anysda/$name" "$runtime"
    elif [[ ! -e /etc/anysda/$name ]]; then
      rm -f "$runtime"
    fi
    ln -sfn "$runtime" "/etc/anysda/$name"
  done
  chown vpn2-panel:vpn2-panel /var/lib/anysda-vpn2
  [[ ! -f /var/lib/anysda-vpn2/db.sqlite ]] || chown -h vpn2-panel:vpn2-panel /var/lib/anysda-vpn2/db.sqlite
fi
if id vpn2-control >/dev/null 2>&1; then
  systemctl start vpn2-control.socket vpn2-control.service
fi
docker start anysda-vpn2 anysda-tgbot 2>/dev/null || true
trap - ERR INT TERM
set +E

# ── Pere-sync AGH bcrypt (admin-password.txt мог вернуться из бэкапа, а
#    /opt/AdGuardHome/AdGuardHome.yaml — нет, и hash остался от старого
#    пароля → 403). Стадия 22-adguard идемпотентно перевыпустит hash. ─────
# Стадии при деплое получают склейку envs/all.env + envs/ru.env (run_stage в
# infra/deploy.sh): AGH_PORT и прочие общие переменные лежат в all.env, и на
# одном ru.env 22-adguard падает сразу. Собираем env так же, как деплой.
ENVS_DIR=/opt/anysda-vpn2/infra/envs
STAGE_ENV="$WORK/ru.env"
if [[ -r "$ENVS_DIR/all.env" && -r "$ENVS_DIR/ru.env" ]]; then
  { cat "$ENVS_DIR/all.env"; echo; grep -v '^SSH_PASS=' "$ENVS_DIR/ru.env";
    # Стадии должны читать восстановленный файл, а не перетирать его засевом.
    echo 'unset ADMIN_PASSWORD'; } > "$STAGE_ENV"
  chmod 600 "$STAGE_ENV"
fi

# Контейнеры панели и бота получают пароль админа, секреты clash и бота через
# env при создании, `docker start` выше оставил им значения прежнего деплоя:
# панель не входила в AdGuard паролем из бэкапа. Стадии 30 и 35 пересоздают их.
STAGE_FAIL=""
for stage in 22-adguard 30-frontend 35-telegram; do
  script=/opt/anysda-vpn2/infra/scripts/$stage.sh
  if [[ ! -x "$script" ]]; then
    STAGE_FAIL="нет $script"
  elif [[ ! -s "$STAGE_ENV" ]]; then
    STAGE_FAIL="нет $ENVS_DIR/all.env или $ENVS_DIR/ru.env (их пишет ./deploy.sh из config.yaml)"
  else
    echo "anysda-restore: $stage с восстановленными паролями…"
    if "$script" "$STAGE_ENV" >"$WORK/$stage.log" 2>&1; then
      echo "anysda-restore: $stage re-synced"
      continue
    fi
    STAGE_FAIL="$stage завершился с ошибкой, хвост вывода:"$'\n'"$(tail -n 15 "$WORK/$stage.log" | sed 's/^/    /')"
  fi
  echo "anysda-restore: ERROR: пароли не пересинхронизированы — $STAGE_FAIL" >&2
  break
done

# ── Smoke-test через 99-verify ──────────────────────────────────────────────
VERIFY=/opt/anysda-vpn2/infra/scripts/99-verify.sh
if [[ -x "$VERIFY" && -s "$STAGE_ENV" ]]; then
  echo "anysda-restore: 99-verify…"
  if "$VERIFY" "$STAGE_ENV"; then
    echo "anysda-restore: 99-verify PASSED"
  else
    verify_rc=$?
    if [[ $verify_rc -eq 2 ]]; then
      echo 'anysda-restore: ⚠ 99-verify: не все проверки измерены; причины выше' >&2
    else
      echo "anysda-restore: 99-verify FAILED — проверь руками; pre-restore снимок: $PRESNAP_FINAL" >&2
      exit 7
    fi
  fi
else
  echo "anysda-restore: WARN: нет 99-verify ($VERIFY) или env стадий — пропуск smoke-test" >&2
fi

# Данные восстановлены, но вход с паролем из бэкапа не пройдёт — это не SUCCESS.
if [[ -n "$STAGE_FAIL" ]]; then
  cat >&2 <<-EOM

	anysda-restore: FAILED — данные восстановлены, но пароли не пересинхронизированы:
	  $STAGE_FAIL
	  Почини причину и прогони ./deploy.sh <стадия> ru; pre-restore снимок: $PRESNAP_FINAL
	EOM
  exit 8
fi

cat <<-EOM

	anysda-restore: SUCCESS
	  source:       $ARCHIVE
	  pre-snapshot: $PRESNAP_FINAL
	  Существующие клиенты должны переподключаться без переоформления конфигов.
EOM
