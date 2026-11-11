#!/usr/bin/env bash
# Orchestrator lifetime lock on entry, shared by all deploy workstations.
VPN2_BACKED_UP=()

# Команды выпуска из пакета этого дерева; venv для них не нужен (stdlib).
release_cli() { PYTHONPATH="$DEPLOY_ROOT/vpn2/src" python3 -m vpn2.release "$@"; }
# Дерево (стадии, роли, пакет, миграции) должно совпадать с проверенной сборкой.
release_verify_tree() { release_cli --manifest "$VPN2_RELEASE_MANIFEST" verify-tree "$DEPLOY_ROOT/.."; }

release_unlock() {
  local code=$?
  trap - EXIT
  if [[ "$code" -ne 0 && "${VPN2_DEPLOY_MUTATING:-false}" != true ]]; then
    local host
    for host in "${VPN2_BACKED_UP[@]}"; do
      load_env "$host"
      ssh_exec 'python3 /tmp/anysda/deploy_state.py phase --phase aborted' || true
    done
  fi
  if [[ -n "${VPN2_LOCK_IN:-}" ]]; then
    # Если ssh замка уже оборван, coproc мёртв: встроенный printf в его трубу
    # получил бы SIGPIPE и убил deploy кодом 141 до строки итога. В подоболочке
    # SIGPIPE игнорируется, запись просто вернёт EPIPE.
    ( trap '' PIPE; printf '\n' >&"$VPN2_LOCK_IN" ) 2>/dev/null || true
    exec {VPN2_LOCK_IN}>&-
    wait "$VPN2_LOCK_PID" || true
  fi
  if [[ "$code" -ne 0 && "${VPN2_DEPLOY_MUTATING:-false}" == true ]]; then
    echo 'deploy interrupted: operation journal retained; inspect services, then run ./deploy.sh recover (or ./deploy.sh rollback)' >&2
  fi
  exit "$code"
}

