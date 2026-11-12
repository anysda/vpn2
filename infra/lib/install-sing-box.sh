#!/usr/bin/env bash
# Общий установщик для стадий 10/20. Аргумент — путь бинарника (по умолчанию системный).
set -euo pipefail

# Выше 1.12 не берём: на 1.14.4 hysteria2 при потерях 3-5 % теряет ~95 % скорости
# к клиенту (медиана iperf3 168 -> 6 Мбит/с против 1.12.25), bbr_profile aggressive
# даёт лишь 20-22 Мбит/с.
readonly SB_VER='1.12.25'
# GitHub release assets API, tag v1.12.25, поле digest; сверено sha256sum архивов:
# https://api.github.com/repos/SagerNet/sing-box/releases/tags/v1.12.25
readonly SB_SHA256_AMD64='a1ec76e2b6b139eb747a1b1ebee7d14b8d4be5a833596cad8070a31ef960301f'
readonly SB_SHA256_ARM64='719b76196c8b31efa636b2d8f669e314547e0da0a5ab38a75e1882d307bbd154'
dest=${1:-/usr/local/bin/sing-box}

if [[ -x "$dest" ]] && installed=$("$dest" version 2>/dev/null) &&
    [[ "${installed%%$'\n'*}" == "sing-box version $SB_VER" ]]; then
  echo "sing-box $SB_VER уже установлен"
  exit 0
fi

case "$(uname -m)" in
  x86_64|amd64) arch=amd64; sha256=$SB_SHA256_AMD64 ;;
  aarch64|arm64) arch=arm64; sha256=$SB_SHA256_ARM64 ;;
  *) echo 'sing-box: неподдерживаемая архитектура' >&2; exit 1 ;;
esac

work=$(mktemp -d "${TMPDIR:-/tmp}/sing-box.XXXXXXXXXX")
candidate=''
trap 'rm -rf -- "$work"; if [[ -n "$candidate" ]]; then rm -f -- "$candidate"; fi' EXIT

release_src=$(dirname "$0")
[[ -d "$release_src/vpn2/release" ]] || release_src=$release_src/../vpn2/src
archive="sing-box-${SB_VER}-linux-${arch}.tar.gz"
if [[ "${VPN2_DEVELOPMENT:-false}" == true ]]; then
  # Режим разработки: манифеста нет, адрес релиза и хеш - закреплённые выше.
  PYTHONPATH=$release_src python3 -m vpn2.release artifact "sing-box-$arch" "$work/$archive" \
    --url "https://github.com/SagerNet/sing-box/releases/download/v${SB_VER}/$archive" --sha256 "$sha256"
else
  PYTHONPATH=$release_src python3 -m vpn2.release --manifest "${VPN2_RELEASE_MANIFEST:-/tmp/anysda/release-manifest.json}" \
    artifact "sing-box-$arch" "$work/$archive"
fi
printf '%s  %s\n' "$sha256" "$work/$archive" | sha256sum --check --status
# До проверки digest архив не распаковывается, загруженный код не исполняется.
tar -xzf "$work/$archive" -C "$work" "sing-box-${SB_VER}-linux-${arch}/sing-box"

# То же файловое устройство: ошибка install/version/mv сохраняет прежний бинарник.
candidate=$(mktemp "$(dirname -- "$dest")/.sing-box.XXXXXXXXXX")
install -m0755 "$work/sing-box-${SB_VER}-linux-${arch}/sing-box" "$candidate"
installed=$("$candidate" version)
[[ "${installed%%$'\n'*}" == "sing-box version $SB_VER" ]]
mv -fT -- "$candidate" "$dest"
