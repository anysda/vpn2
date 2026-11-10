#!/usr/bin/env bash
# Stage 20 — sing-box роутер на RU-ноде.
# Поднимает sing-box: трафик клиентов (WG/OpenVPN, заведённый стадиями 28/29
# в sing-box TPROXY :7898) роутится по наборам geoip/geosite в hy2-туннели до
# exit-нод (см. vpn2 router config). Идемпотентен.

set -euo pipefail

[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}" "${PUB_IP_OUT:?}" "${HY2_DIRECT_PORT:?}" "${HY2_WARP_PORT:?}" "${HY2_MGMT_PORT:?}" "${EXIT_TAGS:?}"

case "$HOST_TAG" in ru) ;; *) echo "[$HOST_TAG] 20-ru-router is ru-only — skipping"; exit 0;; esac

RESOURCE_LIB=$(dirname "$0")/entry-resources.sh
[[ -f "$RESOURCE_LIB" ]] || RESOURCE_LIB=$(dirname "$0")/../lib/entry-resources.sh
# shellcheck source=infra/lib/entry-resources.sh
source "$RESOURCE_LIB"

STAMP_DIR=/var/anysda/.stamps
STAGE='20-ru-router'
# Генератор конфига, набор адресов и ручные маршруты живут в пакете vpn2.
VPN2_BIN=/opt/vpn2/venv/bin/vpn2
[[ -x "$VPN2_BIN" ]] || { echo "[$HOST_TAG] $VPN2_BIN не найден (venv vpn2 не установлен)"; exit 1; }

mkdir -p /etc/anysda /etc/sing-box /var/lib/sing-box

# ----------------------------------------------------------------------------
# 1. sing-box install
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [1/6] sing-box"
bash /tmp/anysda/install-sing-box.sh
/usr/local/bin/sing-box version | sed -n '1p' | HOST_TAG="$HOST_TAG" awk '{print "[" ENVIRON["HOST_TAG"] "]   " $0}'

# ----------------------------------------------------------------------------
# 2. Наборы правил geoip/geosite (.srs)
# ----------------------------------------------------------------------------
# geoip.db/geosite.db sing-box 1.12+ не читает. Имена наборов берём у
# генератора конфига, чтобы список был в одном месте. Файл кладём через mv:
# sing-box следит за локальными наборами и перечитывает их сам, недокачанный
# файл он бы прочитал.
echo "[$HOST_TAG] [2/6] rule-set geoip/geosite"
RS_DIR=/var/lib/sing-box/rule-set
mkdir -p "$RS_DIR"
for _rs in $("$VPN2_BIN" router config --rule-sets); do
  _f="$RS_DIR/${_rs}.srs"
  if [[ -f "$_f" && -z $(find "$_f" -mtime +7 2>/dev/null) ]] \
    && /usr/local/bin/sing-box rule-set decompile --output /dev/null "$_f" >/dev/null 2>&1; then
    continue
  fi
  curl -sSfL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
    -o "$_f.new" "https://raw.githubusercontent.com/SagerNet/sing-${_rs%%-*}/rule-set/${_rs}.srs"
  # HTTP 200 не гарантирует SRS: проверяем до rename, пока живой sing-box
  # ещё читает предыдущий набор. Свежий, но повреждённый кеш тоже заменяем.
  if ! /usr/local/bin/sing-box rule-set decompile --output /dev/null "$_f.new"; then
    rm -f "$_f.new"
    echo "[$HOST_TAG] набор $_rs повреждён — сохраняю предыдущий файл"
    exit 1
  fi
  mv -f "$_f.new" "$_f"
done
rm -f /var/lib/sing-box/geoip.db /var/lib/sing-box/geosite.db
ls -la "$RS_DIR" | sed -n '/\.srs$/p' | HOST_TAG="$HOST_TAG" awk '{print "[" ENVIRON["HOST_TAG"] "]   " $0}'

# Адреса самого entry: клиентам VPN до них открыты только публичные порты,
# панель, API и прочие локальные службы закрыты (правило в генераторе).
# Набор адресов пишется перед каждым стартом sing-box (ExecStartPre ниже), а
# не один раз при deploy: адрес, добавленный на интерфейс или полученный
# заново по DHCP, закрыт клиентам после перезапуска sing-box или ноды.
mkdir -p /etc/sing-box
rm -f /usr/local/sbin/vpn2-entry-addrs
"$VPN2_BIN" router entry-addrs | HOST_TAG="$HOST_TAG" awk '{print "[" ENVIRON["HOST_TAG"] "]   " $0}'

