#!/usr/bin/env bash
# Stage 22 — AdGuard Home on RU server.
# Listens on 10.8.0.1:53 (DNS for WireGuard clients).
# Web UI on 127.0.0.1:${AGH_PORT} — proxied by Caddy at https://vpn.example.com:3001 (stage 30).

set -euo pipefail

[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}" "${AGH_PORT:?}"

case "$HOST_TAG" in ru) ;; *) echo "[$HOST_TAG] 22-adguard is ru-only — skipping"; exit 0;; esac

STAMP_DIR=/var/anysda/.stamps
STAGE='22-adguard'
AGH_DIR=/opt/AdGuardHome
AGH_BIN=$AGH_DIR/AdGuardHome
AGH_VER='v0.107.61'

mkdir -p "$AGH_DIR" /etc/anysda

# ----------------------------------------------------------------------------
# 1. Install AdGuard Home binary
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [1/4] AdGuard Home binary"
need_install=0
if [[ ! -x "$AGH_BIN" ]]; then
  need_install=1
else
  installed=$("$AGH_BIN" --version 2>/dev/null | grep -oP 'v[\d.]+' | head -1 || echo "")
  [[ "$installed" == "$AGH_VER" ]] || need_install=1
fi

if [[ "$need_install" -eq 1 ]]; then
  ARCH='linux_amd64'
  TMP=$(mktemp -d)
  curl -sSL "https://github.com/AdguardTeam/AdGuardHome/releases/download/${AGH_VER}/AdGuardHome_${ARCH}.tar.gz" \
    | tar -xz -C "$TMP"
  install -m0755 "$TMP/AdGuardHome/AdGuardHome" "$AGH_BIN"
  rm -rf "$TMP"
fi
"$AGH_BIN" --version | sed "s/^/[$HOST_TAG]   /"

# ----------------------------------------------------------------------------
# 2. Generate or load admin password + bcrypt hash
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [2/4] credentials"
apt-get install -y -qq apache2-utils >/dev/null 2>&1
# Единый логин/пароль для веб-панели и AdGuard.
# ADMIN_USER / ADMIN_PASSWORD приходят из config.yaml через ru.env.
# Если ADMIN_PASSWORD пуст — генерим один раз и кладём в admin-password.txt,
# 30-frontend потом этот файл переиспользует.
AGH_USER="${ADMIN_USER:-anysda}"
if [[ -n "${ADMIN_PASSWORD:-}" ]]; then
  printf '%s' "$ADMIN_PASSWORD" > /etc/anysda/admin-password.txt
  chmod 600 /etc/anysda/admin-password.txt
elif [[ ! -f /etc/anysda/admin-password.txt ]]; then
  openssl rand -base64 24 | tr -d '\n/+=' | head -c 18 > /etc/anysda/admin-password.txt
  chmod 600 /etc/anysda/admin-password.txt
fi
AGH_PASS=$(cat /etc/anysda/admin-password.txt)
AGH_PASS_HASH=$(htpasswd -bnBC 10 "" "$AGH_PASS" | tr -d ':\n' | sed 's/$2y/$2a/')
echo "[$HOST_TAG]   $AGH_USER / $AGH_PASS"

# ----------------------------------------------------------------------------
# 3. Write config (only on first install; never overwrite live config)
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [3/4] config"
AGH_CFG=$AGH_DIR/AdGuardHome.yaml
if [[ ! -f "$AGH_CFG" ]]; then
cat > "$AGH_CFG" <<YAML
http:
  address: 127.0.0.1:${AGH_PORT}

users:
  - name: ${AGH_USER}
    password: '${AGH_PASS_HASH}'

auth_attempts: 5
block_auth_min: 15

dns:
  bind_hosts:
    - 10.8.0.1
    - 127.0.0.1
  port: 53
  upstream_dns:
    - 77.88.8.8
    - https://1.1.1.1/dns-query
    - https://dns.google/dns-query
  bootstrap_dns:
    - 77.88.8.8
    - 8.8.8.8
  fallback_dns:
    - 77.88.8.8
  upstream_mode: parallel
  cache_size: 4194304
  cache_ttl_min: 0
  cache_ttl_max: 0
  enable_dnssec: false
  blocked_response_ttl: 10
  filtering_enabled: true
  filters_update_interval: 24

filters:
  - enabled: true
    url: https://adguardteam.github.io/HostlistsRegistry/assets/filter_1.txt
    name: AdGuard DNS filter
    id: 1
  - enabled: true
    url: https://adguardteam.github.io/HostlistsRegistry/assets/filter_2.txt
    name: AdAway Default Blocklist
    id: 2

log:
  file: ''
  max_backups: 0
  max_size: 100
  max_age: 3
  compress: false
  local_time: false
  verbose: false

schema_version: 28
YAML
chmod 600 "$AGH_CFG"
else
  # Config exists — sync credentials (user/password may have changed)
  python3 - <<PYEOF
import yaml, sys
cfg_path = "$AGH_CFG"
with open(cfg_path) as f:
    cfg = yaml.safe_load(f)
cfg["users"] = [{"name": "$AGH_USER", "password": "$AGH_PASS_HASH"}]
with open(cfg_path, "w") as f:
    yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
print("credentials synced")
PYEOF
fi

# ----------------------------------------------------------------------------
# 4. systemd service
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [4/4] systemd"
cat > /etc/systemd/system/adguardhome.service <<'EOF'
[Unit]
Description=AdGuard Home (anysda-vpn DNS)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/AdGuardHome
ExecStart=/opt/AdGuardHome/AdGuardHome --no-check-update --config /opt/AdGuardHome/AdGuardHome.yaml --work-dir /opt/AdGuardHome
Restart=on-failure
RestartSec=5s
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable adguardhome >/dev/null 2>&1
systemctl restart adguardhome
sleep 2
systemctl status adguardhome --no-pager -n 4 | head -6 | sed "s/^/[$HOST_TAG]   /"

# ufw: allow DNS only from WG clients
ufw allow proto udp from 10.8.0.0/24 to 10.8.0.1 port 53 comment 'AdGuard DNS WG clients' >/dev/null 2>&1 || true
ufw allow proto tcp from 10.8.0.0/24 to 10.8.0.1 port 53 comment 'AdGuard DNS WG clients TCP' >/dev/null 2>&1 || true
ufw reload >/dev/null

mkdir -p "$STAMP_DIR"
touch "$STAMP_DIR/$STAGE"
echo "[$HOST_TAG] $STAGE done — AdGuard UI proxied via Caddy after stage 30"
