#!/usr/bin/env bash
# Stage 99 — smoke-проверки роли: 0 успешно, 1 отказ, 2 не проверено.
set -Eeuo pipefail

[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}" "${MGMT_IP:?}"
trap 'echo "[$HOST_TAG] verify: ошибка команды в строке $LINENO" >&2' ERR

FAILURES=0
UNMEASURED=0
# Строки отчёта (JSON Lines): проверка, компонент, результат, причина, время.
# Итог для CI собирается в VERIFY_REPORT; секретов в строках нет.
VERIFY_REPORT=${VERIFY_REPORT:-/var/lib/anysda/verify-report.json}
VERIFY_ROWS=$(mktemp)
# Предупреждения не меняют код, но идут в итог и в отчёт при любом исходе.
VERIFY_WARNINGS=$(mktemp)
trap 'rm -f -- "$VERIFY_ROWS" "$VERIFY_WARNINGS"' EXIT
record() {
  python3 -c '
import datetime, json, sys
label, result, reason = sys.argv[1:4]
component, _, check = label.partition(": ")
if not check:
    component, check = sys.argv[4], label
print(json.dumps({"check": check, "component": component, "result": result, "reason": reason,
                  "time": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                 ensure_ascii=False))
' "$1" "$2" "$3" "$HOST_TAG" >> "$VERIFY_ROWS"
}
fail() {
  echo "[$HOST_TAG] ✗ $1" >&2
  record "$1" Ошибка "${2:-проверка не прошла}"
  FAILURES=$((FAILURES + 1))
}
check() {
  local allow_unmeasured=0
  if [[ "$1" == --allow-unmeasured ]]; then allow_unmeasured=1; shift; fi
  local label=$1 output rc=0
  shift
  output=$("$@" 2>&1) || rc=$?
  [[ -z "$output" ]] || printf '%s\n' "$output"
  case $rc in
    0) echo "[$HOST_TAG] ✓ $label"; record "$label" Работает 'проверка прошла' ;;
    2)
      if [[ $allow_unmeasured -eq 0 ]]; then fail "$label" "код $rc"; return; fi
      output=${output:-команда завершилась с кодом 2}
      echo "[$HOST_TAG] ? $label — не проверено: ${output##*$'\n'}"
      record "$label" 'Не проверено' "${output##*$'\n'}"
      UNMEASURED=$((UNMEASURED + 1)) ;;
    *) fail "$label" "код $rc${output:+: ${output##*$'\n'}}" ;;
  esac
}
warning() {
  echo "[$HOST_TAG] ⚠ $1" >&2
  printf '%s\n' "$1" >> "$VERIFY_WARNINGS"
}
# exits[].hy2_insecure: true - туннель до экзита без проверки сертификата.
insecure_exits() {
  local tag var
  for tag in ${EXIT_TAGS:-}; do
    var="$(printf '%s' "$tag" | tr 'a-z-' 'A-Z_')_HY2_INSECURE"
    if [[ "${!var:-}" == 1 ]]; then
      warning "$tag: exits[].hy2_insecure: true — сертификат экзита не проверяется, подмену экзита на пути entry → экзит не видно"
    fi
  done
}
service() { systemctl is-active --quiet "$1"; }
# Аварийка ставится стадией 23 из образа: версия образа и всё, что нужно
# активации, лежат на узле заранее (docker для проверки не будим).
emergency_image() {
  python3 - /opt/vpn2-emergency <<'PY'
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
release = json.loads((root / 'release.json').read_text())
print(f"аварийный образ: версия {release['version']}, ревизия {release['revision'][:12]}, {release['image'][:19]}")
package = root / 'vpn2_emergency'
missing = [name for name in ('agent.py', 'web.py', 'hy2.py', 'static/index.html', 'static/instructions.html')
           if not (package / name).is_file()]
if missing:
    print('нет файлов аварийки: ' + ', '.join(missing))
    raise SystemExit(1)
PY
}
# Аварийный Hysteria2 ждёт выключенным: его порт никто не слушает.
emergency_standby() {
  local port listeners
  port=$(python3 -c 'import json, sys; print(int(json.load(open(sys.argv[1]))["port"]))' /etc/vpn2/emergency-hy2.json) || return
  listeners=$(ss -H -uln "sport = :$port") || return
  [[ -z "$listeners" ]] || { echo "udp/$port уже слушает"; return 1; }
  echo "аварийный Hysteria2: udp/$port закрыт"
}
forwarding() { local value; value=$(sysctl -n net.ipv4.ip_forward) && [[ "$value" == 1 ]]; }
firewall() {
  LC_ALL=C ufw status | grep -x 'Status: active' >/dev/null \
    && iptables -C INPUT -j ufw-before-input \
    && iptables -S ufw-user-input >/dev/null
}
tproxy_chains() {
  local iface=$1 chain=$2 proto
  iptables -t mangle -C PREROUTING -i "$iface" -j "$chain" || return
  for proto in tcp udp; do
    iptables -t mangle -C "$chain" -p "$proto" -j TPROXY \
      --tproxy-mark 0x42/0x42 --on-port 7898 --on-ip 127.0.0.1 || return
  done
  iptables -C INPUT -m mark --mark 0x42/0x42 -j ACCEPT
}
udp() { ss -H -lun "sport = :$1" | grep . >/dev/null; }
tproxy_rule() { ip rule list | grep -E 'fwmark 0x42 lookup 101([[:space:]]|$)' >/dev/null; }
tproxy_route() { ip route show table 101 | grep -E '^local default dev lo([[:space:]]|$)' >/dev/null; }
ikev2_conn() { swanctl --list-conns | grep '^anysda-ikev2:' >/dev/null; }
ikev2_eap_plugin() {
  # Статистика живого charon, а не наличие .so или загрузка плагина самим CLI.
  swanctl --stats | sed -n 's/^[[:space:]]*loaded plugins: *//p' \
    | tr ' ' '\n' | grep -Fx eap-mschapv2
}

