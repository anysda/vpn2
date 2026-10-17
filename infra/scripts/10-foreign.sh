#!/usr/bin/env bash
# Stage 10 — wgcf + sing-box server on US/SE.
# Provisions:
#   - wgcf: register Cloudflare WARP, generate wg profile
#   - sing-box: install binary, 2 Hysteria2 inbounds (direct + warp)
#                с самоподписанным TLS-сертификатом (entry пинит его)
#   - systemd unit
# Orchestrator must push: /etc/anysda/sing-box.json (rendered)
#                         /etc/anysda/clash-secret.txt (rendered)
# Idempotent.

set -euo pipefail

[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}" "${PUB_IP:?}" "${HY2_DIRECT_PORT:?}" "${HY2_WARP_PORT:?}" "${HY2_MGMT_PORT:?}"

case "$HOST_TAG" in ru) echo "[$HOST_TAG] 10-foreign is foreign-only — skipping"; exit 0;; esac

SVC_LIB=$(dirname "$0")/anysda-svc.sh
[[ -f "$SVC_LIB" ]] || SVC_LIB=$(dirname "$0")/../lib/anysda-svc.sh
# shellcheck source=infra/lib/anysda-svc.sh
source "$SVC_LIB"

STAMP_DIR=/var/anysda/.stamps
STAGE='10-foreign'

mkdir -p /etc/anysda /etc/sing-box /var/lib/sing-box /var/lib/sing-box/acme

# ----------------------------------------------------------------------------
# 1. wgcf — register WARP and generate WG profile
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [1/4] wgcf"
WGCF_VER='2.3.0'
# Версию wgcf не печатает (ни `version`, ни `--version`), сверяем по sha256 релиза.
WGCF_SHA256='01614e38c0eb5f3405232e71cfaf02d64d4809e4988ad8f5a8071af16d193405'
if [[ "$(sha256sum /usr/local/bin/wgcf 2>/dev/null | cut -d' ' -f1)" != "$WGCF_SHA256" ]]; then
  curl -sSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
    -o /usr/local/bin/wgcf "https://github.com/ViRb3/wgcf/releases/download/v${WGCF_VER}/wgcf_${WGCF_VER}_linux_amd64"
  chmod +x /usr/local/bin/wgcf
fi

cd /etc/anysda
# Возвращает 2 только для аккаунта от wgcf < 2.3: API тогда не отдавал
# key_type, без которого текущий wgcf не может построить профиль.
generate_wgcf_profile() {
  local generate_output
  if generate_output=$(/usr/local/bin/wgcf generate 2>&1); then
    return 0
  fi
  [[ "$generate_output" == *key_type* ]] && return 2
  return 1
}

# Вывод register несёт Id аккаунта и устройства WARP: в лог деплоя его не
# пишем, при сбое печатаем только хвост с причиной.
register_wgcf() {
  local out
  if out=$(timeout 15 /usr/local/bin/wgcf register --accept-tos 2>&1); then
    return 0
  fi
  printf '%s\n' "$out" | tail -n 3
  return 1
}

WGCF_OK=1
if [[ ! -f wgcf-account.toml ]]; then
  echo "[$HOST_TAG]   регистрирую WARP устройство"
  # Без таймаута register при фильтрации Cloudflare висит ~145 с на TCP connect.
  if ! register_wgcf; then
    echo "[$HOST_TAG]   ⚠ wgcf register не прошёл (Cloudflare API недоступен) — WARP outbound будет placeholder"
    WGCF_OK=0
  fi
fi
if [[ "$WGCF_OK" == "1" ]] && [[ -f wgcf-account.toml ]]; then
  if generate_wgcf_profile; then
    chmod 600 wgcf-account.toml wgcf-profile.conf
  else
    generate_status=$?
    if [[ "$generate_status" == "2" ]]; then
      # Аккаунт без key_type несовместим с wgcf 2.3. Профиль от него больше не
      # оставляем навсегда: создаём устройство нового формата и заменяем ключ
      # WARP в конфиге sing-box свежим профилем.
      mv -f wgcf-account.toml wgcf-account.toml.old
      echo "[$HOST_TAG]   регистрирую WARP устройство заново"
      if register_wgcf; then
        if generate_wgcf_profile; then
          chmod 600 wgcf-account.toml wgcf-profile.conf
        else
          echo "[$HOST_TAG]   ⚠ wgcf generate не прошёл после регистрации — WARP outbound будет placeholder"
          WGCF_OK=0
        fi
      elif [[ -s wgcf-profile.conf ]]; then
        # Cloudflare недоступен при замене устаревшего устройства: только в
        # этом случае безопасно сохранить рабочий профиль прошлого прогона.
        # Аккаунт возвращаем на место, иначе следующий прогон пойдёт по ветке
        # первой регистрации и при новом сбое потеряет рабочий профиль.
        mv -f wgcf-account.toml.old wgcf-account.toml
        echo "[$HOST_TAG]   ⚠ wgcf register не прошёл (Cloudflare API недоступен) — оставляю профиль прошлого прогона"
      else
        echo "[$HOST_TAG]   ⚠ wgcf register не прошёл (Cloudflare API недоступен) — WARP outbound будет placeholder"
        WGCF_OK=0
      fi
    else
      echo "[$HOST_TAG]   ⚠ wgcf generate не прошёл — WARP outbound будет placeholder"
      WGCF_OK=0
    fi
  fi
