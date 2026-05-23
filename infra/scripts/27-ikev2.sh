#!/usr/bin/env bash
# Stage 27 — IKEv2/IPsec server on RU (strongSwan + swanctl).
#
# Аутентификация: сервер — pubkey (свой CA + server-cert на $ENTRY_HOST в SAN),
# клиент — EAP-MSCHAPv2 (логин+пароль). Креды устройств живут в БД панели и
# раскатываются панелью в /etc/swanctl/conf.d/anysda.conf на следующих этапах
# (этап 4 — render из БД). Тут — серверный PKI + базовый swanctl conn без
# клиентов.
#
# TPROXY-зацеп туннельного интерфейса (xfrm0) → sing-box :7898 будет на
# отдельной стадии-наследнике / расширении этого скрипта (этап 3 IKEv2-фичи).
#
# Идемпотентно: CA генерится один раз (НЕ ПЕРЕТИРАЕТСЯ — выдачи бы умерли).
# Server-cert ПЕРЕВЫПУСКАЕТСЯ если CN не равен текущему $ENTRY_HOST (смена IP
# entry — клиентам надо просто сменить «Server» в профиле; логин/пароль живут).

set -euo pipefail
[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}"

if [[ "$HOST_TAG" != "ru" ]]; then
  echo "[$HOST_TAG] stage 27-ikev2: skipped (only RU)"
  exit 0
fi

: "${ENTRY_HOST:?ENTRY_HOST not set (envs/all.env)}"
IKEV2_SUBNET="${IKEV2_SUBNET:-10.68.68.0/24}"
IKEV2_DNS="${IKEV2_DNS:-10.99.0.1}"
PKI=/etc/strongswan/pki
CONF=/etc/swanctl/conf.d/anysda.conf

mkdir -p /var/anysda/.stamps "$PKI" /etc/swanctl/conf.d

# ── 1. apt strongswan + плагины ─────────────────────────────────────────────
echo "[$HOST_TAG] [1/6] strongswan apt"
if ! command -v swanctl >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq \
    strongswan strongswan-pki strongswan-swanctl \
    libcharon-extra-plugins libstrongswan-extra-plugins >/dev/null
fi

# ── 2. CA (один раз — не перетирать!) ────────────────────────────────────────
echo "[$HOST_TAG] [2/6] CA"
if [[ ! -f "$PKI/ca.key" ]]; then
  echo "[$HOST_TAG]   generating CA (ECDSA P-256, 10y)"
  pki --gen --type ecdsa --size 256 --outform pem > "$PKI/ca.key"
  pki --self --in "$PKI/ca.key" --type ecdsa \
      --dn "CN=anysda-vpn2 IKEv2 CA" --ca \
      --lifetime 3650 --outform pem > "$PKI/ca.crt"
fi
chmod 600 "$PKI/ca.key"
chmod 644 "$PKI/ca.crt"

# ── 3. Server cert (CN/SAN = $ENTRY_HOST, перевыпуск при смене IP) ──────────
echo "[$HOST_TAG] [3/6] server cert (CN=$ENTRY_HOST)"
NEED_SERVER=0
if [[ ! -f "$PKI/server.crt" ]]; then
  NEED_SERVER=1
else
  # Сверяем SAN текущего cert с $ENTRY_HOST. Если не совпадает — перевыпустить.
  if ! openssl x509 -in "$PKI/server.crt" -noout -ext subjectAltName 2>/dev/null \
        | grep -qE "IP Address:${ENTRY_HOST}( |$|,)"; then
    echo "[$HOST_TAG]   SAN не содержит $ENTRY_HOST — перевыпуск server cert"
    NEED_SERVER=1
  fi
fi

if [[ "$NEED_SERVER" == "1" ]]; then
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT
  pki --gen --type ecdsa --size 256 --outform pem > "$TMP/server.key"
  pki --pub --in "$TMP/server.key" --type ecdsa --outform pem > "$TMP/server.pub"
  pki --issue --cacert "$PKI/ca.crt" --cakey "$PKI/ca.key" \
      --in "$TMP/server.pub" --type pub \
      --dn "CN=$ENTRY_HOST" \
      --san "$ENTRY_HOST" \
      --flag serverAuth --flag ikeIntermediate \
      --lifetime 1825 \
      --outform pem > "$TMP/server.crt"
  install -m 0600 "$TMP/server.key" "$PKI/server.key"
  install -m 0644 "$TMP/server.crt" "$PKI/server.crt"
  rm -rf "$TMP"
  trap - EXIT
