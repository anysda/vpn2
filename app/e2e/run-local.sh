#!/usr/bin/env bash
# Сквозные тесты панели без стенда: собрать образ, поднять контейнер с
# одноразовым паролем за Caddy, как ставит стадия 30, разложить ключи WG, CA OpenVPN и пометки IKEv2 теми же
# командами, что стадии 27-29, подставить VictoriaMetrics и clash API, прогнать
# Playwright. Аргументы уходят в `playwright test`.
#
#   DOCKER="sudo -n docker" bash e2e/run-local.sh            # всё
#   E2E_SKIP_BUILD=1 bash e2e/run-local.sh e2e/routes.spec.ts
#
# E2E_IMAGE, E2E_CONTAINER, E2E_PORT — имя образа, контейнера, порт Caddy на 127.0.0.1;
# E2E_KEEP=1 оставляет контейнер после прогона.
set -euo pipefail

APP_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
REPO_DIR=$(cd "$APP_DIR/.." && pwd)
read -r -a DK <<< "${DOCKER:-docker}"
IMAGE=${E2E_IMAGE:-vpn2-panel:e2e}
NAME=${E2E_CONTAINER:-vpn2-panel-e2e}
PORT=${E2E_PORT:-51899}
PUBLIC_HOST=vpn.example.test
EXIT_TAGS="nl fi us"
MGMT_IPS="ru:10.99.0.1,nl:10.99.0.2,fi:10.99.0.3,us:10.99.0.4"

dk() { "${DK[@]}" "$@"; }

if [[ -z ${E2E_SKIP_BUILD:-} ]]; then
  dk build -t "$IMAGE" "$APP_DIR"
fi

