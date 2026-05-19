#!/usr/bin/env bash
# Stage 30 — anysda-vpn2 panel container on RU.
# Loads pre-built image (built by orchestrator on dev machine, pushed as tarball),
# renders /etc/anysda/config.yaml from admin secrets, installs Caddy, runs
# the panel as host-network container on :51821 (Caddy reverse-proxies :80).
# Container has NET_ADMIN cap so it can manage iptables for sing-box if needed —
# NO SYS_MODULE, NO /etc/wireguard, NO /lib/modules (no kernel WG client anymore).

set -euo pipefail

[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}" "${ENTRY_HOST:?}" "${SS_PORT:?}" "${SS_CIPHER:?}" "${AGH_PORT:?}"

case "$HOST_TAG" in ru) ;; *) echo "[$HOST_TAG] 30-frontend is ru-only — skipping"; exit 0;; esac

STAMP_DIR=/var/anysda/.stamps
STAGE='30-frontend'

mkdir -p /etc/anysda /opt/anysda-vpn2 /var/lib/anysda-vpn2

# ----------------------------------------------------------------------------
# 1. Load pre-built Docker image
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [1/4] docker load"
IMG=/tmp/anysda/anysda-vpn2.tar.gz
[[ -f "$IMG" ]] || { echo "[$HOST_TAG] $IMG не найден — оркестратор должен был его загрузить"; exit 1; }
docker load < "$IMG" 2>&1 | sed "s/^/[$HOST_TAG]   /"

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
echo "[$HOST_TAG]   admin pass:  $ADMIN_PASS"
echo "[$HOST_TAG]   ss endpoint: ${ENTRY_HOST}:${SS_PORT} (${SS_CIPHER})"

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

# host networking — panel needs to write SS config + SIGHUP outline-ss-server.
# NET_ADMIN is enough; no SYS_MODULE, no /etc/wireguard mount.
docker run -d \
  --name anysda-vpn2 \
  --restart unless-stopped \
  --network host \
  --pid host \
  --security-opt apparmor=unconfined \
  --cap-add NET_ADMIN \
  -v /etc/anysda/anysda-config.yaml:/etc/anysda/config.yaml:ro \
  -v /etc/anysda:/etc/anysda \
  -v /etc/outline-ss-server:/etc/outline-ss-server \
  -v /var/lib/anysda-vpn2:/var/lib/anysda-vpn2 \
  -e NODE_ENV=production \
  -e PORT=51821 \
  -e HOST=127.0.0.1 \
  -e NUXT_SESSION_PASSWORD="$SESSION_SECRET" \
  -e NUXT_SESSION_COOKIE_SECURE="$([[ -n "${PANEL_DOMAIN:-}" ]] && echo true || echo false)" \
  -e NUXT_DATABASE_URL="file:/var/lib/anysda-vpn2/db.sqlite" \
  -e NUXT_ANYSDA_CONFIG_PATH=/etc/anysda/config.yaml \
  -e NUXT_SS_CONFIG_PATH=/etc/outline-ss-server/config.yml \
  -e NUXT_SS_PORT="${SS_PORT}" \
  -e NUXT_SS_CIPHER="${SS_CIPHER}" \
  -e NUXT_SS_PUBLIC_HOST="${ENTRY_HOST}" \
  -e NUXT_ROUTES_FILE_PATH=/etc/anysda/manual-routes.json \
  -e NUXT_CLASH_SECRET="$(cat /etc/anysda/clash-secret.txt 2>/dev/null || true)" \
  -e NUXT_CLASH_API_URL="http://${MGMT_IP}:9090" \
  -e NUXT_AGH_URL="http://127.0.0.1:${AGH_PORT}" \
  -e NUXT_AGH_USER="${ADMIN_USER}" \
  -e NUXT_AGH_PASSWORD="${ADMIN_PASS}" \
  -e NUXT_EXIT_TAGS="${EXIT_TAGS}" \
  -e NUXT_MGMT_MESH_IP_PREFIX="10.99.0." \
  -e NUXT_VM_URL="http://127.0.0.1:8428" \
  -e NUXT_TGBOT_SECRET="$(cat /etc/anysda/tgbot-secret.txt 2>/dev/null || true)" \
  -e NUXT_TGBOT_EVENT_PORT=8877 \
  -e LOG_LEVEL=info \
  anysda-vpn2:local \
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

# Refresh anysda-iptables after the panel writes initial SS config (the user
# `outline` already exists from stage 27; iptables rules are safe to refresh).
systemctl restart anysda-iptables >/dev/null 2>&1 || true

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
echo "[$HOST_TAG]  Логин:         $ADMIN_USER / $ADMIN_PASS"
echo "[$HOST_TAG]  SS-сервер:     ${ENTRY_HOST}:${SS_PORT} (${SS_CIPHER})"
echo "[$HOST_TAG] -------------------------------------------------------------"
