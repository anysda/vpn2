#!/usr/bin/env bash
# Сквозные тесты панели без стенда: собрать образ, поднять контейнер с
# одноразовым паролем за Caddy, как ставит стадия 30, разложить ключи WG, CA OpenVPN и пометки IKEv2 теми же
# командами, что стадии 27-29, подставить VictoriaMetrics и clash API, прогнать
# Playwright. Аргументы уходят в `playwright test`. Подъём панели общий
# с e2e/front-test.sh, он в e2e/support/panel-stand.sh.
#
#   DOCKER="sudo -n docker" bash e2e/run-local.sh            # всё
#   E2E_SKIP_BUILD=1 bash e2e/run-local.sh e2e/routes.spec.ts
#
# E2E_IMAGE, E2E_CONTAINER, E2E_PORT — имя образа, контейнера, порт Caddy на 127.0.0.1;
# E2E_KEEP=1 оставляет контейнер после прогона.
set -euo pipefail

APP_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source-path=SCRIPTDIR source=support/panel-stand.sh
source "$APP_DIR/e2e/support/panel-stand.sh"
IMAGE=${E2E_IMAGE:-vpn2-panel:e2e}
NAME=${E2E_CONTAINER:-vpn2-panel-e2e}
PORT=${E2E_PORT:-51899}

if [[ -z ${E2E_SKIP_BUILD:-} ]]; then
  dk build -t "$IMAGE" "$APP_DIR"
fi

# Пустая БД и недоступный файл пароля: безопасный отказ старта без утечки в лог.
dk run --rm --name "$NAME-init" --read-only --tmpfs /var/lib/anysda-vpn2 --tmpfs /tmp \
  -i --entrypoint node "$IMAGE" --input-type=module < "$APP_DIR/e2e/support/admin-password-check.mjs"
dk run --rm --name "$NAME-config" --read-only --tmpfs /var/lib/anysda-vpn2 --tmpfs /tmp \
  -i --entrypoint node "$IMAGE" --input-type=module < "$APP_DIR/e2e/support/config-error-check.mjs"

PASS=$(openssl rand -hex 12)
# Проверяем панель именно с установочным профилем entry 2 ГБ.
resource_args=$(python3 "$APP_DIR/../infra/lib/entry_resources.py" \
  --ram-mib 2048 --free-mib 8192 --service panel)
eval "$resource_args"
# Зовёт trap EXIT ниже; shellcheck этого не видит: 0.11 пишет SC2329 (из-за
# `exit $rc` в конце файла), 0.9-0.10 на раннере CI - SC2317 на теле функции.
# shellcheck disable=SC2317,SC2329
cleanup() {
  [[ -n ${E2E_KEEP:-} ]] && { echo "контейнер $NAME оставлен, пароль admin: $PASS"; return; }
  stand_down "$NAME"
}
trap cleanup EXIT

# Все тесты входят с одного адреса (Caddy в docker), а лимит входов панели -
# 10 неудачных за 5 минут на адрес: прогон упёрся бы в него на середине.
stand_up "$NAME" "$IMAGE" "127.0.0.1:$PORT" "$PASS" --tmpfs /var/lib/anysda-vpn2 \
  "${ENTRY_DOCKER_ARGS[@]}" \
  -e NUXT_AUTH_LOGIN_MAX_ATTEMPTS=1000 -e NUXT_TGBOT_SECRET="$PASS"
stand_wait "$NAME" "http://127.0.0.1:$PORT"

rc=0
(
  cd "$APP_DIR"
  E2E_BASE_URL="http://127.0.0.1:$PORT" E2E_ADMIN_PASSWORD="$PASS" \
  E2E_FAULT_CONTAINER="$NAME" \
  E2E_PUBLIC_HOST=$PUBLIC_HOST E2E_NODES="ru $EXIT_TAGS" \
    ./node_modules/.bin/playwright test "$@"
) || rc=$?
if (( rc != 0 )); then
  echo "--- журнал панели (хвост) ---"
  dk logs "$NAME" 2>&1 | tail -80
fi
exit $rc
