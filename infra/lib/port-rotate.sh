#!/usr/bin/env bash
# После 20-ru-router: канал hy2-direct каждого экзита меряется через clash-api роутера entry.
# Молчит - UDP-пробник по высоким портам: путь есть - подбирается другой порт hy2,
# пути нет вовсе - экзит исключается из деплоя (excluded-exits.txt в каталоге состояния).
HY2_PORT_CANDIDATES=(443 4443 4444 8443 8444 33445)
UDP_PROBE_PORTS=(33445 39847 41001 51234 60123)

_clash_delay() {
  local secret
  secret=$(cat /etc/anysda/clash-secret.txt 2>/dev/null) || return 1
  curl -sS -m 8 -H "Authorization: Bearer $secret" \
    "http://10.99.0.1:9090/proxies/hy2-$1-direct/delay?timeout=5000&url=http://cp.cloudflare.com/generate_204" \
    2>/dev/null | grep -oE '"delay":[1-9][0-9]*' | grep -oE '[0-9]+$'
}

_env_value() ( source "$DEPLOY_ROOT/envs/all.env"; source "$DEPLOY_ROOT/envs/$1.env"; echo "${!2:-}" )

_on_exit_node() {
  ANSIBLE_CONFIG="$DEPLOY_ROOT/ansible/ansible.cfg" VPN2_CONFIG="$REPO/config.yaml" \
    "$VPN2_ROOT/venv/bin/ansible" "$1" -m ansible.builtin.raw -a "$2" </dev/null >/dev/null 2>&1
}

# 0 - UDP на порт экзита доходит и возвращается, 1 - нет, 4 - на экзит не войти по ssh.
_probe_udp() {
  local tag=$1 host=$2 port=$3 rc=0 answer
  _on_exit_node "$tag" "ufw allow $port/udp >/dev/null 2>&1; nohup timeout 9 python3 -c 'import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind((\"\", $port))
data, addr = s.recvfrom(1024); s.sendto(b\"PONG-\" + data, addr)' </dev/null >/dev/null 2>&1 & sleep 1" || rc=$?
  (( rc != 4 )) || return 4
  answer=$(timeout 5 python3 -c "import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(4)
s.sendto(b'PING-$port', ('$host', $port)); print(s.recvfrom(1024)[0].decode())" 2>/dev/null || true)
  _on_exit_node "$tag" "ufw delete allow $port/udp >/dev/null 2>&1 || true" || true
  [[ "$answer" == "PONG-PING-$port" ]]
}

_set_port() {
  python3 "$DEPLOY_ROOT/lib/config2env.py" "$REPO" --set-port "$1" "$2" >/dev/null
  python3 "$DEPLOY_ROOT/lib/config2env.py" "$REPO" >/dev/null
}

_redeploy() {
  local log=$1; shift
  ( for pair in "$@"; do run_stage "${pair%%:*}" "${pair##*:}"; done ) >"$log" 2>&1 && return 0
  printf '%b    ✗ перекатка упала, полный вывод: %s%b\n' "$C_R" "$log" "$C_END"
  tail -n 5 "$log" | sed 's/^/      /'
  return 1
}

verify_and_rotate_ports() {
  local tag delay current port prc alive tries found t dead=() rotated=false t0 list=$VPN2_STATE_DIR/excluded-exits.txt
  step 'verify: проверяю tunnels via clash-api + UDP-пробник для мертвецов'
  t0=$(date +%s)
  for tag in $(expand_hosts foreign); do
    current=$(_env_value "$tag" HY2_DIRECT_PORT); current=${current:-443}
    if delay=$(_clash_delay "$tag"); then
      printf '  %-16s port=%-5s %bRTT=%sms ✓%b\n' "hy2-$tag-direct" "$current" "$C_G" "$delay" "$C_END"
      continue
    fi
    printf '  %-16s port=%-5s %b×%b\n    UDP-пробник: ' "hy2-$tag-direct" "$current" "$C_Y" "$C_END"
    alive='' prc=1
    for port in "${UDP_PROBE_PORTS[@]}"; do
      prc=0; _probe_udp "$tag" "$(_env_value "$tag" SSH_HOST)" "$port" || prc=$?
      (( prc != 0 )) || alive=$port
      (( prc == 1 )) || break
    done
    if (( prc == 4 )); then
      printf '%bсбой входа по ssh на экзит %s — не проверен, не исключаю, порт не меняю%b\n' "$C_R" "$tag" "$C_END"
      continue
    elif [[ -z "$alive" ]]; then
      printf '%bвсе 5 портов фильтруются → exit МЁРТВ, исключаю%b\n' "$C_R" "$C_END"
      dead+=("$tag")
      continue
    fi
    printf '%bUDP проходит на :%s%b — пробую hy2 rotation\n' "$C_G" "$alive" "$C_END"
    tries=0 found=false
    for port in "${HY2_PORT_CANDIDATES[@]}"; do
      [[ "$port" != "$current" ]] || continue
      (( ++tries <= 2 )) || break
      printf '    → пробую hy2 :%s ' "$port"
      _set_port "$tag" "$port"
      _redeploy "$VPN2_STATE_DIR/rotate-$tag-$port.log" "10-foreign:$tag" 20-ru-router:ru || continue
      sleep 4
      if delay=$(_clash_delay "$tag"); then
        printf '%bRTT=%sms ✓%b\n' "$C_G" "$delay" "$C_END"; found=true rotated=true
        break
      fi
      printf '%b×%b\n' "$C_R" "$C_END"
    done
    "$found" && continue
    printf '  %b⚠ hy2 rotation не подобрала рабочий порт для %s (UDP-путь есть, но\n' "$C_Y" "$tag"
    printf '    hy2-кандидаты режутся). Откатываю порт на дефолт, оставляю как direct-dead.%b\n' "$C_END"
    _set_port "$tag" "$(_env_value ru HY2_DIRECT_PORT)"
    _redeploy "$VPN2_STATE_DIR/rotate-$tag-rollback.log" "10-foreign:$tag" 20-ru-router:ru || true
  done
  if (( ${#dead[@]} )); then
    [[ "${dead[*]}" != "$(expand_hosts foreign)" ]] || die 'verify_and_rotate_ports: все экзиты недостижимы по UDP с RU направления'
    { echo '# Экзиты, исключённые проверкой после 20-ru-router: UDP с entry до них не доходит.'
      echo '# Чтобы попробовать снова - удалить тег (или весь файл) и перезапустить ./deploy.sh.'
      printf '%s\n' "${dead[@]}"; } > "$list"
    chmod 644 "$list"
    printf '\n  %b⚠ Полностью недостижимые экзиты исключены из деплоя: %s%b\n    Записано в %s\n' "$C_Y" "${dead[*]}" "$C_END" "$list"
    python3 "$DEPLOY_ROOT/lib/config2env.py" "$REPO" >/dev/null
    _redeploy "$VPN2_STATE_DIR/excluded-reroute.log" 20-ru-router:ru \
      || die 'verify_and_rotate_ports: entry не пересобран после исключения экзитов'
  else
    rm -f "$list"
  fi
  t=$(( $(date +%s) - t0 )); t="$(( t / 60 ))m $(( t % 60 ))s"; t=${t#0m }
  ! "$rotated" || _STAGE_LOG+=($'[port-rotate]\tforeign\t'"$t")
  (( ! ${#dead[@]} )) || _STAGE_LOG+=($'[excluded]\t'"${dead[*]}"$'\t'"$t")
  echo
}
