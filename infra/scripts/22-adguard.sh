#!/usr/bin/env bash
# Stage 22 — AdGuard Home on RU server.
# Listens on 127.0.0.1:53 and ${MGMT_IP}:53 (mgmt mesh). WG/OpenVPN-клиенты
# резолвят через него (см. ufw-правила → 10.99.0.1:53 в стадиях 28/29);
# sing-box тоже использует его как DNS-резолвер (gen-router-config.py).
# Web UI on 127.0.0.1:${AGH_PORT} — панель отдаёт его по /adguard/ под своей сессией.

set -euo pipefail

[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}" "${AGH_PORT:?}"
# Перезапуск служб только при смене их входов (infra/lib/anysda-svc.sh): деплой
# кладёт библиотеку рядом со стадией, anysda-restore зовёт стадию из клона репо.
SVC_LIB=$(dirname "$0")/anysda-svc.sh
[[ -f "$SVC_LIB" ]] || SVC_LIB=$(dirname "$0")/../lib/anysda-svc.sh
# shellcheck source=infra/lib/anysda-svc.sh
source "$SVC_LIB"

case "$HOST_TAG" in ru) ;; *) echo "[$HOST_TAG] 22-adguard is ru-only — skipping"; exit 0;; esac

RESOURCE_LIB=$(dirname "$0")/entry-resources.sh
[[ -f "$RESOURCE_LIB" ]] || RESOURCE_LIB=$(dirname "$0")/../lib/entry-resources.sh
# shellcheck source=infra/lib/entry-resources.sh
source "$RESOURCE_LIB"
entry_resources agh

STAMP_DIR=/var/anysda/.stamps
STAGE='22-adguard'
AGH_DIR=/opt/AdGuardHome
AGH_BIN=$AGH_DIR/AdGuardHome
AGH_VER='v0.107.79'

mkdir -p "$AGH_DIR" /etc/anysda

# ----------------------------------------------------------------------------
# 1. Install AdGuard Home binary
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [1/4] AdGuard Home binary"
need_install=0
if [[ ! -x "$AGH_BIN" ]]; then
  need_install=1
else
  installed=$("$AGH_BIN" --version 2>/dev/null | grep -oP 'v[\d.]+' | sed -n 1p || echo "")
  [[ "$installed" == "$AGH_VER" ]] || need_install=1
fi

if [[ "$need_install" -eq 1 ]]; then
  ARCH='linux_amd64'
  TMP=$(mktemp -d)
  # Скачиваем архив и checksums-файл по отдельности и проверяем SHA-256 перед
  # распаковкой — `curl | tar -xz` уязвим к compromise CDN. checksums.txt подписан тем же релизом — атакеру нужно
  # подменить оба файла одновременно (всё ещё уязвимо к compromise GitHub
  # releases, но не к одностороннему MITM/CDN-corruption).
  REL_URL="https://github.com/AdguardTeam/AdGuardHome/releases/download/${AGH_VER}"
  ARCHIVE="AdGuardHome_${ARCH}.tar.gz"
  curl -sSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
    "$REL_URL/$ARCHIVE" -o "$TMP/$ARCHIVE"
  curl -sSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
    "$REL_URL/checksums.txt" -o "$TMP/checksums.txt"
  ( cd "$TMP" && grep -E "[[:space:]]\*?(\./)?${ARCHIVE}\$" checksums.txt | sha256sum -c - ) \
    || { rm -rf "$TMP"; echo "[$HOST_TAG] AdGuardHome SHA-256 mismatch — abort" >&2; exit 1; }
  tar -xz -C "$TMP" -f "$TMP/$ARCHIVE"
  install -m0755 "$TMP/AdGuardHome/AdGuardHome" "$AGH_BIN"
  rm -rf "$TMP"
fi
"$AGH_BIN" --version | HOST_TAG="$HOST_TAG" awk '{print "[" ENVIRON["HOST_TAG"] "]   " $0}'

