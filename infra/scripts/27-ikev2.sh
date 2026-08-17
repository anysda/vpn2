#!/usr/bin/env bash
# Stage 27 — IKEv2/IPsec server on RU (strongSwan + swanctl).
#
# Аутентификация: сервер — pubkey, клиент — EAP-MSCHAPv2 (логин+пароль).
# Креды устройств живут в БД панели и раскатываются панелью в
# /etc/swanctl/conf.d/anysda-clients.conf через server/utils/ikev2.ts.
#
# Server-cert — ДВА режима:
#   1) letsencrypt: PANEL_DOMAIN задан И Caddy уже выдал LE-cert на него.
#      Берём cert/key из /var/lib/caddy/.local/share/caddy/certificates/...
#      CA клиенту не нужен (LE-корень в trust-store iOS/macOS/Win/Android).
#      systemd path-watcher на Caddy-cert: ротация Caddy → re-copy + reload.
#   2) self-signed: иначе. Свой ECDSA CA + server-cert на $ENTRY_HOST.
#      Клиенту нужно скачать CA и доверить вручную.
#
# Режим записывается в /etc/anysda/ikev2-mode (letsencrypt|self-signed) и
# /etc/anysda/ikev2-server-host (домен или IP) — панель читает оба, чтобы
# показать клиенту правильный server-address и решить включать ли CA.
#
# Идемпотентно: CA генерится один раз. Server-cert перевыпускается если
# (self-signed) SAN не равен $ENTRY_HOST или (letsencrypt) Caddy выдал
# обновлённый cert. Переключения self→LE происходит автоматически когда
# Caddy выдаст cert; обратное (LE→self) — никогда без явного удаления
# PANEL_DOMAIN из config.yaml.

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
CADDY_CERT_DIR="/var/lib/caddy/.local/share/caddy/certificates/acme-v02.api.letsencrypt.org-directory"

mkdir -p /var/anysda/.stamps "$PKI" /etc/swanctl/conf.d /etc/anysda

# ── Detect mode: letsencrypt (если есть PANEL_DOMAIN И Caddy выдал cert) ───
LE_CERT=""
LE_KEY=""
if [[ -n "${PANEL_DOMAIN:-}" ]]; then
  LE_CERT="${CADDY_CERT_DIR}/${PANEL_DOMAIN}/${PANEL_DOMAIN}.crt"
  LE_KEY="${CADDY_CERT_DIR}/${PANEL_DOMAIN}/${PANEL_DOMAIN}.key"
fi
if [[ -n "$LE_CERT" && -r "$LE_CERT" && -r "$LE_KEY" ]]; then
  IKEV2_MODE="letsencrypt"
  IKEV2_SERVER_HOST="$PANEL_DOMAIN"
else
  IKEV2_MODE="self-signed"
  IKEV2_SERVER_HOST="$ENTRY_HOST"
  if [[ -n "${PANEL_DOMAIN:-}" ]]; then
    echo "[$HOST_TAG] PANEL_DOMAIN=$PANEL_DOMAIN задан, но Caddy ещё не выдал LE-cert"
    echo "[$HOST_TAG]   → fallback на self-signed; повторный запуск 27-ikev2 после Caddy auto-issue переключит в letsencrypt"
  fi
fi
echo "[$HOST_TAG] mode=$IKEV2_MODE server-host=$IKEV2_SERVER_HOST"

# Маркеры для панели (читает server/utils/ikev2.ts)
echo -n "$IKEV2_MODE"        > /etc/anysda/ikev2-mode
echo -n "$IKEV2_SERVER_HOST" > /etc/anysda/ikev2-server-host
chmod 644 /etc/anysda/ikev2-mode /etc/anysda/ikev2-server-host

# ── 1. apt strongswan + плагины ─────────────────────────────────────────────
# ⚠️ python3-vici (биндинги к сокету charon) в Ubuntu 24.04 НЕТ ни в одном
# компоненте — anysda-ikev2-sync (шаг 8) читает состояние через swanctl.
echo "[$HOST_TAG] [1/8] strongswan apt"
if ! command -v swanctl >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq \
    strongswan strongswan-pki strongswan-swanctl \
    libcharon-extra-plugins libstrongswan-extra-plugins >/dev/null
fi

