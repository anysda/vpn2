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
# Ожидание блокировок apt с понятной строкой в лог (infra/lib/anysda-svc.sh):
# деплой кладёт библиотеку рядом со стадией, anysda-restore зовёт стадию из клона репо.
SVC_LIB=$(dirname "$0")/anysda-svc.sh
[[ -f "$SVC_LIB" ]] || SVC_LIB=$(dirname "$0")/../lib/anysda-svc.sh
# shellcheck source=infra/lib/anysda-svc.sh
source "$SVC_LIB"

case "$HOST_TAG" in ru) ;; *) echo "[$HOST_TAG] 25-monitoring is ru-only — skipping"; exit 0;; esac

STAMP_DIR=/var/anysda/.stamps
STAGE='25-monitoring'

# ----------------------------------------------------------------------------
# 1. Docker (shared with stage 30)
# ----------------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  echo "[$HOST_TAG] устанавливаю docker"
  export DEBIAN_FRONTEND=noninteractive
  docker_apt_source
  apt_get update -qq
  apt_get install -y -qq "${DOCKER_PKGS[@]}" "${DOCKER_SKIP[@]}" >/dev/null
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
# instance задаём явно = MGMT_IP:9100: панель и бот ищут ноду по нему
# (NUXT_MGMT_IPS в 30-frontend.sh), а по адресу цели все ноды одинаковые.
# ----------------------------------------------------------------------------
mkdir -p /etc/anysda /var/lib/vmsingle

{
  # 2s scrape — чтобы потерю ноды замечать за ~5с, а не за полминуты.
  printf 'global:\n  scrape_interval: 2s\n  scrape_timeout: 1s\n\n'
  printf 'scrape_configs:\n'
  printf '  - job_name: node-ru\n    static_configs:\n'
  printf "      - targets: ['127.0.0.1:9100']\n        labels: { host: ru, instance: '%s:9100' }\n" "${MGMT_IP_RU:-10.99.0.1}"
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
    printf "      - targets: ['127.0.0.1:9100']\n        labels: { host: %s, instance: '%s:9100' }\n" "$_t" "$_ip"
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
# /etc/timezone не монтируем: в Ubuntu 26.04 его нет, docker создаёт на его
# месте каталог и контейнер не стартует. Время берут из /etc/localtime.
[[ -d /etc/timezone ]] && rmdir /etc/timezone 2>/dev/null || true
docker run -d \
  --name vmsingle \
  --restart unless-stopped \
  --network host \
  --security-opt apparmor=unconfined \
  -v /etc/localtime:/etc/localtime:ro \
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
# Ждём первый опрос всех целей (scrape_interval 2s), не дольше прежних 5 с.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  _t=$(curl -sS http://127.0.0.1:8428/api/v1/targets 2>/dev/null || true)
  [[ "$_t" == *'"health":"'* && "$_t" != *'"health":"unknown"'* ]] && break
  sleep 0.5
done
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