# ----------------------------------------------------------------------------
# 2. Generate or load admin password + bcrypt hash
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [2/4] credentials"
apt_have apache2-utils || apt_get install -y -qq apache2-utils >/dev/null 2>&1
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
# Соль bcrypt случайная: новый хэш на каждом прогоне менял конфиг, и AdGuard
# перезапускался зря. Хэш из конфига оставляем, если он подходит к паролю.
AGH_PASS_HASH=$(python3 -c 'import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
print(next((u.get("password") or "" for u in cfg.get("users") or [] if u.get("name") == sys.argv[2]), ""))' \
  "$AGH_DIR/AdGuardHome.yaml" "$AGH_USER" 2>/dev/null || true)
AGH_HTP=$(mktemp)
printf '%s:%s\n' "$AGH_USER" "$AGH_PASS_HASH" > "$AGH_HTP"
if [[ -z "$AGH_PASS_HASH" ]] || ! htpasswd -vb "$AGH_HTP" "$AGH_USER" "$AGH_PASS" >/dev/null 2>&1; then
  AGH_PASS_HASH=$(htpasswd -bnBC 10 "" "$AGH_PASS" | tr -d ':\n' | sed 's/$2y/$2a/')
fi
rm -f "$AGH_HTP"
echo "[$HOST_TAG]   $AGH_USER / (пароль в /etc/anysda/admin-password.txt)"

# ----------------------------------------------------------------------------
# 3. Write config (only on first install; never overwrite live config)
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [3/4] config"
AGH_WORK=$(mktemp -d)
AGH_CFG=$AGH_WORK/AdGuardHome.yaml
AGH_PLAIN_CFG=$AGH_WORK/AdGuardHome-unfiltered.yaml
trap 'rm -f "$AGH_CFG" "$AGH_PLAIN_CFG"; rmdir "$AGH_WORK"' EXIT
[[ ! -f "$AGH_DIR/AdGuardHome.yaml" ]] || cp "$AGH_DIR/AdGuardHome.yaml" "$AGH_CFG"

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
  [[ $changed -eq 1 ]] && echo "[$HOST_TAG]   patched: aaaa_disabled: true (no AAAA leak through tunnel)"
  # ratelimit_whitelist: пулы туннелей. AdGuard считает лимит (20 rps) на
  # подсеть /24, а клиенты WG/OpenVPN/IKEv2 сидят каждый в своей /24 и делят
  # одно ведро на всех. Поле принимает только адреса, не CIDR, поэтому пулы
  # раскрываем. Пулы - из тех же переменных, что в 28/29/27. Чужие адреса
  # в списке не трогаем, лимит для остальных остаётся.
  local pools
  pools="$(vpn_pool_subnets wg ovpn ikev2) ${WG_SERVER_IP:-10.66.66.1}/24 ${OVPN_SUBNET:-10.67.67.0}/24 ${IKEV2_SUBNET:-10.68.68.0/24}"
  local wl
  wl=$(CFG="$cfg" POOLS="$pools" python3 - <<'PYEOF'
import copy, ipaddress, os, yaml
with open(os.environ["CFG"]) as f:
    cfg = yaml.safe_load(f)
before = copy.deepcopy(cfg)
dns = cfg.setdefault("dns", {})
# Политика общая для новых и установленных хабов. Убираем также прежние
# глобальный fallback Яндекса и исключение YouTube: они обходили разделение.
dns["upstream_dns"] = ["[/ru/рф/xn--p1ai/su/]77.88.8.8",
                       "https://1.1.1.1/dns-query", "https://dns.google/dns-query"]
dns["upstream_dns_file"] = ""
dns["fallback_dns"] = []
dns["bootstrap_dns"] = ["8.8.8.8", "8.8.4.4"]
dns["upstream_mode"] = "parallel"
dns["cache_optimistic"] = True
# OUTPUT перехватывает TCP; не позволяем включённому в UI HTTP/3 обойти его.
dns["use_http3_upstreams"] = False
have = [str(a) for a in dns.get("ratelimit_whitelist") or []]
need = dict.fromkeys(str(h) for p in os.environ["POOLS"].split()
                     for h in ipaddress.ip_network(p, strict=False).hosts())
have_set = set(have)
missing = [a for a in need if a not in have_set]
if missing:
    dns["ratelimit_whitelist"] = have + missing
if cfg != before:
    with open(os.environ["CFG"], "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    print("DNS policy synced; ratelimit_whitelist +=", len(missing))
PYEOF
)
  if [[ -n "$wl" ]]; then
    echo "[$HOST_TAG]   patched: $wl"
    changed=1
  fi
  [[ $changed -eq 1 ]] && svc_forget adguardhome
  return 0
}

agh_fresh=()
if [[ ! -f "$AGH_CFG" ]]; then
agh_fresh=(--fresh)
# JSON-строки являются YAML-скалярами: сохраняют тип, кавычки и переводы строк.
AGH_USER_YAML=$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$AGH_USER")
AGH_HASH_YAML=$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$AGH_PASS_HASH")
cat > "$AGH_CFG" <<YAML
http:
  address: 127.0.0.1:${AGH_PORT}

users:
  - name: ${AGH_USER_YAML}
    password: ${AGH_HASH_YAML}

auth_attempts: 5
block_auth_min: 15

dns:
  bind_hosts:
    - 127.0.0.1
    - ${MGMT_IP}
  port: 53
  upstream_dns:
    - '[/ru/рф/xn--p1ai/su/]77.88.8.8'
    - https://1.1.1.1/dns-query
    - https://dns.google/dns-query
  upstream_dns_file: ''
  bootstrap_dns:
    - 8.8.8.8
    - 8.8.4.4
  fallback_dns: []
  upstream_mode: parallel
  cache_optimistic: true
  use_http3_upstreams: false
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
  # ECS не добавляем: апстримам незачем знать подсеть клиента. ECS, который
  # клиент прислал сам, AdGuard всё равно пробрасывает как есть.
  edns_client_subnet:
    custom_ip: ""
    enabled: false
    use_custom: false

filters:
  - enabled: true
    url: https://adguardteam.github.io/HostlistsRegistry/assets/filter_1.txt
    name: AdGuard DNS filter
    id: 1
  - enabled: ${AGH_ADAWAY}
    url: https://adguardteam.github.io/HostlistsRegistry/assets/filter_2.txt
    name: AdAway Default Blocklist
    id: 2

querylog:
  size_memory: ${AGH_QUERYLOG}

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
svc_forget adguardhome
else
  # Config exists — sync credentials (user/password may have changed)
  # + idempotent-патчи поверх (aaaa_disabled и т.п. — см. patch_agh_config)
  # Конфиг AdGuard читает только при старте: переписали — нужен рестарт.
  # Пишем лишь при смысловой разнице, формат yaml AdGuard ведёт сам.
  agh_sync=$(python3 - "$AGH_CFG" "$AGH_USER" "$AGH_PASS_HASH" <<'PYEOF'
import copy, sys, yaml
cfg_path, user, password = sys.argv[1:]
with open(cfg_path) as f:
    cfg = yaml.safe_load(f)
before = copy.deepcopy(cfg)
cfg["users"] = [{"name": user, "password": password}]
# Гарантируем aaaa_disabled: true (мерджим в dns секцию)
cfg.setdefault("dns", {})["aaaa_disabled"] = True
cfg["dns"].setdefault("edns_client_subnet", {})["enabled"] = False
if cfg != before:
    with open(cfg_path, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    print("changed")
PYEOF
)
  if [[ "$agh_sync" == changed ]]; then
    echo "[$HOST_TAG]   credentials + aaaa_disabled + ecs off synced"
    svc_forget adguardhome
  else
    echo "[$HOST_TAG]   конфиг уже такой, как нужно"
  fi
fi
patch_agh_config "$AGH_CFG"
entry_adguard_resources "$AGH_CFG" "${agh_fresh[@]}" >/dev/null

# Вариант без фильтра: отдельный loopback-слушатель, отдельные данные.
# Берём только DNS-транспорт и кеш; списки, rewrites и настройки клиентов
# основного экземпляра сюда не переносятся.
AGH_PLAIN_DIR=/opt/AdGuardHome-unfiltered
mkdir -p "$AGH_PLAIN_DIR"
[[ ! -f "$AGH_PLAIN_DIR/AdGuardHome.yaml" ]] || cp "$AGH_PLAIN_DIR/AdGuardHome.yaml" "$AGH_PLAIN_CFG"
plain_sync=$(python3 - "$AGH_CFG" "$AGH_PLAIN_CFG" "$AGH_PORT" <<'PYEOF'
import copy, sys, yaml
with open(sys.argv[1]) as f:
    filtered = yaml.safe_load(f)
dns_keys = ("upstream_dns", "upstream_dns_file", "bootstrap_dns", "fallback_dns",
            "upstream_mode", "cache_size", "cache_ttl_min", "cache_ttl_max",
            "cache_optimistic", "aaaa_disabled", "edns_client_subnet",
            "use_http3_upstreams")
cfg = {
    "http": {"address": "127.0.0.2:" + sys.argv[3]},
    "users": filtered["users"],
    "dns": {k: filtered["dns"][k] for k in dns_keys if k in filtered["dns"]},
    "filtering": {"protection_enabled": False, "filtering_enabled": False,
                  "parental_enabled": False, "safebrowsing_enabled": False,
                  "safe_search": {"enabled": False}},
    "filters": [], "whitelist_filters": [], "user_rules": [],
    "clients": {"persistent": []},
    "schema_version": 29,
}
# Журнал в памяти - по тому же ресурсному профилю entry, что и основной.
if "size_memory" in (filtered.get("querylog") or {}):
    cfg["querylog"] = {"size_memory": filtered["querylog"]["size_memory"]}
cfg["dns"].update(bind_hosts=["127.0.0.2"], port=53, ratelimit=0)
try:
    with open(sys.argv[2]) as f:
        before = yaml.safe_load(f)
except FileNotFoundError:
    before = {}
# Не откатываем миграцию схемы, которую выполнил сам AdGuard.
cfg["schema_version"] = max(cfg["schema_version"], before.get("schema_version", 0))
# AdGuard дописывает значения по умолчанию. Сравниваем только управляемые
# поля, сохраняя остальные, иначе повторный деплой всегда давал бы рестарт.
def merge(have, want):
    for key, value in want.items():
        if isinstance(value, dict):
            merge(have.setdefault(key, {}), value)
        else:
            have[key] = value
after = copy.deepcopy(before)
merge(after, cfg)
if after != before:
    with open(sys.argv[2], "w") as f:
        yaml.safe_dump(after, f, allow_unicode=True, sort_keys=False)
    print("changed")
PYEOF
)
chmod 600 "$AGH_PLAIN_CFG"
[[ "$plain_sync" != changed ]] || svc_forget adguardhome-unfiltered

# AdGuard сохраняет YAML при остановке. Сначала готовим патч на копии,
# затем останавливаем только изменившийся экземпляр и публикуем файл.
publish_agh_config() {
  local unit=$1 staged=$2 target=$3
  if ! cmp -s "$staged" "$target"; then
    if systemctl is-active --quiet "$unit"; then
      systemctl stop "$unit"
    fi
    install -m600 "$staged" "$target"
    svc_forget "$unit"
  fi
}
publish_agh_config adguardhome "$AGH_CFG" "$AGH_DIR/AdGuardHome.yaml"
publish_agh_config adguardhome-unfiltered "$AGH_PLAIN_CFG" "$AGH_PLAIN_DIR/AdGuardHome.yaml"

# ----------------------------------------------------------------------------
# 4. systemd service
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [4/4] systemd"
# AdGuard создаёт DoH-сокеты локально: клиентский PREROUTING их не видит.
# Только адреса Cloudflare/Google и TCP/443 попадают в redirect sing-box.
# UDP/53 (Яндекс и bootstrap) остаётся прямым. IPv6 Google тоже перехватываем.
cat > /usr/local/sbin/anysda-doh-routing <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
for cmd in iptables ip6tables; do
  "$cmd" -w -t nat -N ANYSDA_DOH 2>/dev/null || true
  "$cmd" -w -t nat -F ANYSDA_DOH
  if [[ "$cmd" == iptables ]]; then
    addresses=(1.1.1.1 8.8.8.8 8.8.4.4)
  else
    addresses=(2001:4860:4860::8888 2001:4860:4860::8844)
  fi
  for address in "${addresses[@]}"; do
    "$cmd" -w -t nat -A ANYSDA_DOH -p tcp -d "$address" --dport 443 -j REDIRECT --to-ports 7899
  done
  "$cmd" -w -t nat -C OUTPUT -j ANYSDA_DOH 2>/dev/null || \
    "$cmd" -w -t nat -A OUTPUT -j ANYSDA_DOH
done
EOF
chmod 755 /usr/local/sbin/anysda-doh-routing
cat > /etc/systemd/system/anysda-doh-routing.service <<'EOF'
[Unit]
Description=DoH upstream routing through sing-box
After=network-online.target ufw.service sing-box.service
Wants=sing-box.service
Before=adguardhome.service adguardhome-unfiltered.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/anysda-doh-routing

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/adguardhome.service <<EOF
[Unit]
Description=AdGuard Home (anysda-vpn DNS)
After=network-online.target anysda-doh-routing.service
Wants=network-online.target
Requires=anysda-doh-routing.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/AdGuardHome
ExecStart=/opt/AdGuardHome/AdGuardHome --no-check-update --config /opt/AdGuardHome/AdGuardHome.yaml --work-dir /opt/AdGuardHome
Restart=on-failure
RestartSec=5s
LimitNOFILE=65536
# Go начинает GC до cgroup pressure; запас до MemoryMax нужен при обновлении
# фильтров (старый и новый индексы кратковременно живут одновременно).
Environment=GOMEMLIMIT=${AGH_GO_LIMIT}
MemoryAccounting=true
MemoryHigh=${AGH_MEMORY_HIGH}
MemoryMax=${AGH_MEMORY_MAX}
MemorySwapMax=${AGH_SWAP_MAX}

[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/adguardhome-unfiltered.service <<EOF
[Unit]
Description=AdGuard Home (DNS without filters)
After=network-online.target anysda-doh-routing.service
Requires=anysda-doh-routing.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/AdGuardHome-unfiltered
ExecStart=/opt/AdGuardHome/AdGuardHome --no-check-update --config /opt/AdGuardHome-unfiltered/AdGuardHome.yaml --work-dir /opt/AdGuardHome-unfiltered
Restart=on-failure
RestartSec=5s
LimitNOFILE=65536
# Go начинает GC до cgroup pressure; запас до MemoryMax нужен при обновлении
# фильтров (старый и новый индексы кратковременно живут одновременно).
Environment=GOMEMLIMIT=${AGH_GO_LIMIT}
MemoryAccounting=true
MemoryHigh=${AGH_MEMORY_HIGH}
MemoryMax=${AGH_MEMORY_MAX}
MemorySwapMax=${AGH_SWAP_MAX}

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable anysda-doh-routing adguardhome adguardhome-unfiltered >/dev/null 2>&1
# Восстанавливаем правила также после внешнего сброса firewall.
/usr/local/sbin/anysda-doh-routing
systemctl start anysda-doh-routing
svc_restart_if_changed adguardhome "$AGH_BIN" /etc/systemd/system/adguardhome.service
svc_restart_if_changed adguardhome-unfiltered "$AGH_BIN" /etc/systemd/system/adguardhome-unfiltered.service
agh_listening() { [[ "$(ss -Hlunp 'sport = :53' 2>/dev/null)" == *'"AdGuardHome"'* ]]; }
svc_settle 2 agh_listening
systemctl status adguardhome --no-pager -n 4 | sed -n '1,6p' | HOST_TAG="$HOST_TAG" awk '{print "[" ENVIRON["HOST_TAG"] "]   " $0}'

# AdGuard слушает 127.0.0.1 и ${MGMT_IP} (lo-алиас на самой entry) — оба адреса
# локальны, наружу не торчат. Клиенты WG/OpenVPN/IKEv2 спрашивают AdGuard
# напрямую (DNS 10.99.0.1 в их конфигах, sing-box этот трафик не трогает);
# порт для подсетей туннелей открывают стадии 27/28/29. Здесь только снимаем
# legacy mesh-правила ufw (WG-mesh снят).
ufw delete allow proto udp from 10.99.0.0/24 to "${MGMT_IP}" port 53 >/dev/null 2>&1 || true
ufw delete allow proto tcp from 10.99.0.0/24 to "${MGMT_IP}" port 53 >/dev/null 2>&1 || true

mkdir -p "$STAMP_DIR"
touch "$STAMP_DIR/$STAGE"
echo "[$HOST_TAG] $STAGE done — веб-интерфейс AdGuard открывает панель по /adguard/"
