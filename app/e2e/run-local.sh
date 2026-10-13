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
# E2E_BROWSER_IMAGE — образ Playwright той же версии, что в node_modules;
# E2E_BROWSER_PORT — порт браузерного сервера на 127.0.0.1 (E2E_PORT + 1).
# E2E_KEEP=1 оставляет контейнер после прогона.
# Полный прогон: E2E_SHARDS=4 — независимые панели/БД, один worker на каждую.
# С аргументами Playwright или E2E_KEEP по умолчанию 1 (без пустых шардов).
set -euo pipefail

APP_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# Optional local gate: the same selector as CI, before Node/Docker setup.
if [[ ${1:-} == --changed-only ]]; then
  shift
  selected=$(python3 "$APP_DIR/../infra/lib/e2e_paths.py")
  if [[ $selected == false ]]; then
    echo 'panel-e2e: skipped (no panel dependencies changed)'
    exit 0
  fi
fi
# shellcheck source-path=SCRIPTDIR source=support/panel-stand.sh
source "$APP_DIR/e2e/support/panel-stand.sh"
IMAGE=${E2E_IMAGE:-vpn2-panel:e2e}
NAME=${E2E_CONTAINER:-vpn2-panel-e2e}
# 16 пар портов целиком ниже стандартного Linux ip_local_port_range (32768+).
PORT=${E2E_PORT:-31710}
BROWSER_PORT=${E2E_BROWSER_PORT:-$((PORT + 1))}
PW_VERSION=$(node -p 'require(process.argv[1]).version' "$APP_DIR/node_modules/@playwright/test/package.json")
BROWSER_IMAGE=${E2E_BROWSER_IMAGE:-mcr.microsoft.com/playwright:v$PW_VERSION-noble}
shards=${E2E_SHARDS:-4}
if (( $# > 0 )) || [[ -n ${E2E_KEEP:-} ]]; then
  shards=${E2E_SHARDS:-1}
fi
if [[ ! $shards =~ ^([1-9]|1[0-6])$ ]]; then
  echo 'E2E_SHARDS must be between 1 and 16' >&2
  exit 1
fi
if [[ -n ${E2E_KEEP:-} && $shards != 1 ]]; then
  echo 'E2E_KEEP requires E2E_SHARDS=1' >&2
  exit 1
fi

if [[ -z ${E2E_SKIP_BUILD:-} && -z ${E2E_SHARD:-} ]]; then
  started=$SECONDS
  dk build -t "$IMAGE" "$APP_DIR"
  echo "timing: build=$((SECONDS - started))s"
fi

if [[ -z ${E2E_SHARD:-} ]]; then
started=$SECONDS
RUN_CADDY_TESTS=1 python3 "$APP_DIR/../infra/tests/test_panel_proxy.py"
# Пустая БД и недоступный файл пароля: безопасный отказ старта без утечки в лог.
dk run --rm --name "$NAME-init" --read-only --tmpfs /var/lib/anysda-vpn2 --tmpfs /tmp \
  -i --entrypoint node "$IMAGE" --input-type=module < "$APP_DIR/e2e/support/admin-password-check.mjs"
dk run --rm --name "$NAME-config" --read-only --tmpfs /var/lib/anysda-vpn2 --tmpfs /tmp \
  -i --entrypoint node "$IMAGE" --input-type=module < "$APP_DIR/e2e/support/config-error-check.mjs"
dk run --rm --name "$NAME-password" --read-only --tmpfs /var/lib/anysda-vpn2 --tmpfs /tmp \
  -i --entrypoint node "$IMAGE" --input-type=module < "$APP_DIR/e2e/support/long-password-check.mjs"
dk run --rm --name "$NAME-wg-endpoint" --network none --read-only \
  --tmpfs /var/lib/anysda-vpn2 --tmpfs /tmp --tmpfs /etc/wireguard \
  -i --entrypoint node "$IMAGE" --input-type=module < "$APP_DIR/e2e/support/wg-endpoint-check.mjs"
dk run --rm --name "$NAME-sso-secret" --read-only \
  --tmpfs /var/lib/anysda-vpn2 --tmpfs /tmp --tmpfs /etc/anysda \
  -i --entrypoint node "$IMAGE" --input-type=module < "$APP_DIR/e2e/support/sso-secret-check.mjs"
echo "timing: image-checks=$((SECONDS - started))s"

if [[ $shards != 1 ]]; then
  export E2E_IMAGE="$IMAGE" E2E_SHARDS="$shards"
  exec python3 "$APP_DIR/e2e/run-shards.py" "$@"
fi
fi

started=$SECONDS
PASS=$(openssl rand -hex 12)
# Проверяем панель именно с установочным профилем entry 2 ГБ.
resource_args=$(python3 "$APP_DIR/../infra/lib/entry_resources.py" \
  --ram-mib 2048 --free-mib 8192 --service panel)
eval "$resource_args"
# Зовёт trap EXIT ниже; shellcheck этого не видит: 0.11 пишет SC2329 (из-за
# `exit $rc` в конце файла), 0.9-0.10 на раннере CI - SC2317 на теле функции.
# shellcheck disable=SC2317,SC2329
cleanup() {
  dk rm -f "$NAME-browser" >/dev/null 2>&1 || true
  [[ -n ${E2E_KEEP:-} ]] && { echo "контейнер $NAME оставлен, пароль admin: $PASS"; return; }
  stand_down "$NAME"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

# Все тесты входят с одного адреса (Caddy в docker), а лимит входов панели -
# 10 неудачных за 5 минут на адрес: прогон упёрся бы в него на середине.
stand_up "$NAME" "$IMAGE" "127.0.0.1:$PORT" "$PASS" --tmpfs /var/lib/anysda-vpn2 \
  "${ENTRY_DOCKER_ARGS[@]}" \
  -e NUXT_AUTH_LOGIN_MAX_ATTEMPTS=1000 -e NUXT_TGBOT_SECRET="$PASS"
stand_wait "$NAME" "http://127.0.0.1:$PORT"

# Chromium слушает netlink: смена интерфейсов хоста обрывает даже запросы к
# localhost (ERR_NETWORK_CHANGED). У браузера своя сеть; exposeNetwork в
# playwright.config.ts передаёт localhost тестового процесса через Playwright.
# Только браузер в контейнере: тесты отказов по-прежнему управляют Docker с хоста.
dk run -d --rm --init --name "$NAME-browser" --network "$NAME" --shm-size=1g \
  -p "127.0.0.1:$BROWSER_PORT:3000" \
  -v "$APP_DIR/node_modules:/app/node_modules:ro" \
  "$BROWSER_IMAGE" node /app/node_modules/@playwright/test/cli.js \
  run-server --host 0.0.0.0 --port 3000 >/dev/null
for _ in $(seq 30); do
  curl -fsS "http://127.0.0.1:$BROWSER_PORT/json" >/dev/null 2>&1 && break
  sleep 1
done
if ! curl -fsS "http://127.0.0.1:$BROWSER_PORT/json" >/dev/null; then
  dk logs "$NAME-browser"
  exit 1
fi

echo "timing: stand=$((SECONDS - started))s"
started=$SECONDS
rc=0
(
  cd "$APP_DIR"
  E2E_BASE_URL="http://127.0.0.1:$PORT" E2E_ADMIN_PASSWORD="$PASS" \
  E2E_BROWSER_WS="ws://127.0.0.1:$BROWSER_PORT/" \
  E2E_FAULT_CONTAINER="$NAME" \
  E2E_PUBLIC_HOST=$PUBLIC_HOST E2E_NODES="ru $EXIT_TAGS" \
    ./node_modules/.bin/playwright test "$@"
) || rc=$?
echo "timing: playwright=$((SECONDS - started))s"
if (( rc != 0 )); then
  diagnostics=${E2E_REPORT_DIR:-$APP_DIR/test-results}
  mkdir -p "$diagnostics"
  dk logs "$NAME" > "$diagnostics/panel.log" 2>&1 || true
  dk logs "$NAME-caddy" > "$diagnostics/caddy.log" 2>&1 || true
  dk inspect --format '{{json .State}}' "$NAME" > "$diagnostics/panel-state.json" || true
  echo "--- журнал панели (хвост) ---"
  dk logs "$NAME" 2>&1 | tail -80
  echo "--- журнал Caddy (хвост) ---"
  dk logs "$NAME-caddy" 2>&1 | tail -40
fi
exit $rc