# ----------------------------------------------------------------------------
# 3. Secrets: clash-api на RU + пароли Hysteria2 с выходных нод
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [3/6] secrets"
gen_pwd() { openssl rand -base64 32 | tr -d '\n=' | head -c 32; }
[[ -f /etc/anysda/clash-secret.txt ]] || gen_pwd > /etc/anysda/clash-secret.txt
chmod 600 /etc/anysda/clash-secret.txt
RU_CLASH_SECRET=$(cat /etc/anysda/clash-secret.txt)

FS=/tmp/anysda/foreign-secrets.env
[[ -f "$FS" ]] || { echo "[$HOST_TAG] $FS не найден — оркестратор должен был его загрузить"; exit 1; }
# Не source: значения пришли с экзитов. Берём только TAG_KEY=пароль в
# алфавите gen_pwd из 10-foreign, на остальном стадия падает.
while IFS= read -r _line || [[ -n "$_line" ]]; do
  [[ -z "$_line" ]] && continue
  if [[ ! "$_line" =~ ^([A-Z][A-Z0-9_]*_(DIRECT|WARP|OBFS|MGMT|CLASH))=([A-Za-z0-9+/]{16,128})$ ]]; then
    echo "[$HOST_TAG] в $FS чужая строка (${_line%%=*}) — экзит отдал не пароль, стадию прерываю"
    exit 1
  fi
  printf -v "${BASH_REMATCH[1]}" '%s' "${BASH_REMATCH[3]}"
done < "$FS"

# Проверяем, что для каждой выходной ноды есть все нужные секреты
for _t in $EXIT_TAGS; do
  _T=$(echo "$_t" | tr 'a-z-' 'A-Z_')
  for _kind in DIRECT WARP MGMT OBFS; do
    _key="${_T}_${_kind}"
    [[ -n "${!_key:-}" ]] || {
      echo "[$HOST_TAG] секрет ${_t} ${_kind} не найден в foreign-secrets.env" >&2
      exit 1
    }
  done
done

# ----------------------------------------------------------------------------
# 4. Генерация sing-box конфига через Python-скрипт (поддерживает N нод)
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [4/6] sing-box router config"

WAN_IFACE=$(ip -4 -o route show default | awk '!f {print $5; f=1}')
: "${WAN_IFACE:?}"
# Backward-compat: vpn2 router config читает WG_OUT_IFACE (имя из v1).
export WG_OUT_IFACE="$WAN_IFACE"

export RU_CLASH_SECRET EXIT_TAGS HY2_DIRECT_PORT HY2_WARP_PORT HY2_MGMT_PORT MGMT_IP
export HY2_HOP_ENABLED HY2_HOP_DIRECT HY2_HOP_WARP HY2_HOP_MGMT HY2_HOP_INTERVAL
# YouTube (стадия 19-yt-zapret). `source` env-файла делает переменные
# ЛОКАЛЬНЫМИ для шелла — до python-генератора они без export не доезжают, и
# конфиг молча собирается без YouTube-правил (стадия при этом отрабатывает
# «успешно»). Дефолты — на случай env-файла, сгенерённого старым config2env.py.
export YT_ROUTE="${YT_ROUTE:-off}" YT_QUIC="${YT_QUIC:-block}" YT_MARK="${YT_MARK:-256}"
[[ -n "${YT_DOMAINS:-}" ]] && export YT_DOMAINS

for _t in $EXIT_TAGS; do
  _T=$(echo "$_t" | tr 'a-z-' 'A-Z_')
  export "DOMAIN_${_T}"
  export "${_T}_DIRECT" "${_T}_WARP" "${_T}_MGMT" "${_T}_OBFS"
  # MGMT_IP_{T} задаёт mon-порт локального SOCKS-инбаунда (см. генератор).
  export "MGMT_IP_${_T}"
  export "${_T}_HY2_UP_MBPS" "${_T}_HY2_DOWN_MBPS"
  _key="${_T}_HY2_DIRECT_PORT"
  export "$_key=${!_key:-$HY2_DIRECT_PORT}"
  _key="${_T}_HY2_MGMT_PORT"
  export "$_key=${!_key:-$HY2_MGMT_PORT}"
done

