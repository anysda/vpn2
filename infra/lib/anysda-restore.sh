#!/usr/bin/env bash
# anysda-vpn2 restore — восстанавливает entry-ноду из *.tar.gz.age бэкапа.
#
# Использование:
#   /usr/local/bin/anysda-restore.sh [<archive-name|latest>] [--force]
#     archive-name — имя файла из $BACKUP_LOCAL_DIR (только basename) или 'latest'
#     --force      — overwrite не-пустой db.sqlite без вопросов
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

ARCHIVE_REQ="${1:-latest}"
FORCE=0
shift 2>/dev/null || true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force|-f) FORCE=1 ;;
    *) echo "anysda-restore: неизвестный аргумент: $1" >&2; exit 2 ;;
  esac
  shift
done

if [[ -z "${ANYSDA_BACKUP_PASSPHRASE:-}" ]]; then
  echo "anysda-restore FATAL: ANYSDA_BACKUP_PASSPHRASE пуст в $ENV_FILE." >&2
  echo "  Заполни backup.passphrase в config.yaml и прогони ./deploy.sh 26-backup ru" >&2
  exit 2
fi

# ── Резолв архива (latest | имя). Поддерживаем local; для s3 — pull в кеш. ─
resolve_archive() {
  local req="$1"
  if [[ "$req" == "latest" ]]; then
    # Самый свежий по mtime, без парсинга ls
    local found
    found=$(find "$BACKUP_LOCAL_DIR" -maxdepth 1 -type f -name 'anysda-vpn2-*.tar.gz.age' \
            -printf '%T@\t%p\n' 2>/dev/null | sort -n | tail -1 | cut -f2)
    if [[ -z "$found" && "$BACKUP_BACKEND" == "s3" ]]; then
      # Тянем самый свежий ключ из bucket
      local s3_name
      s3_name=$(AWS_ACCESS_KEY_ID="${ANYSDA_S3_ACCESS_KEY:?}" AWS_SECRET_ACCESS_KEY="${ANYSDA_S3_SECRET_KEY:?}" \
        aws --endpoint-url "${BACKUP_S3_ENDPOINT:?}" s3 ls "s3://${BACKUP_S3_BUCKET:?}/" \
        | awk '{print $NF}' | grep -E '^anysda-vpn2-.*\.tar\.gz\.age$' | sort | tail -1)
      [[ -z "$s3_name" ]] && { echo "anysda-restore: ни одного backup'а ни локально, ни в S3" >&2; exit 3; }
      mkdir -p "$BACKUP_LOCAL_DIR"
      AWS_ACCESS_KEY_ID="$ANYSDA_S3_ACCESS_KEY" AWS_SECRET_ACCESS_KEY="$ANYSDA_S3_SECRET_KEY" \
        aws --endpoint-url "$BACKUP_S3_ENDPOINT" s3 cp \
        "s3://$BACKUP_S3_BUCKET/$s3_name" "$BACKUP_LOCAL_DIR/$s3_name" --only-show-errors
      chmod 600 "$BACKUP_LOCAL_DIR/$s3_name"   # aws s3 cp следует umask → выставляем явно
      found="$BACKUP_LOCAL_DIR/$s3_name"
    fi
    [[ -z "$found" ]] && { echo "anysda-restore: $BACKUP_LOCAL_DIR пуст — нечего восстанавливать" >&2; exit 3; }
    echo "$found"
    return
  fi
  # Явное имя
  local base; base=$(basename "$req")
  if [[ -f "$BACKUP_LOCAL_DIR/$base" ]]; then
    echo "$BACKUP_LOCAL_DIR/$base"; return
  fi
  if [[ "$BACKUP_BACKEND" == "s3" ]]; then
    mkdir -p "$BACKUP_LOCAL_DIR"
    AWS_ACCESS_KEY_ID="${ANYSDA_S3_ACCESS_KEY:?}" AWS_SECRET_ACCESS_KEY="${ANYSDA_S3_SECRET_KEY:?}" \
      aws --endpoint-url "${BACKUP_S3_ENDPOINT:?}" s3 cp \
      "s3://${BACKUP_S3_BUCKET:?}/$base" "$BACKUP_LOCAL_DIR/$base" --only-show-errors \
      || { echo "anysda-restore: $base не найден ни локально, ни в S3" >&2; exit 3; }
    chmod 600 "$BACKUP_LOCAL_DIR/$base"   # aws s3 cp следует umask → выставляем явно
    echo "$BACKUP_LOCAL_DIR/$base"; return
  fi
  echo "anysda-restore: архив $base не найден в $BACKUP_LOCAL_DIR" >&2
  exit 3
}

ARCHIVE=$(resolve_archive "$ARCHIVE_REQ")
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

python3 - "$BUNDLE_DIR" <<'PY' || exit 5
import hashlib, json, os, sys
bdir = sys.argv[1]
with open(os.path.join(bdir, 'manifest.json')) as f:
    m = json.load(f)

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

