#!/usr/bin/env bash
# Панель с выдуманными данными для просмотра в браузере: ./setup.sh --front-test.
# Только панель в docker на этой машине, без нод: клиенты, устройства, маршруты,
# бот и живые метрики - подставные (e2e/support/fake-backends.mjs в живом
# режиме и e2e/support/front-test-seed.mjs). Повторный запуск пересоздаёт
# контейнеры и данные.
#
#   ./setup.sh --front-test          поднять (или пересоздать)
#   ./setup.sh --front-test-down     остановить и удалить
#
# Образ панели тот же, что ставит стадия 30-frontend: PANEL_IMAGE
# (ghcr.io/anysda/vpn2/panel:dev), docker pull, без сборки.
# FRONT_TEST_DEV=1 - вместо собранной панели `nuxt dev` из app/ этой рабочей
# копии (в том же образе: node 24 и wg/openssl/ip, нужные серверу панели):
# правка файла в app/ сразу видна в браузере (HMR), node_modules и .nuxt лежат
# в томах docker, а не в рабочей копии.
# Вход admin / admin: стенд с выдуманными данными, пароль печатает итог и его не
# надо доставать из контейнера, если скрипт не дошёл до конца. Обычный setup.sh
# так не ставит. FRONT_TEST_PORT - порт на 0.0.0.0 (8080), FRONT_TEST_PASSWORD -
# другой пароль admin, FRONT_TEST_CONTAINER - имя контейнера;
# DOCKER - команда docker («sudo -n docker»).
set -euo pipefail

APP_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source-path=SCRIPTDIR source=support/panel-stand.sh
source "$APP_DIR/e2e/support/panel-stand.sh"
NAME=${FRONT_TEST_CONTAINER:-vpn2-front-test}
PORT=${FRONT_TEST_PORT:-8080}
VOLUME=$NAME-data
DEV_VOLUMES=("$NAME-node-modules" "$NAME-nuxt")
DATA=/var/lib/anysda-vpn2

# fresh - контейнеры и данные заново; тома dev (node_modules, .nuxt) остаются,
# повторный запуск в dev не ставит пакеты с нуля. down - убрать всё.
fresh() {
  stand_down "$NAME"
  dk volume rm -f "$VOLUME" >/dev/null 2>&1 || true
}
down() {
  fresh
  dk volume rm -f "${DEV_VOLUMES[@]}" >/dev/null 2>&1 || true
}

case ${1:-up} in
  down)
    down
    echo "front-test остановлен: контейнеры $NAME, $NAME-caddy и тома $VOLUME ${DEV_VOLUMES[*]} удалены"
    exit 0
    ;;
  up) ;;
  *)
    echo "использование: $0 [up|down]" >&2
    exit 2
    ;;
esac

# Нет docker - ставим тем же кодом, что стадия 25-monitoring на ноде
# (infra/lib/anysda-svc.sh). Так же доставляем curl и openssl: ими пользуется
# panel-stand.sh. Нужен root.
ensure_host_tools() {
  local need=()
  command -v "${DK[0]}" >/dev/null 2>&1 || need+=(docker)
  command -v curl >/dev/null 2>&1 || need+=(curl)
  command -v openssl >/dev/null 2>&1 || need+=(openssl)
  ((${#need[@]})) || return 0
  if [[ $EUID -ne 0 || ! -f /etc/debian_version ]]; then
    echo "не хватает: ${need[*]}. Поставлю сам под root на Debian/Ubuntu, иначе поставь руками." >&2
    exit 1
  fi
  echo "ставлю недостающее: ${need[*]}"
  # shellcheck source-path=SCRIPTDIR source=../../infra/lib/anysda-svc.sh
  source "$STAND_REPO_DIR/infra/lib/anysda-svc.sh"
  export DEBIAN_FRONTEND=noninteractive
  apt_prepare
  local pkgs=(ca-certificates curl openssl)
  if [[ " ${need[*]} " == *" docker "* ]]; then
    docker_apt_source
    pkgs+=("${DOCKER_PKGS[@]}" "${DOCKER_SKIP[@]}")
  fi
  apt_get update -qq
  apt_get install -y -qq "${pkgs[@]}" >/dev/null
  [[ " ${need[*]} " != *" docker "* ]] || systemctl enable --now docker >/dev/null
}
ensure_host_tools

# Как 30-frontend: registry недоступен - берём образ, если он уже есть.
IMAGE=${PANEL_IMAGE:-ghcr.io/anysda/vpn2/panel:dev}
dk pull "$IMAGE" || dk image inspect "$IMAGE" >/dev/null \
  || { echo "образа $IMAGE нет ни в registry, ни локально" >&2; exit 1; }

# Собранная панель: наполнитель кладём к модулям сервера (.output/server/
# node_modules, там есть drizzle-orm и @libsql/client). Dev: всё из app/.
SEED=/app/.output/server/front-test
EXTRA=()
WAIT=60
if [[ "${FRONT_TEST_DEV:-}" == 1 ]]; then
  SEED=/src
  # pnpm 10, как в Dockerfile и CI; хранилище пакетов в том же томе.
  EXTRA=(-w /src -e NODE_ENV=development -e COREPACK_ENABLE_DOWNLOAD_PROMPT=0
    -v "$APP_DIR:/src" -v "${DEV_VOLUMES[0]}:/src/node_modules" -v "${DEV_VOLUMES[1]}:/src/.nuxt")
  STAND_CMD=(sh -c 'corepack pnpm@10 install --frozen-lockfile --store-dir /src/node_modules/.pnpm-store \
    && exec corepack pnpm@10 exec nuxt dev --host 0.0.0.0 --port 31821')
  # первый запуск ставит node_modules в пустой том
  WAIT=600
fi

PASS=${FRONT_TEST_PASSWORD:-admin}
fresh
dk volume create "$VOLUME" >/dev/null
stand_up "$NAME" "$IMAGE" "0.0.0.0:$PORT" "$PASS" \
  -v "$VOLUME:$DATA" \
  -e FAKE_DEMO=1 -e FAKE_LIVE_FILE=$DATA/front-test-live.json -e HOURLY_RETENTION_DAYS=400 \
  -e NUXT_TGBOT_SECRET="$(openssl rand -hex 16)" \
  -e NUXT_NODE_HOSTS="ru=203.0.113.10,nl=198.51.100.21,fi=198.51.100.37,us=192.0.2.54" \
  ${EXTRA[@]+"${EXTRA[@]}"}
stand_wait "$NAME" "http://127.0.0.1:$PORT" "$WAIT"

# Наполнитель пишет через схему БД панели и идёт в контейнере панели;
# schema.ts node 24 читает сам.
[[ "$SEED" == /src ]] || tar -c -C "$APP_DIR" e2e/support/front-test-seed.mjs server/database/schema.ts \
  | dk exec -i "$NAME" sh -c "mkdir -p $SEED && tar -x -C $SEED"
dk exec -e PANEL_URL=http://127.0.0.1:31821 -e ADMIN_PASSWORD="$PASS" -e EXIT_TAGS="$EXIT_TAGS" \
  -e DB_URL=file:$DATA/db.sqlite -e LIVE_FILE=$DATA/front-test-live.json \
  "$NAME" node --experimental-strip-types $SEED/e2e/support/front-test-seed.mjs

HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "Панель:  http://${HOST_IP:-127.0.0.1}:$PORT"
echo "Логин:   admin"
echo "Пароль:  $PASS"
echo "Остановить: ./setup.sh --front-test-down"
