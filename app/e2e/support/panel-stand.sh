# shellcheck shell=bash
# Панель в docker без стенда: контейнер панели за Caddy, как ставит стадия 30,
# ключи WG, CA OpenVPN и пометки IKEv2 теми же командами, что стадии 27-29,
# подставные бэкенды из fake-backends.mjs. Общая часть e2e/run-local.sh и
# e2e/front-test.sh, подключается через source.
#
# DOCKER - команда docker («sudo -n docker» и т.п.).

STAND_APP_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
STAND_REPO_DIR=$(cd "$STAND_APP_DIR/.." && pwd)
read -r -a DK <<< "${DOCKER:-docker}"
PUBLIC_HOST=vpn.example.test
EXIT_TAGS="nl fi us"
MGMT_IPS="ru:10.99.0.1,nl:10.99.0.2,fi:10.99.0.3,us:10.99.0.4"

dk() { "${DK[@]}" "$@"; }

# stand_down <имя>: убрать контейнеры панели и Caddy и их сеть.
stand_down() {
  dk rm -f "$1" "$1-caddy" >/dev/null 2>&1 || true
  dk network rm "$1" >/dev/null 2>&1 || true
}

# stand_up <имя> <образ> <адрес:порт Caddy на хосте> <пароль admin> [аргументы docker run панели...]
# Хранилище /var/lib/anysda-vpn2 (tmpfs, том) и лишние -e передаёт вызывающий,
# команду вместо штатной (если нужна) - массив STAND_CMD.
stand_up() {
  local name=$1 image=$2 publish=$3 pass=$4
  shift 4
  stand_down "$name"
  dk network create "$name" >/dev/null
  dk run -d --name "$name" --network "$name" --network-alias panel \
    -e HOST=0.0.0.0 -e PORT=31821 \
    -e NUXT_SESSION_PASSWORD="$(openssl rand -hex 32)" -e NUXT_SESSION_COOKIE_SECURE=false \
    -e NUXT_DATABASE_URL=file:/var/lib/anysda-vpn2/db.sqlite \
    -e NUXT_ADMIN_USER=admin -e NUXT_ADMIN_PASSWORD="$pass" \
    -e NUXT_WG_ENABLED=true -e NUXT_WG_PUBLIC_HOST=$PUBLIC_HOST \
    -e NUXT_OVPN_ENABLED=true -e NUXT_OVPN_PUBLIC_HOST=$PUBLIC_HOST \
    -e NUXT_EXIT_TAGS="$EXIT_TAGS" -e NUXT_MGMT_IPS="$MGMT_IPS" \
    -e NUXT_NODE_HOSTS='ru=192.0.2.1,nl=192.0.2.2,fi=192.0.2.3,us=192.0.2.4' \
    -e NUXT_VM_URL=http://127.0.0.1:8428 -e NUXT_CLASH_API_URL=http://127.0.0.1:9090 \
    -e NUXT_PUBLIC_SSO_ENABLED=false \
    "$@" "$image" ${STAND_CMD[@]+"${STAND_CMD[@]}"} >/dev/null

  dk exec -i "$name" sh -c 'cat > /tmp/fake-backends.mjs' < "$STAND_APP_DIR/e2e/support/fake-backends.mjs"
  dk exec -d "$name" node /tmp/fake-backends.mjs

  # openssl.cnf берём из стадии 29 как есть, чтобы `openssl ca` в панели работал
  # с тем же конфигом, что на ноде.
  awk '/cat > "\$PKI\/openssl.cnf" <</ {f=1; next} f && /^EOF$/ {exit} f' \
    "$STAND_REPO_DIR/infra/scripts/29-openvpn.sh" \
    | dk exec -i "$name" sh -c 'mkdir -p /etc/openvpn/server/pki && cat > /etc/openvpn/server/pki/openssl.cnf'

  dk exec -i -e PUBLIC_HOST=$PUBLIC_HOST "$name" bash -s <<'IN'
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
  local caddyfile='{
    servers {
        protocols h1 h2
    }
}
:80 {
    encode gzip
    reverse_proxy panel:31821 {
        transport http {
            keepalive off
        }
    }
}'
  dk run -d --name "$name-caddy" --network "$name" -p "$publish:80" \
    -e CADDYFILE="$caddyfile" mirror.gcr.io/library/caddy:2 \
    sh -c 'printf "%s\n" "$CADDYFILE" > /tmp/Caddyfile && exec caddy run --adapter caddyfile --config /tmp/Caddyfile' >/dev/null
}

# stand_wait <имя> <url> [секунд, 60]: дождаться init и /api/version.
# Не дождались - хвост журнала панели и код 1.
stand_wait() {
  local name=$1 url=$2 sec=${3:-60}
  for _ in $(seq "$sec"); do
    if dk logs "$name" 2>&1 | grep -F 'panel init complete' >/dev/null \
      && curl -fsS "$url/api/version" >/dev/null 2>&1; then
      curl -fsS "$url/api/version"
      echo
      return 0
    fi
    sleep 1
  done
  dk logs "$name" | tail -50
  return 1
}
