#!/usr/bin/env bash
# Реальное обновление БД/PKI с прежнего образа на новый, только локальный Docker.
# DOCKER='sudo -n docker' E2E_UPGRADE_FROM=old-image E2E_IMAGE=new-image bash e2e/pool-upgrade.sh
set -euo pipefail
APP_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source-path=SCRIPTDIR source=support/panel-stand.sh
source "$APP_DIR/e2e/support/panel-stand.sh"
: "${E2E_UPGRADE_FROM:?}" "${E2E_IMAGE:?}"
NAME=${E2E_CONTAINER:-vpn2-pool-upgrade}
PORT=${E2E_PORT:-31810}
PASS=$(openssl rand -hex 12)
MIGRATION_TMP=$(mktemp -d)
cleanup() {
  stand_down "$NAME-base"
  dk rm -f "$NAME" >/dev/null 2>&1 || true
  rm -rf -- "$MIGRATION_TMP"
}
trap cleanup EXIT
STAND_CMD=(bash -c 'mkdir -p /var/lib/anysda-vpn2; exec node .output/server/index.mjs')
stand_up "$NAME-base" "$E2E_UPGRADE_FROM" "127.0.0.1:$PORT" "$PASS"
stand_wait "$NAME-base" "http://127.0.0.1:$PORT"
dk exec -i "$NAME-base" node --input-type=module - seed < "$APP_DIR/e2e/support/pool-upgrade.mjs"
dk stop "$NAME-base" >/dev/null
dk create --name "$NAME" --network none \
  -e HOST=0.0.0.0 -e PORT=31821 \
  -e NUXT_DATABASE_URL=file:/var/lib/anysda-vpn2/db.sqlite \
  -e NUXT_ADMIN_USER=admin -e NUXT_ADMIN_PASSWORD="$PASS" \
  -e NUXT_SESSION_COOKIE_SECURE=false -e NUXT_SESSION_PASSWORD="$(openssl rand -hex 32)" \
  -e NUXT_WG_ENABLED=true -e NUXT_WG_PUBLIC_HOST="$PUBLIC_HOST" \
  -e NUXT_OVPN_ENABLED=true -e NUXT_OVPN_PUBLIC_HOST="$PUBLIC_HOST" \
  "$E2E_IMAGE" >/dev/null
for dir in /var/lib/anysda-vpn2 /etc/wireguard /etc/openvpn /etc/strongswan /etc/swanctl /etc/anysda; do
  dk cp "$NAME-base:$dir" - | tar -x -C "$MIGRATION_TMP"
  dk cp "$MIGRATION_TMP/${dir##*/}" "$NAME:${dir%/*}/"
done
dk start "$NAME" >/dev/null
dk exec -i "$NAME" node --input-type=module - check < "$APP_DIR/e2e/support/pool-upgrade.mjs"