fi

if [[ "$WGCF_OK" == "1" ]] && [[ -f wgcf-profile.conf ]]; then
  WGCF_PRIVKEY=$(awk -F' = ' '/^PrivateKey/{print $2}' /etc/anysda/wgcf-profile.conf | tr -d '\r')
  WGCF_LOCAL_IPV4=$(awk -F' = ' '/^Address/{print $2; exit}' /etc/anysda/wgcf-profile.conf | cut -d, -f1 | cut -d/ -f1 | tr -d '\r ')
  WGCF_PEER_PUBKEY=$(awk -F' = ' '/^PublicKey/{print $2}' /etc/anysda/wgcf-profile.conf | tr -d '\r')
  WGCF_PEER_EP=$(awk -F' = ' '/^Endpoint/{print $2}' /etc/anysda/wgcf-profile.conf | tr -d '\r ')
  WGCF_PEER_ENDPOINT_HOST=${WGCF_PEER_EP%:*}
  WGCF_PEER_ENDPOINT_PORT=${WGCF_PEER_EP##*:}
  # reserved = client_id устройства WARP (3 байта), как у официального клиента.
  # wgcf его не сохраняет: берём из API регистрации (тот же адрес и токен, что
  # у wgcf) и кладём рядом с аккаунтом вместе с device_id. Новый аккаунт -
  # новый запрос. API недоступен - reserved нулевой, следующий прогон повторит.
  WGCF_DEVICE_ID=$(awk '/^device_id/{sub(/^[^=]*=/, ""); gsub(/["'\'' ]/, ""); print; exit}' wgcf-account.toml)
  WGCF_CID_FILE=/etc/anysda/wgcf-client-id
  WGCF_CLIENT_ID=$(awk -v d="$WGCF_DEVICE_ID" '$1 == d {print $2}' "$WGCF_CID_FILE" 2>/dev/null || true)
  if [[ -z "$WGCF_CLIENT_ID" ]]; then
    WGCF_CLIENT_ID=$(awk '/^access_token/{sub(/^[^=]*=/, ""); gsub(/["'\'' ]/, ""); printf "Authorization: Bearer %s\n", $0; exit}' wgcf-account.toml \
      | curl -fsS --max-time 15 -H @- "https://api.cloudflareclient.com/v0a5641/reg/${WGCF_DEVICE_ID}" \
      | python3 -c 'import json, sys; print(json.load(sys.stdin)["config"]["client_id"])' 2>/dev/null) || WGCF_CLIENT_ID=''
    if [[ -n "$WGCF_CLIENT_ID" ]]; then
      echo "$WGCF_DEVICE_ID $WGCF_CLIENT_ID" > "$WGCF_CID_FILE"
      chmod 600 "$WGCF_CID_FILE"
    else
      echo "[$HOST_TAG]   ⚠ client_id WARP из API не получен — reserved нулевой до следующего прогона"
    fi
  fi
  WGCF_RESERVED_JSON=''
  if [[ -n "$WGCF_CLIENT_ID" ]]; then
    WGCF_RESERVED_JSON=$(echo "$WGCF_CLIENT_ID" | base64 -d 2>/dev/null \
      | od -An -tu1 -N3 \
      | awk 'NF==3{printf "[%d,%d,%d]", $1,$2,$3}')
  fi
  WGCF_RESERVED_JSON="${WGCF_RESERVED_JSON:-[0,0,0]}"
  echo "[$HOST_TAG]   wgcf: local=${WGCF_LOCAL_IPV4}, peer=${WGCF_PEER_ENDPOINT_HOST}:${WGCF_PEER_ENDPOINT_PORT}, reserved=${WGCF_RESERVED_JSON}"