# TLS pinning Hysteria2 outbound: deploy забирает сертификат каждого экзита в
# /tmp/anysda/exit-certs/{tag}.pem — кладём его в /etc/sing-box/exit-certs
# и экспортируем {T}_HY2_CERT_PATH; vpn2 router config подставит
# tls.certificate_path. Если в этот раз сертификат не приехал (экзит пропущен,
# deploy с другой машины), остаётся закреплённый ранее. Без закрепления
# генератор откажет: insecure — только явным exits[].hy2_insecure.
mkdir -p /etc/sing-box/exit-certs
chmod 700 /etc/sing-box/exit-certs
for _t in $EXIT_TAGS; do
  _T=$(echo "$_t" | tr 'a-z-' 'A-Z_')
  export "${_T}_HY2_INSECURE"
  _src="/tmp/anysda/exit-certs/${_t}.pem"
  if [[ -f "$_src" ]]; then
    install -m 600 "$_src" "/etc/sing-box/exit-certs/${_t}.pem"
  fi
  if [[ -f "/etc/sing-box/exit-certs/${_t}.pem" ]]; then
    export "${_T}_HY2_CERT_PATH=/etc/sing-box/exit-certs/${_t}.pem"
  fi
  # Открытая часть Reality экзита: запасной канал VLESS по TCP при обрыве UDP.
  # Строки PUB/SID/SNI/PORT читаем по одной, файл не исполняем.
  _src="/tmp/anysda/exit-certs/${_t}.reality"
  [[ -f "$_src" ]] && install -m 600 "$_src" "/etc/sing-box/exit-certs/${_t}.reality"
  if [[ -f "/etc/sing-box/exit-certs/${_t}.reality" ]]; then
    while IFS='=' read -r _k _v; do
      case "$_k" in
        PUB) export "${_T}_REALITY_PUBKEY=$_v" ;;
        SID) export "${_T}_REALITY_SHORT_ID=$_v" ;;
        SNI) export "${_T}_REALITY_SNI=$_v" ;;
        PORT) export "${_T}_REALITY_PORT=$_v" ;;
      esac
    done < "/etc/sing-box/exit-certs/${_t}.reality"
  fi
done

# Write the BASE config (without manual routes). The manual-routes watcher
# derives /etc/sing-box/config.json = config-base.json + panel rules, so
# config-base.json MUST be refreshed on every run — a stale base silently
# drops inbounds added to vpn2 router config since it was first
# snapshotted (this is how the wg/ovpn TPROXY :7898 inbound got lost).
_cfg_tmp=$(mktemp /etc/sing-box/.config-base.XXXXXX)
if ! VPN_POOLS_FILE="$(dirname "$0")/vpn-pools.json" "$VPN2_BIN" router config > "$_cfg_tmp" \
  || ! /usr/local/bin/sing-box check -c "$_cfg_tmp"; then
  rm -f "$_cfg_tmp"
  echo "[$HOST_TAG] конфиг невалиден — сервис не трогаю"
  exit 1
fi
chmod 600 "$_cfg_tmp"
mv -f "$_cfg_tmp" /etc/sing-box/config-base.json
# config.json (база + ручные маршруты панели) собирается в шаге 6, и sing-box
# перезапускается один раз уже на нём (VPN2-52).

# ----------------------------------------------------------------------------
# 5. systemd service для sing-box
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [5/6] sing-box service"

# Очистка legacy Shadowsocks/Outline — SS выпилен из проекта. Снимаем
# артефакты прошлых деплоев (стадия 27, anysda-iptables --uid-owner outline),
# если они ещё остались на ноде. Идемпотентно: на чистой ноде — no-op.
for _u in anysda-iptables outline-ss-server; do
  if [[ -f "/etc/systemd/system/${_u}.service" ]]; then
    echo "[$HOST_TAG]   снимаю legacy ${_u}.service"
    systemctl disable --now "$_u" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/${_u}.service"
  fi
done
[[ -x /usr/local/sbin/anysda-iptables.sh ]] && /usr/local/sbin/anysda-iptables.sh down >/dev/null 2>&1 || true
rm -f /usr/local/sbin/anysda-iptables.sh /usr/local/bin/outline-ss-server
rm -rf /etc/outline-ss-server
id outline >/dev/null 2>&1 && userdel outline >/dev/null 2>&1 || true

cat > /etc/systemd/system/sing-box.service <<'EOF'
[Unit]
Description=sing-box (anysda-vpn2 RU router)
After=network-online.target nss-lookup.target
Wants=network-online.target
# StartLimitIntervalSec=0 — роутер обязан подниматься ВСЕГДА: убираем лимит
# systemd «5 крашей за 10с → сдаться», иначе при краш-цикле весь VPN ляжет.
StartLimitIntervalSec=0

