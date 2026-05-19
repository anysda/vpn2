#!/usr/bin/env bash
# Stage 35 — Telegram bot on RU.
#
# Builds a small Python image from source (pushed by orchestrator),
# runs it with --network host so it can reach:
#   - 127.0.0.1:51821  (anysda-vpn Nuxt API)
#   - 127.0.0.1:7897   (sing-box SOCKS5 inbound → foreign-best exit)
#
# Traffic to Telegram API goes via the SOCKS5 proxy, so the bot appears
# to Telegram as a foreign IP, not the RU datacenter.
#
# Skipped automatically if TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set.
# Prerequisites: stage 20 (sing-box with SOCKS5 inbound).

set -euo pipefail

[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}"

case "$HOST_TAG" in ru) ;; *) echo "[$HOST_TAG] 35-telegram is ru-only — skipping"; exit 0;; esac

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]] || [[ -z "${TELEGRAM_CHAT_ID:-}" ]]; then
  echo "[$HOST_TAG] 35-telegram: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы — пропуск"
  echo "[$HOST_TAG] Чтобы включить бота: добавь секцию telegram: в config.yaml и перезапусти деплой"
  exit 0
fi

STAMP_DIR=/var/anysda/.stamps
STAGE='35-telegram'

mkdir -p /etc/anysda

# ----------------------------------------------------------------------------
# 1. Generate shared secret (bot ↔ Nuxt)
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [1/3] tgbot secret"
if [[ ! -f /etc/anysda/tgbot-secret.txt ]]; then
  openssl rand -base64 32 | tr -d '\n/+=' | head -c 32 > /etc/anysda/tgbot-secret.txt
  chmod 600 /etc/anysda/tgbot-secret.txt
  echo "[$HOST_TAG]   новый секрет создан"
else
  echo "[$HOST_TAG]   секрет уже существует — не перегенерируем"
fi
TGBOT_SECRET=$(cat /etc/anysda/tgbot-secret.txt)

# Создаём runtime.json при первом запуске — UI (Nuxt) использует его как
# source of truth для отображения. После изменения через UI бот рестартит сам
# (watcher mtime), наш стейдж его не перетирает на повторном запуске.
if [[ ! -f /etc/anysda/telegram-runtime.json ]]; then
  python3 - <<PYEOF
import json
json.dump(
  {'bot_token': '${TELEGRAM_BOT_TOKEN}', 'chat_id': '${TELEGRAM_CHAT_ID}'},
  open('/etc/anysda/telegram-runtime.json', 'w'),
)
PYEOF
  chmod 600 /etc/anysda/telegram-runtime.json
  echo "[$HOST_TAG]   telegram-runtime.json создан из config.yaml"
fi

# ----------------------------------------------------------------------------
# 2. Build Docker image from pushed source
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [2/3] docker build"
SRC=/tmp/anysda/telegram
[[ -d "$SRC" ]] || { echo "[$HOST_TAG] $SRC не найден — оркестратор должен был его загрузить"; exit 1; }
docker build -q -t anysda-tgbot:local "$SRC" 2>&1 | sed "s/^/[$HOST_TAG]   /"

# ----------------------------------------------------------------------------
# 3. Run bot container
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [3/3] run container"
docker rm -f anysda-tgbot >/dev/null 2>&1 || true
docker run -d \
  --name anysda-tgbot \
  --restart unless-stopped \
  --network host \
  -v /etc/anysda:/etc/anysda:ro \
  -e TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" \
  -e TELEGRAM_CHAT_ID="$TELEGRAM_CHAT_ID" \
  -e TGBOT_SECRET="$TGBOT_SECRET" \
  -e TGBOT_EVENT_PORT=8877 \
  -e ANYSDA_URL=http://127.0.0.1:51821 \
  -e SOCKS5_PROXY=socks5://127.0.0.1:7897 \
  anysda-tgbot:local >/dev/null

echo "[$HOST_TAG] жду пока бот запустится..."
sleep 6
echo "[$HOST_TAG] последние логи:"
docker logs --tail 6 anysda-tgbot 2>&1 | sed "s/^/[$HOST_TAG]   /"

# Restart anysda-vpn so it picks up TGBOT_SECRET (if it was already running)
# This is needed when stage 35 is re-run independently after stage 30.
if docker ps --filter name=anysda-vpn --format '{{.Names}}' | grep -q anysda-vpn; then
  echo "[$HOST_TAG] перезапуск anysda-vpn для передачи TGBOT_SECRET..."
  docker restart anysda-vpn >/dev/null 2>&1 || true
fi

mkdir -p "$STAMP_DIR"
touch "$STAMP_DIR/$STAGE"
echo "[$HOST_TAG] $STAGE done"
