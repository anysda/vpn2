#!/usr/bin/env bash
# Подготовка деплоя на entry: зависимости, вход на узлы, ключи, секреты экзитов.

install_prereqs() {
  step 'проверяю локальные зависимости'
  local cmds=(ssh sshpass curl python3 openssl) cmd missing=() pkgs=()
  for cmd in "${cmds[@]}"; do
    command -v "$cmd" >/dev/null 2>&1 && continue
    missing+=("$cmd")
    [[ "$cmd" == ssh ]] && pkgs+=(openssh-client) || pkgs+=("$cmd")
  done
  apt_have python3-venv || pkgs+=(python3-venv)
  if ! apt_have python3-wheel && dpkg --compare-versions \
      "$(apt-cache policy python3-setuptools-whl 2>/dev/null | awk '/Candidate:/ {print $2}')" lt 70.1; then
    pkgs+=(python3-wheel)
  fi
  if (( ${#pkgs[@]} )); then
    apt_have ca-certificates || pkgs+=(ca-certificates)
    [[ -f /etc/debian_version ]] || die "не хватает: ${missing[*]}. На не-Debian/Ubuntu поставь руками."
    printf '  ставлю недостающее: %s\n' "${missing[*]}"
    export DEBIAN_FRONTEND=noninteractive
    apt_prepare
    command -v docker >/dev/null 2>&1 || docker_apt_source
    apt_get update -qq
    NEEDRESTART_SUSPEND=1 apt_get install -y -qq --no-install-recommends "${pkgs[@]}" >/dev/null
  fi
  for cmd in "${cmds[@]}"; do printf '  %-10s %b✓%b\n' "$cmd" "$C_G" "$C_END"; done
  ansible_venv_ensure
  VPN2_WHEEL=$(ansible_wheel_path) || die "ansible: колеса vpn2 нет в $VPN2_ROOT/wheels"
  export VPN2_WHEEL
}

# Ключ оркестратора: им entry ходит на экзиты после 00-bootstrap; копия в config.yaml
# переживает пересоздание entry. Ключ entry для ssh на экзиты - отдельный.
prep_keys() {
  local ssh=$HOME/.ssh cfg=$REPO/config.yaml out=$DEPLOY_ROOT/secrets/rendered src
  install -d -m 700 "$ssh" "$out"
  if [[ ! -f "$ssh/id_ed25519" ]]; then
    log "keypair оркестратора отсутствует — генерю ed25519"
    ssh-keygen -t ed25519 -N '' -C 'anysda-orchestrator' -f "$ssh/id_ed25519" -q
  fi
  [[ -f "$ssh/id_ed25519.pub" ]] || ssh-keygen -y -f "$ssh/id_ed25519" > "$ssh/id_ed25519.pub"
  if [[ -f "$cfg" ]] && ! grep -q '^orchestrator_key:' "$cfg"; then
    log "сохраняю orchestrator-ключ в config.yaml (переживёт пересоздание entry)"
    printf '\n%s\n%s\norchestrator_key: %s\norchestrator_pubkey: %s\n' \
      '# orchestrator SSH-ключ — НЕ удалять: нужен для повторного деплоя' \
      '# с пересозданной entry-ноды. config.yaml надо сохранять/переносить.' \
      "$(base64 -w0 < "$ssh/id_ed25519")" "$(cat "$ssh/id_ed25519.pub")" >> "$cfg"
  fi
  cat "$ssh/id_ed25519.pub" >> "$ssh/authorized_keys"
  { cat "$ssh/id_ed25519.pub" "$ssh/id_rsa.pub" "$ssh/authorized_keys" 2>/dev/null || true; } > "$out/orchestrator_keys"
  for src in "$ssh/authorized_keys" "$out/orchestrator_keys"; do
    awk 'NF && !seen[$0]++' "$src" > "$src.u" && mv "$src.u" "$src" && chmod 600 "$src"
  done
  [[ -f /root/.ssh/id_ed25519_mgmt ]] \
    || ssh-keygen -t ed25519 -N '' -q -C anysda-entry-mgmt -f /root/.ssh/id_ed25519_mgmt
  install -m 600 /root/.ssh/id_ed25519_mgmt.pub "$out/entry_mgmt_key"
}

# Перед 20-ru-router: пароли каналов, сертификат Hysteria2, host-key и ключ Reality каждого экзита.
pull_secrets() {
  log "prep: тяну секреты Hysteria2 + clash-api с экзит-нод"
  local dir=$DEPLOY_ROOT/secrets sec cached='' active host upper kept got
  sec=$dir/foreign-secrets.env
  [[ ! -f "$sec" ]] || cached=$(<"$sec")
  install -d -m 700 "$dir" "$dir/exit-certs" "$dir/exit-hostkeys"
  install -m 600 /dev/null "$sec"
  active=$(expand_hosts foreign)
  got=$(mktemp -d -p "$TMP")
  [[ -z "$active" ]] || service --tags secrets --limit "${active// /,}" -e "out=$got" >"$got/log" 2>&1 \
    || { grep -vE '^$|^(PLAY|TASK|ok:|skipping:)' "$got/log" | tail -n 8 >&2 || true; die 'секреты экзитов не получены, стадию 20 не запускаю'; }
  for host in $(exit_tags); do
    if ! grep -qw -- "$host" <<<"$active"; then
      upper=$(tr 'a-z-' 'A-Z_' <<<"$host")
      kept=$(grep -E "^${upper}_(DIRECT|WARP|OBFS|MGMT|CLASH)=" <<<"$cached" || true)
      [[ $(grep -c . <<<"$kept") -eq 5 ]] \
        || die "$host пропущен в pre-flight, но в прошлом деплое нет его 5 секретов; сначала верни SSH и запусти ./deploy.sh"
      printf '%s\n' "$kept" >> "$sec"
      warn "prep: $host пропущен, использую сохранённые секреты"
      continue
    fi
    cat "$got/$host.env" >> "$sec"
    if grep -q . "$got/$host.pem"; then
      openssl x509 -noout -in "$got/$host.pem" 2>/dev/null || die "$host отдал не PEM-сертификат Hysteria2"
      install -m 600 "$got/$host.pem" "$dir/exit-certs/$host.pem"
    else
      warn "на $host нет сертификата Hysteria2 — закрепится ранее сохранённый на entry, иначе 20-ru-router откажет (передеплой 10-foreign)"
    fi
    if grep -q . "$got/$host.pub"; then
      ssh-keygen -lf "$got/$host.pub" >/dev/null 2>&1 || die "$host отдал не ssh-ключ в /etc/ssh/ssh_host_ed25519_key.pub"
      install -m 644 "$got/$host.pub" "$dir/exit-hostkeys/$host.pub"
    fi
    if [[ $(grep -cE '^(PUB=[A-Za-z0-9_-]{43}|SID=[0-9a-f]{0,16}|SNI=[A-Za-z0-9.-]{1,253}|PORT=[0-9]{1,5})$' "$got/$host.reality") -eq 4 \
          && $(wc -l < "$got/$host.reality") -eq 4 ]]; then
      install -m 600 "$got/$host.reality" "$dir/exit-certs/$host.reality"
    else
      warn "на $host нет ключа Reality - запасной TCP-канал до него не включится (передеплой 10-foreign)"
    fi
  done
  ok "секреты записаны в secrets/foreign-secrets.env"
}

warn_insecure_exits() {
  local host
  for host in $(exit_tags); do
    ! grep -qx "$(tr 'a-z-' 'A-Z_' <<<"$host")_HY2_INSECURE=1" "$ENVS/ru.env" 2>/dev/null \
      || warn "$host: exits[].hy2_insecure: true — сертификат экзита не проверяется, подмену экзита на пути entry → экзит не видно"
  done
}

# Вход на каждый узел до любых изменений. Экзит без связи пропускается (деплой идёт без него),
# сменившийся host-key и отказ во входе останавливают деплой: это чинит оператор.
preflight_ssh() {
  step 'pre-flight: доступность SSH'
  local dir host addr reason rc=0 reasons=() fixes=() skipped=() auth=0 net=0
  dir=$(mktemp -d -p "$TMP")
  service --tags preflight -e "out=$dir" >"$dir/log" 2>&1 || rc=$?
  for host in $(expand_hosts all); do
    if [[ ! -f "$dir/$host.fail" ]]; then
      (( rc == 0 )) || { tail -n 20 "$dir/log" >&2; die 'pre-flight: проверка входа не выполнилась, причина выше'; }
      printf '  %-30s %b✓%b\n' "$host" "$C_G" "$C_END"
      continue
    fi
    addr=$(source "$ENVS/$host.env"; echo "$SSH_HOST")
    reason=$(tr '\n' ' ' < "$dir/$host.fail")
    case "$reason" in
      *'IDENTIFICATION HAS CHANGED'*|*'Host key verification failed'*)
        fixes+=("ssh-keygen -R $addr -f $VPN2_STATE_DIR/known_hosts") ;;
      *'Permission denied'*|*'Authentication failed'*) auth=1 ;;
      *) if [[ "$host" != ru ]]; then
           skipped+=("$host")
           printf '  %b⚠ Экзит %s пропущен: SSH недоступен в pre-flight%b\n' "$C_Y" "$host" "$C_END"
           continue
         fi
         net=1 ;;
    esac
    printf '  %-30s %b✗%b\n' "$host" "$C_R" "$C_END"
    reasons+=("нода $host ($addr) недоступна по ssh с entry: $reason")
  done
  # Экзиты без входа: их не берут стадии (expand_hosts в infra/deploy.sh).
  export SKIPPED_EXIT_TAGS="${skipped[*]}"
  if (( ${#reasons[@]} == 0 )); then
    (( ${#skipped[@]} == 0 )) || printf '  %b⚠ Продолжаю без экзитов: %s%b\n' "$C_Y" "${skipped[*]}" "$C_END"
    printf '\n'
    return 0
  fi
  printf '\n%b✗ Недоступны по SSH:%b\n' "$C_R" "$C_END"
  printf '  %s\n' "${reasons[@]}"
  if (( ${#fixes[@]} )); then
    printf '  %s\n' 'Сменился host-key ноды: так бывает после переустановки ОС на том же IP.' \
      'Новый ключ сам не принимается - это защита от подмены ноды.' \
      'Если ОС переустанавливал ты, убери старый ключ и повтори ./deploy.sh:'
    printf '    %s\n' "${fixes[@]}"
  fi
  (( ! auth )) || printf '  %s\n' 'Нода отказала во входе:' \
    '  1. ключ anysda-orchestrator в /root/.ssh/authorized_keys на ноде: после 00-bootstrap экзит пускает только по нему' \
    '  2. пароль root в config.yaml' \
    '  3. PasswordAuthentication=yes в /etc/ssh/sshd_config на ноде' \
    '  4. если ОС на ноде только что переустановили, пароль мог ещё не выставиться: повтори ./deploy.sh через минуту'
  (( ! net )) || printf '  %s\n' 'Проверь:' '  1. IP ноды в config.yaml' '  2. sshd на ноде запущен и слушает :22' \
    '  3. firewall / geo-блок на стороне провайдера ноды'
  die "pre-flight: недоступных по ssh нод: ${#reasons[@]}, деплой не запускался"
}
