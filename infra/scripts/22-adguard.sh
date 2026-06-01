#!/usr/bin/env bash
# Stage 22 — AdGuard Home on RU server.
# Listens on 127.0.0.1:53 and ${MGMT_IP}:53 (mgmt mesh). WG/OpenVPN-клиенты
# резолвят через него (см. ufw-правила → 10.99.0.1:53 в стадиях 28/29);
# sing-box тоже использует его как DNS-резолвер (gen-router-config.py).
# Web UI on 127.0.0.1:${AGH_PORT} — proxied by Caddy at :3001 (stage 30).

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
  # Скачиваем архив и checksums-файл по отдельности и проверяем SHA-256 перед
  # распаковкой — `curl | tar -xz` уязвим к compromise CDN (см. SECURITY-AUDIT
  # 2026-06-01.md / H8). checksums.txt подписан тем же релизом — атакеру нужно
  # подменить оба файла одновременно (всё ещё уязвимо к compromise GitHub
  # releases, но не к одностороннему MITM/CDN-corruption).
  REL_URL="https://github.com/AdguardTeam/AdGuardHome/releases/download/${AGH_VER}"
  ARCHIVE="AdGuardHome_${ARCH}.tar.gz"
  curl -sSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
    "$REL_URL/$ARCHIVE" -o "$TMP/$ARCHIVE"
  curl -sSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
    "$REL_URL/checksums.txt" -o "$TMP/checksums.txt"
  ( cd "$TMP" && grep -E "[[:space:]]\*?${ARCHIVE}\$" checksums.txt | sha256sum -c - ) \
    || { rm -rf "$TMP"; echo "[$HOST_TAG] AdGuardHome SHA-256 mismatch — abort" >&2; exit 1; }
  tar -xz -C "$TMP" -f "$TMP/$ARCHIVE"
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
echo "[$HOST_TAG]   $AGH_USER / (пароль в /etc/anysda/admin-password.txt)"

# ----------------------------------------------------------------------------
# 3. Write config (only on first install; never overwrite live config)
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [3/4] config"
AGH_CFG=$AGH_DIR/AdGuardHome.yaml

# Idempotent-патчи для уже существующего конфига (не перезаписываем
# полностью — пользователь может что-то менять в UI). Применяются ПЕРЕД
# первичной генерацией, чтобы для новых установок дефолты были правильные.
patch_agh_config() {
  local cfg="$1"
  [[ ! -f "$cfg" ]] && return 0
  local changed=0
  # aaaa_disabled: true — иначе VPN-клиент получает AAAA, идёт через тоннель
  # IPv6, на entry нет v6-форвардинга/TPROXY → пакет уходит нативно, manual-
  # routes обходятся (см. деталь в фикс-блоке). Возвращать v6 — только после
  # полной поддержки v6 на entry.
  if grep -q '^  aaaa_disabled:' "$cfg"; then
    if ! grep -q '^  aaaa_disabled: true$' "$cfg"; then
      sed -i 's/^  aaaa_disabled: .*/  aaaa_disabled: true/' "$cfg"
      changed=1
    fi
  else
    # вставляем после строки `dns:` (на верхнем уровне)
    sed -i '/^dns:$/a\  aaaa_disabled: true' "$cfg"
    changed=1
  fi
  if [[ $changed -eq 1 ]]; then
    echo "[$HOST_TAG]   patched: aaaa_disabled: true (no AAAA leak through tunnel)"
  fi
}

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
    - 127.0.0.1
    - ${MGMT_IP}
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
  # IPv6 AAAA-leak: на entry нет IPv6 forwarding (wg0 без v6, ip6tables
  # PREROUTING пустой, sing-box слушает только v4). Если AdGuard отдаёт
  # AAAA — телефон/клиент пытается v6 через тоннель, на entry пакет либо
  # дропается, либо уходит нативно (sniff обходит manual-routes), и
  # назначения вроде gemini.google.com блокируют по RU IP. Режем AAAA
  # на стороне DNS — клиент получает только A, manual-routes и authoritative
  # роутинг работают штатно. Чтобы вернуть v6 — нужно поднимать v6-стек
  # на entry полностью (wg0 v6 + ip6tables TPROXY + sing-box v6 listen).
  aaaa_disabled: true

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
  # + idempotent-патчи поверх (aaaa_disabled и т.п. — см. patch_agh_config)
  python3 - <<PYEOF
import yaml, sys
cfg_path = "$AGH_CFG"
with open(cfg_path) as f:
    cfg = yaml.safe_load(f)
cfg["users"] = [{"name": "$AGH_USER", "password": "$AGH_PASS_HASH"}]
# Гарантируем aaaa_disabled: true (мерджим в dns секцию)
cfg.setdefault("dns", {})["aaaa_disabled"] = True
with open(cfg_path, "w") as f:
    yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
print("credentials + aaaa_disabled synced")
PYEOF
fi
patch_agh_config "$AGH_CFG"

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

# ufw: AdGuard listens only on 127.0.0.1 and mgmt mesh (${MGMT_IP})
# Both are reachable from inside the host (sing-box) and from peer nodes via wgmgmt.
# No firewall opening needed for client traffic — clients hit AdGuard indirectly
# through sing-box DNS forwarding, not directly.
ufw allow proto udp from "${MGMT_NET}" to "${MGMT_IP}" port 53 comment 'AdGuard DNS mgmt mesh UDP' >/dev/null 2>&1 || true
ufw allow proto tcp from "${MGMT_NET}" to "${MGMT_IP}" port 53 comment 'AdGuard DNS mgmt mesh TCP' >/dev/null 2>&1 || true
ufw reload >/dev/null

mkdir -p "$STAMP_DIR"
touch "$STAMP_DIR/$STAGE"
echo "[$HOST_TAG] $STAGE done — AdGuard UI proxied via Caddy after stage 30"