fi

# Раскладываем по местам, которые swanctl читает по умолчанию.
install -d -m 700 /etc/swanctl/x509ca /etc/swanctl/x509 /etc/swanctl/private
install -m 644 "$PKI/ca.crt"      /etc/swanctl/x509ca/anysda-ca.crt
install -m 644 "$PKI/server.crt"  /etc/swanctl/x509/anysda-server.crt
install -m 600 "$PKI/server.key"  /etc/swanctl/private/anysda-server.key

# ── 4. swanctl conf — базовый conn без клиентов (этап 4 наполнит динамику) ──
echo "[$HOST_TAG] [4/6] swanctl conf"
cat > "$CONF" <<EOF
# Managed by stage 27-ikev2 — НЕ редактировать вручную.
# Динамический conf с клиент-кредами раскатывает панель (server/utils/ikev2.ts)
# в /etc/swanctl/conf.d/anysda-clients.conf (этап 4); этот файл — только conn
# и pool.

connections {
  anysda-ikev2 {
    version       = 2
    proposals     = aes256gcm16-prfsha384-ecp384
    dpd_delay     = 30s
    pools         = anysda-ikev2-pool
    fragmentation = yes
    encap         = yes
    rekey_time    = 0s

    local-server {
      auth  = pubkey
      certs = anysda-server.crt
      id    = $ENTRY_HOST
    }
    remote-client {
      auth   = eap-mschapv2
      eap_id = %any
    }
    children {
      net {
        local_ts      = 0.0.0.0/0
        esp_proposals = aes256gcm16-ecp384
        rekey_time    = 0s
        # mark будем включать на этапе 3 (xfrm0 + TPROXY).
      }
    }
  }
}

pools {
  anysda-ikev2-pool {
    addrs = $IKEV2_SUBNET
    dns   = $IKEV2_DNS
  }
}

# secrets {} — раскатывает панель (этап 4) в conf.d/anysda-clients.conf.
EOF
chmod 644 "$CONF"

# ── 5. ufw 500 + 4500 ──────────────────────────────────────────────────────
echo "[$HOST_TAG] [5/6] ufw 500/udp + 4500/udp"
ufw allow 500/udp  comment 'ikev2 IKE'    >/dev/null 2>&1 || true
ufw allow 4500/udp comment 'ikev2 NAT-T'  >/dev/null 2>&1 || true
ufw reload >/dev/null 2>&1 || true

sysctl -w net.ipv4.ip_forward=1 >/dev/null
# Loose RPF: policy-routing IPsec иначе режется reverse-path filter.
sysctl -w net.ipv4.conf.all.rp_filter=2 >/dev/null
sysctl -w net.ipv4.conf.default.rp_filter=2 >/dev/null

# ── 6. systemd: strongswan ──────────────────────────────────────────────────
echo "[$HOST_TAG] [6/6] strongswan service"
systemctl enable strongswan >/dev/null 2>&1 || true
systemctl restart strongswan
sleep 2
systemctl status strongswan --no-pager -n 4 2>/dev/null | head -6 | sed "s/^/[$HOST_TAG]   /"

# Применяем CA + creds + conns в работающий daemon.
swanctl --load-creds >/dev/null
swanctl --load-pools >/dev/null
swanctl --load-conns >/dev/null

# Sanity: conn anysda-ikev2 в списке, порты слушают.
echo "[$HOST_TAG]   conns:"
swanctl --list-conns 2>/dev/null | grep -E '^[a-z]|local|remote|child' | head -10 | sed "s/^/[$HOST_TAG]     /"
echo "[$HOST_TAG]   listening:"
ss -lun 2>/dev/null | awk '/:500 |:4500 /{print "    " $0}' | sed "s/^/[$HOST_TAG]/"

touch /var/anysda/.stamps/27-ikev2
echo "[$HOST_TAG] 27-ikev2 done — strongSwan up, server-cert CN=$ENTRY_HOST"