# ── 2. Server certs — раскладка по режиму ─────────────────────────────────
install -d -m 700 /etc/swanctl/x509ca /etc/swanctl/x509 /etc/swanctl/private

if [[ "$IKEV2_MODE" == "letsencrypt" ]]; then
  echo "[$HOST_TAG] [2/8] LE-cert от Caddy ($PANEL_DOMAIN)"
  # ⚠️ В файле Caddy лежит ЦЕПОЧКА (leaf + промежуточный + кросс-подписи), а
  # swanctl из `certs = ...` берёт только ПЕРВЫЙ сертификат. Раскладываем
  # руками: leaf → x509, всё остальное → x509ca. Иначе charon шлёт клиенту
  # голый leaf, и цепочка не сходится ни у strongSwan, ни у нативных
  # клиентов: в trust-store лежит корень ISRG, а промежуточный (LE «YE1»,
  # «E5» и т.п.) обязан прийти от сервера — иначе AUTH_FAILED.
  cat > /usr/local/sbin/anysda-ikev2-le-sync.sh <<'LESYNC'
#!/usr/bin/env bash
# Раскладка LE-cert Caddy в swanctl: leaf → x509, промежуточные → x509ca.
# Зовётся стадией 27-ikev2 и path-юнитом anysda-ikev2-cert-sync (ротация LE).
set -euo pipefail
LE_CERT="$1"
LE_KEY="$2"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

csplit -sz -f "$TMP/cert-" -b '%02d.pem' "$LE_CERT" '/-----BEGIN CERTIFICATE-----/' '{*}'
install -m 0644 "$TMP/cert-00.pem" /etc/swanctl/x509/anysda-server.crt
install -m 0600 "$LE_KEY"          /etc/swanctl/private/anysda-server.key

rm -f /etc/swanctl/x509ca/anysda-le-chain-*.crt
n=1
for f in "$TMP"/cert-*.pem; do
  [[ "$f" == "$TMP/cert-00.pem" ]] && continue
  install -m 0644 "$f" "/etc/swanctl/x509ca/anysda-le-chain-${n}.crt"
  n=$((n + 1))
done

# На первом прогоне стадии charon ещё не запущен (это делает шаг 6, который
# сам перечитает creds) — здесь неудача не повод валить деплой.
swanctl --load-creds >/dev/null 2>&1 || true
LESYNC
  chmod +x /usr/local/sbin/anysda-ikev2-le-sync.sh
  # Свой CA из self-signed-режима больше не нужен — иначе путает charon при
  # поиске цепочки.
  rm -f /etc/swanctl/x509ca/anysda-ca.crt 2>/dev/null || true
  /usr/local/sbin/anysda-ikev2-le-sync.sh "$LE_CERT" "$LE_KEY"

  # ── 3. path-watcher: Caddy ротирует cert раз в 60 дней ────────────────
  echo "[$HOST_TAG] [3/8] systemd path-watcher на Caddy-cert (LE-ротация)"
  cat > /etc/systemd/system/anysda-ikev2-cert-sync.service <<EOF
[Unit]
Description=anysda-vpn2 — sync Caddy LE-cert into swanctl + reload
After=strongswan-starter.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/anysda-ikev2-le-sync.sh "$LE_CERT" "$LE_KEY"
EOF
  cat > /etc/systemd/system/anysda-ikev2-cert-sync.path <<EOF
[Unit]
Description=anysda-vpn2 — watch Caddy LE-cert file for changes

