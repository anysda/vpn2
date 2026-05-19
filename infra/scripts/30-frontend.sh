#!/usr/bin/env bash
# Stage 30 — anysda-vpn frontend container on RU.
# Builds the image from the source tree pushed by the orchestrator,
# renders anysda-config.yaml from secrets, runs as a host-network container.
#
# Pre-DNS (vpn.anysda.space → 193.233.245.247): no Caddy/LE.
# Access via SSH tunnel:  ssh -L 51821:127.0.0.1:51821 root@193.233.245.247
# After DNS lands: add Caddy reverse-proxy in stage 30 (TODO subsection).

set -euo pipefail

[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}" "${DOMAIN_WG:?}" "${WG_PORT:?}" "${WG_CLIENT_CIDR:?}" "${AGH_PORT:?}"

case "$HOST_TAG" in ru) ;; *) echo "[$HOST_TAG] 30-frontend is ru-only — skipping"; exit 0;; esac

STAMP_DIR=/var/anysda/.stamps
STAGE='30-frontend'

mkdir -p /etc/anysda /opt/anysda-vpn

# ----------------------------------------------------------------------------
# 1. Load pre-built Docker image (built locally on orchestrator machine)
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [1/4] docker load"
IMG=/tmp/anysda/anysda-vpn.tar.gz
[[ -f "$IMG" ]] || { echo "[$HOST_TAG] $IMG не найден — оркестратор должен был его загрузить"; exit 1; }
docker load < "$IMG" 2>&1 | sed "s/^/[$HOST_TAG]   /"

# ----------------------------------------------------------------------------
# 2. Generate or load admin password, render anysda-config.yaml
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [2/4] config"
# Учётка из config.yaml (admin.user / admin.password) пробрасывается через ru.env.
# Если ADMIN_PASSWORD пуст и 22-adguard уже сгенерил admin-password.txt — используем его.
if [[ -n "${ADMIN_PASSWORD:-}" ]]; then
  printf '%s' "$ADMIN_PASSWORD" > /etc/anysda/admin-password.txt
  chmod 600 /etc/anysda/admin-password.txt
elif [[ ! -f /etc/anysda/admin-password.txt ]]; then
  openssl rand -base64 24 | tr -d '\n/+=' | head -c 18 > /etc/anysda/admin-password.txt
  chmod 600 /etc/anysda/admin-password.txt
fi
ADMIN_PASS=$(cat /etc/anysda/admin-password.txt)
ADMIN_USER="${ADMIN_USER:-anysda}"
WG_HOST="$DOMAIN_WG"

TPL=/tmp/anysda/anysda-config.yaml.tpl
[[ -f "$TPL" ]] || { echo "[$HOST_TAG] $TPL не найден"; exit 1; }

export ADMIN_USER ADMIN_PASS WG_HOST WG_PORT WG_CLIENT_CIDR
envsubst < "$TPL" > /etc/anysda/anysda-config.yaml
chmod 600 /etc/anysda/anysda-config.yaml

echo "[$HOST_TAG]   admin user: $ADMIN_USER"
echo "[$HOST_TAG]   admin pass: $ADMIN_PASS"
echo "[$HOST_TAG]   wg endpoint: $WG_HOST:$WG_PORT"

# ----------------------------------------------------------------------------
# 3. Caddy (reverse-proxy + LE)
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
# Если задан PANEL_DOMAIN — Caddy автоматически получит Let's Encrypt
# сертификат (ACME на :80, обслуживание HTTPS на :443) для веб-панели.
# Иначе панель отдаётся по HTTP на :80 (трафик и так в WireGuard-туннеле).
# AdGuard UI остаётся на :3001 HTTP — редко используется и WG-only.
if [[ -n "${PANEL_DOMAIN:-}" ]]; then
  echo "[$HOST_TAG]   домен панели: ${PANEL_DOMAIN} (HTTPS через Let's Encrypt)"
  cat > /etc/caddy/Caddyfile <<EOF
${PANEL_DOMAIN} {
    encode gzip
    reverse_proxy 127.0.0.1:51821
}

