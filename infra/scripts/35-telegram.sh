#!/usr/bin/env bash
# Stage 35 — Telegram bot on RU.
#
# Pulls the bot image from the GitHub Container Registry (ghcr.io)
# (ghcr.io/anysda/vpn2/tgbot) and runs it with
# --network host so it can reach:
#   - 127.0.0.1:51821  (anysda-vpn Nuxt API — direct)
#   - 127.0.0.1:7897   (sing-box HTTP proxy → foreign-best exit)
#
# The bot's Telegram API traffic egresses through an exit node — the RU
# entry node in Moscow can't reach api.telegram.org directly. Only the
# Nuxt API calls stay local.
#
# Installs the bot even without TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID: it waits
# for them in telegram-runtime.json, which the panel writes.
# Prerequisites: stage 20 (sing-box with the local tg-proxy inbound).

set -euo pipefail

[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}"

case "$HOST_TAG" in ru) ;; *) echo "[$HOST_TAG] 35-telegram is ru-only — skipping"; exit 0;; esac

# docker restart сохраняет старое окружение. Пересоздаём панель штатной
# стадией 30, только если секрет изменился. Сохраняем установленный образ:
# включение бота не должно заодно обновлять панель из registry.
sync_panel_secret() {
  [[ "${VPN2_PANEL_FOLLOWS:-false}" != true ]] || return 0
  local panel current image frontend
  panel=$(docker ps -a --filter name='^anysda-vpn2$' --format '{{.Names}}')
  [[ -n "$panel" ]] || return 0 # При полной установке 30 идёт после 35.
  current=$(docker inspect --format '{{json .Config.Env}}' anysda-vpn2 | python3 -c '
import json, sys
print(next((v.split("=", 1)[1] for v in json.load(sys.stdin)
            if v.startswith("NUXT_TGBOT_SECRET=")), ""))')
  [[ "$current" != "${TGBOT_SECRET:-}" ]] || return 0
  image=$(docker inspect --format '{{.Image}}' anysda-vpn2)
  frontend=$(dirname "$0")/30-frontend.sh
  echo "[$HOST_TAG] пересоздание anysda-vpn2 для передачи TGBOT_SECRET..."
  (
    set --
    PANEL_IMAGE="$image"
    # During an upgrade the old image cannot stand in for the release manifest.
    [[ "${VPN2_DEVELOPMENT:-false}" == true ]] || PANEL_IMAGE=''
    PANEL_IMAGE_PULL=false
    # shellcheck source=infra/scripts/30-frontend.sh
    source "$frontend"
  )
}

TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN:-}
TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID:-}

STAMP_DIR=/var/anysda/.stamps
STAGE='35-telegram'

RESOURCE_LIB=$(dirname "$0")/entry-resources.sh
[[ -f "$RESOURCE_LIB" ]] || RESOURCE_LIB=$(dirname "$0")/../lib/entry-resources.sh
# shellcheck source=infra/lib/entry-resources.sh
source "$RESOURCE_LIB"
entry_resources bot

mkdir -p /etc/anysda

# ----------------------------------------------------------------------------
# 1. Generate shared secret (bot ↔ Nuxt)
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [1/3] tgbot secret"
if [[ ! -s /etc/anysda/tgbot-secret.txt ]]; then
  openssl rand -base64 32 | tr -d '\n/+=' | head -c 32 > /etc/anysda/tgbot-secret.txt
  chmod 600 /etc/anysda/tgbot-secret.txt
  echo "[$HOST_TAG]   новый секрет создан"
else
  echo "[$HOST_TAG]   секрет уже существует — не перегенерируем"
fi
TGBOT_SECRET=$(cat /etc/anysda/tgbot-secret.txt)
chmod 600 /etc/anysda/tgbot-secret.txt

# Создаём runtime.json при первом запуске — UI (Nuxt) использует его как
# source of truth для отображения. После изменения через UI бот рестартит сам
# (watcher mtime), наш стейдж его не перетирает на повторном запуске.
if [[ ! -f /etc/anysda/telegram-runtime.json ]]; then
  TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" TELEGRAM_CHAT_ID="$TELEGRAM_CHAT_ID" \
    TELEGRAM_ADMIN_USERNAME="${TELEGRAM_ADMIN_USERNAME:-}" python3 - <<'PYEOF'
import json, os
json.dump(
  {
    'bot_token': os.environ['TELEGRAM_BOT_TOKEN'],
    'chat_id': os.environ['TELEGRAM_CHAT_ID'],
    'admin_username': os.environ['TELEGRAM_ADMIN_USERNAME'],
  },
  open('/etc/anysda/telegram-runtime.json', 'w'),
)
PYEOF
  chmod 600 /etc/anysda/telegram-runtime.json
  echo "[$HOST_TAG]   telegram-runtime.json создан из config.yaml"
fi

# Изменённые в config.yaml поля применяем и к runtime, иначе старый файл
# перекроет новые env. Токен и чат, убранные из config, стираем (бот выключен),
# если в runtime то же, что прошлый деплой взял из config; заданное в панели
# остаётся. Взятое из
# config храним в /etc/anysda: оно живёт и без контейнера бота и входит в копию.
# Узел старше этого файла: взятое из config есть только в env контейнера бота.
previous_env='[]'
if [[ ! -f /etc/anysda/telegram-from-config.json ]] \
    && [[ -n "$(docker ps -a --filter name='^anysda-tgbot$' --format '{{.Names}}')" ]]; then
  previous_env=$(docker inspect --format '{{json .Config.Env}}' anysda-tgbot)
