#!/usr/bin/env bash
# Stage 30 — anysda-vpn2 panel container on RU.
# Pulls the panel image from the GitHub Container Registry (ghcr.io)
# (ghcr.io/anysda/vpn2/panel), renders /etc/anysda/config.yaml
# from admin secrets, installs Caddy, runs the panel as host-network
# container on :51821 (Caddy reverse-proxies :80/:443), without capabilities.

set -euo pipefail

[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}" "${ENTRY_HOST:?}" "${AGH_PORT:?}"
# Перезапуск служб только при смене их входов (infra/lib/anysda-svc.sh): деплой
# кладёт библиотеку рядом со стадией, anysda-restore зовёт стадию из клона репо.
SVC_LIB=$(dirname "$0")/anysda-svc.sh
[[ -f "$SVC_LIB" ]] || SVC_LIB=$(dirname "$0")/../lib/anysda-svc.sh
# shellcheck source=infra/lib/anysda-svc.sh
source "$SVC_LIB"

case "$HOST_TAG" in ru) ;; *) echo "[$HOST_TAG] 30-frontend is ru-only — skipping"; exit 0;; esac

RESOURCE_LIB=$(dirname "$0")/entry-resources.sh
[[ -f "$RESOURCE_LIB" ]] || RESOURCE_LIB=$(dirname "$0")/../lib/entry-resources.sh
# shellcheck source=infra/lib/entry-resources.sh
source "$RESOURCE_LIB"
entry_resources panel

STAMP_DIR=/var/anysda/.stamps
STAGE='30-frontend'

mkdir -p /etc/anysda /opt/anysda-vpn2 /var/lib/anysda-vpn2

# ----------------------------------------------------------------------------
# 1. Pull panel image from the GitHub Container Registry (ghcr.io)
# ----------------------------------------------------------------------------
RELEASE_LIB=$(dirname "$0")/release.sh
[[ -f "$RELEASE_LIB" ]] || RELEASE_LIB=$(dirname "$0")/../lib/release.sh
# shellcheck source=infra/lib/release.sh
source "$RELEASE_LIB"
PANEL_IMAGE=$(release_image panel "${PANEL_IMAGE:-}" "${PANEL_IMAGE_PULL:-true}")
echo "[$HOST_TAG] [1/4] panel image digest verified"

TRANSACTION_LIB=$(dirname "$0")/panel-transaction.sh
[[ -f "$TRANSACTION_LIB" ]] || TRANSACTION_LIB=$(dirname "$0")/../lib/panel-transaction.sh
# shellcheck source=infra/lib/panel-transaction.sh
source "$TRANSACTION_LIB"
# Candidate code checks its own API requirements using the actual container UID.
PANEL_UID=$(id -u vpn2-panel)
PANEL_GID=$(id -g vpn2-panel)
CONTROL_GID=$(getent group vpn2-control | cut -d: -f3)
docker run --rm --network none --user "$PANEL_UID:$PANEL_GID" \
  --group-add "$CONTROL_GID" --cap-drop ALL --security-opt no-new-privileges \
  -v /run/vpn2-control:/run/vpn2-control:ro \
  --entrypoint node "$PANEL_IMAGE" scripts/control-probe.mjs
panel_begin
chown vpn2-panel:vpn2-panel /var/lib/anysda-vpn2
for name in db.sqlite db.sqlite-wal db.sqlite-shm; do
  [[ ! -e /var/lib/anysda-vpn2/$name ]] || chown -h vpn2-panel:vpn2-panel "/var/lib/anysda-vpn2/$name"
done

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

# --- SSO (Authentik / OIDC) -------------------------------------------------
# Приезжает из блока `sso:` в config.yaml через config2env.py. Выключено —
# панель ведёт себя ровно как раньше (форма логин/пароль).
SSO_ENABLED="${SSO_ENABLED:-false}"
SSO_CLIENT_SECRET="${SSO_CLIENT_SECRET:-}"
# После первого сохранения в профиле панели главнее её настройки: env
# только засевает их, файл стадия не трогает и не проверяет неиспользуемый засев.
SSO_RUNTIME=/etc/anysda/sso-runtime.json
SSO_REDIRECT_URL=""
# Засев обязан оставаться безопасным и после удаления/restore runtime-файла.
# Проверяем также старые env, созданные до защиты в config2env.
if [[ -z "${SSO_CLIENT_SECRET//[[:space:]]/}" ]]; then
  SSO_ENABLED=false