[Path]
PathChanged=$LE_CERT
Unit=anysda-ikev2-cert-sync.service

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now anysda-ikev2-cert-sync.path >/dev/null 2>&1
else
  echo "[$HOST_TAG] [2/8] self-signed CA"
  if [[ ! -f "$PKI/ca.key" ]]; then
    echo "[$HOST_TAG]   generating CA (ECDSA P-256, 10y)"
    pki --gen --type ecdsa --size 256 --outform pem > "$PKI/ca.key"
    pki --self --in "$PKI/ca.key" --type ecdsa \
        --dn "CN=anysda-vpn2 IKEv2 CA" --ca \
        --lifetime 3650 --outform pem > "$PKI/ca.crt"
  fi
  chmod 600 "$PKI/ca.key"
  chmod 644 "$PKI/ca.crt"

  echo "[$HOST_TAG] [3/8] self-signed server cert (CN=$ENTRY_HOST)"
  NEED_SERVER=0
  if [[ ! -f "$PKI/server.crt" ]]; then
    NEED_SERVER=1
  else
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
  install -m 644 "$PKI/ca.crt"      /etc/swanctl/x509ca/anysda-ca.crt
  install -m 644 "$PKI/server.crt"  /etc/swanctl/x509/anysda-server.crt
  install -m 600 "$PKI/server.key"  /etc/swanctl/private/anysda-server.key
  # Снять watcher если переходили из LE → self-signed (downgrade — не
  # автоматический, но если файл удалили вручную — корректно почистим).
  systemctl disable --now anysda-ikev2-cert-sync.path 2>/dev/null || true
fi

# ── 4. swanctl conf — базовый conn без клиентов (этап 4 наполнит динамику) ──
echo "[$HOST_TAG] [4/8] swanctl conf"
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
      id    = $IKEV2_SERVER_HOST
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
        # XFRM-interface if_id=42 — pakets из IPsec policy кладутся в xfrm0.
        # iptables PREROUTING -i xfrm0 ставит mark 0x42 → TPROXY 7898 (sing-box).
        if_id_in  = 42
        if_id_out = 42
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
echo "[$HOST_TAG] [5/8] ufw 500/udp + 4500/udp"
ufw allow 500/udp  comment 'ikev2 IKE'    >/dev/null 2>&1 || true
ufw allow 4500/udp comment 'ikev2 NAT-T'  >/dev/null 2>&1 || true
# DNS клиентам отдаётся AdGuard на mgmt-адресе entry, а он для пакетов из
# xfrm0 — форвардинг, а ufw по умолчанию routed=deny. Без этих двух правил
# туннель поднимется, но резолва у клиента не будет. Ровно так же сделано
# для wg0 (стадия 28) и tun0 (стадия 29).
ufw allow proto udp from "$IKEV2_SUBNET" to "$IKEV2_DNS" port 53 comment 'ikev2 → AdGuard DNS' >/dev/null 2>&1 || true
ufw allow proto tcp from "$IKEV2_SUBNET" to "$IKEV2_DNS" port 53 comment 'ikev2 → AdGuard DNS' >/dev/null 2>&1 || true
ufw reload >/dev/null 2>&1 || true

sysctl -w net.ipv4.ip_forward=1 >/dev/null
# Loose RPF: policy-routing IPsec иначе режется reverse-path filter.
sysctl -w net.ipv4.conf.all.rp_filter=2 >/dev/null
sysctl -w net.ipv4.conf.default.rp_filter=2 >/dev/null

# ── 6. systemd: strongswan-starter (Ubuntu 24.04 / strongSwan 5.9) ──────────
# На свежих стронгсванах сервис называется strongswan-starter.service (alias —
# ipsec.service). Юнит strongswan.service отсутствует.
echo "[$HOST_TAG] [6/8] strongswan-starter service"
systemctl enable strongswan-starter >/dev/null 2>&1 || true
systemctl restart strongswan-starter
sleep 2
systemctl status strongswan-starter --no-pager -n 4 2>/dev/null | head -6 | sed "s/^/[$HOST_TAG]   /"

# Применяем CA + creds + conns в работающий daemon.
swanctl --load-creds >/dev/null
swanctl --load-pools >/dev/null
swanctl --load-conns >/dev/null

# Sanity: conn anysda-ikev2 в списке, порты слушают.
echo "[$HOST_TAG]   conns:"
swanctl --list-conns 2>/dev/null | grep -E '^[a-z]|local|remote|child' | head -10 | sed "s/^/[$HOST_TAG]     /"
echo "[$HOST_TAG]   listening:"
ss -lun 2>/dev/null | awk '/:500 |:4500 /{print "    " $0}' | sed "s/^/[$HOST_TAG]/"

# ── 7. xfrm0 + TPROXY hook (как anysda-ovpn-routing для tun0) ──────────────
echo "[$HOST_TAG] [7/8] xfrm0 + anysda-ikev2-routing"
WAN_IF=$(ip route show default | awk '/default/{print $5; exit}')
: "${WAN_IF:?не определился WAN-интерфейс по default route}"

