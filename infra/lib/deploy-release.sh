#!/usr/bin/env bash
# Orchestrator lifetime lock on entry, shared by all deploy workstations.
VPN2_BACKED_UP=()

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
    printf '\n' >&"$VPN2_LOCK_IN" || true
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
      VPN2_RELEASE_MANIFEST=$DEPLOY_ROOT/state/release-manifest.json
      python3 "$DEPLOY_ROOT/lib/release.py" --manifest "$VPN2_RELEASE_MANIFEST" fetch "$DEPLOY_ROOT/.." || exit 1
    fi
    python3 "$DEPLOY_ROOT/lib/release.py" --manifest "$VPN2_RELEASE_MANIFEST" check "$DEPLOY_ROOT/.." || exit 1
  fi
  # The remote lock itself needs sshpass/ssh on a clean orchestrator.
  install_prereqs
  coproc VPN2_REMOTE_LOCK {
    load_env ru
    ssh_exec 'umask 077; mkdir -p /var/lib/vpn2-deploy; exec flock -n /var/lib/vpn2-deploy/deploy.lock sh -c '\''echo locked; read -r release_lock'\'
  }
  VPN2_LOCK_PID=$VPN2_REMOTE_LOCK_PID
  exec {VPN2_LOCK_IN}>&"${VPN2_REMOTE_LOCK[1]}"
  local locked
  read -r locked <&"${VPN2_REMOTE_LOCK[0]}" || die 'deploy already running or entry lock unavailable'
  # Метка ssh_exec отсекает текст .bashrc; чужая строка здесь значит, что shell
  # entry при входе печатает текст и до команды не дошёл.
  [[ "$locked" == locked ]] || die "entry did not confirm deploy lock: ssh returned '$locked' instead of the command output; check what root's .bashrc/profile on entry prints and runs at ssh login"
  trap release_unlock EXIT
  local host lib
  for host in $(expand_hosts all); do
    load_env "$host"
    for lib in release.py deploy_state.py panel-transaction.sh anysda-svc.sh; do push "$DEPLOY_ROOT/lib/$lib"; done
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
    for lib in release.py deploy_state.py; do push "$DEPLOY_ROOT/lib/$lib"; done
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
  ssh_exec 'bash --norc -euo pipefail -c '\''source /tmp/anysda/panel-transaction.sh; install -d -m 700 "$panel_state"; exec 9>"$panel_state/transaction.lock"; flock -n 9; panel_rollback; python3 /tmp/anysda/deploy_state.py phase --phase rolled-back'\'
}