fi
if [[ "$SSO_ENABLED" != "true" ]]; then
  SSO_AUTO_REDIRECT=false
  SSO_PASSWORD_LOGIN=true
fi
if [[ "$SSO_ENABLED" == "true" && -n "${PANEL_DOMAIN:-}" ]]; then
  SSO_REDIRECT_URL="https://${PANEL_DOMAIN}/auth/authentik"
fi
if [[ -f "$SSO_RUNTIME" ]]; then
  echo "[$HOST_TAG]   SSO: настроен в профиле панели ($SSO_RUNTIME), блок sso: из config.yaml не применяется"
elif [[ "$SSO_ENABLED" == "true" ]]; then
  # ⚠️ ЖЁСТКАЯ ЗАВИСИМОСТЬ ОТ HTTPS. Адрес возврата у провайдера в Authentik
  # строится от домена панели (https://<домен>/auth/authentik), а куки
  # state/PKCE получают флаг Secure вместе с доменом (NUXT_SESSION_COOKIE_SECURE
  # ниже). Без домена вход падал бы на возврате, и симптом выглядел бы как
  # поломка Authentik — поэтому падаем заранее и вслух.
  [[ -n "${PANEL_DOMAIN:-}" ]] || {
    echo "[$HOST_TAG] ✗ sso.enabled=true, но panel.domain не задан."
    echo "[$HOST_TAG]   SSO работает только по HTTPS: куки state/PKCE — Secure-only."
    exit 1
  }
  : "${SSO_DISCOVERY_URL:?SSO_DISCOVERY_URL пуст}" \
    "${SSO_CLIENT_ID:?SSO_CLIENT_ID пуст}" \
    "${SSO_CLIENT_SECRET:?SSO_CLIENT_SECRET пуст}" \
    "${SSO_ALLOWED_SUBS:?SSO_ALLOWED_SUBS пуст — войти не сможет никто}"
  echo "[$HOST_TAG]   SSO: ${SSO_CLIENT_ID} @ ${SSO_DISCOVERY_URL}"
  echo "[$HOST_TAG]   SSO redirect_uri: ${SSO_REDIRECT_URL} (должен совпадать с провайдером в Authentik)"
else
  SSO_REDIRECT_URL=""
  echo "[$HOST_TAG]   SSO: выключен"
fi
TPL=/tmp/anysda/anysda-config.yaml.tpl
[[ -f "$TPL" ]] || { echo "[$HOST_TAG] $TPL не найден"; exit 1; }

yaml_str() { python3 -c 'import json,sys; print(json.dumps(sys.argv[1], ensure_ascii=False))' "$1"; }
ADMIN_USER_YAML=$(yaml_str "$ADMIN_USER")
ADMIN_PASS_YAML=$(yaml_str "$ADMIN_PASS")
export ADMIN_USER_YAML ADMIN_PASS_YAML
envsubst '${ADMIN_USER_YAML} ${ADMIN_PASS_YAML}' < "$TPL" > /etc/anysda/anysda-config.yaml
chmod 600 /etc/anysda/anysda-config.yaml
chown root:vpn2-panel /etc/anysda/anysda-config.yaml
chmod 0640 /etc/anysda/anysda-config.yaml

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
caddy_install

# HTTPS: с доменом — сертификат на домен, без домена — на IP entry (профиль
# Let's Encrypt shortlived, ~6 дней). Выпускает и обновляет Caddy; окно
# обновления, выкладку в /etc/vpn2/tls/<ip|domain>, переход на HTTPS после
# первого сертификата и уведомления о сбое ведёт vpn2_tls.py (он же у экзитов).
TLS_LIB=$(dirname "$0")/vpn2_tls.py
[[ -f "$TLS_LIB" ]] || TLS_LIB=$(dirname "$0")/../lib/vpn2_tls.py
install -D -m 755 "$TLS_LIB" /usr/local/lib/vpn2/vpn2_tls.py
systemctl stop vpn2-tls.timer vpn2-tls.service 2>/dev/null || true
if [[ -n "${PANEL_DOMAIN:-}" ]]; then
  TLS_HOST=$PANEL_DOMAIN SITE=$PANEL_DOMAIN TLS_OUT=/etc/vpn2/tls/domain
  echo "[$HOST_TAG]   домен панели: ${PANEL_DOMAIN} (HTTPS через Let's Encrypt)"