cat > /etc/systemd/system/anysda-ikev2-routing.service <<EOF
[Unit]
Description=anysda-vpn2 — xfrm0 (IKEv2) + TPROXY to sing-box
After=network-online.target sing-box.service strongswan-starter.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/anysda-ikev2-routing.sh up
ExecStop=/usr/local/sbin/anysda-ikev2-routing.sh down

[Install]
WantedBy=multi-user.target
EOF

cat > /usr/local/sbin/anysda-ikev2-routing.sh <<IPTSEOF
#!/usr/bin/env bash
# xfrm0-interface + TPROXY-зацеп IPsec-трафика в sing-box :7898.
# IPsec policies (swanctl conn anysda-ikev2 с if_id_in/out=42) кладут
# расшифрованные пакеты в xfrm0; PREROUTING -i xfrm0 шлёт их в TPROXY.
set -euo pipefail
ACTION=\${1:-up}

XFRM_IF='xfrm0'
WAN_IF='$WAN_IF'
MARK='0x42'
TABLE=101
TPROXY_PORT=7898
IKEV2_SUBNET='${IKEV2_SUBNET%/*}/24'

if [[ "\$ACTION" == "up" ]]; then
  # xfrm0 интерфейс (idempotent)
  if ! ip link show "\$XFRM_IF" >/dev/null 2>&1; then
    ip link add "\$XFRM_IF" type xfrm dev "\$WAN_IF" if_id 42
  fi
  ip link set "\$XFRM_IF" mtu 1400 up
  # IP на xfrm0 не нужен — TPROXY работает по mark'у, не по адресу интерфейса.
  # А вот МАРШРУТ на пул нужен: обратные пакеты (ответы sing-box и AdGuard)
  # адресованы 10.68.68.x, и без него ядро не знает, куда их слать — SA
  # поднимается, трафик уходит в sing-box, а ответы молча дохнут на xfrm0
  # (TX errors). Через xfrm0 они попадают в out-политику и шифруются.
  ip route replace "\$IKEV2_SUBNET" dev "\$XFRM_IF"

  # Маршрут для пакетов с mark — в local lookup (TPROXY ловит)
  ip rule list | grep -q "fwmark \$MARK lookup \$TABLE" || \\
    ip rule add fwmark "\$MARK" lookup "\$TABLE"
  ip route show table "\$TABLE" 2>/dev/null | grep -q 'local default' || \\
    ip route add local 0.0.0.0/0 dev lo table "\$TABLE"

  iptables -t mangle -N ANYSDA_IKEV2_TPROXY 2>/dev/null || true
  iptables -t mangle -F ANYSDA_IKEV2_TPROXY
  # DNS клиентам резолвится AdGuard'ом на entry mgmt IP — он local, не TPROXY.
  iptables -t mangle -A ANYSDA_IKEV2_TPROXY -d 10.99.0.1 -j RETURN
  iptables -t mangle -A ANYSDA_IKEV2_TPROXY -p tcp -j TPROXY --tproxy-mark "\${MARK}/\${MARK}" --on-port "\$TPROXY_PORT" --on-ip 127.0.0.1
  iptables -t mangle -A ANYSDA_IKEV2_TPROXY -p udp -j TPROXY --tproxy-mark "\${MARK}/\${MARK}" --on-port "\$TPROXY_PORT" --on-ip 127.0.0.1

  iptables -t mangle -C PREROUTING -i "\$XFRM_IF" -j ANYSDA_IKEV2_TPROXY 2>/dev/null || \\
    iptables -t mangle -I PREROUTING 1 -i "\$XFRM_IF" -j ANYSDA_IKEV2_TPROXY

  iptables -C INPUT -m mark --mark "\${MARK}/\${MARK}" -j ACCEPT 2>/dev/null || \\
    iptables -I INPUT 1 -m mark --mark "\${MARK}/\${MARK}" -j ACCEPT

  echo "anysda-ikev2-routing up (xfrm=\$XFRM_IF dev=\$WAN_IF if_id=42, mark=\$MARK)"

