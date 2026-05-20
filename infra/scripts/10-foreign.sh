#!/usr/bin/env bash
# Stage 10 — wgcf + sing-box server on US/SE.
# Provisions:
#   - wgcf: register Cloudflare WARP, generate wg profile
#   - sing-box: install binary, 2 Hysteria2 inbounds (direct + warp)
#                with built-in ACME (HTTP-01 on :80 for TLS cert)
#   - systemd unit
# Orchestrator must push: /etc/anysda/sing-box.json (rendered)
#                         /etc/anysda/clash-secret.txt (rendered)
# Idempotent.

set -euo pipefail

[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}" "${PUB_IP:?}" "${MGMT_IP:?}" "${HY2_DIRECT_PORT:?}" "${HY2_WARP_PORT:?}"

case "$HOST_TAG" in ru) echo "[$HOST_TAG] 10-foreign is foreign-only — skipping"; exit 0;; esac

STAMP_DIR=/var/anysda/.stamps
STAGE='10-foreign'

mkdir -p /etc/anysda /etc/sing-box /var/lib/sing-box /var/lib/sing-box/acme

# ----------------------------------------------------------------------------
# 1. wgcf — register WARP and generate WG profile
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [1/4] wgcf"
WGCF_VER='2.2.27'
if [[ ! -x /usr/local/bin/wgcf ]] || ! /usr/local/bin/wgcf --version 2>&1 | grep -q "$WGCF_VER"; then
  curl -sSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
    -o /usr/local/bin/wgcf "https://github.com/ViRb3/wgcf/releases/download/v${WGCF_VER}/wgcf_${WGCF_VER}_linux_amd64"
  chmod +x /usr/local/bin/wgcf
fi

cd /etc/anysda
WGCF_OK=1
if [[ ! -f wgcf-account.toml ]]; then
  echo "[$HOST_TAG]   регистрирую WARP устройство"
  if ! /usr/local/bin/wgcf register --accept-tos; then
    echo "[$HOST_TAG]   ⚠ wgcf register не прошёл (Cloudflare API недоступен) — WARP outbound будет placeholder"
    WGCF_OK=0
  fi
fi
if [[ "$WGCF_OK" == "1" ]] && [[ -f wgcf-account.toml ]]; then
  if /usr/local/bin/wgcf generate >/dev/null 2>&1; then
    chmod 600 wgcf-account.toml wgcf-profile.conf
  else
    echo "[$HOST_TAG]   ⚠ wgcf generate не прошёл — WARP outbound будет placeholder"
    WGCF_OK=0
  fi
fi

