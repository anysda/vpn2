#!/usr/bin/env bash
# Stage 30 — anysda-vpn2 panel container on RU.
# Pulls the panel image from the public GitLab Container Registry
# (registry.anysda.space/anysda/vpn2/panel), renders /etc/anysda/config.yaml
# from admin secrets, installs Caddy, runs the panel as host-network
# container on :51821 (Caddy reverse-proxies :80). NET_ADMIN cap only —
# NO SYS_MODULE, NO /etc/wireguard, NO /lib/modules.

set -euo pipefail

[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}" "${ENTRY_HOST:?}" "${AGH_PORT:?}"

case "$HOST_TAG" in ru) ;; *) echo "[$HOST_TAG] 30-frontend is ru-only — skipping"; exit 0;; esac

STAMP_DIR=/var/anysda/.stamps
STAGE='30-frontend'

mkdir -p /etc/anysda /opt/anysda-vpn2 /var/lib/anysda-vpn2

# ----------------------------------------------------------------------------
# 1. Pull panel image from the public GitLab Container Registry
# ----------------------------------------------------------------------------
PANEL_IMAGE="${PANEL_IMAGE:-registry.anysda.space/anysda/vpn2/panel:dev}"
echo "[$HOST_TAG] [1/4] docker pull ${PANEL_IMAGE}"
# Registry живёт дома, за домашним каналом: если он лёг (или образ собрали и
# загрузили на ноду напрямую через `docker save | ssh … docker load`), стадия
# не должна ронять деплой — берём уже лежащий локально образ. Нет ни там, ни
# там — вот тогда падаем.
if ! docker pull "$PANEL_IMAGE" 2>&1 | sed "s/^/[$HOST_TAG]   /"; then
  docker image inspect "$PANEL_IMAGE" >/dev/null 2>&1 \
    || { echo "[$HOST_TAG] ✗ образа ${PANEL_IMAGE} нет ни в registry, ни локально"; exit 1; }
  echo "[$HOST_TAG]   ⚠ registry недоступен — беру локальный образ ${PANEL_IMAGE}"
fi

# ----------------------------------------------------------------------------
# 2. Generate or load admin password, render anysda-config.yaml
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [2/4] config"
if [[ -n "${ADMIN_PASSWORD:-}" ]]; then
  printf '%s' "$ADMIN_PASSWORD" > /etc/anysda/admin-password.txt
  chmod 600 /etc/anysda/admin-password.txt
elif [[ ! -f /etc/anysda/admin-password.txt ]]; then
  openssl rand -base64 24 | tr -d '\n/+=' | head -c 18 > /etc/anysda/admin-password.txt
  chmod 600 /etc/anysda/admin-password.txt
fi
ADMIN_PASS=$(cat /etc/anysda/admin-password.txt)
ADMIN_USER="${ADMIN_USER:-anysda}"

TPL=/tmp/anysda/anysda-config.yaml.tpl
[[ -f "$TPL" ]] || { echo "[$HOST_TAG] $TPL не найден"; exit 1; }

export ADMIN_USER ADMIN_PASS
envsubst < "$TPL" > /etc/anysda/anysda-config.yaml
chmod 600 /etc/anysda/anysda-config.yaml

# Session secret — random 64-char hex, persisted so cookies survive container rebuilds
if [[ ! -f /etc/anysda/session-secret.txt ]]; then
  openssl rand -hex 32 > /etc/anysda/session-secret.txt
  chmod 600 /etc/anysda/session-secret.txt
fi
SESSION_SECRET=$(cat /etc/anysda/session-secret.txt)

echo "[$HOST_TAG]   admin user:  $ADMIN_USER"
echo "[$HOST_TAG]   admin pass:  (см. /etc/anysda/admin-password.txt на ноде)"

# ----------------------------------------------------------------------------
# 3. Caddy (reverse-proxy + optional LE)
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [3/4] caddy"
if ! command -v caddy >/dev/null 2>&1; then
  apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https >/dev/null
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
    | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy >/dev/null
fi

if [[ -n "${PANEL_DOMAIN:-}" ]]; then
  echo "[$HOST_TAG]   домен панели: ${PANEL_DOMAIN} (HTTPS через Let's Encrypt)"
  cat > /etc/caddy/Caddyfile <<EOF
${PANEL_DOMAIN} {
    encode gzip
    reverse_proxy 127.0.0.1:51821
}
:80 {
    redir https://${PANEL_DOMAIN}{uri} permanent
}
${PANEL_DOMAIN}:3001 {
    reverse_proxy 127.0.0.1:${AGH_PORT}
}
EOF
else
  echo "[$HOST_TAG]   домен не задан — HTTP на :80"
  # Note: h3's DEFAULT_COOKIE.secure=true is patched to false at image build
  # time (see app/Dockerfile sed step). So the session cookie works over HTTP.
  cat > /etc/caddy/Caddyfile <<EOF
:80 {
    encode gzip
    reverse_proxy 127.0.0.1:51821
}
:3001 {
    reverse_proxy 127.0.0.1:${AGH_PORT}
}
EOF
fi
ufw allow 80/tcp   comment 'caddy HTTP + ACME challenge' || true
ufw allow 443/tcp  comment 'caddy HTTPS панель'          || true
ufw allow 3001/tcp comment 'AdGuard Home web UI'         || true
ufw reload >/dev/null
caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1 \
  && systemctl restart caddy \
  || { echo "[$HOST_TAG] конфиг caddy невалиден"; exit 1; }

# ----------------------------------------------------------------------------
# 4. Run container
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [4/4] run container"
docker rm -f anysda-vpn2 >/dev/null 2>&1 || true