elif [[ "\$ACTION" == "down" ]]; then
  iptables -t mangle -D PREROUTING -i "\$XFRM_IF" -j ANYSDA_IKEV2_TPROXY 2>/dev/null || true
  iptables -t mangle -F ANYSDA_IKEV2_TPROXY 2>/dev/null || true
  iptables -t mangle -X ANYSDA_IKEV2_TPROXY 2>/dev/null || true
  ip link del "\$XFRM_IF" 2>/dev/null || true
fi
IPTSEOF

chmod +x /usr/local/sbin/anysda-ikev2-routing.sh
systemctl daemon-reload
systemctl enable anysda-ikev2-routing >/dev/null 2>&1 || true
systemctl restart anysda-ikev2-routing

# ── 8. anysda-ikev2-sync: применение кредов панели + счётчики ──────────────
# Панель живёт в контейнере, где нет ни swanctl, ни сокета charon.vici:
# её вызовы swanctl (server/utils/ikev2.ts) — best-effort и молча
# пропускаются. Реальный применитель — этот таймер на хосте: он видит
# переписанный панелью anysda-clients.conf и грузит его в charon, рвёт SA
# устройств с изменённым/удалённым паролем и выкладывает счётчики байт
# в /etc/anysda/ikev2-status.json (панель читает его как status-файл
# OpenVPN — см. server/utils/traffic-collector.ts).
echo "[$HOST_TAG] [8/8] anysda-ikev2-sync (creds + counters)"

