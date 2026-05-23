#!/usr/bin/env bash
# anysda-vpn2 backup-list — табличный листинг доступных бэкапов в active backend.
# Для каждого: name, UTC-timestamp, archive size, manifest (version+commit) если
# можно прочитать без passphrase (manifest зашифрован — НЕ читаем; берём только
# мета по имени файла + размер).

set -uo pipefail

ENV_FILE=/etc/anysda/backup.env
[[ -r "$ENV_FILE" ]] || { echo "anysda-backup-list: $ENV_FILE не доступен" >&2; exit 2; }
# shellcheck disable=SC1090
source "$ENV_FILE"

: "${BACKUP_BACKEND:?BACKUP_BACKEND не задан}"
: "${BACKUP_LOCAL_DIR:=/var/backups/anysda-vpn2}"

printf '%-50s %-22s %12s\n' 'Name' 'Timestamp (UTC)' 'Size'
printf '%-50s %-22s %12s\n' '----' '---------------' '----'

# ── Локально ────────────────────────────────────────────────────────────────
if [[ -d "$BACKUP_LOCAL_DIR" ]]; then
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    name=$(basename "$f")
    # Имя формата anysda-vpn2-2026-05-23T020000Z.tar.gz.age → выдёргиваем TS
    ts=$(echo "$name" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z' | head -1)
    [[ -z "$ts" ]] && ts="?"
    sz=$(stat -c %s "$f" 2>/dev/null)
    printf '%-50s %-22s %12s\n' "$name" "$ts" "$(numfmt --to=iec "$sz" 2>/dev/null || echo "$sz")"
  done < <(find "$BACKUP_LOCAL_DIR" -maxdepth 1 -type f -name 'anysda-vpn2-*.tar.gz.age' \
           -printf '%T@\t%p\n' 2>/dev/null | sort -nr | cut -f2)
fi

# ── S3 (если активен) ──────────────────────────────────────────────────────
if [[ "$BACKUP_BACKEND" == "s3" ]]; then
  : "${BACKUP_S3_ENDPOINT:?}"; : "${BACKUP_S3_BUCKET:?}"
  : "${ANYSDA_S3_ACCESS_KEY:?}"; : "${ANYSDA_S3_SECRET_KEY:?}"
  echo
  echo "── S3: s3://$BACKUP_S3_BUCKET/ ──"
  AWS_ACCESS_KEY_ID="$ANYSDA_S3_ACCESS_KEY" AWS_SECRET_ACCESS_KEY="$ANYSDA_S3_SECRET_KEY" \
    aws --endpoint-url "$BACKUP_S3_ENDPOINT" s3 ls "s3://$BACKUP_S3_BUCKET/" 2>&1 \
    | grep -E 'anysda-vpn2-.*\.tar\.gz\.age$' \
    | sort -k1,2 -r \
    | awk '{
        # aws s3 ls: <date> <time> <size> <key>
        ts = $1 "T" $2 "Z";
        size = $3;
        key = $4;
        printf "  %-50s %-22s %12s\n", key, ts, size;
      }'
fi