# host networking — panel manages WG/OpenVPN configs and talks to sing-box
# clash-api. NET_ADMIN is enough; no SYS_MODULE.
docker run -d \
  --name anysda-vpn2 \
  --restart unless-stopped \
  --network host \
  --pid host \
  --security-opt apparmor=unconfined \
  --cap-add NET_ADMIN \
  -v /etc/localtime:/etc/localtime:ro \
  -v /etc/timezone:/etc/timezone:ro \
  -v /etc/anysda/anysda-config.yaml:/etc/anysda/config.yaml:ro \
  -v /etc/anysda:/etc/anysda \
  -v /etc/wireguard:/etc/wireguard \
  -v /etc/openvpn:/etc/openvpn \
  -v /etc/strongswan:/etc/strongswan \
  -v /etc/swanctl:/etc/swanctl \
  -v /var/lib/anysda-vpn2:/var/lib/anysda-vpn2 \
  -e NODE_ENV=production \
  -e PORT=51821 \
  -e HOST=127.0.0.1 \
  -e NUXT_SESSION_PASSWORD="$SESSION_SECRET" \
  -e NUXT_SESSION_COOKIE_SECURE="$([[ -n "${PANEL_DOMAIN:-}" ]] && echo true || echo false)" \
  -e NUXT_DATABASE_URL="file:/var/lib/anysda-vpn2/db.sqlite" \
  -e NUXT_ANYSDA_CONFIG_PATH=/etc/anysda/config.yaml \
  -e NUXT_WG_ENABLED=true \
  -e NUXT_WG_LISTEN_PORT="${WG_LISTEN_PORT:-51820}" \
  -e NUXT_WG_SERVER_IP="${WG_SERVER_IP:-10.66.66.1}" \
  -e NUXT_WG_SUBNET_PREFIX="${WG_SUBNET_PREFIX:-10.66.66.}" \
  -e NUXT_WG_PUBLIC_HOST="${WG_PUBLIC_HOST:-$ENTRY_HOST}" \
  -e NUXT_WG_DNS="${WG_DNS:-10.99.0.1}" \
  -e NUXT_WG_MTU="${WG_MTU:-1420}" \
  -e NUXT_WG_SPLIT_LOCAL="${WG_SPLIT_LOCAL:-true}" \
  -e NUXT_OVPN_ENABLED=true \
  -e NUXT_OVPN_PORT="${OVPN_PORT:-1194}" \
  -e NUXT_OVPN_PROTO="${OVPN_PROTO:-udp}" \
  -e NUXT_OVPN_PUBLIC_HOST="${OVPN_PUBLIC_HOST:-$ENTRY_HOST}" \
  -e NUXT_ROUTES_FILE_PATH=/etc/anysda/manual-routes.json \
  -e NUXT_CLASH_SECRET="$(cat /etc/anysda/clash-secret.txt 2>/dev/null || true)" \
  -e NUXT_CLASH_API_URL="http://${MGMT_IP}:9090" \
  -e NUXT_AGH_URL="http://127.0.0.1:${AGH_PORT}" \
  -e NUXT_AGH_USER="${ADMIN_USER}" \
  -e NUXT_AGH_PASSWORD="${ADMIN_PASS}" \
  -e NUXT_EXIT_TAGS="${EXIT_TAGS}" \
  -e NUXT_MGMT_MESH_IP_PREFIX="10.99.0." \
  -e NUXT_MGMT_IPS="$(_pairs="ru:${MGMT_IP_RU:-10.99.0.1}"; for _t in ${EXIT_TAGS}; do _u=$(echo "$_t" | tr a-z A-Z); _v=$(eval echo "\${MGMT_IP_${_u}:-}"); [ -n "$_v" ] && _pairs="$_pairs,$_t:$_v"; done; echo "$_pairs")" \
  -e NUXT_VM_URL="http://127.0.0.1:8428" \
  -e NUXT_TGBOT_SECRET="$(cat /etc/anysda/tgbot-secret.txt 2>/dev/null || true)" \
  -e NUXT_TGBOT_EVENT_PORT=8877 \
  -e LOG_LEVEL=info \
  "$PANEL_IMAGE" \
  >/dev/null

echo "[$HOST_TAG] жду пока контейнер откроет HTTP…"
for i in $(seq 1 60); do
  curl -fsS -o /dev/null http://127.0.0.1:51821/api/version 2>/dev/null && break
  sleep 1
done
echo "[$HOST_TAG] статус контейнера:"
docker ps --filter name=anysda-vpn2 --format '  {{.Names}} {{.Status}} {{.Ports}}' | sed "s/^/[$HOST_TAG]   /"
echo "[$HOST_TAG] /api/version:"
curl -sS http://127.0.0.1:51821/api/version 2>/dev/null | sed "s/^/[$HOST_TAG]   /"

mkdir -p "$STAMP_DIR"
touch "$STAMP_DIR/$STAGE"
echo "[$HOST_TAG] $STAGE done"
echo
echo "[$HOST_TAG] -------------------------------------------------------------"
if [[ -n "${PANEL_DOMAIN:-}" ]]; then
  echo "[$HOST_TAG]  Панель:        https://${PANEL_DOMAIN}/"
else
  echo "[$HOST_TAG]  Панель:        http://${ENTRY_HOST}/"
fi
echo "[$HOST_TAG]  Логин:         $ADMIN_USER / (пароль в /etc/anysda/admin-password.txt)"
echo "[$HOST_TAG] -------------------------------------------------------------"