cat > /usr/local/sbin/anysda-ikev2-sync.py <<'PYEOF'
#!/usr/bin/env python3
"""anysda-vpn2 — применение IKEv2-кредов панели и снятие счётчиков.

Раскатывается стадией 27-ikev2, гоняется таймером anysda-ikev2-sync.timer.

  1. /etc/swanctl/conf.d/anysda-clients.conf изменился (панель переписала) →
     `swanctl --load-creds` + разрыв SA тех устройств, чей пароль изменился
     или чья запись исчезла (удаление / заморозка / истёкший срок);
  2. каждый прогон → счётчики байт по каждой живой SA в
     /etc/anysda/ikev2-status.json.

Снимок кредов держится в /var/anysda/ikev2-clients.state.json как
username → sha256(пароль): сам пароль на диск вне swanctl не кладём.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

CLIENTS_CONF = '/etc/swanctl/conf.d/anysda-clients.conf'
STATE_FILE = '/var/anysda/ikev2-clients.state.json'
STATUS_FILE = '/etc/anysda/ikev2-status.json'

RE_ID = re.compile(r'^\s*id\s*=\s*"?([^"]+?)"?\s*$')
RE_SECRET = re.compile(r'^\s*secret\s*=\s*"?(.*?)"?\s*$')

# Разбор `swanctl --list-sas`. Биндингов python3-vici в Ubuntu 24.04 нет,
# поэтому состояние читаем из человекочитаемого вывода:
#
#   anysda-ikev2: #7, ESTABLISHED, IKEv2, cbd5867eca6763b0_i 0cf6cdcbdda975d6_r*
#     remote '192.168.1.210' @ 109.252.101.47[1445] EAP: 'phone' [10.68.68.1]
#     net: #7, reqid 1, INSTALLED, TUNNEL, ESP:AES_GCM_16-256
#       in  c6cbc42a (-|0x0000002a),  50650 bytes,   540 packets,    11s ago
#       out ceebcf43 (-|0x0000002a), 1023795 bytes,  1100 packets,    11s ago
#
# ⚠️ После SPI может стоять «(-|0x…)» с if_id — до числа байт идёт не один
# токен, а произвольный кусок до первой запятой.
RE_SA_HEAD = re.compile(r'^(\S+):\s+#(\d+),\s+(\S+?),')
RE_EAP_ID = re.compile(r"EAP:\s+'([^']+)'")
RE_REMOTE_ID = re.compile(r"^\s+remote\s+'([^']+)'")
RE_BYTES = re.compile(r'^\s+(in|out)\s+.*?,\s*(\d+)\s+bytes')


def parse_clients(path):
    """secrets-блок swanctl → {username: sha256(secret)}."""
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return {}
    out, ident = {}, None
    for line in text.splitlines():
        m = RE_ID.match(line)
        if m:
            ident = m.group(1)
            continue
        m = RE_SECRET.match(line)
        if m and ident:
            out[ident] = hashlib.sha256(m.group(1).encode()).hexdigest()
            ident = None
    return out


def read_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data, mode):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def live_sas():
    """[{uniqueid, username, rx, tx}] по живым IKE_SA (rx/tx — в терминах клиента).

    `in` в выводе — принято сервером от клиента, то есть отдача клиента (tx);
    `out` — отдано клиенту, то есть его скачивание (rx). Это то же соглашение,
    что у сборщика трафика панели.
    """
    try:
        out = subprocess.run(['swanctl', '--list-sas'], timeout=30,
                             capture_output=True, text=True, check=True).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        print(f'swanctl --list-sas: {exc}', file=sys.stderr)
        return None

    sas, cur = [], None
    for line in out.splitlines():
        head = RE_SA_HEAD.match(line)
        if head:
            cur = {'uniqueid': head.group(2), 'username': '', 'rx': 0, 'tx': 0}
            sas.append(cur)
            continue
        if cur is None:
            continue
        eap = RE_EAP_ID.search(line)
        if eap:
            cur['username'] = eap.group(1)
            continue
        remote = RE_REMOTE_ID.match(line)
        if remote and not cur['username']:
            cur['username'] = remote.group(1)
            continue
        counter = RE_BYTES.match(line)
        if counter:
            key = 'tx' if counter.group(1) == 'in' else 'rx'
            cur[key] += int(counter.group(2))
    return sas


def main():
    current = parse_clients(CLIENTS_CONF)
    previous = read_json(STATE_FILE, {})

    if current != previous:
        subprocess.run(['swanctl', '--load-creds'], check=True,
                       stdout=subprocess.DEVNULL, timeout=30)
        # Пароль сменился или устройство исчезло из secrets → рвём живую SA,
        # иначе старая сессия висит до перезагрузки (rekey_time = 0s).
        stale = {u for u, h in previous.items() if current.get(u) != h}
        if stale:
            for sa in live_sas() or []:
                if sa['username'] in stale and sa['uniqueid']:
                    print(f'terminate ike-id={sa["uniqueid"]} ({sa["username"]})')
                    # --ike-id ждёт ЧИСЛОВОЙ uniqueid IKE_SA, не EAP-логин.
                    rc = subprocess.run(
                        ['swanctl', '--terminate', '--ike-id', sa['uniqueid']],
                        stdout=subprocess.DEVNULL, timeout=30).returncode
                    if rc != 0:
                        print(f'terminate {sa["username"]} rc={rc}', file=sys.stderr)
        write_json(STATE_FILE, current, 0o600)

    sas = live_sas()
    if sas is None:  # charon не отвечает — это авария, пусть видно в journal
        return 1
    sessions = [sa for sa in sas if sa['username']]
    write_json(STATUS_FILE, {'updated': int(time.time()), 'sessions': sessions}, 0o644)
    return 0


if __name__ == '__main__':
    sys.exit(main())
PYEOF
chmod +x /usr/local/sbin/anysda-ikev2-sync.py

cat > /etc/systemd/system/anysda-ikev2-sync.service <<'EOF'
[Unit]
Description=anysda-vpn2 — apply panel IKEv2 creds + dump traffic counters
After=strongswan-starter.service
Requires=strongswan-starter.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/anysda-ikev2-sync.py
EOF

cat > /etc/systemd/system/anysda-ikev2-sync.timer <<'EOF'
[Unit]
Description=anysda-vpn2 — IKEv2 creds/counters sync every 10s

[Timer]
OnBootSec=30s
OnUnitActiveSec=10s
AccuracySec=1s
Unit=anysda-ikev2-sync.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now anysda-ikev2-sync.timer >/dev/null 2>&1 || true
systemctl start anysda-ikev2-sync.service || true
# ⚠️ `systemctl status` у отработавшего oneshot возвращает 3, а в стадии
# включён pipefail — без `|| true` стадия падала уже ПОСЛЕ всей работы.
{ systemctl status anysda-ikev2-sync.service --no-pager -n 3 2>/dev/null || true; } \
  | head -5 | sed "s/^/[$HOST_TAG]   /"

touch /var/anysda/.stamps/27-ikev2
echo "[$HOST_TAG] 27-ikev2 done — mode=$IKEV2_MODE server-host=$IKEV2_SERVER_HOST"