release_start() {
  local action="${1:-}"
  case "$action" in -h|--help|99-verify|backup|backup-list) return ;; esac
  # Пробный restore только спрашивает entry, какой архив взял бы: ни замка,
  # ни снимка, ни записи о выпуске.
  if [[ "$action" == restore ]]; then
    local arg
    for arg in "${@:2}"; do [[ "$arg" != --dry-run ]] || return 0; done
  fi
  if [[ "${VPN2_DEVELOPMENT:-false}" != true && "$action" != rollback ]]; then
    # Без --manifest берётся опубликованный манифест commit этого дерева.
    if [[ -z "${VPN2_RELEASE_MANIFEST:-}" ]]; then
      VPN2_RELEASE_MANIFEST=$VPN2_STATE_DIR/release-manifest.json
      release_cli --manifest "$VPN2_RELEASE_MANIFEST" fetch "$DEPLOY_ROOT/.." || exit 1
    fi
    release_verify_tree || exit 1
    # Манифест из --manifest остаётся в состоянии деплоя: anysda-restore.sh
    # прогоняет стадии по нему, когда /tmp/anysda уже вычищен перезагрузкой.
    if [[ ! "$VPN2_RELEASE_MANIFEST" -ef "$VPN2_STATE_DIR/release-manifest.json" ]]; then
      install -m 600 "$VPN2_RELEASE_MANIFEST" "$VPN2_STATE_DIR/release-manifest.json" || exit 1
    fi
  fi
  # The remote lock itself needs sshpass/ssh on a clean orchestrator.
  install_prereqs
  # Сессия sshd замка может пережить клиента: связь оборвалась, ssh умер, а
  # stdin держателя на entry EOF не получает (clientaliveinterval 0, ~2 ч до
  # TCP keepalive). Поэтому coproc раз в секунду шлёт держателю «alive», а тот
  # отпускает замок по пустой строке или после LOCK_PULSE_WINDOW секунд
  # тишины. Окно с запасом на сетевые задержки в единицы секунд. Пульс идёт в
  # coproc и не зависит от того, чем занят deploy; он кончается вместе с
  # deploy: по строке отпускания, EOF, SIGPIPE от мёртвого ssh или смерти $$.
  # На entry sh - dash без read -t, держатель - bash.
  local LOCK_PULSE_WINDOW=12
  # Своя группа процессов: Ctrl-Z оператора останавливает deploy, но не пульс,
  # иначе держатель через окно тишины отпустит замок, и после fg deploy пойдёт
  # без него. Ctrl-C группе до пульса тоже не доходит - его кончает deploy.
  set -m
  coproc VPN2_REMOTE_LOCK {
    load_env ru
    local deploy_pid=$$
    { while kill -0 "$deploy_pid" 2>/dev/null; do
        if read -r -t 1 _; then echo; break; elif (( $? <= 128 )); then break; fi
        echo alive
      done; } | ssh_exec "umask 077; mkdir -p /var/lib/vpn2-deploy; exec flock -n -E 75 /var/lib/vpn2-deploy/deploy.lock bash --norc -c 'echo locked; while read -r -t $LOCK_PULSE_WINDOW && [ \"\$REPLY\" = alive ]; do :; done'"
  }
  set +m
  VPN2_LOCK_PID=$VPN2_REMOTE_LOCK_PID
  exec {VPN2_LOCK_IN}>&"${VPN2_REMOTE_LOCK[1]}"
  local locked rc=0
  if ! read -r locked <&"${VPN2_REMOTE_LOCK[0]}"; then
    # Без строки coproc уже завершился. Занятый замок - только код 75 от
    # flock -E 75 на entry: код 1 отдают и сбои в coproc до ssh (нет envs,
    # пустой SSH_PASS), их причина напечатана выше. ssh_exec отдаёт 255 при
    # отказе входа или сети, sshpass - 5 на отказ во входе (неверный пароль
    # или sshd entry не пускает root: DenyUsers/AllowUsers/Match), 6 и 7 на
    # неизвестный и сменившийся host-key.
    wait "$VPN2_LOCK_PID" || rc=$?
    case $rc in
      75) die 'deploy already running: entry deploy lock is held' ;;
      5) die 'cannot take deploy lock: entry refused ssh login: wrong password (SSH_PASS), or sshd on entry does not let root in (DenyUsers/AllowUsers/Match)' ;;
      6|7) die "cannot take deploy lock: entry host key is unknown or changed (sshpass exit $rc); see the ssh reason above" ;;
      255) die "cannot take deploy lock: ssh to entry failed (login refused or entry unreachable, ssh exit $rc); see the ssh reason above" ;;
      "$_SSH_NO_MARK") die "entry did not confirm deploy lock: root's shell on entry never reached the command; check what root's .bashrc/profile on entry runs at ssh login" ;;
      *) die "cannot take deploy lock: entry lock command exited with $rc, see the reason above" ;;
    esac
  fi
  # Метка ssh_exec отсекает текст .bashrc; чужая строка здесь значит, что shell
  # entry при входе печатает текст и до команды не дошёл.
  [[ "$locked" == locked ]] || die "entry did not confirm deploy lock: ssh returned '$locked' instead of the command output; check what root's .bashrc/profile on entry prints and runs at ssh login"
  trap release_unlock EXIT
  local host lib
  for host in $(expand_hosts all); do
    load_env "$host"
    push "$DEPLOY_ROOT/vpn2/src/vpn2" vpn2
    for lib in deploy_state.py anysda-svc.sh; do push "$DEPLOY_ROOT/lib/$lib"; done
    if [[ "$action" != rollback && "$action" != recover ]]; then
      ssh_exec 'python3 -c '\''import json,pathlib; p=pathlib.Path("/var/lib/vpn2-deploy/operation.json"); assert not p.exists() or json.loads(p.read_text())["phase"] in ("complete","aborted","rolled-back"), "unfinished deploy: inspect operation.json and recover first"'\'
    fi
    if [[ "${VPN2_DEVELOPMENT:-false}" != true && "$action" != rollback ]]; then
      push "$VPN2_RELEASE_MANIFEST" release-manifest.json
      local role=exit
      [[ "$host" != ru ]] || role=entry
      local partial=''
      [[ -z "$action" || "$action" == recover ]] || partial=--partial
      ssh_exec "python3 /tmp/anysda/deploy_state.py preflight $partial"
      if [[ "$host" == ru ]]; then
        # A fresh host needs the container runtime to verify images. Existing
        # installations must never enter this prerequisite-only bootstrap path.
        ssh_exec 'bash --norc -euo pipefail -c '\''if ! command -v docker >/dev/null; then
          test ! -e /var/lib/anysda-vpn2/db.sqlite && test ! -d /etc/anysda || exit 1
          HOST_TAG=ru; source /tmp/anysda/anysda-svc.sh
          apt_update_if_stale; apt_get install -y -qq ca-certificates curl gnupg
          docker_apt_source; apt_get update -qq
          apt_get install -y -qq "${DOCKER_PKGS[@]}" "${DOCKER_SKIP[@]}"
          systemctl enable --now docker
        fi'\'
      fi
      local image_args
      printf -v image_args '%q ' "--panel-image=${PANEL_IMAGE:-}" "--bot-image=${TGBOT_IMAGE:-}"
      if [[ "${PANEL_IMAGE_PULL:-true}" == false || "${TGBOT_IMAGE_PULL:-true}" == false ]]; then
        image_args+=' --offline-images'
      fi
      ssh_exec "python3 /tmp/anysda/deploy_state.py prepare --role '$role' $image_args"
    fi
  done
  [[ "$action" != rollback ]] || return 0
  # Every node completes preflight/downloads before any service is changed.
  for host in $(expand_hosts all); do
    load_env "$host"
    ssh_exec 'python3 /tmp/anysda/deploy_state.py backup'
    VPN2_BACKED_UP+=("$host")
  done
  VPN2_DEPLOY_MUTATING=true
  for host in $(expand_hosts all); do
    load_env "$host"
    ssh_exec 'python3 /tmp/anysda/deploy_state.py phase --phase applying'
  done
}

release_complete() {
  [[ "${VPN2_DEPLOY_MUTATING:-false}" == true ]] || return 0
  local host lib
  for host in $(expand_hosts all); do
    load_env "$host"
    # Exit stages erase their staging directory, including the helper and manifest.
    push "$DEPLOY_ROOT/vpn2/src/vpn2" vpn2
    push "$DEPLOY_ROOT/lib/deploy_state.py"
    if [[ "${VPN2_DEVELOPMENT:-false}" == true ]]; then
      ssh_exec 'python3 /tmp/anysda/deploy_state.py phase --phase complete'
    else
      push "$VPN2_RELEASE_MANIFEST" release-manifest.json
      local role=exit
      [[ "$host" != ru ]] || role=entry
      ssh_exec "python3 /tmp/anysda/deploy_state.py complete --role '$role'"
    fi
  done
  VPN2_DEPLOY_MUTATING=false
}

release_rollback() {
  load_env ru
  ssh_exec '/opt/vpn2/venv/bin/vpn2 containers rollback && python3 /tmp/anysda/deploy_state.py phase --phase rolled-back'
}