mgmt_scrape() {
  local since deadline remaining targets detail='API targets недоступен'
  since=$(date +%s.%N)
  deadline=$((SECONDS + ${VERIFY_SCRAPE_TIMEOUT:-30}))
  echo "[$HOST_TAG] mgmt scrape: жду свежие targets up (до ${VERIFY_SCRAPE_TIMEOUT:-30} с)"
  while (( SECONDS < deadline )); do
    remaining=$((deadline - SECONDS))
    (( remaining <= 5 )) || remaining=5
    if targets=$(curl -fsS --max-time "$remaining" http://127.0.0.1:8428/api/v1/targets); then
      if detail=$(python3 -c '
import datetime, json, sys, time
since = float(sys.argv[1])
expected = set(sys.argv[2].split())
try:
    data = json.load(sys.stdin)
    targets = data["data"]["activeTargets"]
    if data["status"] != "success" or not isinstance(targets, list) or not targets:
        raise ValueError("пустой/некорректный список targets")
    seen, failures = set(), []
    for target in targets:
        host = target["labels"].get("host", "?")
        seen.add(host)
        stamp = datetime.datetime.fromisoformat(target["lastScrape"].replace("Z", "+00:00"))
        fresh = stamp.tzinfo is not None and since <= stamp.timestamp() <= time.time()
        health = target["health"]
        if health != "up" or not fresh:
            failures.append(f"{host}: health={health}, свежий={fresh}")
    failures.extend(f"{host}: отсутствует" for host in sorted(expected - seen))
    if failures:
        raise ValueError("; ".join(failures))
except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
    print(f"targets: {error}")
    sys.exit(1)
' "$since" "ru ${EXIT_TAGS:-}" <<<"$targets"); then
        echo "[$HOST_TAG] mgmt scrape: все targets up, скрейпы свежее начала проверки"
        return 0
      fi
    else
      detail='API targets недоступен'
    fi
    (( SECONDS < deadline )) && sleep "${VERIFY_SCRAPE_INTERVAL:-1}"
  done
  echo "[$HOST_TAG] mgmt scrape: таймаут — $detail" >&2
  return 1
}

# Настоящий трафик: временный клиент WireGuard/OpenVPN/IKEv2 в своём netns.
# Строки отчёта модуль печатает и дописывает сам; код 0/1/2 как у стадии.
traffic_check() {
  local module=${VERIFY_TRAFFIC:-} rc=0 dir rows failed unmeasured
  dir=$(dirname "${BASH_SOURCE[0]}")
  if [[ -z "$module" ]]; then
    for module in "$dir/verify-traffic.py" "$dir/../lib/verify-traffic.py"; do
      [[ ! -f "$module" ]] || break
    done
  fi
  if [[ ! -f "$module" ]]; then
    fail 'трафик: контрольный клиент' 'модуль verify-traffic.py не найден'
    return
  fi
  echo "[$HOST_TAG] трафик: контрольный клиент в netns (до нескольких минут)"
  rows=$(wc -l < "$VERIFY_ROWS")
  python3 -I "$module" check --tag "$HOST_TAG" --exits "${EXIT_TAGS:-}" \
    --public-host "${ENTRY_HOST:-}" --report "$VERIFY_ROWS" || rc=$?
  # Итог считает строки модуля, а не один его код: три «Ошибка» - три провала.
  failed=$(tail -n "+$((rows + 1))" "$VERIFY_ROWS" | grep -c '"result": "Ошибка"' || true)
  unmeasured=$(tail -n "+$((rows + 1))" "$VERIFY_ROWS" | grep -c '"result": "Не проверено"' || true)
  case $rc in
    0) ;;
    2) UNMEASURED=$((UNMEASURED + (unmeasured > 0 ? unmeasured : 1))) ;;
    *) FAILURES=$((FAILURES + (failed > 0 ? failed : 1)))
       UNMEASURED=$((UNMEASURED + unmeasured))
       # Модуль упал до своих строк: без строки отчёт выглядел бы чистым.
       if (( $(wc -l < "$VERIFY_ROWS") <= rows )); then
         echo "[$HOST_TAG] ✗ трафик: контрольный клиент — код $rc"
         record 'трафик: контрольный клиент' Ошибка "код $rc"
       fi ;;
  esac
}