else
  # Placeholder values so envsubst doesn't barf. WARP outbound будет нерабочим
  # на этой ноде, но sing-box стартует и hy2-direct работает нормально.
  WGCF_PRIVKEY='wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA0='
  WGCF_LOCAL_IPV4='10.111.111.1'
  WGCF_PEER_PUBKEY='wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA0='
  WGCF_PEER_ENDPOINT_HOST='127.0.0.1'
  WGCF_PEER_ENDPOINT_PORT='2408'
  WGCF_RESERVED_JSON='[0,0,0]'
  echo "[$HOST_TAG]   wgcf: placeholder (WARP outbound disabled on this node)"
fi

# ----------------------------------------------------------------------------
# 2. sing-box install
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [2/4] sing-box"
bash /tmp/anysda/install-sing-box.sh
/usr/local/bin/sing-box version | sed -n '1p' | HOST_TAG="$HOST_TAG" awk '{print "[" ENVIRON["HOST_TAG"] "]   " $0}'

# ----------------------------------------------------------------------------
# 3. Secrets (Hysteria2 passwords + obfs + clash-api)
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [3/4] secrets"
gen_pwd() { openssl rand -base64 32 | tr -d '\n=' | head -c 32; }
[[ -f /etc/anysda/hy2-direct.pwd ]]  || gen_pwd > /etc/anysda/hy2-direct.pwd
[[ -f /etc/anysda/hy2-warp.pwd ]]    || gen_pwd > /etc/anysda/hy2-warp.pwd
[[ -f /etc/anysda/hy2-obfs.pwd ]]    || gen_pwd > /etc/anysda/hy2-obfs.pwd
# hy2-mgmt.pwd — пароль ОТДЕЛЬНОГО служебного Hysteria2-инбаунда (мониторинг).
# Отдельный от клиентских: ротация hy2-direct/warp его не трогает, и по
# клиентским кредам метрики не достать.
[[ -f /etc/anysda/hy2-mgmt.pwd ]]    || gen_pwd > /etc/anysda/hy2-mgmt.pwd
[[ -f /etc/anysda/clash-secret.txt ]] || gen_pwd > /etc/anysda/clash-secret.txt
chmod 600 /etc/anysda/hy2-*.pwd /etc/anysda/clash-secret.txt

HY2_PWD_DIRECT=$(cat /etc/anysda/hy2-direct.pwd)
HY2_PWD_WARP=$(cat /etc/anysda/hy2-warp.pwd)
HY2_OBFS_PWD=$(cat /etc/anysda/hy2-obfs.pwd)
HY2_PWD_MGMT=$(cat /etc/anysda/hy2-mgmt.pwd)
CLASH_SECRET=$(cat /etc/anysda/clash-secret.txt)

# Note: these passwords MUST be communicated back to the RU router (stage 20).
# prep_ru_router (deploy.sh) забирает их по SSH при подготовке стадии 20.

# ----------------------------------------------------------------------------
# 4. TLS cert + sing-box config + systemd service
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [4/4] TLS + sing-box config + service"

export HY2_DIRECT_PORT HY2_WARP_PORT HY2_MGMT_PORT \
       HY2_PWD_DIRECT HY2_PWD_WARP HY2_PWD_MGMT HY2_OBFS_PWD CLASH_SECRET \
       WGCF_PRIVKEY WGCF_LOCAL_IPV4 WGCF_PEER_PUBKEY \
       WGCF_PEER_ENDPOINT_HOST WGCF_PEER_ENDPOINT_PORT \
       WGCF_RESERVED_JSON