[Service]
Type=simple
User=root
# Адреса entry на момент старта (набор entry-addrs). «-»: не прочитались -
# sing-box всё равно поднимается на наборе прошлого старта.
ExecStartPre=-/opt/vpn2/venv/bin/vpn2 router entry-addrs
ExecStart=/usr/local/bin/sing-box run -c /etc/sing-box/config.json
# Restart=always (а не on-failure): роутер обязан подниматься после ЛЮБОЙ
# остановки, включая чистый SIGTERM — иначе случайный `systemctl stop`/`kill`
# (или pkill по имени) кладёт маршрутизацию насовсем.
Restart=always
RestartSec=2s
LimitNOFILE=1048576
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW

[Install]
WantedBy=multi-user.target
EOF
entry_unit_resources sing-box
systemctl daemon-reload
systemctl enable sing-box >/dev/null 2>&1

# clash-api слушает 10.99.0.1:9090 — это lo-алиас на самой entry, наружу не
# торчит. Снимаем legacy mesh-правило ufw (mesh снят, служебный трафик ушёл на
# Hysteria2, см. стадию 05).
ufw delete allow proto tcp from 10.99.0.0/24 to any port 9090 >/dev/null 2>&1 || true

# route_localnet — нужен TPROXY WG/OpenVPN (--on-ip 127.0.0.1, стадии 28/29).
cat > /etc/sysctl.d/99-anysda-vpn.conf <<'EOF'
net.ipv4.conf.all.route_localnet=1
EOF
sysctl -p /etc/sysctl.d/99-anysda-vpn.conf >/dev/null

# ----------------------------------------------------------------------------
# 6. Sing-box manual routes: host-side apply script + systemd.path watcher
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [6/6] sing-box manual-routes watcher"

rm -f /usr/local/sbin/anysda-apply-routes.py

cat > /etc/systemd/system/anysda-apply-routes.service << 'EOF'
[Unit]
Description=anysda-vpn2 — apply manual sing-box routes from panel
After=sing-box.service
# Дефолтный лимит (5 стартов за 10с) выбивался одной правкой в панели: она
# пишет manual-routes.json несколько раз подряд, .path триггерит нас на каждую
# запись, и юнит навсегда уходил в failed (start-limit-hit) — правки маршрутов
# переставали применяться молча. Скрипт идемпотентен и при отсутствии изменений
# не трогает sing-box, поэтому лимит здесь не нужен.
StartLimitIntervalSec=0

[Service]
Type=oneshot
# Дебаунс: пока мы спим, повторные срабатывания .path схлопываются systemd'ом
# в одно отложенное задание вместо пачки отдельных запусков.
ExecStartPre=/bin/sleep 2
ExecStart=/opt/vpn2/venv/bin/vpn2 router apply-routes
EOF
entry_unit_resources anysda-apply-routes

cat > /etc/systemd/system/anysda-apply-routes.path << 'EOF'
[Unit]
Description=Watch /etc/anysda/manual-routes.json and client-prefs.json for changes

[Path]
PathModified=/etc/anysda/manual-routes.json
PathModified=/etc/anysda/client-prefs.json
Unit=anysda-apply-routes.service

[Install]
WantedBy=multi-user.target
EOF

# Touch empty file so the path-watcher is happy on first boot
[[ -f /etc/anysda/manual-routes.json ]] || echo '[]' > /etc/anysda/manual-routes.json
[[ -f /etc/anysda/client-prefs.json ]] || echo '{}' > /etc/anysda/client-prefs.json

systemctl daemon-reload
systemctl enable anysda-apply-routes.path >/dev/null 2>&1
systemctl start  anysda-apply-routes.path

# config.json = свежая config-base.json + ручные маршруты панели, затем ОДИН
# рестарт sing-box уже на итоговом конфиге (раньше было два: на голой базе,
# которая на секунды сносила ручные маршруты, и ещё один из apply-routes).
if ! "$VPN2_BIN" router apply-routes --no-restart 2>&1 | HOST_TAG="$HOST_TAG" awk '{print "[" ENVIRON["HOST_TAG"] "]   " $0}'; then
  echo "[$HOST_TAG]   ручные маршруты не применились — сохраняю действующий конфиг, сервис не трогаю"
  exit 1