else
  TLS_HOST=$ENTRY_HOST SITE="https://${ENTRY_HOST}" TLS_OUT=/etc/vpn2/tls/ip
  echo "[$HOST_TAG]   домен не задан — HTTPS на IP ${ENTRY_HOST} (Let's Encrypt, ~6 дней)"
fi
# Вставки TLS принадлежат релизу панели: при откате панели возвращаются вместе
# с ней, Caddy перечитывает прежние, таймер TLS снова запущен.
panel_files_restored() {
  systemctl reload-or-restart caddy || true
  systemctl try-restart openvpn-server@server || true
  [[ -f /etc/systemd/system/vpn2-tls.timer ]] && systemctl start vpn2-tls.timer || true
}
panel_guard_files /etc/caddy/Caddyfile /etc/caddy/vpn2-tls.conf /etc/caddy/vpn2-http.conf \
  /etc/vpn2/tls/config.json /etc/vpn2/tls/panel/secure /etc/vpn2-control/api-managed \
  /etc/openvpn/server/server.conf
TLS_PATHS=(--config /etc/vpn2/tls/config.json --caddy-dir /etc/caddy
  --storage /var/lib/caddy/.local/share/caddy --secure-file /etc/vpn2/tls/panel/secure
  --bot-secret /etc/anysda/tgbot-secret.txt)
python3 /usr/local/lib/vpn2/vpn2_tls.py --prepare --role panel "${TLS_PATHS[@]}" \
  --panel-container anysda-vpn2 --host "$TLS_HOST" --output-dir "$TLS_OUT" \
  --acme-url "${TLS_ACME_URL:-https://acme-v02.api.letsencrypt.org/directory}" \
  --acme-root "${TLS_ACME_ROOT:-}"

