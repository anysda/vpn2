#!/usr/bin/env bash
# Проверки образа панели без браузера: Caddy перед панелью и безопасный отказ
# старта (пустая БД, нет файла пароля, ошибка конфига, длинный пароль, адрес WG,
# секрет SSO). Зовёт run-local.sh; в CI - один раз, в задании QR.
#
#   bash e2e/image-checks.sh vpn2-panel:e2e-vpn
set -euo pipefail

APP_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source-path=SCRIPTDIR source=support/panel-stand.sh
source "$APP_DIR/e2e/support/panel-stand.sh"
IMAGE=${1:?использование: image-checks.sh <образ панели со службами VPN>}
NAME=${E2E_CONTAINER:-vpn2-panel-e2e}

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
