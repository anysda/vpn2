#!/usr/bin/env bash
# Stage 25 — VictoriaMetrics single-node на RU.
# Скрейп node_exporter: entry — локально (127.0.0.1:9100), экзиты — через
# служебный Hysteria2-туннель. WG-mesh через границу блокируется, поэтому VM
# ходит к экзиту как через SOCKS-прокси (proxy_url: socks5://127.0.0.1:<port>),
# который sing-box (стадия 20, инбаунд mon-{tag}) заворачивает в hy2-{tag}-mgmt
# и бьёт в 127.0.0.1:9100 на экзите. См. docs/mgmt-over-hysteria2-design.md.
# Зависит от: 00-bootstrap (node_exporter), 20-ru-router (mon-инбаунды).

set -euo pipefail

[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}" "${EXIT_TAGS:?}"

case "$HOST_TAG" in ru) ;; *) echo "[$HOST_TAG] 25-monitoring is ru-only — skipping"; exit 0;; esac

STAMP_DIR=/var/anysda/.stamps
STAGE='25-monitoring'

# ----------------------------------------------------------------------------
# 1. Docker (shared with stage 30)
# ----------------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  echo "[$HOST_TAG] устанавливаю docker"
  export DEBIAN_FRONTEND=noninteractive
  apt-get install -y -qq ca-certificates curl >/dev/null
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
  systemctl enable --now docker >/dev/null
else
  echo "[$HOST_TAG] docker уже установлен: $(docker --version)"
fi

# ----------------------------------------------------------------------------
# 2. VictoriaMetrics scrape config (динамический по EXIT_TAGS + MGMT_IP_*)
#
# entry — прямой скрейп по loopback. Каждый экзит — отдельный job со своим
# proxy_url: socks5://127.0.0.1:<mon_port>; mon_port выводится из последнего
# октета MGMT_IP_{T} той же формулой, что и в gen-router-config.py (инбаунд
# mon-{tag}), чтобы стороны сошлись без общего конфига. Цель у всех экзитов —
# 127.0.0.1:9100: это loopback НА ЭКЗИТЕ, куда туннель приводит запрос.
# ----------------------------------------------------------------------------
mkdir -p /etc/anysda /var/lib/vmsingle

{
  # 2s scrape — чтобы потерю ноды замечать за ~5с, а не за полминуты.
  printf 'global:\n  scrape_interval: 2s\n  scrape_timeout: 1s\n\n'
  printf 'scrape_configs:\n'
  printf '  - job_name: node-ru\n    static_configs:\n'
  printf "      - targets: ['127.0.0.1:9100']\n        labels: { host: ru }\n"
  for _t in $EXIT_TAGS; do
    _T=$(echo "$_t" | tr a-z A-Z)
    _var="MGMT_IP_${_T}"
    _ip="${!_var:-}"
    if [[ -z "$_ip" ]]; then
      echo "[$HOST_TAG] ВНИМАНИЕ: MGMT_IP_${_T} не задан — ${_t} пропущен в scrape config" >&2
      continue
    fi
    _mon_port=$(( 10100 + ${_ip##*.} ))
    printf '  - job_name: node-%s\n' "$_t"
    printf "    proxy_url: 'socks5://127.0.0.1:%s'\n" "$_mon_port"
    printf '    static_configs:\n'
    printf "      - targets: ['127.0.0.1:9100']\n        labels: { host: %s }\n" "$_t"
  done
} > /etc/anysda/vmsingle-scrape.yml

echo "[$HOST_TAG] scrape targets:"
grep 'targets:' /etc/anysda/vmsingle-scrape.yml | sed "s/^/[$HOST_TAG]   /"

# ----------------------------------------------------------------------------
# 3. vmsingle container (idempotent recreate)
# latencyOffset=1s: дефолт VM — 30с, инстант-запросы тогда смотрят на now-30с
# и панель видит метрики с задержкой полминуты (мёртвая нода «живёт» ~40с).
# ----------------------------------------------------------------------------
docker rm -f vmsingle >/dev/null 2>&1 || true
docker run -d \
  --name vmsingle \
  --restart unless-stopped \
  --network host \
  --security-opt apparmor=unconfined \
  -v /etc/localtime:/etc/localtime:ro \
  -v /etc/timezone:/etc/timezone:ro \
  -v /var/lib/vmsingle:/storage \
  -v /etc/anysda/vmsingle-scrape.yml:/etc/vm/scrape.yml:ro \
  victoriametrics/victoria-metrics:latest \
  -storageDataPath=/storage \
  -httpListenAddr=127.0.0.1:8428 \
  -retentionPeriod=7d \
  -search.latencyOffset=1s \
  -promscrape.config=/etc/vm/scrape.yml \
  >/dev/null

# ----------------------------------------------------------------------------
# 4. Проверка targets
# ----------------------------------------------------------------------------
sleep 5
echo "[$HOST_TAG] scrape health:"
if command -v jq >/dev/null 2>&1; then
  curl -sS http://127.0.0.1:8428/api/v1/targets \
    | jq -r '.data.activeTargets[] | "  " + (.labels.host // "?") + " (" + .scrapeUrl + "): " + .health'
else
  curl -sS http://127.0.0.1:8428/api/v1/targets \
    | grep -oE '"health":"[a-z]+"' | sort | uniq -c | sed "s/^/  /"
fi

mkdir -p "$STAMP_DIR"
touch "$STAMP_DIR/$STAGE"
echo "[$HOST_TAG] $STAGE done"