PASS=$(openssl rand -hex 12)
# Зовёт trap EXIT ниже; shellcheck этого не видит: 0.11 пишет SC2329 (из-за
# `exit $rc` в конце файла), 0.9-0.10 на раннере CI - SC2317 на теле функции.
# shellcheck disable=SC2317,SC2329
cleanup() {
  [[ -n ${E2E_KEEP:-} ]] && { echo "контейнер $NAME оставлен, пароль admin: $PASS"; return; }
  dk rm -f "$NAME" "$NAME-caddy" >/dev/null 2>&1 || true
  dk network rm "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

dk rm -f "$NAME" "$NAME-caddy" >/dev/null 2>&1 || true
dk network rm "$NAME" >/dev/null 2>&1 || true
dk network create "$NAME" >/dev/null
dk run -d --name "$NAME" --network "$NAME" --network-alias panel \
  --tmpfs /var/lib/anysda-vpn2 \
  -e HOST=0.0.0.0 -e PORT=51821 \
  -e NUXT_SESSION_PASSWORD="$(openssl rand -hex 32)" -e NUXT_SESSION_COOKIE_SECURE=false \
  -e NUXT_DATABASE_URL=file:/var/lib/anysda-vpn2/db.sqlite \
  -e NUXT_ADMIN_USER=admin -e NUXT_ADMIN_PASSWORD="$PASS" \
  -e NUXT_WG_ENABLED=true -e NUXT_WG_PUBLIC_HOST=$PUBLIC_HOST \
  -e NUXT_OVPN_ENABLED=true -e NUXT_OVPN_PUBLIC_HOST=$PUBLIC_HOST \
  -e NUXT_EXIT_TAGS="$EXIT_TAGS" -e NUXT_MGMT_IPS="$MGMT_IPS" \
  -e NUXT_VM_URL=http://127.0.0.1:8428 -e NUXT_CLASH_API_URL=http://127.0.0.1:9090 \
  -e NUXT_PUBLIC_SSO_ENABLED=false \
  "$IMAGE" >/dev/null

dk exec -i "$NAME" sh -c 'cat > /tmp/fake-backends.mjs' < "$APP_DIR/e2e/support/fake-backends.mjs"
dk exec -d "$NAME" node /tmp/fake-backends.mjs

# openssl.cnf берём из стадии 29 как есть, чтобы `openssl ca` в панели работал
# с тем же конфигом, что на ноде.
awk '/cat > "\$PKI\/openssl.cnf" <</ {f=1; next} f && /^EOF$/ {exit} f' \
  "$REPO_DIR/infra/scripts/29-openvpn.sh" \
  | dk exec -i "$NAME" sh -c 'mkdir -p /etc/openvpn/server/pki && cat > /etc/openvpn/server/pki/openssl.cnf'

dk exec -i -e PUBLIC_HOST=$PUBLIC_HOST "$NAME" bash -s <<'IN'
set -euo pipefail
# стадия 28: ключ сервера WireGuard
mkdir -p /etc/wireguard
( umask 077; wg genkey > /etc/wireguard/server.priv )
wg pubkey < /etc/wireguard/server.priv > /etc/wireguard/server.pub

# стадия 29: CA, серверный сертификат, tls-crypt, CRL
PKI=/etc/openvpn/server/pki
mkdir -p "$PKI/newcerts" /etc/openvpn/server/ccd
: > "$PKI/index.txt"; echo 01 > "$PKI/serial"; echo 01 > "$PKI/crlnumber"
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:prime256v1 -out "$PKI/ca.key"
openssl req -x509 -new -key "$PKI/ca.key" -days 3650 -sha256 -out "$PKI/ca.crt" \
  -subj "/CN=anysda-vpn2 CA" -config "$PKI/openssl.cnf" -extensions v3_ca
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:prime256v1 -out "$PKI/server.key"
openssl req -new -key "$PKI/server.key" -out "$PKI/server.csr" -subj "/CN=anysda-vpn2-server" -config "$PKI/openssl.cnf"
openssl ca -batch -notext -config "$PKI/openssl.cnf" -extensions server_ext -days 3650 \
  -in "$PKI/server.csr" -out "$PKI/server.crt" 2>/dev/null
rm -f "$PKI/server.csr"
# в образе нет openvpn: ключ в формате `openvpn --genkey secret`
{ echo '-----BEGIN OpenVPN Static key V1-----'; openssl rand -hex 256 | fold -w32; echo '-----END OpenVPN Static key V1-----'; } > "$PKI/tls-crypt.key"
openssl ca -config "$PKI/openssl.cnf" -gencrl -out "$PKI/crl.pem" 2>/dev/null
chmod 600 "$PKI/ca.key" "$PKI/server.key" "$PKI/tls-crypt.key"

# стадия 27: self-signed IKEv2
mkdir -p /etc/anysda /etc/strongswan/pki /etc/swanctl/conf.d
echo -n self-signed > /etc/anysda/ikev2-mode
echo -n "$PUBLIC_HOST" > /etc/anysda/ikev2-server-host
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
  -keyout /etc/strongswan/pki/ca.key -out /etc/strongswan/pki/ca.crt -days 30 \
  -subj "/CN=anysda-vpn2 IKEv2 CA" 2>/dev/null
IN

# Caddy перед панелью, как на ноде: он подменяет текст статуса ответа на
# стандартный («401 Unauthorized»), и фронт обязан это пережить.
CADDYFILE='{
    servers {
        protocols h1 h2
    }
}
:80 {
    encode gzip
    reverse_proxy panel:51821
}'
dk run -d --name "$NAME-caddy" --network "$NAME" -p "127.0.0.1:$PORT:80" \
  -e CADDYFILE="$CADDYFILE" mirror.gcr.io/library/caddy:2 \
  sh -c 'printf "%s\n" "$CADDYFILE" > /tmp/Caddyfile && exec caddy run --adapter caddyfile --config /tmp/Caddyfile' >/dev/null

for _ in $(seq 60); do
  curl -fsS "http://127.0.0.1:$PORT/api/version" >/dev/null 2>&1 && break
  sleep 1
done
if ! curl -fsS "http://127.0.0.1:$PORT/api/version"; then
  dk logs "$NAME" | tail -50
  exit 1
fi
echo

rc=0
(
  cd "$APP_DIR"
  E2E_BASE_URL="http://127.0.0.1:$PORT" E2E_ADMIN_PASSWORD="$PASS" \
  E2E_PUBLIC_HOST=$PUBLIC_HOST E2E_NODES="ru $EXIT_TAGS" \
    ./node_modules/.bin/playwright test "$@"
) || rc=$?
if (( rc != 0 )); then
  echo "--- журнал панели (хвост) ---"
  dk logs "$NAME" 2>&1 | tail -80
fi
exit $rc