# ── Проверка совместимости версии (мягкая для v1: только warn) ──────────────
MANIFEST_SPEC=$(python3 -c "import json; print(json.load(open('$BUNDLE_DIR/manifest.json')).get('spec_version', 1))")
if [[ "$MANIFEST_SPEC" != "1" ]]; then
  echo "anysda-restore: WARN: archive spec_version=$MANIFEST_SPEC, current code понимает 1" >&2
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
fi
for path in \
  etc/anysda etc/wireguard etc/openvpn \
  etc/strongswan etc/swanctl etc/strongswan.conf etc/strongswan.d \
  etc/ipsec.conf etc/ipsec.secrets \
  etc/caddy var/lib/caddy/.local/share/caddy \
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
if [[ -f "$BUNDLE_DIR/db.sqlite" ]]; then
  mkdir -p /var/lib/anysda-vpn2
  # После остановки писателей старые WAL/SHM не должны примениться к новой
  # базе. Сначала готовим файл рядом, затем атомарно заменяем основной файл.
  install -m 600 "$BUNDLE_DIR/db.sqlite" /var/lib/anysda-vpn2/.db-restore.sqlite
  rm -f /var/lib/anysda-vpn2/db.sqlite-wal /var/lib/anysda-vpn2/db.sqlite-shm /var/lib/anysda-vpn2/db.sqlite-journal
  mv /var/lib/anysda-vpn2/.db-restore.sqlite /var/lib/anysda-vpn2/db.sqlite
  chown root:root /var/lib/anysda-vpn2/db.sqlite
fi
if [[ -d "$BUNDLE_DIR/etc-anysda" ]]; then
  mkdir -p /etc/anysda
  cp -a "$BUNDLE_DIR/etc-anysda/." /etc/anysda/
  # Старые архивы не содержат настроек SSO из профиля. Их отсутствие тоже
  # восстанавливаем, иначе настройки текущей панели перекроют архивный засев.
  if [[ ! -f "$BUNDLE_DIR/etc-anysda/sso-runtime.json" ]]; then
    rm -f /etc/anysda/sso-runtime.json
  fi
  # backup.env мог не быть в архиве (старый формат) — оставляем текущий нетронутым
fi
if [[ -f "$BUNDLE_DIR/etc-wireguard/wg0.conf" ]]; then
  mkdir -p /etc/wireguard
  cp -a "$BUNDLE_DIR/etc-wireguard/wg0.conf" /etc/wireguard/wg0.conf
  chmod 600 /etc/wireguard/wg0.conf
fi
if [[ -f "$WG_BUNDLE/server.priv" ]]; then
  # Стадия 28 переиспользует keys/. Если обновить только верхний уровень,
  # следующий деплой вернёт ключ, который был на хосте до restore.
  install -d -m 700 /etc/wireguard /etc/wireguard/keys
  for key in server.priv server.pub; do
    install -m 600 "$WG_BUNDLE/$key" "/etc/wireguard/keys/$key"
    ln -sfn "keys/$key" "/etc/wireguard/$key"
  done
fi
if [[ -d "$BUNDLE_DIR/etc-openvpn" ]]; then
  mkdir -p /etc/openvpn
  cp -a "$BUNDLE_DIR/etc-openvpn/." /etc/openvpn/
fi
# IKEv2 (strongSwan + swanctl). Восстанавливаем CA, server cert/key, conf'ы.
# Если IP entry поменялся — server cert умрёт по SAN; стадия 27-ikev2 при
# следующем прогоне перевыпустит его тем же CA.
if [[ -d "$BUNDLE_DIR/etc-strongswan" ]]; then
  mkdir -p /etc/strongswan
  cp -a "$BUNDLE_DIR/etc-strongswan/." /etc/strongswan/
fi
if [[ -d "$BUNDLE_DIR/etc-swanctl" ]]; then
  mkdir -p /etc/swanctl
  cp -a "$BUNDLE_DIR/etc-swanctl/." /etc/swanctl/
fi
for name in strongswan.conf strongswan.d ipsec.conf ipsec.secrets; do
  if [[ -d "$BUNDLE_DIR/etc-$name" ]]; then
    mkdir -p "/etc/$name"
    cp -a "$BUNDLE_DIR/etc-$name/." "/etc/$name/"
  elif [[ -f "$BUNDLE_DIR/etc-$name" ]]; then
    cp -a "$BUNDLE_DIR/etc-$name" "/etc/$name"
  fi
done

# Применяем до любого старта Caddy (в том числе из стадии 30). UID/GID на
# новом хосте отличаются; архивные числовые владельцы не годятся.
if [[ -d "$BUNDLE_DIR/caddy-storage" ]]; then
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
systemctl start sing-box 2>/dev/null || true
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
systemctl start openvpn-server@server 2>/dev/null || true
systemctl start anysda-failover-watchdog 2>/dev/null || true
systemctl start strongswan-starter 2>/dev/null || true
systemctl start anysda-ikev2-routing 2>/dev/null || true
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
docker start anysda-vpn2 anysda-tgbot 2>/dev/null || true

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
