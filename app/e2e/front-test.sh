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
# FRONT_TEST_PORT - порт на 0.0.0.0 (8080), FRONT_TEST_PASSWORD - пароль admin
# (иначе случайный), FRONT_TEST_CONTAINER, FRONT_TEST_IMAGE - имена;
# DOCKER - команда docker («sudo -n docker»).
set -euo pipefail

APP_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source-path=SCRIPTDIR source=support/panel-stand.sh
source "$APP_DIR/e2e/support/panel-stand.sh"
NAME=${FRONT_TEST_CONTAINER:-vpn2-front-test}
IMAGE=${FRONT_TEST_IMAGE:-vpn2-panel:front-test}
PORT=${FRONT_TEST_PORT:-8080}
VOLUME=$NAME-data
DATA=/var/lib/anysda-vpn2

down() {
  stand_down "$NAME"
  dk volume rm -f "$VOLUME" >/dev/null 2>&1 || true
}

case ${1:-up} in
  down)
    down
    echo "front-test остановлен: контейнеры $NAME, $NAME-caddy и том $VOLUME удалены"
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

dk build -t "$IMAGE" "$APP_DIR"
# Стадия builder того же Dockerfile (из кэша): в ней исходники и node_modules,
# наполнитель пишет через схему БД панели.
dk build --target builder -t "$IMAGE-seed" "$APP_DIR"

PASS=${FRONT_TEST_PASSWORD:-$(openssl rand -hex 8)}
down
dk volume create "$VOLUME" >/dev/null
stand_up "$NAME" "$IMAGE" "0.0.0.0:$PORT" "$PASS" \
  -v "$VOLUME:$DATA" \
  -e FAKE_DEMO=1 -e FAKE_LIVE_FILE=$DATA/front-test-live.json \
  -e NUXT_TGBOT_SECRET="$(openssl rand -hex 16)" \
  -e NUXT_NODE_HOSTS="ru=203.0.113.10,nl=198.51.100.21,fi=198.51.100.37,us=192.0.2.54"
stand_wait "$NAME" "http://127.0.0.1:$PORT"

dk run --rm --network "$NAME" -v "$VOLUME:$DATA" \
  -e PANEL_URL=http://panel:51821 -e ADMIN_PASSWORD="$PASS" -e EXIT_TAGS="$EXIT_TAGS" \
  -e DB_URL=file:$DATA/db.sqlite -e LIVE_FILE=$DATA/front-test-live.json \
  "$IMAGE-seed" node e2e/support/front-test-seed.mjs

HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "Панель:  http://${HOST_IP:-127.0.0.1}:$PORT"
echo "Логин:   admin"
echo "Пароль:  $PASS"
echo "Остановить: ./setup.sh --front-test-down"
