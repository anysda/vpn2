#!/usr/bin/env bash
# Stage 26 — backup tooling on RU.
# Раскатывает /usr/local/bin/anysda-{backup,restore,backup-list}.sh
# Рендерит /etc/anysda/backup.env (chmod 600) с passphrase + S3-кредами из
# config.yaml. Опционально ставит systemd timer на ежедневный backup в 02:00 UTC.
# Запускается ТОЛЬКО на ru. На exit-нодах skip.
#
# Idempotent: повторный запуск перезаписывает скрипты+env, рестартит timer.
#
# Зависимости (apt): sqlite3 (для .backup), age + expect (шифрование с
# passphrase из env), aws-cli (только если backend: s3).

set -euo pipefail

[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}"
# Ожидание блокировок apt с понятной строкой в лог (infra/lib/anysda-svc.sh):
# деплой кладёт библиотеку рядом со стадией, anysda-restore зовёт стадию из клона репо.
SVC_LIB=$(dirname "$0")/anysda-svc.sh
[[ -f "$SVC_LIB" ]] || SVC_LIB=$(dirname "$0")/../lib/anysda-svc.sh
# shellcheck source=infra/lib/anysda-svc.sh
source "$SVC_LIB"

case "$HOST_TAG" in ru) ;; *) echo "[$HOST_TAG] 26-backup is ru-only — skipping"; exit 0;; esac

# Если backup-секция в config.yaml отсутствует или enabled != true — стадия
# снимаем прежний таймер; архивы и passphrase нужны для восстановления.
if [[ "${BACKUP_ENABLED:-}" != "true" ]]; then
  systemctl disable --now anysda-backup.timer >/dev/null 2>&1 || true
  echo "[$HOST_TAG] 26-backup: backup.enabled != true — пропуск"
  exit 0
fi

: "${BACKUP_BACKEND:?BACKUP_BACKEND не задан в env}"
: "${BACKUP_LOCAL_DIR:=/var/backups/anysda-vpn2}"
: "${BACKUP_RETENTION:=10}"
: "${BACKUP_SCHEDULE:=off}"
: "${BACKUP_PASSPHRASE:?BACKUP_PASSPHRASE пуст — заполни setup.sh или backup.passphrase в config.yaml}"

STAMP_DIR=/var/anysda/.stamps
STAGE='26-backup'

mkdir -p "$STAMP_DIR" /etc/anysda "$BACKUP_LOCAL_DIR" "$BACKUP_LOCAL_DIR/.pre-restore"
chmod 700 "$BACKUP_LOCAL_DIR" "$BACKUP_LOCAL_DIR/.pre-restore"

# ── 1. Apt deps: age + expect + sqlite3 ─────────────────────────────────────
# aws-cli ставится отдельно (Ubuntu 24.04 выкинул пакет awscli из репов —
# используем официальный бинарь aws-cli v2 от Amazon).
echo "[$HOST_TAG] [1/4] apt deps (age, expect, sqlite3$([[ "$BACKUP_BACKEND" == "s3" ]] && echo ' + aws-cli v2'))"
APT_PKGS="age expect sqlite3"
[[ "$BACKUP_BACKEND" == "s3" ]] && APT_PKGS="$APT_PKGS unzip curl gnupg"
export DEBIAN_FRONTEND=noninteractive
# Списки обновлял 00-bootstrap этого же деплоя минуты назад - второй
# apt-get update ~5 с впустую. Каталог списков старше часа - обновляем;
# пакет не нашёлся по старым спискам - обновляем и ставим ещё раз.
apt_install() {
  [[ -n "$(find /var/lib/apt/lists -maxdepth 0 -mmin -60)" ]] || apt_get update -qq
  apt_get install -y -qq "$@" >/dev/null 2>&1 && return 0
  apt_get update -qq && apt_get install -y -qq "$@" >/dev/null
}
# shellcheck disable=SC2086
apt_have $APT_PKGS || apt_install $APT_PKGS

if [[ "$BACKUP_BACKEND" == "s3" ]] && ! command -v aws >/dev/null 2>&1; then
  # Для обновления релиза меняется только эта версия; подпись проверяется
  # постоянным официальным ключом AWS CLI Team, а не ключом из загрузки.
  AWS_CLI_VERSION=2.37.12
  AWS_CLI_URL="https://awscli.amazonaws.com/awscli-exe-linux-x86_64-${AWS_CLI_VERSION}.zip"
  echo "[$HOST_TAG]   download aws-cli v${AWS_CLI_VERSION}…"
  TMPD=$(mktemp -d)
  trap 'rm -rf "$TMPD"' EXIT
  # Изолированный keyring: пользовательские ключи и gpg.conf не участвуют.
  mkdir -m 700 "$TMPD/gnupg"
  # https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html
  # Fingerprint: FB5DB77FD5C118B80511ADA8A6310ACC4672475C
  gpg --no-options --homedir "$TMPD/gnupg" --batch --no-autostart --import <<'AWS_CLI_KEY'