fi
# Перезапуск рвёт все соединения клиентов, поэтому только если sing-box читает
# уже не то, с чем стартовал: отпечаток бинарника, юнита, конфига, сертификатов
# экзитов и sysctl (буферы сокетов) сверяем с записанным при прошлом старте.
# Наборы ручных маршрутов и geoip/geosite не считаем: локальные наборы
# sing-box перечитывает сам.
SB_FP_FILE=/var/lib/sing-box/applied.sha256
sb_fp=$({ cat /usr/local/bin/sing-box /etc/systemd/system/sing-box.service \
  /etc/sing-box/config.json /etc/sing-box/exit-certs/*.pem \
  /etc/sysctl.d/99-anysda.conf 2>/dev/null || true; } \
  | sha256sum | cut -d' ' -f1)
if systemctl is-active --quiet sing-box && [[ "$(cat "$SB_FP_FILE" 2>/dev/null)" == "$sb_fp" ]]; then
  echo "[$HOST_TAG]   конфиг, сертификаты и бинарник прежние — sing-box не перезапускаю"
else
  systemctl restart sing-box
  echo "$sb_fp" > "$SB_FP_FILE"
  # Ждём, пока поднимется clash-api (по нему verify сразу после стадии), а не
  # слепые 2 с. Упал sing-box - через 10 с статус ниже это покажет.
  for _i in $(seq 1 50); do
    [[ -n "$(ss -Hltn 'sport = :9090')" ]] && break
    sleep 0.2
  done
fi
systemctl status sing-box --no-pager -n 4 | sed -n '1,6p' | HOST_TAG="$HOST_TAG" awk '{print "[" ENVIRON["HOST_TAG"] "]   " $0}'

# ----------------------------------------------------------------------------
# ssh <tag> на экзиты через служебный hy2-туннель (VPN2-38). nc идёт в
# локальный SOCKS-инбаунд mon-{tag} (порт = 10100 + последний октет
# MGMT_IP_{T}, как в vpn2 router config), туннель приводит на 127.0.0.1:22
# экзита. Ключ entry (/root/.ssh/id_ed25519_mgmt) раскладывает 00-bootstrap.
# Адрес у всех экзитов 127.0.0.1, поэтому хост-ключ сверяем по псевдониму
# anysda-<tag>; known_hosts собирается заново с ключей, которые deploy.sh
# снял с экзитов, — переустановленный экзит не упрётся в старый ключ.
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] ssh-конфиг на экзиты через служебный туннель"
mkdir -p /root/.ssh && chmod 700 /root/.ssh
SSH_MGMT_CONF=/root/.ssh/anysda-mgmt.conf
SSH_MGMT_KNOWN=/root/.ssh/known_hosts_mgmt
{
  echo "# anysda-vpn — генерирует infra/scripts/20-ru-router.sh, руками не править"
  for _t in $EXIT_TAGS; do
    _T=$(echo "$_t" | tr 'a-z-' 'A-Z_')
    _var="MGMT_IP_${_T}"
    _ip="${!_var:-}"
    if [[ -z "$_ip" ]]; then
      echo "[$HOST_TAG]   ВНИМАНИЕ: MGMT_IP_${_T} не задан — ssh ${_t} не настроен" >&2
      continue
    fi
    cat <<EOF

Host ${_t}
  HostName 127.0.0.1
  Port 22
  User root
  IdentityFile /root/.ssh/id_ed25519_mgmt
  IdentitiesOnly yes
  HostKeyAlias anysda-${_t}
  UserKnownHostsFile ${SSH_MGMT_KNOWN}
  StrictHostKeyChecking accept-new
  ServerAliveInterval 15
  ProxyCommand nc -X 5 -x 127.0.0.1:$(( 10100 + ${_ip##*.} )) %h %p
EOF
  done
} > "$SSH_MGMT_CONF"
chmod 600 "$SSH_MGMT_CONF"

: > "$SSH_MGMT_KNOWN"
for _t in $EXIT_TAGS; do
  _hk="/tmp/anysda/exit-hostkeys/${_t}.pub"
  if [[ -s "$_hk" ]]; then
    awk -v h="anysda-${_t}" 'NF >= 2 {print h, $1, $2; exit}' "$_hk" >> "$SSH_MGMT_KNOWN"
  fi
done
chmod 600 "$SSH_MGMT_KNOWN"

touch /root/.ssh/config
chmod 600 /root/.ssh/config
if ! grep -qxF "Include ${SSH_MGMT_CONF}" /root/.ssh/config; then
  # Include обязан стоять до первого Host, иначе попадёт внутрь его блока.
  { echo "Include ${SSH_MGMT_CONF}"; cat /root/.ssh/config; } > /root/.ssh/config.new
  mv /root/.ssh/config.new /root/.ssh/config
  chmod 600 /root/.ssh/config
fi
echo "[$HOST_TAG]   хосты: $(awk '$1 == "Host" {printf "%s ", $2}' "$SSH_MGMT_CONF")"

mkdir -p "$STAMP_DIR"
touch "$STAMP_DIR/$STAGE"
