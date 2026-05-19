#!/usr/bin/env bash
# Stage 99 — smoke-проверка всей системы после деплоя.

set -euo pipefail

[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}" "${MGMT_IP:?}"

echo "[$HOST_TAG] verify — basic checks"
echo

# --- sysctl
echo "[$HOST_TAG] sysctl net.ipv4.ip_forward: $(sysctl -n net.ipv4.ip_forward)"

# --- ufw
echo "[$HOST_TAG] ufw status: $(ufw status | head -1)"

# --- fail2ban
echo "[$HOST_TAG] fail2ban: $(systemctl is-active fail2ban)"

# --- node_exporter
NE_STATUS=$(systemctl is-active node_exporter 2>/dev/null || echo 'inactive')
echo "[$HOST_TAG] node_exporter: $NE_STATUS"

# --- mgmt mesh (если поднят — пингуем все остальные ноды)
if ip link show wgmgmt >/dev/null 2>&1; then
  echo "[$HOST_TAG] wgmgmt:        UP, address $(ip -4 -o addr show wgmgmt | awk '{print $4}')"

  # Собираем список MGMT IP всех нод из env (MGMT_IP_RU + MGMT_IP_{TAG} для каждого exit)
  _ALL_PEERS="${MGMT_IP_RU:-10.99.0.1}"
  for _t in ${EXIT_TAGS:-}; do
    _T=$(echo "$_t" | tr a-z A-Z)
    _var="MGMT_IP_${_T}"
    _ip="${!_var:-}"
    [[ -n "$_ip" ]] && _ALL_PEERS="$_ALL_PEERS $_ip"
  done

  for peer_ip in $_ALL_PEERS; do
    [[ "$peer_ip" == "$MGMT_IP" ]] && continue
    if ping -c1 -W2 "$peer_ip" >/dev/null 2>&1; then
      echo "[$HOST_TAG]   peer $peer_ip: reachable"
    else
      echo "[$HOST_TAG]   peer $peer_ip: UNREACHABLE"
    fi
  done
else
  echo "[$HOST_TAG] wgmgmt:        ещё не настроен (запусти стадию 05)"
fi

# --- host-specific
case "$HOST_TAG" in
  ru)
    if curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:80/ 2>/dev/null; then
      echo "[$HOST_TAG] frontend:    127.0.0.1:80 responds (via Caddy)"
    else
      echo "[$HOST_TAG] frontend:    не запущен (стадия 30 не применена)"
    fi
    if curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8428/-/healthy 2>/dev/null; then
      echo "[$HOST_TAG] vmsingle:    127.0.0.1:8428 healthy"
    else
      echo "[$HOST_TAG] vmsingle:    не запущен (стадия 25 не применена)"
    fi
    if systemctl is-active --quiet sing-box 2>/dev/null; then
      echo "[$HOST_TAG] sing-box:    active (router)"
    else
      echo "[$HOST_TAG] sing-box:    неактивен (стадия 20 не применена)"
    fi
    ;;
  *)
    # Любая выходная нода
    if systemctl is-active --quiet sing-box 2>/dev/null; then
      echo "[$HOST_TAG] sing-box:    active"
    else
      echo "[$HOST_TAG] sing-box:    неактивен (стадия 10 не применена)"
    fi
    # Проверяем порты Hysteria2
    _dp="${HY2_DIRECT_PORT:-443}"
    _wp="${HY2_WARP_PORT:-8443}"
    echo "[$HOST_TAG] UDP ports (Hysteria2):"
    ss -lun 2>/dev/null \
      | awk -v dp="$_dp" -v wp="$_wp" '$5 ~ ":"dp"$" || $5 ~ ":"wp"$" {print}' \
      | sed "s/^/[$HOST_TAG]   /"
    ;;
esac

echo "[$HOST_TAG] verify done"
