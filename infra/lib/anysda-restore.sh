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
#   - стоп → swap → start affected services
#   - 99-verify по завершению (если найден)

set -euo pipefail

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

BUNDLE_DIR=$(find "$WORK" -maxdepth 1 -mindepth 1 -type d -name 'anysda-vpn2-*' | head -1)
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

# ── Защита от перетирания не-пустой БД ─────────────────────────────────────
LIVE_DB=/var/lib/anysda-vpn2/db.sqlite
LIVE_DB_NONEMPTY=0
if [[ -f "$LIVE_DB" ]]; then
  LIVE_DB_SIZE=$(stat -c %s "$LIVE_DB")
  LIVE_CLIENTS=$(sqlite3 "$LIVE_DB" 'SELECT COUNT(*) FROM clients;' 2>/dev/null || echo 0)
  if (( LIVE_DB_SIZE > 0 )) && (( LIVE_CLIENTS > 0 )); then
    LIVE_DB_NONEMPTY=1
    if (( FORCE == 0 )); then
      cat >&2 <<-MSG
	anysda-restore: ABORT — текущая db.sqlite не пустая ($LIVE_CLIENTS клиентов, $LIVE_DB_SIZE байт).
	Будут перетёрты: db.sqlite, /etc/anysda/*, /etc/wireguard/wg0.conf, /etc/openvpn/*
	Запусти повторно с --force чтобы согласиться.
	MSG
      exit 6
    fi
  fi
fi

# ── Pre-restore safety snapshot — отдельная директория, исключена из ротации ─
PRESNAP_DIR="$BACKUP_LOCAL_DIR/.pre-restore"
mkdir -p "$PRESNAP_DIR"; chmod 700 "$PRESNAP_DIR"
PRESNAP_TS=$(date -u +%Y-%m-%dT%H%M%SZ)
PRESNAP_TAR="$WORK/pre-restore-$PRESNAP_TS.tar.gz"
PRESNAP_FINAL="$PRESNAP_DIR/pre-restore-$PRESNAP_TS.tar.gz.age"

echo "anysda-restore: pre-restore snapshot → $PRESNAP_FINAL"
# Снимок текущего состояния в plaintext tar.gz внутри WORK
{
  cd /
  tar -czf "$PRESNAP_TAR" \
    var/lib/anysda-vpn2/db.sqlite \
    etc/anysda \
    etc/wireguard/wg0.conf \
    etc/openvpn 2>/dev/null || true
}
# Шифруем (тот же passphrase)
expect <<EXPECT > /dev/null
log_user 0
set timeout 60
set pass \$env(ANYSDA_BACKUP_PASSPHRASE)
spawn -noecho age -p -o "$PRESNAP_FINAL" "$PRESNAP_TAR"
expect {
  "Enter passphrase*"   { send -- "\$pass\r"; exp_continue }
  "Confirm passphrase*" { send -- "\$pass\r"; exp_continue }
  eof
}
catch wait result
exit [lindex \$result 3]
EXPECT
shred -uz "$PRESNAP_TAR" 2>/dev/null || rm -f "$PRESNAP_TAR"
chmod 600 "$PRESNAP_FINAL"
echo "anysda-restore: pre-restore snapshot saved ($(stat -c %s "$PRESNAP_FINAL") bytes)"

# ── Остановка сервисов перед swap ───────────────────────────────────────────
echo "anysda-restore: останавливаю сервисы…"
systemctl stop anysda-failover-watchdog 2>/dev/null || true
systemctl stop sing-box 2>/dev/null || true
systemctl stop wg-quick@wg0 2>/dev/null || true
systemctl stop openvpn-server@server 2>/dev/null || true
systemctl stop anysda-ikev2-routing 2>/dev/null || true
systemctl stop strongswan-starter 2>/dev/null || true
docker stop anysda-vpn2 anysda-tgbot 2>/dev/null || true

# ── Применение ──────────────────────────────────────────────────────────────
# Каждый блок защищён от отсутствия файла в архиве — допускаем частичные бэкапы
if [[ -f "$BUNDLE_DIR/db.sqlite" ]]; then
  mkdir -p /var/lib/anysda-vpn2
  cp -a "$BUNDLE_DIR/db.sqlite" /var/lib/anysda-vpn2/db.sqlite
  chown root:root /var/lib/anysda-vpn2/db.sqlite
fi
if [[ -d "$BUNDLE_DIR/etc-anysda" ]]; then
  mkdir -p /etc/anysda
  cp -a "$BUNDLE_DIR/etc-anysda/." /etc/anysda/
  # backup.env мог не быть в архиве (старый формат) — оставляем текущий нетронутым
fi
if [[ -f "$BUNDLE_DIR/etc-wireguard/wg0.conf" ]]; then
  mkdir -p /etc/wireguard
  cp -a "$BUNDLE_DIR/etc-wireguard/wg0.conf" /etc/wireguard/wg0.conf
  chmod 600 /etc/wireguard/wg0.conf
fi
if [[ -d "$BUNDLE_DIR/etc-openvpn" ]]; then
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

# ── Старт сервисов обратно ──────────────────────────────────────────────────
echo "anysda-restore: запускаю сервисы…"
systemctl start sing-box 2>/dev/null || true
systemctl start wg-quick@wg0 2>/dev/null || true
systemctl start openvpn-server@server 2>/dev/null || true
systemctl start anysda-failover-watchdog 2>/dev/null || true
systemctl start strongswan-starter 2>/dev/null || true
systemctl start anysda-ikev2-routing 2>/dev/null || true
# Reload swanctl creds/conns/pools после восстановления конфигов.
command -v swanctl >/dev/null 2>&1 && swanctl --load-all >/dev/null 2>&1 || true
docker start anysda-vpn2 anysda-tgbot 2>/dev/null || true

# ── Smoke-test через 99-verify ──────────────────────────────────────────────
VERIFY=/opt/anysda-vpn2/infra/scripts/99-verify.sh
VERIFY_ENV=/opt/anysda-vpn2/infra/envs/ru.env
if [[ -x "$VERIFY" && -r "$VERIFY_ENV" ]]; then
  echo "anysda-restore: 99-verify…"
  if "$VERIFY" "$VERIFY_ENV"; then
    echo "anysda-restore: 99-verify PASSED"
  else
    echo "anysda-restore: 99-verify FAILED — проверь руками; pre-restore снимок: $PRESNAP_FINAL" >&2
    exit 7
  fi
else
  echo "anysda-restore: WARN: 99-verify ($VERIFY) не найден — пропуск smoke-test" >&2
fi

cat <<-EOM

	anysda-restore: SUCCESS
	  source:       $ARCHIVE
	  pre-snapshot: $PRESNAP_FINAL
	  Существующие клиенты должны переподключаться без переоформления конфигов.
EOM
