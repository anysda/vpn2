#!/usr/bin/env bash
# Shared by the orchestrator and shipped stages. No implicit development mode.

release_lib_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

release_image() {
  local component="$1" requested="$2" pull="$3"
  if [[ "${VPN2_DEVELOPMENT:-false}" == true ]]; then
    local ref="${requested:-ghcr.io/anysda/vpn2/${component}:dev}"
    if [[ "$pull" != false ]]; then docker pull "$ref" >&2 || true; fi
    docker image inspect --format '{{.Id}}' "$ref"
    return
  fi
  local args=()
  [[ "$pull" != false ]] || args+=(--offline)
  python3 "$release_lib_dir/release.py" --manifest "${VPN2_RELEASE_MANIFEST:-/tmp/anysda/release-manifest.json}" \
    image "$component" --requested "$requested" "${args[@]}"
}

release_artifact() {
  local name="$1" destination="$2"
  python3 "$release_lib_dir/release.py" --manifest "${VPN2_RELEASE_MANIFEST:-/tmp/anysda/release-manifest.json}" \
    artifact "$name" "$destination"
}