-----BEGIN PGP PUBLIC KEY BLOCK-----

mQINBF2Cr7UBEADJZHcgusOJl7ENSyumXh85z0TRV0xJorM2B/JL0kHOyigQluUG
ZMLhENaG0bYatdrKP+3H91lvK050pXwnO/R7fB/FSTouki4ciIx5OuLlnJZIxSzx
PqGl0mkxImLNbGWoi6Lto0LYxqHN2iQtzlwTVmq9733zd3XfcXrZ3+LblHAgEt5G
TfNxEKJ8soPLyWmwDH6HWCnjZ/aIQRBTIQ05uVeEoYxSh6wOai7ss/KveoSNBbYz
gbdzoqI2Y8cgH2nbfgp3DSasaLZEdCSsIsK1u05CinE7k2qZ7KgKAUIcT/cR/grk
C6VwsnDU0OUCideXcQ8WeHutqvgZH1JgKDbznoIzeQHJD238GEu+eKhRHcz8/jeG
94zkcgJOz3KbZGYMiTh277Fvj9zzvZsbMBCedV1BTg3TqgvdX4bdkhf5cH+7NtWO
lrFj6UwAsGukBTAOxC0l/dnSmZhJ7Z1KmEWilro/gOrjtOxqRQutlIqG22TaqoPG
fYVN+en3Zwbt97kcgZDwqbuykNt64oZWc4XKCa3mprEGC3IbJTBFqglXmZ7l9ywG
EEUJYOlb2XrSuPWml39beWdKM8kzr1OjnlOm6+lpTRCBfo0wa9F8YZRhHPAkwKkX
XDeOGpWRj4ohOx0d2GWkyV5xyN14p2tQOCdOODmz80yUTgRpPVQUtOEhXQARAQAB
tCFBV1MgQ0xJIFRlYW0gPGF3cy1jbGlAYW1hem9uLmNvbT6JAlQEEwEIAD4CGwMF
CwkIBwIGFQoJCAsCBBYCAwECHgECF4AWIQT7Xbd/1cEYuAURraimMQrMRnJHXAUC
akV0ygUJDqP4lQAKCRCmMQrMRnJHXFHjD/9eyZLYcKuQOlLvtqSDtUBiEZf6ZZjM
i3ygYH8rJNtuToUH+HvSpe819urJCquXhDrlK6N+aqW0hCLtNABJG/vsafIgvIYJ
hSGgpgtNnQyMV1jViRWqPjbouw8OkYKBThUfT1i2Y+wn58ifs6ODBCmTexWtXspA
Si+Gt49xDOW0APmbOPnI+a4HJW6tVEo6MWS0WjzpiBayR3d1A4pt4YrPfSdDgpLo
h2SLQqlRqvvVZJaWBjhkErNFpfsBA06sDcPEOb0G8LBUbR4WOcdvhe5LubJbZuxC
AG9kNPCVeQP1ixwjgjXKysaxeQ6rv0VzIQgRp6tLVLWhy6AKDNvLjFSsmXZ1Wl08
Y/RlOHXlzLuQMRE6sR1wOdRxc9TsrNWTGiBK65cvSWOy03JeBkQQ8pesqltiyxI9
U21kkgiXtTSKNGfKK8pO27D81YANhRqPK7iTp6kuFiY2WtOg90KTMNlIT+Ff85Y2
b1rHj6Z0SrCkJujhWk3IBPic/wJgz01LEc/OAdUPlby90RJZcIBhSlWhT7mXnXIO
c0HWlNQrns2s3CTyYwZSiSlYe9ApeLwhjDo8NhbFuCAy61l6O5UsR4AfZxx/rGKv
2wFb1/RN/P4gNe6vmxZAPjR0AQcwD3tc2McimOLr/22kmPz8IH3I0X7WoSFr0Biz
E91G7bb0hOb/cA==
=knv7
-----END PGP PUBLIC KEY BLOCK-----
AWS_CLI_KEY
  curl -fsSL "$AWS_CLI_URL" -o "$TMPD/awscli.zip"
  curl -fsSL "$AWS_CLI_URL.sig" -o "$TMPD/awscli.zip.sig"
  if ! gpg --no-options --homedir "$TMPD/gnupg" --batch --no-autostart --no-auto-key-retrieve \
    --verify "$TMPD/awscli.zip.sig" "$TMPD/awscli.zip"; then
    echo "[$HOST_TAG] ERROR: AWS CLI signature verification failed" >&2
    exit 1
  fi
  unzip -q "$TMPD/awscli.zip" -d "$TMPD"
  "$TMPD/aws/install" --bin-dir /usr/local/bin --install-dir /usr/local/aws-cli --update >/dev/null
  rm -rf "$TMPD"
  trap - EXIT
  aws --version