fi
PREVIOUS_ENV="$previous_env" TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" \
  TELEGRAM_CHAT_ID="$TELEGRAM_CHAT_ID" TELEGRAM_ADMIN_USERNAME="${TELEGRAM_ADMIN_USERNAME:-}" python3 - <<'PYEOF'
import json, os
from pathlib import Path
path = Path('/etc/anysda/telegram-runtime.json')
taken = Path('/etc/anysda/telegram-from-config.json')
runtime = json.loads(path.read_text())
if taken.exists():
    previous = json.loads(taken.read_text())
else:
    env = dict(item.split('=', 1) for item in json.loads(os.environ['PREVIOUS_ENV']))
    previous = {field: env.get('TELEGRAM_' + field.upper(), '')
                for field in ('bot_token', 'chat_id', 'admin_username')}
current = {}
changed = False
for field in ('bot_token', 'chat_id', 'admin_username'):
    value = current[field] = os.environ['TELEGRAM_' + field.upper()]
    before = previous.get(field, '')
    if value and value != before:
        runtime[field] = value
        changed = True
    elif field != 'admin_username' and not value and before and runtime.get(field) == before:
        runtime[field] = ''
        changed = True
if changed:
    path.write_text(json.dumps(runtime) + '\n')
fd = os.open(taken, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w') as file:
    json.dump(current, file)
os.chmod(taken, 0o600)
PYEOF

# ----------------------------------------------------------------------------
# 2. Pull bot image from the GitHub Container Registry (ghcr.io)
# ----------------------------------------------------------------------------
RELEASE_LIB=$(dirname "$0")/release.sh
[[ -f "$RELEASE_LIB" ]] || RELEASE_LIB=$(dirname "$0")/../lib/release.sh
# shellcheck source=infra/lib/release.sh
source "$RELEASE_LIB"
TGBOT_IMAGE=$(release_image tgbot "${TGBOT_IMAGE:-}" "${TGBOT_IMAGE_PULL:-true}")
echo "[$HOST_TAG] [2/3] bot image digest verified"

# Панель должна принять новый секрет до первого опроса ботом. Ошибка стадии
# 30 прерывает деплой: успешный rc при старом env скрывал неработающего бота.
sync_panel_secret

# ----------------------------------------------------------------------------
# 3. Run bot container
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [3/3] run container"
install -d -o vpn2-bot -g vpn2-bot -m 700 /var/lib/anysda-tgbot
chown -hR vpn2-bot:vpn2-bot /var/lib/anysda-tgbot
install -d -o vpn2-panel -g vpn2-panel -m 0700 /var/lib/anysda-vpn2/runtime
if [[ ! -L /etc/anysda/telegram-runtime.json ]]; then
  cp -p /etc/anysda/telegram-runtime.json /var/lib/anysda-vpn2/runtime/telegram-runtime.json
  ln -sfn /var/lib/anysda-vpn2/runtime/telegram-runtime.json /etc/anysda/telegram-runtime.json
fi
chown vpn2-panel:vpn2-panel /var/lib/anysda-vpn2/runtime/telegram-runtime.json
chmod 0640 /var/lib/anysda-vpn2/runtime/telegram-runtime.json
docker rm -f anysda-tgbot >/dev/null 2>&1 || true
# /etc/timezone не монтируем: в Ubuntu 26.04 его нет, docker создаёт на его
# месте каталог и контейнер не стартует. Время берут из /etc/localtime.
[[ -d /etc/timezone ]] && rmdir /etc/timezone 2>/dev/null || true
# Runtime-бюджет рассчитан на entry, swap не скрывает превышение бюджета.
docker run -d \
  --name anysda-tgbot \
  "${ENTRY_DOCKER_ARGS[@]}" \
  --restart always \
  --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 \
  --network host \
  --user "$(id -u vpn2-bot):$(id -g vpn2-bot)" --group-add "$(id -g vpn2-panel)" \
  --cap-drop ALL --security-opt no-new-privileges \
  -v /etc/localtime:/etc/localtime:ro \
  -v /var/lib/anysda-vpn2/runtime/telegram-runtime.json:/etc/anysda/telegram-runtime.json:ro \
  -v /var/lib/anysda-tgbot:/var/lib/anysda-tgbot \
  -e TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" \
  -e TELEGRAM_CHAT_ID="$TELEGRAM_CHAT_ID" \
  -e TELEGRAM_ADMIN_USERNAME="${TELEGRAM_ADMIN_USERNAME:-}" \
  -e TGBOT_SECRET="$TGBOT_SECRET" \
  -e TGBOT_EVENT_PORT=8877 \
  -e TGBOT_POLL_STALL_SEC="${TGBOT_POLL_STALL_SEC:-300}" \
  -e ANYSDA_URL=http://127.0.0.1:51821 \
  -e TG_PROXY=http://127.0.0.1:7897 \
  "$TGBOT_IMAGE" >/dev/null

echo "[$HOST_TAG] жду пока бот запустится..."
# Сервер событий поднимается до getMe, даже если Telegram недоступен.
for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
  [[ "$(docker logs anysda-tgbot 2>&1)" == *'Event server on'* ]] && break
  sleep 0.5
done
echo "[$HOST_TAG] последние логи:"
docker logs --tail 6 anysda-tgbot 2>&1 | HOST_TAG="$HOST_TAG" awk '{print "[" ENVIRON["HOST_TAG"] "]   " $0}'

mkdir -p "$STAMP_DIR"
touch "$STAMP_DIR/$STAGE"
echo "[$HOST_TAG] $STAGE done"