# IP-fallback на HTTP (если кто-то заходит по IP, не через домен) →
# редирект на HTTPS-домен.
:80 {
    redir https://${PANEL_DOMAIN}{uri} permanent
}

# AdGuard UI — HTTPS через тот же LE-серт домена.
${PANEL_DOMAIN}:3001 {
    reverse_proxy 127.0.0.1:${AGH_PORT}
}
EOF
else
  echo "[$HOST_TAG]   домен не задан — HTTP на :80"
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
# 5. Run container
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [4/4] run container"
docker rm -f anysda-vpn >/dev/null 2>&1 || true

# host networking — panel talks to the kernel WG interface directly
docker run -d \
  --name anysda-vpn \
  --restart unless-stopped \
  --network host \
  --cap-add NET_ADMIN \
  --cap-add SYS_MODULE \
  -v /etc/wireguard:/etc/wireguard \
  -v /etc/anysda/anysda-config.yaml:/etc/anysda/config.yaml:ro \
  -v /etc/anysda:/host-anysda \
  -e HOST_ANYSDA_DIR=/host-anysda \
  -v /lib/modules:/lib/modules:ro \
  -e PORT=51821 \
  -e HOST=127.0.0.1 \
  -e INSECURE=true \
  -e DISABLE_IPV6=true \
  -e ANYSDA_CONFIG_PATH=/etc/anysda/config.yaml \
  -e CLASH_SECRET="$(cat /etc/anysda/clash-secret.txt 2>/dev/null || true)" \
  -e CLASH_API_URL="http://${MGMT_IP}:9090" \
  -e EXIT_TAGS="${EXIT_TAGS}" \
  -e VM_URL="http://127.0.0.1:8428" \
  -e TGBOT_SECRET="$(cat /etc/anysda/tgbot-secret.txt 2>/dev/null || true)" \
  -e TGBOT_EVENT_PORT=8877 \
  anysda-vpn:local \
  >/dev/null

echo "[$HOST_TAG] жду пока контейнер откроет HTTP…"
for i in $(seq 1 30); do
  curl -fsS -o /dev/null http://127.0.0.1:51821/ 2>/dev/null && break
  sleep 1
done
echo "[$HOST_TAG] статус контейнера:"
docker ps --filter name=anysda-vpn --format '  {{.Names}} {{.Status}} {{.Ports}}' | sed "s/^/[$HOST_TAG]   /"
echo "[$HOST_TAG] ответ на login:"
curl -sS -o /dev/null -w "  http=%{http_code}\n" http://127.0.0.1:51821/ | sed "s/^/[$HOST_TAG]   /"

# Wait for wg0: WireGuard.Startup() is fire-and-forget inside the Node process
# so HTTP may respond before the interface is actually up.
echo "[$HOST_TAG] жду пока поднимется wg0…"
for i in $(seq 1 30); do
  ip link show wg0 >/dev/null 2>&1 && break
  sleep 1
done
if ip link show wg0 >/dev/null 2>&1; then
  echo "[$HOST_TAG]   wg0 поднят"
else
  echo "[$HOST_TAG]   wg0 не поднялся за 30 сек — tproxy правила отложены; запусти: systemctl restart anysda-iptables"
fi

# Refresh tproxy iptables now that wg0 exists (idempotent)
systemctl restart anysda-iptables >/dev/null 2>&1 || true

mkdir -p "$STAMP_DIR"
touch "$STAMP_DIR/$STAGE"
echo "[$HOST_TAG] $STAGE done"
echo
echo "[$HOST_TAG] -------------------------------------------------------------"
if [[ -n "${PANEL_DOMAIN:-}" ]]; then
  echo "[$HOST_TAG]  Панель:       https://${PANEL_DOMAIN}/"
else
  echo "[$HOST_TAG]  Панель:       http://${DOMAIN_WG}/"
fi
echo "[$HOST_TAG]  Логин:        $ADMIN_USER / $ADMIN_PASS"
echo "[$HOST_TAG]  (Caddy :80 reverse-proxy → 127.0.0.1:51821 — без TLS, IP-only режим)"
echo "[$HOST_TAG] -------------------------------------------------------------"
