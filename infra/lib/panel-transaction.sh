#!/usr/bin/env bash
# Panel update transaction. VPN daemons and current DB/PKI survive rollback.
panel_state=/var/lib/vpn2-deploy/panel
panel_lib=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
panel_changed=false
panel_old=false

panel_compatible() {
  python3 "$panel_lib/deploy_state.py" compatible --contract "$1"
}

panel_health() {
  local _attempt
  for _attempt in {1..30}; do
    if curl -fsS --max-time 2 -o /dev/null http://127.0.0.1:51821/api/version 2>/dev/null; then return 0; fi
    sleep 1
  done
  return 1
}

panel_rollback() {
  [[ -f "$panel_state/previous-schema.json" ]] || {
    echo 'rollback refused: no previous release; repair the current installation' >&2; return 1;
  }
  panel_compatible "$panel_state/previous-schema.json" || {
    echo 'rollback refused: incompatible schema; keep current DB/PKI and deploy a compatible image' >&2; return 1;
  }
  docker inspect anysda-vpn2-previous >/dev/null || return 1
  docker rm -f anysda-vpn2 >/dev/null 2>&1 || true
  docker rename anysda-vpn2-previous anysda-vpn2 || return 1
  docker start anysda-vpn2 >/dev/null || return 1
  panel_health || { echo 'rollback failed health check; inspect service status' >&2; return 1; }
  python3 "$panel_lib/deploy_state.py" record-rollback || return 1
  echo 'panel rollback verified; current database, revocations, expirations, operations and PKI retained'
}

# panel_guard_files FILE... — host config that belongs to the panel release
# (Caddy TLS snippets): a failed deploy restores it, absent files are removed,
# then the stage's panel_files_restored (if defined) reloads the services.
panel_files=()
panel_guard_files() {
  local file
  rm -rf "$panel_state/files"
  install -d -m 700 "$panel_state/files"
  for file; do
    panel_files+=("$file")
    if [[ -e "$file" ]]; then cp -a --parents "$file" "$panel_state/files"; fi
  done
}

panel_restore_files() {
  local file
  (( ${#panel_files[@]} )) || return 0
  for file in "${panel_files[@]}"; do
    if [[ -e "$panel_state/files$file" ]]; then cp -a "$panel_state/files$file" "$file"; else rm -f "$file"; fi
  done
  if declare -F panel_files_restored >/dev/null; then panel_files_restored; fi
  echo 'panel host config restored'
}

panel_failure() {
  local code=$?
  trap - EXIT
  if [[ "$code" -ne 0 && "$panel_changed" == true ]]; then
    panel_restore_files || echo 'panel host config restore failed' >&2
    if [[ "$panel_old" == true ]]; then
      panel_rollback || echo 'automatic rollback unavailable; deploy a schema-compatible release; do not restore an old access snapshot' >&2
    else
      echo 'installation failed; no previous release exists' >&2
    fi
  fi
  exit "$code"
}

panel_begin() {
  install -d -m 700 "$panel_state"
  exec {panel_lock_fd}>"$panel_state/transaction.lock"
  flock -n "$panel_lock_fd" || { echo 'panel deploy already running' >&2; return 1; }
  python3 "$panel_lib/deploy_state.py" image-contract --image "$PANEL_IMAGE" --contract "$panel_state/candidate-schema.json"
  python3 "$panel_lib/deploy_state.py" compatible --contract "$panel_state/candidate-schema.json" --migrating
  # First backup is mandatory while the old panel and VPN are still serving.
  python3 "$panel_lib/deploy_state.py" --state "$panel_state" backup
  if [[ -f /var/lib/vpn2-deploy/installed.json ]]; then
    cp /var/lib/vpn2-deploy/installed.json "$panel_state/previous-installed.json"
  else
    rm -f "$panel_state/previous-installed.json"
  fi
  if docker inspect anysda-vpn2 >/dev/null 2>&1; then
    local old_image
    old_image=$(docker inspect --format '{{.Image}}' anysda-vpn2)
    python3 "$panel_lib/deploy_state.py" image-contract --image "$old_image" --contract "$panel_state/previous-schema.json"
    # Never remove an earlier fallback until the mandatory backup succeeds.
    docker rm -f anysda-vpn2-previous >/dev/null 2>&1 || true
    docker rename anysda-vpn2 anysda-vpn2-previous
    panel_old=true
    panel_changed=true
    trap panel_failure EXIT
    docker stop anysda-vpn2-previous >/dev/null
  fi
  panel_changed=true
  trap panel_failure EXIT
  echo 'administrative changes paused for migration; VPN services remain running'
  # Capture changes completed between the first backup and the maintenance window.
  python3 "$panel_lib/deploy_state.py" --state "$panel_state" backup
  docker run --rm --network none --user 0:0 \
    -v /var/lib/anysda-vpn2:/var/lib/anysda-vpn2 \
    -e NUXT_DATABASE_URL=file:/var/lib/anysda-vpn2/db.sqlite \
    --entrypoint flock "$PANEL_IMAGE" -n /var/lib/anysda-vpn2/.migration.lock node scripts/migrate.mjs
  panel_compatible "$panel_state/candidate-schema.json"
}

panel_finish() {
  panel_health
  python3 "$panel_lib/deploy_state.py" --state "$panel_state" phase --phase complete
  panel_changed=false
  trap - EXIT
}
