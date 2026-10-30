#!/usr/bin/env bash
# VPN2-598: размер QR во всех местах продукта одной командой, без стенда.
# Две панели (как в run-local.sh, общие функции из support/panel-stand.sh):
#   сплит со вырезанным IP входа 45.x - худший конфиг (QR_MODE=split);
#   NUXT_WG_SPLIT_LOCAL=false - полный туннель (QR_MODE=full).
# Затем e2e/qr-size.spec.ts на каждой и telegram/tests/test_qr_size.py на
# конфиге, который выдала первая панель.
#
# Так же идёт в CI (panel-e2e), там образ панели уже собран:
#   DOCKER="sudo -n docker" bash app/e2e/qr-size.sh
#   E2E_SKIP_BUILD=1 E2E_BASE_IMAGE=vpn2-panel:e2e ... - не пересобирать панель,
#   только слой со службами VPN (e2e/Dockerfile.vpn) поверх E2E_BASE_IMAGE
set -euo pipefail

APP_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source-path=SCRIPTDIR source=support/panel-stand.sh
source "$APP_DIR/e2e/support/panel-stand.sh"
REPO_DIR=$(cd "$APP_DIR/.." && pwd)
IMAGE=${E2E_IMAGE:-vpn2-panel:qr}
BASE_IMAGE=${E2E_BASE_IMAGE:-$IMAGE-base}
NAME=${E2E_CONTAINER:-vpn2-panel-qr}
PORT=${E2E_PORT:-31790}
ENTRY_IP=${QR_ENTRY_IP:-45.137.198.213}
PW_VERSION=$(node -p 'require(process.argv[1]).version' "$APP_DIR/node_modules/@playwright/test/package.json")
BROWSER_IMAGE=${E2E_BROWSER_IMAGE:-mcr.microsoft.com/playwright:v$PW_VERSION-noble}
OUT=${QR_OUT:-$(mktemp -d "${TMPDIR:-/tmp}/vpn2-qr.XXXXXX")}
VENV=${QR_VENV:-${TMPDIR:-/tmp}/vpn2-qr-venv}
mkdir -p "$OUT"

for tool in qrencode zbarimg; do
  command -v "$tool" >/dev/null || { echo "нужен $tool (apt install qrencode zbar-tools)" >&2; exit 1; }
done

total=$SECONDS
if [[ -z ${E2E_SKIP_BUILD:-} ]]; then
  started=$SECONDS
  dk build -q -t "$BASE_IMAGE" "$APP_DIR"
  echo "timing: build=$((SECONDS - started))s"
fi
dk build -q --build-arg PANEL_IMAGE="$BASE_IMAGE" -f "$APP_DIR/e2e/Dockerfile.vpn" -t "$IMAGE" "$APP_DIR/e2e"
export E2E_VPN_SERVICES=1

# shellcheck disable=SC2317,SC2329
cleanup() {
  dk rm -f "$NAME-browser" >/dev/null 2>&1 || true
  stand_down "$NAME-split"
  stand_down "$NAME-full"
}
trap cleanup EXIT

started=$SECONDS
PASS=$(openssl rand -hex 12)
stand_up "$NAME-split" "$IMAGE" "127.0.0.1:$PORT" "$PASS" --tmpfs /var/lib/anysda-vpn2 \
  -e NUXT_AUTH_LOGIN_MAX_ATTEMPTS=1000 -e NUXT_WG_ENDPOINT_IP="$ENTRY_IP"
stand_up "$NAME-full" "$IMAGE" "127.0.0.1:$((PORT + 2))" "$PASS" --tmpfs /var/lib/anysda-vpn2 \
  -e NUXT_AUTH_LOGIN_MAX_ATTEMPTS=1000 -e NUXT_WG_SPLIT_LOCAL=false
stand_wait "$NAME-split" "http://127.0.0.1:$PORT"
stand_wait "$NAME-full" "http://127.0.0.1:$((PORT + 2))"

BROWSER_PORT=$((PORT + 4))
dk run -d --rm --init --name "$NAME-browser" --network "$NAME-split" --shm-size=1g \
  -p "127.0.0.1:$BROWSER_PORT:3000" \
  -v "$APP_DIR/node_modules:/app/node_modules:ro" \
  "$BROWSER_IMAGE" node /app/node_modules/@playwright/test/cli.js \
  run-server --host 0.0.0.0 --port 3000 --unsafe >/dev/null
for _ in $(seq 30); do
  curl -fsS "http://127.0.0.1:$BROWSER_PORT/json" >/dev/null 2>&1 && break
  sleep 1
done
echo "timing: stand=$((SECONDS - started))s"

# Отдельный venv: зависимости бота точными версиями из requirements.txt.
if [[ ! -x $VENV/bin/python ]]; then
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q -r "$REPO_DIR/telegram/requirements.txt"
fi

rc=0
playwright() {
  local port=$1 mode=$2
  (
    cd "$APP_DIR"
    E2E_BASE_URL="http://127.0.0.1:$port" E2E_ADMIN_PASSWORD="$PASS" \
    E2E_BROWSER_WS="ws://127.0.0.1:$BROWSER_PORT/" E2E_PUBLIC_HOST=$PUBLIC_HOST \
    E2E_REPORT_DIR="$OUT/$mode" QR_MODE=$mode QR_ENTRY_IP="$ENTRY_IP" QR_CONF_OUT="$OUT/wg-worst.conf" \
      ./node_modules/.bin/playwright test --project=desktop --grep "@$mode" e2e/qr-size.spec.ts
  )
}
started=$SECONDS
echo "=== панель: сплит, вход $ENTRY_IP ==="
playwright "$PORT" split || rc=1
echo "=== панель: сплит выключен ==="
playwright "$((PORT + 2))" full || rc=1
echo "timing: playwright=$((SECONDS - started))s"

echo "=== бот ==="
if [[ -s $OUT/wg-worst.conf ]]; then
  QR_WORST_CONF="$OUT/wg-worst.conf" "$VENV/bin/python" -m unittest discover \
    -s "$REPO_DIR/telegram/tests" -p test_qr_size.py -v || rc=1
else
  echo 'панель не выдала худший конфиг: тест бота не запущен' >&2
  rc=1
fi
echo "timing: total=$((SECONDS - total))s, отчёты: $OUT"
exit $rc