if [[ "$WGCF_OK" == "1" ]] && [[ -f wgcf-profile.conf ]]; then
  WGCF_PRIVKEY=$(awk -F' = ' '/^PrivateKey/{print $2}' /etc/anysda/wgcf-profile.conf | tr -d '\r')
  WGCF_LOCAL_IPV4=$(awk -F' = ' '/^Address/{print $2}' /etc/anysda/wgcf-profile.conf | head -1 | cut -d, -f1 | cut -d/ -f1 | tr -d '\r ')
  WGCF_PEER_PUBKEY=$(awk -F' = ' '/^PublicKey/{print $2}' /etc/anysda/wgcf-profile.conf | tr -d '\r')
  WGCF_PEER_EP=$(awk -F' = ' '/^Endpoint/{print $2}' /etc/anysda/wgcf-profile.conf | tr -d '\r ')
  WGCF_PEER_ENDPOINT_HOST=${WGCF_PEER_EP%:*}
  WGCF_PEER_ENDPOINT_PORT=${WGCF_PEER_EP##*:}
  WGCF_CLIENT_ID=$(awk -F'=' '/^client_id/{gsub(/[" ]/, "", $2); print $2; exit}' /etc/anysda/wgcf-account.toml)
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
SB_VER='1.10.3'
if [[ ! -x /usr/local/bin/sing-box ]] || ! /usr/local/bin/sing-box version 2>&1 | grep -q "$SB_VER"; then
  curl -sSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
    "https://github.com/SagerNet/sing-box/releases/download/v${SB_VER}/sing-box-${SB_VER}-linux-amd64.tar.gz" \
    | tar -xz -C /tmp
  install -m0755 "/tmp/sing-box-${SB_VER}-linux-amd64/sing-box" /usr/local/bin/sing-box
  rm -rf "/tmp/sing-box-${SB_VER}-linux-amd64"
fi
/usr/local/bin/sing-box version | sed "s/^/[$HOST_TAG]   /" | head -1

# ----------------------------------------------------------------------------
# 3. Secrets (Hysteria2 passwords + obfs + clash-api)
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [3/4] secrets"
gen_pwd() { openssl rand -base64 32 | tr -d '\n=' | head -c 32; }
[[ -f /etc/anysda/hy2-direct.pwd ]]  || gen_pwd > /etc/anysda/hy2-direct.pwd
[[ -f /etc/anysda/hy2-warp.pwd ]]    || gen_pwd > /etc/anysda/hy2-warp.pwd
[[ -f /etc/anysda/hy2-obfs.pwd ]]    || gen_pwd > /etc/anysda/hy2-obfs.pwd
[[ -f /etc/anysda/clash-secret.txt ]] || gen_pwd > /etc/anysda/clash-secret.txt
chmod 600 /etc/anysda/hy2-*.pwd /etc/anysda/clash-secret.txt

HY2_PWD_DIRECT=$(cat /etc/anysda/hy2-direct.pwd)
HY2_PWD_WARP=$(cat /etc/anysda/hy2-warp.pwd)
HY2_OBFS_PWD=$(cat /etc/anysda/hy2-obfs.pwd)
CLASH_SECRET=$(cat /etc/anysda/clash-secret.txt)

# Note: these passwords MUST be communicated back to the RU router (stage 20).
# We expose them via the mgmt mesh — see deploy.sh post-step that fetches them.

# ----------------------------------------------------------------------------
# 4. TLS cert + sing-box config + systemd service
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [4/4] TLS + sing-box config + service"

export MGMT_IP \
       HY2_DIRECT_PORT HY2_WARP_PORT \
       HY2_PWD_DIRECT HY2_PWD_WARP HY2_OBFS_PWD CLASH_SECRET \
       WGCF_PRIVKEY WGCF_LOCAL_IPV4 WGCF_PEER_PUBKEY \
       WGCF_PEER_ENDPOINT_HOST WGCF_PEER_ENDPOINT_PORT \
       WGCF_RESERVED_JSON

# Self-signed TLS cert (SAN = server IP, 10 лет)
echo "[$HOST_TAG]   генерирую self-signed TLS cert для $PUB_IP"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 \
  -keyout /etc/sing-box/tls.key \
  -out    /etc/sing-box/tls.crt \
  -days   3650 -nodes \
  -subj   "/CN=${PUB_IP}" \
  -addext "subjectAltName=IP:${PUB_IP}" \
  2>/dev/null
chmod 600 /etc/sing-box/tls.key /etc/sing-box/tls.crt
echo "[$HOST_TAG]   cert SAN: IP:${PUB_IP}"

TPL=/tmp/anysda/sing-box-server.json.tpl
[[ -f "$TPL" ]] || { echo "[$HOST_TAG] $TPL не найден"; exit 1; }
envsubst < "$TPL" > /etc/sing-box/config.json
chmod 600 /etc/sing-box/config.json

# Validate
if ! /usr/local/bin/sing-box check -c /etc/sing-box/config.json; then
  echo "[$HOST_TAG] конфиг sing-box невалиден — сервис не трогаю"
  exit 1
fi

cat > /etc/systemd/system/sing-box.service <<'EOF'
[Unit]
Description=sing-box (anysda-vpn foreign exit)
After=network-online.target nss-lookup.target
Wants=network-online.target

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
systemctl daemon-reload
systemctl enable sing-box >/dev/null 2>&1
systemctl restart sing-box
sleep 2

# UFW: Hysteria2 порты (00-bootstrap открывает дефолтные; здесь — для per-exit
# override) + clash-api на 9090 в mgmt-сеть.
ufw allow "${HY2_DIRECT_PORT}/udp" comment 'hysteria2 direct' >/dev/null 2>&1 || true
ufw allow "${HY2_WARP_PORT}/udp"   comment 'hysteria2 warp'   >/dev/null 2>&1 || true
ufw allow proto tcp from 10.99.0.0/24 to any port 9090 comment 'sing-box clash-api mesh' >/dev/null 2>&1 || true
ufw reload >/dev/null

# Verify
echo "[$HOST_TAG] статус sing-box:"
systemctl status sing-box --no-pager -n 5 | head -10 | sed "s/^/[$HOST_TAG]   /"
echo "[$HOST_TAG] слушаемые UDP порты:"
ss -lun | awk -v p="$HY2_DIRECT_PORT|$HY2_WARP_PORT" '$5 ~ ":(" p ")$"' | sed "s/^/[$HOST_TAG]   /"

mkdir -p "$STAMP_DIR"
touch "$STAMP_DIR/$STAGE"
echo "[$HOST_TAG] $STAGE done"