cat > /etc/caddy/Caddyfile <<EOF
{
    # :80 до первого сертификата отдаёт панель, после — переадресует на HTTPS
    # (/etc/caddy/vpn2-http.conf пишет vpn2_tls.py).
    auto_https disable_redirects
    # Клиент по IP не шлёт SNI.
    default_sni ${TLS_HOST}
    # HTTP/3 выключен намеренно: Caddy с h3 держит *:443/udp, а sing-box при
    # возврате UDP через tproxy биндит transparent-сокет на <адрес-цели>:443.
    # Порт занят -> «bind: address already in use», и QUIC-ответы не доходят
    # НИ ДО ОДНОГО клиента туннеля (VPN2-22). Панели h3 не нужен.
    servers {
        protocols h1 h2
    }
}
# Пути бота — только для самой ноды: бот ходит на 127.0.0.1:51821 мимо
# Caddy. Caddy сам подключается к
# панели с 127.0.0.1, поэтому проверка loopback в панели отсюда бесполезна:
# закрываем здесь (VPN2-51). Остальной /api/ops (nodes, adguard) зовёт
# браузер админа со страницы мониторинга — его не трогаем.
(internal_only) {
    @internal path /api/bot /api/bot/* /api/ops/bot-snapshot
    respond @internal 404
}
# X-Forwarded-For панели ставит только Caddy, перезаписывая присланный
# клиентом: по нему считается лимит попыток входа (VPN2-63). По умолчанию
# Caddy и так перезаписывает, но с trusted_proxies начал бы дописывать;
# его предупреждение «Unnecessary header_up» при запуске ожидаемо.
(to_panel) {
    reverse_proxy 127.0.0.1:51821 {
        header_up X-Forwarded-For {remote_host}
        # Node closes idle HTTP/1 sockets; never reuse one for a mutating POST.
        transport http {
            keepalive off
        }
    }
}
${SITE} {
    import /etc/caddy/vpn2-tls.conf
    import internal_only
    encode gzip
    import to_panel
}
:80 {
    import /etc/caddy/vpn2-http.conf
}
EOF
ufw_sync_rules --alias 'caddy HTTP панель' 'caddy HTTP + ACME challenge' 80/tcp
ufw_sync_rules 'caddy HTTPS панель' 443/tcp
# Веб-интерфейс AdGuard наружу не открыт: только 127.0.0.1, управление — вкладка «DNS».
# Отдельный порт 3001 с прошлых установок закрываем.
ufw delete allow 3001/tcp >/dev/null 2>&1 || true
caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1 \
  || { echo "[$HOST_TAG] конфиг caddy невалиден"; exit 1; }
svc_restart_if_changed caddy /usr/bin/caddy /usr/lib/systemd/system/caddy.service /etc/caddy/Caddyfile \
  /etc/caddy/vpn2-tls.conf /etc/caddy/vpn2-http.conf
cat > /etc/systemd/system/vpn2-tls.service <<'EOF'
[Unit]
Description=VPN2 TLS certificate export and notifications
After=caddy.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/lib/vpn2/vpn2_tls.py
TimeoutStartSec=120
EOF
cat > /etc/systemd/system/vpn2-tls.timer <<'EOF'
[Unit]
Description=Check VPN2 TLS lifecycle every minute
[Timer]
OnBootSec=1min
OnUnitInactiveSec=1min
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload

# ----------------------------------------------------------------------------
# 4. Run container
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [4/4] run container"
# Публичные адреса нод для карточек мониторинга: «ru=<entry>,<тег>=<ip>,…».
NODE_HOSTS="ru=${ENTRY_HOST}"
for _t in ${EXIT_TAGS:-}; do
  _v="DOMAIN_$(printf '%s' "$_t" | tr 'a-z-' 'A-Z_')"
  NODE_HOSTS+=",${_t}=${!_v:-}"
done
# До удаления старого контейнера сохраняем карту для обновления с версии,
# которая ещё не хранила адреса экзитов в БД. Читаем только нужную переменную.
# При неудачном первом запуске повтор деплоя сохраняет исходную карту;
# после успешного запуска панель берёт более свежую карту из БД.
PREVIOUS_NODE_HOSTS=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' anysda-vpn2-previous 2>/dev/null \
  | awk '/^NUXT_NODE_HOSTS=/ {sub(/^[^=]*=/, ""); current=$0}
         /^NUXT_PREVIOUS_NODE_HOSTS=/ {sub(/^[^=]*=/, ""); previous=$0}
         END {print previous != "" ? previous : current}') || PREVIOUS_NODE_HOSTS=
docker rm -f anysda-vpn2 >/dev/null 2>&1 || true
# /etc/timezone не монтируем: в Ubuntu 26.04 его нет, docker создаёт на его
# месте каталог и контейнер не стартует. Время берут из /etc/localtime.
[[ -d /etc/timezone ]] && rmdir /etc/timezone 2>/dev/null || true

# Host networking is retained for loopback AdGuard, metrics and bot HTTP APIs.
# All VPN system effects use the authenticated Unix socket.
systemctl stop anysda-ikev2-sync.timer anysda-ikev2-sync.service 2>/dev/null || true
if [[ -f /etc/openvpn/server/server.conf ]] \
  && ! grep -qx 'management-client-user vpn2-control' /etc/openvpn/server/server.conf; then
  sed -i 's/^management-client-user .*/management-client-user vpn2-control/' /etc/openvpn/server/server.conf
  ovpn_restart_if_changed
fi
touch /etc/vpn2-control/api-managed
systemctl start anysda-ikev2-sync.timer 2>/dev/null || true
# Runtime-бюджет рассчитан на entry; сборка Nuxt остаётся в CI.
# Флаг Secure cookie (и протокол проверки Origin в CSRF) читается из файла при
# каждом старте: vpn2_tls.py включает его после первого сертификата и
# перезапускает контейнер — окружение docker restart не меняет.
docker run -d \
  --name anysda-vpn2 \
  "${ENTRY_DOCKER_ARGS[@]}" \
  --restart always \
  --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 \
  --network host \
  --user "$PANEL_UID:$PANEL_GID" --group-add "$CONTROL_GID" \
  --cap-drop ALL --security-opt no-new-privileges \
  -v /etc/localtime:/etc/localtime:ro \
  -v /etc/vpn2/tls/panel:/run/vpn2-tls:ro \
  -v /etc/anysda/anysda-config.yaml:/etc/anysda/config.yaml:ro \
  -v /run/vpn2-control:/run/vpn2-control:ro \
  -v /var/lib/anysda-vpn2:/var/lib/anysda-vpn2 \
  -e NODE_ENV=production \
  -e PORT=51821 \
  -e HOST=127.0.0.1 \
  -e NUXT_SESSION_PASSWORD="$SESSION_SECRET" \
  -e NUXT_DATABASE_URL="file:/var/lib/anysda-vpn2/db.sqlite" \
  -e NUXT_ANYSDA_CONFIG_PATH=/etc/anysda/config.yaml \
  -e NUXT_WG_ENABLED=true \
  -e NUXT_WG_LISTEN_PORT="${WG_LISTEN_PORT:-51820}" \
  -e NUXT_WG_SERVER_IP="${WG_SERVER_IP:-10.66.66.1}" \
  -e NUXT_WG_SUBNET_PREFIX="${WG_SUBNET_PREFIX:-10.66.66.}" \
  -e NUXT_WG_PUBLIC_HOST="${WG_PUBLIC_HOST:-$ENTRY_HOST}" \
  -e NUXT_WG_ENDPOINT_IP="$ENTRY_HOST" \
  -e NUXT_NODE_HOSTS="$NODE_HOSTS" \
  -e NUXT_PREVIOUS_NODE_HOSTS="${PREVIOUS_NODE_HOSTS:-}" \
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
  -e NUXT_AGH_PASSWORD="$(agh_password "$ADMIN_PASS")" \
  -e NUXT_EXIT_TAGS="${EXIT_TAGS}" \
  -e NUXT_MGMT_MESH_IP_PREFIX="10.99.0." \
  -e NUXT_MGMT_IPS="$(_pairs="ru:${MGMT_IP_RU:-10.99.0.1}"; for _t in ${EXIT_TAGS}; do _u=$(echo "$_t" | tr 'a-z-' 'A-Z_'); _key="MGMT_IP_${_u}"; _v="${!_key:-}"; [ -n "$_v" ] && _pairs="$_pairs,$_t:$_v"; done; printf '%s' "$_pairs")" \
  -e NUXT_VM_URL="http://127.0.0.1:8428" \
  -e NUXT_TGBOT_SECRET="$(cat /etc/anysda/tgbot-secret.txt 2>/dev/null || true)" \
  -e NUXT_TGBOT_EVENT_PORT=8877 \
  -e NUXT_PUBLIC_SSO_ENABLED="${SSO_ENABLED}" \
  -e NUXT_PUBLIC_SSO_AUTO_REDIRECT="${SSO_AUTO_REDIRECT:-true}" \
  -e NUXT_PUBLIC_SSO_LABEL="${SSO_LABEL:-Authentik}" \
  -e NUXT_SSO_ALLOWED_SUBS="${SSO_ALLOWED_SUBS:-}" \
  -e NUXT_SSO_PASSWORD_LOGIN="${SSO_PASSWORD_LOGIN:-true}" \
  -e NUXT_OAUTH_OIDC_CLIENT_ID="${SSO_CLIENT_ID:-}" \
  -e NUXT_OAUTH_OIDC_CLIENT_SECRET="${SSO_CLIENT_SECRET:-}" \
  -e NUXT_OAUTH_OIDC_OPENID_CONFIG="${SSO_DISCOVERY_URL:-}" \
  -e NUXT_OAUTH_OIDC_REDIRECT_URL="${SSO_REDIRECT_URL}" \
  -e LOG_LEVEL=info \
  "$PANEL_IMAGE" \
  sh -ec 'export NUXT_SESSION_COOKIE_SECURE=$(cat /run/vpn2-tls/secure); exec node .output/server/index.mjs' \
  >/dev/null

echo "[$HOST_TAG] жду пока контейнер откроет HTTP…"
http_up=0
for _ in $(seq 1 300); do
  if curl -fsS -o /dev/null http://127.0.0.1:51821/api/version 2>/dev/null; then http_up=1; break; fi
  sleep 0.2
done
# Битый anysda-config.yaml роняет init панели (process.exit(1) в
# server/plugins/init.ts). Причину печатаем раньше общей проверки ниже:
# чинить надо конфиг, а не панель.
if bad=$(docker logs anysda-vpn2 2>&1 | grep 'anysda config .* is invalid' | tail -1); then
  echo "[$HOST_TAG] ✗ панель не разобрала /etc/anysda/anysda-config.yaml, пароль админа не применён:"
  echo "[$HOST_TAG]   ${bad:0:400}"
  exit 1
fi
if [[ $http_up -ne 1 ]] || docker logs anysda-vpn2 2>&1 | grep 'panel init failed' >/dev/null; then
  echo "[$HOST_TAG] ✗ панель не поднялась: /api/version не ответил 2xx за 60 с или init упал"
  echo "[$HOST_TAG]   контейнер: $(docker ps -a --filter name='^anysda-vpn2$' --format '{{.Status}}')"
  echo "[$HOST_TAG]   хвост журнала панели:"
  docker logs --tail 30 anysda-vpn2 2>&1 | HOST_TAG="$HOST_TAG" awk '{print "[" ENVIRON["HOST_TAG"] "]     " $0}'
  exit 1
fi
echo "[$HOST_TAG] статус контейнера:"
# Переносы и сбросы видны и в журнале панели, и в выводе ./deploy.sh.
docker logs anysda-vpn2 2>&1 | sed -n '/client exit preference:/p'
docker ps --filter name=anysda-vpn2 --format '  {{.Names}} {{.Status}} {{.Ports}}' | HOST_TAG="$HOST_TAG" awk '{print "[" ENVIRON["HOST_TAG"] "]   " $0}'
echo "[$HOST_TAG] /api/version:"
curl -sS http://127.0.0.1:51821/api/version 2>/dev/null | HOST_TAG="$HOST_TAG" awk '{print "[" ENVIRON["HOST_TAG"] "]   " $0}'

mkdir -p "$STAMP_DIR"
touch "$STAMP_DIR/$STAGE"
echo "[$HOST_TAG] $STAGE done"
panel_finish
# Первый выпуск — после проверенной панели: неудача ACME не откатывает релиз,
# панель остаётся на HTTP, Caddy повторяет выпуск сам.
python3 /usr/local/lib/vpn2/vpn2_tls.py --initial --config /etc/vpn2/tls/config.json \
  --wait "${TLS_INITIAL_WAIT:-60}"
systemctl enable --now vpn2-tls.timer >/dev/null
echo
echo "[$HOST_TAG] -------------------------------------------------------------"
PANEL_SCHEME=http
[[ "$(cat /etc/vpn2/tls/panel/secure)" == true ]] && PANEL_SCHEME=https
echo "[$HOST_TAG]  Панель:        ${PANEL_SCHEME}://${PANEL_DOMAIN:-$ENTRY_HOST}/"
if [[ -f "$SSO_RUNTIME" ]]; then
  echo "[$HOST_TAG]  Вход:          как настроен в профиле панели; break-glass /login?direct=1"
  echo "[$HOST_TAG]                 $ADMIN_USER / (пароль в /etc/anysda/admin-password.txt)"
elif [[ "$SSO_ENABLED" == "true" ]]; then
  echo "[$HOST_TAG]  Вход:          через Authentik (бесшовно)"
  echo "[$HOST_TAG]  Break-glass:   https://${PANEL_DOMAIN}/login?direct=1"
  echo "[$HOST_TAG]                 $ADMIN_USER / (пароль в /etc/anysda/admin-password.txt)"
else
  echo "[$HOST_TAG]  Логин:         $ADMIN_USER / (пароль в /etc/anysda/admin-password.txt)"
fi
echo "[$HOST_TAG] -------------------------------------------------------------"