fi

# ── 2. Раскатываем скрипты (push'нуты из infra/lib/ оркестратором) ──────────
echo "[$HOST_TAG] [2/4] /usr/local/bin/anysda-{backup,restore,backup-list}.sh"
install -m 0755 /tmp/anysda/anysda-backup.sh      /usr/local/bin/anysda-backup.sh
install -m 0755 /tmp/anysda/anysda-restore.sh     /usr/local/bin/anysda-restore.sh
install -m 0755 /tmp/anysda/anysda-backup-list.sh /usr/local/bin/anysda-backup-list.sh
# Общие функции стадий: anysda-restore берёт отсюда wg0_apply.
install -D -m 0644 /tmp/anysda/anysda-svc.sh     /usr/local/lib/anysda/anysda-svc.sh

# ── 3. /etc/anysda/backup.env — единственное место где passphrase живёт на ─
#    entry. chmod 600, root:root.
echo "[$HOST_TAG] [3/4] /etc/anysda/backup.env"
cat > /etc/anysda/backup.env <<EOF
# Generated by stage 26-backup. НЕ редактировать вручную — перезаписывается
# на каждом ./deploy.sh 26-backup ru. Источник истины — config.yaml на
# оркестраторе.
BACKUP_BACKEND='$BACKUP_BACKEND'
BACKUP_LOCAL_DIR='$BACKUP_LOCAL_DIR'
BACKUP_RETENTION='$BACKUP_RETENTION'
ANYSDA_BACKUP_PASSPHRASE='$(printf '%s' "$BACKUP_PASSPHRASE" | sed "s/'/'\\\\''/g")'
EOF

if [[ "$BACKUP_BACKEND" == "s3" ]]; then
  : "${BACKUP_S3_ENDPOINT:?BACKUP_S3_ENDPOINT не задан}"
  : "${BACKUP_S3_BUCKET:?BACKUP_S3_BUCKET не задан}"
  : "${BACKUP_S3_ACCESS_KEY:?BACKUP_S3_ACCESS_KEY не задан}"
  : "${BACKUP_S3_SECRET_KEY:?BACKUP_S3_SECRET_KEY не задан}"
  cat >> /etc/anysda/backup.env <<EOF
BACKUP_S3_ENDPOINT='$BACKUP_S3_ENDPOINT'
BACKUP_S3_BUCKET='$BACKUP_S3_BUCKET'
BACKUP_S3_REGION='${BACKUP_S3_REGION:-}'
ANYSDA_S3_ACCESS_KEY='$(printf '%s' "$BACKUP_S3_ACCESS_KEY" | sed "s/'/'\\\\''/g")'
ANYSDA_S3_SECRET_KEY='$(printf '%s' "$BACKUP_S3_SECRET_KEY" | sed "s/'/'\\\\''/g")'
EOF
fi
chmod 600 /etc/anysda/backup.env

# ── 4. Systemd timer (только если schedule: daily) ──────────────────────────
echo "[$HOST_TAG] [4/4] systemd (schedule=$BACKUP_SCHEDULE)"
cat > /etc/systemd/system/anysda-backup.service <<'EOF'
[Unit]
Description=anysda-vpn2 — encrypted DB+secrets backup
Wants=anysda-vpn2.target

[Service]
Type=oneshot
EnvironmentFile=/etc/anysda/backup.env
ExecStart=/usr/local/bin/anysda-backup.sh
# Логи приходят в journalctl -u anysda-backup
StandardOutput=journal
StandardError=journal
EOF

cat > /etc/systemd/system/anysda-backup.timer <<'EOF'
[Unit]
Description=Daily anysda-vpn2 backup at 02:00 (local time of entry node)

[Timer]
# OnCalendar без суффикса TZ → systemd берёт системную таймзону entry-ноды.
# Хочешь МСК — `timedatectl set-timezone Europe/Moscow` на entry, таймер
# подхватит автоматически после daemon-reload.
OnCalendar=*-*-* 02:00:00
Persistent=true
Unit=anysda-backup.service
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
if [[ "$BACKUP_SCHEDULE" == "daily" ]]; then
  systemctl enable --now anysda-backup.timer >/dev/null 2>&1
  echo "[$HOST_TAG]   timer enabled, next run:"
  systemctl list-timers anysda-backup.timer --no-pager 2>/dev/null \
    | grep -v '^$' | sed -n "1,3s/^/[$HOST_TAG]   /p" || true
else
  systemctl disable --now anysda-backup.timer >/dev/null 2>&1 || true
  echo "[$HOST_TAG]   timer disabled (schedule: off)"
fi

mkdir -p "$STAMP_DIR"
touch "$STAMP_DIR/$STAGE"
echo "[$HOST_TAG] $STAGE done — try /usr/local/bin/anysda-backup.sh"