# Self-signed TLS cert (SAN = server IP, 10 лет). entry пинит именно этот
# сертификат, поэтому перевыпуск на каждом прогоне ронял туннели до следующего
# 20-ru-router (VPN2-40). Перевыпускаем, только если серта нет, ключ к нему
# не подходит, SAN не тот IP или до конца срока меньше 30 дней.
cert_ok() {
  local crt=/etc/sing-box/tls.crt key=/etc/sing-box/tls.key
  [[ -s "$crt" && -s "$key" ]] || return 1
  openssl x509 -in "$crt" -noout -checkend $((30*86400)) >/dev/null 2>&1 || return 1
  # Без `| grep -q`: под pipefail ранний выход grep даёт SIGPIPE (141), и серт
  # перевыпускался бы через раз.
  local san; san=$(openssl x509 -in "$crt" -noout -ext subjectAltName 2>/dev/null) || return 1
  [[ "$san" =~ IP\ Address:${PUB_IP//./\\.}(,|$'\n'|$) ]] || return 1
  [[ "$(openssl x509 -in "$crt" -noout -pubkey 2>/dev/null)" == "$(openssl pkey -in "$key" -pubout 2>/dev/null)" ]]
}
if cert_ok; then
  echo "[$HOST_TAG]   TLS cert для $PUB_IP на месте — оставляю (entry его пинит)"
else
  echo "[$HOST_TAG]   генерирую self-signed TLS cert для $PUB_IP"
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 \
    -keyout /etc/sing-box/tls.key \
    -out    /etc/sing-box/tls.crt \
    -days   3650 -nodes \
    -subj   "/CN=${PUB_IP}" \
    -addext "subjectAltName=IP:${PUB_IP}" \
    2>/dev/null
fi
chmod 600 /etc/sing-box/tls.key /etc/sing-box/tls.crt
echo "[$HOST_TAG]   cert SAN: IP:${PUB_IP}"

# Публикуем self-signed cert (только публичный PEM, без приватного ключа) —
# prep_ru_router забирает его и кладёт в /etc/sing-box/exit-certs/{tag}.pem
# на RU. gen-router-config.py подставляет его как `tls.certificate` в Hysteria2
# outbound — тогда RU-роутер принимает только этот конкретный cert и MITM на
# RU→exit невозможен.
cp /etc/sing-box/tls.crt /etc/anysda/hy2-tls.crt
chmod 644 /etc/anysda/hy2-tls.crt
echo "[$HOST_TAG]   tls fingerprint: $(openssl x509 -in /etc/anysda/hy2-tls.crt -noout -fingerprint -sha256)"

TPL=/tmp/anysda/sing-box-server.json.tpl
[[ -f "$TPL" ]] || { echo "[$HOST_TAG] $TPL не найден"; exit 1; }
# Пустая пара сохраняет BBR. Настройка не затрагивает служебный hy2-mgmt.
export HY2_UP_MBPS="${HY2_UP_MBPS:-}" HY2_DOWN_MBPS="${HY2_DOWN_MBPS:-}"
HY2_BANDWIDTH_FIELDS=$(python3 /tmp/anysda/hy2_bandwidth.py)
export HY2_BANDWIDTH_FIELDS
# Проверяем приватный кандидат; неудачный рендер/проверка сохраняет конфиг,
# с которым сервис сможет снова стартовать после сбоя или перезагрузки.
SB_CONFIG_TMP=$(mktemp /etc/sing-box/.config.json.XXXXXX)
trap 'rm -f -- "$SB_CONFIG_TMP"' EXIT
envsubst < "$TPL" > "$SB_CONFIG_TMP"

# Validate
if ! /usr/local/bin/sing-box check -c "$SB_CONFIG_TMP"; then
  echo "[$HOST_TAG] конфиг sing-box невалиден — сервис не трогаю"
  exit 1
fi
mv -f -- "$SB_CONFIG_TMP" /etc/sing-box/config.json
trap - EXIT

cat > /etc/systemd/system/sing-box.service <<'EOF'
[Unit]
Description=sing-box (anysda-vpn foreign exit)
After=network-online.target nss-lookup.target
Wants=network-online.target
# StartLimitIntervalSec=0 — НИКОГДА не сдаваться: systemd по умолчанию
# перестаёт рестартить после 5 крашей за 10с. Для экзита это смерти подобно —
# отключаем лимит, sing-box рестартится всегда (RestartSec=5s пейсит).
StartLimitIntervalSec=0

[Service]
Type=simple
User=root
ExecStart=/usr/local/bin/sing-box run -c /etc/sing-box/config.json
# Restart=always — exit поднимается после любой остановки, не только краша.
Restart=always
RestartSec=5s
LimitNOFILE=1048576
# Need NET_ADMIN to manage WireGuard userspace outbound + bind low ports
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW

[Install]
WantedBy=multi-user.target
EOF
# Общий бюджет для таймера и ожидания WARP при деплое. Состояние не удаляем
# даже при отключении WARP: повторная установка не должна обнулять лимиты.
WARP_WD_SRC=/tmp/anysda/warp-watchdog.py
[[ -f "$WARP_WD_SRC" ]] || WARP_WD_SRC=$(dirname "$0")/../lib/warp-watchdog.py
install -m755 "$WARP_WD_SRC" /usr/local/bin/anysda-warp-watchdog
WARP_WD_ENABLED=0
if /usr/local/bin/anysda-warp-watchdog --enabled; then
  WARP_WD_ENABLED=1
  cat > /etc/systemd/system/anysda-warp-watchdog.service <<'EOF'
[Unit]
Description=Recover stalled WARP on the sing-box exit
After=network-online.target sing-box.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/anysda-warp-watchdog
TimeoutStartSec=45
StateDirectory=anysda-warp-watchdog
StateDirectoryMode=0700
UMask=0077
StandardOutput=journal
StandardError=journal
SyslogIdentifier=anysda-warp-watchdog
EOF
  cat > /etc/systemd/system/anysda-warp-watchdog.timer <<'EOF'
[Unit]
Description=Check exit WARP every 30 seconds

[Timer]
OnBootSec=30
OnUnitInactiveSec=30
AccuracySec=1
Unit=anysda-warp-watchdog.service

[Install]
WantedBy=timers.target
EOF
else
  systemctl disable --now anysda-warp-watchdog.timer >/dev/null 2>&1 || true
  systemctl stop anysda-warp-watchdog.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/anysda-warp-watchdog.timer \
    /etc/systemd/system/anysda-warp-watchdog.service /usr/local/bin/anysda-warp-watchdog
fi

systemctl daemon-reload
systemctl enable sing-box >/dev/null 2>&1
# Перезапуск рвёт все туннели entry→экзит, у клиентов это секунды без сети.
# Поэтому только если sing-box читает уже не то, с чем стартовал: отпечаток
# бинарника, юнита, конфига, TLS и sysctl (буферы сокетов) сверяем с
# записанным при прошлом старте.
SB_FP_FILE=/var/lib/sing-box/applied.sha256
sb_fp=$({ cat /usr/local/bin/sing-box /etc/systemd/system/sing-box.service \
  /etc/sing-box/config.json /etc/sing-box/tls.crt /etc/sing-box/tls.key \
  /etc/sysctl.d/99-anysda.conf 2>/dev/null || true; } \
  | sha256sum | cut -d' ' -f1)
if systemctl is-active --quiet sing-box && [[ "$(cat "$SB_FP_FILE" 2>/dev/null)" == "$sb_fp" ]]; then
  echo "[$HOST_TAG]   конфиг, сертификат и бинарник прежние — sing-box не перезапускаю"
else
  systemctl restart sing-box
  echo "$sb_fp" > "$SB_FP_FILE"
  sleep 2
fi

# Даже при неизменном конфиге проверяем WARP. Bootstrap и timer используют
# один flock и бюджет: максимум две дополнительные попытки за этот прогон,
# не более трёх за скользящий час; окно ожидания 9 минут + текущая проба.
if [[ "$WARP_WD_ENABLED" == 1 ]]; then
  if ! /usr/local/bin/anysda-warp-watchdog --bootstrap; then
    echo "[$HOST_TAG] WARNING: проверка WARP не удалась; экзит продолжает работу"
  fi
  systemctl enable --now anysda-warp-watchdog.timer >/dev/null
fi

# UFW: Hysteria2 порты (00-bootstrap открывает дефолтные; здесь — для per-exit
# override). Служебный hy2-mgmt-in (мониторинг) — свой UDP-порт. clash-api
# ушёл на 127.0.0.1 (mgmt-mesh снят), наружу его больше не открываем.
ufw_sync_rules 'hysteria2 direct' "${HY2_DIRECT_PORT}/udp"
ufw_sync_rules 'hysteria2 warp' "${HY2_WARP_PORT}/udp"
ufw_sync_rules --alias 'hysteria2 mgmt (monitoring)' 'hysteria2 mgmt' "${HY2_MGMT_PORT}/udp"
ufw delete allow proto tcp from 10.99.0.0/24 to any port 9090 >/dev/null 2>&1 || true

# Verify
echo "[$HOST_TAG] статус sing-box:"
systemctl status sing-box --no-pager -n 5 | sed -n '1,10p' | HOST_TAG="$HOST_TAG" awk '{print "[" ENVIRON["HOST_TAG"] "]   " $0}'
echo "[$HOST_TAG] слушаемые UDP порты:"
ss -lun | awk -v p="$HY2_DIRECT_PORT|$HY2_WARP_PORT|$HY2_MGMT_PORT" '$5 ~ ":(" p ")$"' | HOST_TAG="$HOST_TAG" awk '{print "[" ENVIRON["HOST_TAG"] "]   " $0}'

mkdir -p "$STAMP_DIR"
touch "$STAMP_DIR/$STAGE"
echo "[$HOST_TAG] $STAGE done"
