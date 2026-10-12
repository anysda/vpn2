#!/usr/bin/env bash
# Stage 99 — обязательные smoke-проверки роли; любой отказ валит деплой.
set -Eeuo pipefail

[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}" "${MGMT_IP:?}"
trap 'echo "[$HOST_TAG] verify: ошибка команды в строке $LINENO" >&2' ERR

FAILURES=0
fail() {
  echo "[$HOST_TAG] ✗ $*" >&2
  FAILURES=$((FAILURES + 1))
}
check() {
  local label=$1
  shift
  if "$@"; then
    echo "[$HOST_TAG] ✓ $label"
  else
    fail "$label"
  fi
}
service() { systemctl is-active --quiet "$1"; }
forwarding() { local value; value=$(sysctl -n net.ipv4.ip_forward) && [[ "$value" == 1 ]]; }
firewall() { LC_ALL=C ufw status | grep -x 'Status: active' >/dev/null; }
udp() { ss -H -lun "sport = :$1" | grep . >/dev/null; }
tproxy_rule() { ip rule list | grep -E 'fwmark 0x42 lookup 101([[:space:]]|$)' >/dev/null; }
tproxy_route() { ip route show table 101 | grep -E '^local default dev lo([[:space:]]|$)' >/dev/null; }
ikev2_conn() { swanctl --list-conns | grep '^anysda-ikev2:' >/dev/null; }

echo "[$HOST_TAG] verify — basic checks"
check 'sysctl net.ipv4.ip_forward = 1' forwarding
check 'ufw active' firewall
check 'fail2ban active' service fail2ban
check 'node_exporter active' service node_exporter
check 'sing-box active' service sing-box

if [[ "$HOST_TAG" == ru ]]; then
  # Один снимок API: пустой список и отсутствующий ожидаемый host — тоже отказ.
  if targets=$(curl -fsS --max-time 5 http://127.0.0.1:8428/api/v1/targets) \
     && jq -e '.status == "success" and (.data.activeTargets | type == "array" and length > 0)' <<<"$targets" >/dev/null; then
    check 'mgmt scrape: все targets up' jq -e \
      'all(.data.activeTargets[]; .health == "up")' <<<"$targets"
    for tag in ru ${EXIT_TAGS:-}; do
      check "mgmt scrape: $tag присутствует и up" jq -e --arg host "$tag" \
        'any(.data.activeTargets[]; .labels.host == $host and .health == "up")' <<<"$targets"
    done
  else
    fail 'mgmt scrape: VictoriaMetrics недоступен или список targets пуст/некорректен'
  fi
  for tag in ${EXIT_TAGS:-}; do
    check "ssh $tag через hy2-mgmt" ssh -n -o BatchMode=yes -o ConnectTimeout=15 "$tag" true
  done
  check 'frontend: 127.0.0.1:80 отвечает через Caddy' curl -fsS --max-time 5 -o /dev/null http://127.0.0.1:80/
  check 'vmsingle: 127.0.0.1:8428 healthy' curl -fsS --max-time 5 -o /dev/null http://127.0.0.1:8428/-/healthy
  check 'tproxy: правило fwmark 0x42 → table 101' tproxy_rule
  check 'tproxy: local default dev lo в table 101' tproxy_route
  if queue=$(ss -H -uln 'sport = :7898' | awk '!f && $4 ~ /^127\.0\.0\.1:/ {print $2; f=1}') && [[ -n "$queue" ]]; then
    echo "[$HOST_TAG] tproxy udp: Recv-Q $queue (устойчивый рост означает, что sing-box не читает UDP)"
  else
    fail 'tproxy udp: нет сокета 127.0.0.1:7898 или ss завершился ошибкой'
  fi
  check 'tproxy wd: таймер active' service anysda-tproxy-watchdog.timer
  check 'networkd: drop-in 60-anysda-foreign.conf существует' test -f /etc/systemd/networkd.conf.d/60-anysda-foreign.conf

  # Выключенный YouTube — штатный пропуск; включённый требует A/B-проверку.
  if [[ "${YT_ROUTE:-off}" == zapret ]]; then
    check 'yt-zapret: A/B-проверка обхода' /usr/local/bin/anysda-yt-check
    # Запасной маршрут: при сбое видеопробы YouTube уходит на VPN.
    check 'yt-failover: selector доступен' /usr/local/bin/anysda-yt-failover --status
    check 'yt-failover: таймер active' service anysda-yt-failover.timer
  else
    echo "[$HOST_TAG] yt-zapret: пропущен (youtube.route=${YT_ROUTE:-off})"
  fi
  for svc in wg-quick@wg0 openvpn-server@server strongswan-starter anysda-ikev2-routing; do
    check "$svc active" service "$svc"
  done
  check 'ikev2: swanctl установлен, conn anysda-ikev2 загружен' ikev2_conn
  check 'ikev2: udp/500 слушает' udp 500
  check 'ikev2: udp/4500 слушает' udp 4500
  check 'ikev2: интерфейс xfrm0 существует' ip link show xfrm0
  check 'ikev2 sync: таймер active' service anysda-ikev2-sync.timer
  # Ошибка чтения/JSON и время из будущего не должны давать свежий статус.
  check 'ikev2 sync: статус обновлён менее 120 секунд назад' python3 -c '
import json, time
with open("/etc/anysda/ikev2-status.json") as status_file:
    age = time.time() - json.load(status_file)["updated"]
print(f"ikev2 status age: {age:.0f}s")
raise SystemExit(0 if 0 <= age < 120 else 1)
'
else
  if [[ -n "${HY2_MGMT_PORT:-}" ]]; then
    check "hy2-mgmt: udp/$HY2_MGMT_PORT слушает" udp "$HY2_MGMT_PORT"
  else
    fail 'hy2-mgmt: HY2_MGMT_PORT не задан'
  fi
  check "Hysteria2 direct: udp/${HY2_DIRECT_PORT:-443} слушает" udp "${HY2_DIRECT_PORT:-443}"
  check "Hysteria2 warp: udp/${HY2_WARP_PORT:-8443} слушает" udp "${HY2_WARP_PORT:-8443}"
fi

if (( FAILURES > 0 )); then
  echo "[$HOST_TAG] verify FAILED: проверок не пройдено: $FAILURES" >&2
  exit 1
fi
echo "[$HOST_TAG] verify done"