write_report() {
  local code=0
  (( UNMEASURED == 0 )) || code=2
  (( FAILURES == 0 )) || code=1
  if mkdir -p "$(dirname "$VERIFY_REPORT")" && python3 -c '
import datetime, json, os, sys
rows = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
warnings = [line.rstrip("\n") for line in open(sys.argv[5], encoding="utf-8") if line.strip()]
report = {"host": sys.argv[2], "code": int(sys.argv[3]), "checks": rows, "warnings": warnings,
          "time": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
tmp = sys.argv[4] + ".tmp"
with open(tmp, "w", encoding="utf-8") as out:
    json.dump(report, out, ensure_ascii=False, indent=1)
os.replace(tmp, sys.argv[4])
counts = {}
for row in rows:
    counts[row["result"]] = counts.get(row["result"], 0) + 1
print(f"[{sys.argv[2]}] отчёт: {sys.argv[4]} — " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
' "$VERIFY_ROWS" "$HOST_TAG" "$code" "$VERIFY_REPORT" "$VERIFY_WARNINGS"; then
    :
  else
    echo "[$HOST_TAG] ✗ отчёт: не записан $VERIFY_REPORT" >&2
    FAILURES=$((FAILURES + 1))
  fi
}

echo "[$HOST_TAG] verify — basic checks"
check 'sysctl net.ipv4.ip_forward = 1' forwarding
check 'ufw active' firewall
check 'fail2ban active' service fail2ban
check 'node_exporter active' service node_exporter
check 'sing-box active' service sing-box
# Первый деплой проверяет экзит сразу после 10-foreign, до стадии 23: там
# аварийки ещё нет. Итоговый verify идёт после 23, и она уже обязана быть.
EMERGENCY_STAMP=/var/anysda/.stamps/23-emergency
if [[ -e "$EMERGENCY_STAMP" ]]; then
  check 'аварийка: агент vpn2-emergency active' service vpn2-emergency.service
  check 'аварийка: образ установлен' emergency_image
else
  echo "[$HOST_TAG] аварийка: стадия 23 ещё не ставилась, проверка в итоговом verify"
fi

if [[ "$HOST_TAG" == ru ]]; then
  # После 10/20 первый снимок ещё может относиться к старому туннелю.
  # 25-monitoring задаёт интервал 2 с: 30 с хватает на несколько новых проб.
  check 'mgmt scrape: все ожидаемые targets up после свежего скрейпа' mgmt_scrape
  if [[ ! ${EXIT_TAGS:-} =~ [^[:space:]] ]]; then
    fail 'ssh экзитов: EXIT_TAGS пуст — нет узлов для проверки'
  fi
  for tag in ${EXIT_TAGS:-}; do
    check "ssh $tag через hy2-mgmt" ssh -n -o BatchMode=yes -o ConnectTimeout=15 "$tag" true
  done
  check 'frontend: 127.0.0.1:80 отвечает через Caddy' curl -fsS --max-time 5 -o /dev/null http://127.0.0.1:80/
  check 'vmsingle: 127.0.0.1:8428 healthy' curl -fsS --max-time 5 -o /dev/null http://127.0.0.1:8428/-/healthy
  check 'tproxy: правило fwmark 0x42 → table 101' tproxy_rule
  check 'tproxy: local default dev lo в table 101' tproxy_route
  check 'tproxy: wg0 цепочка и TCP/UDP правила' tproxy_chains wg0 ANYSDA_WG_TPROXY
  check 'tproxy: tun0 цепочка и TCP/UDP правила' tproxy_chains tun0 ANYSDA_OVPN_TPROXY
  check 'tproxy: xfrm0 цепочка и TCP/UDP правила' tproxy_chains xfrm0 ANYSDA_IKEV2_TPROXY
  if queue=$(ss -H -uln 'sport = :7898' | awk '!f && $4 ~ /^127\.0\.0\.1:/ {print $2; f=1}') && [[ -n "$queue" ]]; then
    echo "[$HOST_TAG] tproxy udp: Recv-Q $queue (устойчивый рост означает, что sing-box не читает UDP)"
  else
    fail 'tproxy udp: нет сокета 127.0.0.1:7898 или ss завершился ошибкой'
  fi
  check 'tproxy wd: таймер active' service anysda-tproxy-watchdog.timer
  check 'networkd: drop-in 60-anysda-foreign.conf существует' test -f /etc/systemd/networkd.conf.d/60-anysda-foreign.conf

  # Выключенный YouTube — штатный пропуск; включённый требует A/B-проверку.
  if [[ "${YT_ROUTE:-off}" == zapret ]]; then
    check --allow-unmeasured 'yt-zapret: A/B-проверка обхода' /usr/local/bin/anysda-yt-check
    # Запасной маршрут: при сбое видеопробы YouTube уходит на VPN.
    # Пробу (до 120 с) выполняет таймер; отсутствие свежей пробы — код 2.
    # На чистой установке стадия 19 идёт раньше конфига sing-box, и systemd
    # пропускает пробу по ConditionPathExists — меряем один раз здесь.
    if /usr/local/bin/anysda-yt-failover --status >/dev/null 2>&1; [[ $? == 2 ]]; then
      systemctl start anysda-yt-failover.service >/dev/null 2>&1 || true
    fi
    check --allow-unmeasured 'yt-failover: selector и последняя видеопроба' /usr/local/bin/anysda-yt-failover --status
    check 'yt-failover: таймер active' service anysda-yt-failover.timer
  else
    echo "[$HOST_TAG] yt-zapret: пропущен (youtube.route=${YT_ROUTE:-off})"
  fi
  for svc in wg-quick@wg0 openvpn-server@server strongswan-starter anysda-ikev2-routing; do
    check "$svc active" service "$svc"
  done
  check 'ikev2: swanctl установлен, conn anysda-ikev2 загружен' ikev2_conn
  check 'ikev2: charon загрузил eap-mschapv2' ikev2_eap_plugin
  check 'ikev2: udp/500 слушает' udp 500
  check 'ikev2: udp/4500 слушает' udp 4500
  check 'ikev2: интерфейс xfrm0 существует' ip link show xfrm0
  check 'ikev2 sync: таймер active' service anysda-ikev2-sync.timer
  traffic_check
  # Ошибка чтения/JSON и время из будущего не должны давать свежий статус.
  check 'ikev2 sync: статус обновлён менее 120 секунд назад' python3 -c '
import json, time
with open("/etc/anysda/ikev2-status.json") as status_file:
    age = time.time() - json.load(status_file)["updated"]
print(f"ikev2 status age: {age:.0f}s")
raise SystemExit(0 if 0 <= age < 120 else 1)
'
  insecure_exits
else
  if [[ -n "${HY2_MGMT_PORT:-}" ]]; then
    check "hy2-mgmt: udp/$HY2_MGMT_PORT слушает" udp "$HY2_MGMT_PORT"
  else
    fail 'hy2-mgmt: HY2_MGMT_PORT не задан'
  fi
  check "Hysteria2 direct: udp/${HY2_DIRECT_PORT:-443} слушает" udp "${HY2_DIRECT_PORT:-443}"
  check "Hysteria2 warp: udp/${HY2_WARP_PORT:-8443} слушает" udp "${HY2_WARP_PORT:-8443}"
  [[ ! -e "$EMERGENCY_STAMP" ]] || check 'аварийка: Hysteria2 в ожидании' emergency_standby
fi

write_report

if (( FAILURES > 0 )); then
  echo "[$HOST_TAG] verify FAILED: проверок не пройдено: $FAILURES; не проверено: $UNMEASURED" >&2
  exit 1
fi
if (( UNMEASURED > 0 )); then
  echo "[$HOST_TAG] verify INCOMPLETE: не проверено: $UNMEASURED" >&2
  exit 2
fi
echo "[$HOST_TAG] verify done"
