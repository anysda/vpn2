#!/usr/bin/env bash
# Shared by the orchestrator and shipped stages. No implicit development mode.

release_lib_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

release_image() {
  local component="$1" requested="$2" pull="$3"
  if [[ "${VPN2_DEVELOPMENT:-false}" == true ]]; then
    local ref="${requested:-ghcr.io/anysda/vpn2/${component}:dev}"
    # Плавающий тег: в журнал идёт ревизия образа, иначе устаревший
    # локальный :dev после неудачного pull не отличить от свежего.
    local source="pulled" id revision
    if [[ "$pull" == false ]]; then
      source="local, pull disabled"
    elif ! docker pull "$ref" >&2; then
      source="local, pull failed"
      printf 'WARNING: [development] %s: pull %s failed, using the local image\n' "$component" "$ref" >&2
    fi
    id=$(docker image inspect --format '{{.Id}}' "$ref") || return
    revision=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$ref" 2>/dev/null)
    printf '[development] %s: %s revision %s (%s)\n' "$component" "$ref" "${revision:-unknown}" "$source" >&2
    printf '%s\n' "$id"
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
